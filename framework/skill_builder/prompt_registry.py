"""PromptRegistry — hot-reloadable versioned YAML prompt store.

Implements ADR-030 §Design §4 exactly.  Call sites replace hard-coded
``_PROMPT_CONSTANT.format(...)`` with::

    from framework.skill_builder.prompt_registry import get_registry
    spec = get_registry().get_prompt("capture_intent", persona=persona, **kwargs)
    result = llm.chat(model=spec.model, ..., max_tokens=spec.max_tokens)

Design invariants (no-stub / no-silent-degradation — see CLAUDE.md):
  * Every failure path raises a typed exception with an actionable message.
  * Malformed YAML → PromptStoreError at load time; the registry is NOT usable.
  * On reload failure the registry retains its last-good state and re-raises.
  * Unknown prompt_id → PromptNotFoundError (hard-fail, no fallback).
  * Missing required vars → MissingVarsError (hard-fail, no fallback).
  * Locked-prompt checksum mismatch → LockedPromptTamperedError (hard-fail).

``persona`` double-use (ADR-030-impl-plan §4, risk #1):
  ``persona`` passed to ``get_prompt(persona=...)`` serves TWO roles:
    1. Overlay key: selects the persona stanza in ``persona_overlays.yaml``.
    2. Format variable: injected into ``fmt_vars`` so ``{persona}`` placeholders
       inside templates resolve correctly.
  Implementation: after overlay merge, always add ``persona`` (or ``""``) to the
  effective fmt_vars dict, giving the overlay dict priority, then the caller, then
  the ``persona`` value itself.  This means a caller that explicitly passes
  ``persona="tpm"`` in ``**fmt_vars`` is never overridden — the explicit arg wins.

Checksum algorithm (ADR-030 §Design §5, verbatim):
    sha256(template.encode("utf-8").rstrip(b"\\n")).hexdigest()
  Stored as the string ``"sha256:<64 hex chars>"``.

Hot-reload (ADR-030 §Design §4):
  On every ``get_prompt()`` call the registry computes ``os.stat().st_mtime`` for
  every YAML file in ``prompts_dir``.  If any mtime has changed since last load,
  ``reload()`` is called atomically before the prompt is served.  The mtime check
  is a single ``os.stat()`` per file per call — negligible overhead.

Startup validation (§Design §4 point 3):
  For every prompt, ``required_vars`` is cross-checked against the ``{placeholder}``
  names found in the template (accounting for ``{{`` literal-brace escapes).
  * A ``required_var`` declared but absent from the template text → PromptStoreError.
  * A ``{placeholder}`` in the template but not in ``required_vars`` → WARNING only
    (some templates use runtime-optional vars or persona-overlay vars).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PromptStoreError(RuntimeError):
    """Base exception for all PromptRegistry failures."""


class PromptNotFoundError(PromptStoreError):
    """Raised when a requested prompt_id is not in the store."""


class MissingVarsError(PromptStoreError):
    """Raised when required_vars are not supplied and cannot be resolved from overlay."""


class LockedPromptTamperedError(PromptStoreError):
    """Raised when a locked prompt's template does not match its stored checksum."""


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass
class PromptSpec:
    """Fully resolved, formatted prompt ready for use at a call site."""

    prompt_id: str
    version: str
    model: str          # "synthesis" | "fast" | "none" (for non-LLM turn templates)
    max_tokens: int     # 0 when model is "none"
    response_format: Dict[str, Any]   # e.g. {"type": "json_object"} or {} for "none"
    text: str           # fully formatted prompt string (all {placeholders} substituted)


@dataclass
class PromptMeta:
    """Lightweight metadata about a loaded prompt — used by list_prompts()."""

    prompt_id: str
    version: str
    description: str
    locked: bool
    model: str
    required_vars: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal data class for raw (un-formatted) prompt records
# ---------------------------------------------------------------------------


@dataclass
class _PromptRecord:
    """Internal representation of one parsed YAML prompt entry."""

    prompt_id: str
    version: str
    model: str
    max_tokens: int
    response_format: Dict[str, Any]
    required_vars: List[str]
    template: str           # raw template text with {placeholder} markers
    locked: bool = False
    checksum: str = ""      # "sha256:<hex>" or "" when not locked
    description: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")


def _extract_template_vars(template: str) -> set:
    """Return the set of {placeholder} names in *template*, excluding {{ escapes."""
    return set(_PLACEHOLDER_RE.findall(template))


def _compute_checksum(template: str) -> str:
    """Compute sha256(template.encode('utf-8').rstrip(b'\\n')).hexdigest().

    This is the exact algorithm specified in ADR-030 §Design §5.
    Returns the string ``"sha256:<64-hex-chars>"``.
    """
    digest = hashlib.sha256(template.encode("utf-8").rstrip(b"\n")).hexdigest()
    return f"sha256:{digest}"


def _parse_response_format(value: Any, prompt_id: str) -> Dict[str, Any]:
    """Normalise the response_format field to a dict.

    Accepts:
      * ``"json_object"`` → ``{"type": "json_object"}``
      * ``"text"`` → ``{"type": "text"}``
      * ``"none"`` or ``None`` → ``{}``
      * A dict (already normalised) → returned as-is
    """
    if value is None or value == "none":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"type": value}
    raise PromptStoreError(
        f"Prompt '{prompt_id}': response_format must be a string or dict, "
        f"got {type(value).__name__!r}: {value!r}"
    )


def _parse_prompt_record(raw: Dict[str, Any], source_file: str) -> _PromptRecord:
    """Parse and validate one YAML prompt stanza.  Raises PromptStoreError on schema failure."""

    def _require(key: str) -> Any:
        val = raw.get(key)
        if val is None:
            raise PromptStoreError(
                f"Prompt '{prompt_id}' in {source_file}: required field '{key}' is missing or null."
            )
        return val

    prompt_id = raw.get("id")
    if not prompt_id or not isinstance(prompt_id, str):
        raise PromptStoreError(
            f"A prompt in {source_file} has a missing or non-string 'id' field: {raw!r}"
        )

    version = str(_require("version"))
    model_raw = _require("model")
    model = str(model_raw) if model_raw is not None else "none"

    # max_tokens: 0 is the sentinel for "none" / turn-only prompts
    max_tokens_raw = raw.get("max_tokens")
    if max_tokens_raw is None:
        if model == "none":
            max_tokens = 0
        else:
            raise PromptStoreError(
                f"Prompt '{prompt_id}' in {source_file}: 'max_tokens' is required when model != 'none'."
            )
    else:
        try:
            max_tokens = int(max_tokens_raw)
        except (TypeError, ValueError):
            raise PromptStoreError(
                f"Prompt '{prompt_id}' in {source_file}: 'max_tokens' must be an integer, "
                f"got {max_tokens_raw!r}."
            )

    response_format = _parse_response_format(raw.get("response_format"), prompt_id)
    required_vars: List[str] = list(raw.get("required_vars") or [])
    template = _require("template")
    if not isinstance(template, str):
        raise PromptStoreError(
            f"Prompt '{prompt_id}' in {source_file}: 'template' must be a string, "
            f"got {type(template).__name__!r}."
        )

    locked = bool(raw.get("locked", False))
    checksum = str(raw.get("checksum", "")) if raw.get("checksum") else ""
    description = str(raw.get("description", ""))
    notes = str(raw.get("notes", ""))

    # Validate required_vars against template placeholders
    template_vars = _extract_template_vars(template)
    for var in required_vars:
        if var not in template_vars:
            raise PromptStoreError(
                f"Prompt '{prompt_id}' in {source_file}: required_var '{var}' is declared "
                f"but does not appear as {{{{var}}}} in the template text. "
                f"Template vars found: {sorted(template_vars)}. "
                f"This indicates a stale required_vars list or a missing placeholder in the template."
            )
    # Warn about template vars not in required_vars (informational only — overlays supply some)
    undeclared = template_vars - set(required_vars)
    if undeclared:
        log.debug(
            "Prompt '%s': template vars %s are not in required_vars — "
            "they may be supplied via persona overlay or be optional.",
            prompt_id, sorted(undeclared),
        )

    # Validate checksum for locked prompts
    if locked:
        if not checksum:
            raise PromptStoreError(
                f"Prompt '{prompt_id}' in {source_file}: locked=true but 'checksum' is absent. "
                f"Compute with: sha256(template.encode('utf-8').rstrip(b'\\n')).hexdigest() "
                f"and prefix with 'sha256:'."
            )
        expected = _compute_checksum(template)
        if checksum != expected:
            raise LockedPromptTamperedError(
                f"Locked prompt '{prompt_id}' in {source_file}: checksum mismatch.\n"
                f"  Stored : {checksum}\n"
                f"  Computed: {expected}\n"
                "The template text has been modified without updating the checksum. "
                "To intentionally change this prompt: (a) edit the template, "
                "(b) recompute the checksum, (c) update both fields in the YAML, "
                "(d) re-run the gate test."
            )

    return _PromptRecord(
        prompt_id=prompt_id,
        version=version,
        model=model,
        max_tokens=max_tokens,
        response_format=response_format,
        required_vars=required_vars,
        template=template,
        locked=locked,
        checksum=checksum,
        description=description,
        notes=notes,
    )


def _parse_overlays(raw_yaml: Any, source_file: str) -> Dict[str, Dict[str, Any]]:
    """Parse persona_overlays.yaml content.

    Expected structure::

        personas:
          tpm:
            applies_to: [capture_intent, design_skill]
            overlay_vars:
              persona_key_fields: "..."
              persona_extraction_style: "..."

    Returns a dict: ``{persona_name: {"applies_to": [...], "overlay_vars": {...}}}``
    """
    if not isinstance(raw_yaml, dict):
        raise PromptStoreError(
            f"persona_overlays.yaml ({source_file}): top-level must be a YAML mapping, "
            f"got {type(raw_yaml).__name__!r}."
        )
    personas_block = raw_yaml.get("personas", {})
    if not isinstance(personas_block, dict):
        raise PromptStoreError(
            f"persona_overlays.yaml ({source_file}): 'personas' key must be a mapping."
        )

    result: Dict[str, Dict[str, Any]] = {}
    for persona_name, stanza in personas_block.items():
        if not isinstance(stanza, dict):
            raise PromptStoreError(
                f"persona_overlays.yaml ({source_file}): stanza for persona '{persona_name}' "
                f"must be a mapping."
            )
        applies_to = list(stanza.get("applies_to") or [])
        overlay_vars = dict(stanza.get("overlay_vars") or {})
        result[str(persona_name)] = {
            "applies_to": applies_to,
            "overlay_vars": overlay_vars,
        }
    return result


# ---------------------------------------------------------------------------
# PromptRegistry
# ---------------------------------------------------------------------------


class PromptRegistry:
    """Hot-reloadable registry of versioned prompt templates loaded from YAML.

    Thread-safety note: the load-then-swap pattern ensures atomic state
    replacement.  The swap is a single attribute assignment (Python GIL
    guarantees single object reference writes are atomic at the C level for
    CPython).  A threading.Lock is used around the reload sequence to prevent
    concurrent reload calls from stomping each other.
    """

    def __init__(self, prompts_dir: Path) -> None:
        """Load all YAML files in *prompts_dir* at construction time.

        Raises PromptStoreError (or LockedPromptTamperedError, a subclass) on
        any failure — hard-fail, no silent degradation.
        """
        self._prompts_dir = Path(prompts_dir)
        self._lock = threading.Lock()

        # Internal state — replaced atomically on each reload
        self._cache: Dict[str, _PromptRecord] = {}
        self._overlays: Dict[str, Dict[str, Any]] = {}
        self._mtimes: Dict[str, float] = {}

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_prompt(
        self,
        prompt_id: str,
        *,
        persona: Optional[str] = None,
        **fmt_vars: str,
    ) -> PromptSpec:
        """Return a fully formatted PromptSpec for *prompt_id*.

        Raises:
          PromptNotFoundError         — prompt_id not in the store
          MissingVarsError            — required vars not supplied/resolvable
          LockedPromptTamperedError   — locked prompt checksum mismatch at
                                        reload time (raised via _check_mtime)
          PromptStoreError            — any other load/validation failure
        """
        self._check_mtime()

        record = self._cache.get(prompt_id)
        if record is None:
            available = sorted(self._cache.keys())
            raise PromptNotFoundError(
                f"Prompt '{prompt_id}' is not in the registry. "
                f"Available prompt IDs: {available}"
            )

        # Build effective fmt_vars:
        # 1. Start with persona overlay vars (lowest priority)
        effective_vars: Dict[str, str] = {}
        if persona is not None:
            overlay = self._overlays.get(persona)
            if overlay is None:
                log.warning(
                    "PromptRegistry: unknown persona '%s' — no overlay stanza found in "
                    "persona_overlays.yaml.  Falling through with empty overlay vars.  "
                    "Add a stanza to framework/config/prompts/persona_overlays.yaml to fix.",
                    persona,
                )
            elif prompt_id not in overlay.get("applies_to", []):
                log.debug(
                    "PromptRegistry: persona '%s' overlay does not apply to prompt '%s' — "
                    "no overlay vars injected.",
                    persona, prompt_id,
                )
            else:
                effective_vars.update(overlay.get("overlay_vars", {}))

        # 2. Caller-supplied vars override overlay vars
        effective_vars.update(fmt_vars)

        # 3. persona double-use (ADR-030-impl-plan §4, risk #1):
        #    inject 'persona' into fmt_vars so {persona} placeholders resolve.
        #    Caller-supplied persona in **fmt_vars wins (already in effective_vars).
        if "persona" not in effective_vars and persona is not None:
            effective_vars["persona"] = persona

        # Validate required_vars are all present
        template_vars = _extract_template_vars(record.template)
        missing = [
            v for v in record.required_vars
            if v not in effective_vars
        ]
        if missing:
            raise MissingVarsError(
                f"Prompt '{prompt_id}': required variable(s) {missing} were not supplied "
                f"and could not be resolved from the persona overlay "
                f"(persona={persona!r}).  "
                f"Caller must supply these kwargs to get_prompt()."
            )

        # Format the template — only pass vars that are actually in the template
        # (extra vars that don't appear in the template are silently ignored by
        # str.format_map, which is the desired behavior for overlay vars that
        # apply to some prompts but not others).
        try:
            text = record.template.format_map({k: v for k, v in effective_vars.items()})
        except KeyError as exc:
            # This means a {placeholder} in the template has no value — it should
            # have been caught by required_vars validation above, but handle it.
            raise MissingVarsError(
                f"Prompt '{prompt_id}': template placeholder {exc} has no value in "
                f"effective_vars {sorted(effective_vars.keys())}."
            ) from exc

        return PromptSpec(
            prompt_id=record.prompt_id,
            version=record.version,
            model=record.model,
            max_tokens=record.max_tokens,
            response_format=dict(record.response_format),
            text=text,
        )

    def reload(self) -> None:
        """Re-read all YAML files from disk.

        If the new load fails (malformed YAML, schema error, checksum mismatch),
        PromptStoreError is raised and the registry retains its last-good state
        (cache and overlays are NOT replaced).

        The mtime snapshot is updated to current disk state even on failure so
        that ``_check_mtime`` does not immediately re-trigger the failing reload
        on every subsequent ``get_prompt`` call.  This matches ADR-030 intent:
        "does NOT partially load; registry stays on last-good state on reload."
        An operator sees the error once (or per explicit reload()) rather than
        on every request.
        """
        with self._lock:
            # Snapshot current mtimes (so on failure we don't keep retrying)
            current_mtimes: Dict[str, float] = {}
            for yaml_path in sorted(self._prompts_dir.glob("*.yaml")):
                try:
                    current_mtimes[str(yaml_path)] = os.stat(yaml_path).st_mtime
                except OSError:
                    current_mtimes[str(yaml_path)] = 0.0
            try:
                self._load()
            except PromptStoreError:
                # Update mtimes to current so _check_mtime stops re-triggering.
                # Cache and overlays remain at their last-good state (load-then-swap
                # in _load() only swaps on success).
                self._mtimes = current_mtimes
                raise

    def list_prompts(self) -> List[PromptMeta]:
        """Return metadata for every loaded prompt, sorted by prompt_id."""
        self._check_mtime()
        return [
            PromptMeta(
                prompt_id=r.prompt_id,
                version=r.version,
                description=r.description,
                locked=r.locked,
                model=r.model,
                required_vars=list(r.required_vars),
            )
            for r in sorted(self._cache.values(), key=lambda r: r.prompt_id)
        ]

    def _raw_template(self, prompt_id: str) -> str:
        """Return the raw (un-formatted) template text for *prompt_id*.

        Used by gate tests (G1) and structural contract tests that inspect
        the template text for required substrings before formatting.

        Raises PromptNotFoundError if prompt_id is not in the store.
        """
        self._check_mtime()
        record = self._cache.get(prompt_id)
        if record is None:
            raise PromptNotFoundError(
                f"Prompt '{prompt_id}' is not in the registry (raw_template lookup)."
            )
        return record.template

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load (or reload) all YAML files from prompts_dir.

        Implements the load-then-swap pattern:
          1. Parse everything into temporary dicts.
          2. Validate fully (checksums, required_vars, schema).
          3. Atomically swap self._cache and self._overlays.
          4. Update self._mtimes.

        Raises PromptStoreError on any failure — caller retains last-good state
        because the swap in step 3 only happens on success.
        """
        yaml_files = sorted(self._prompts_dir.glob("*.yaml"))
        if not yaml_files:
            log.warning(
                "PromptRegistry: no *.yaml files found in %s — registry will be empty.",
                self._prompts_dir,
            )

        new_cache: Dict[str, _PromptRecord] = {}
        new_overlays: Dict[str, Dict[str, Any]] = {}
        new_mtimes: Dict[str, float] = {}

        for yaml_path in yaml_files:
            try:
                raw_text = yaml_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PromptStoreError(
                    f"PromptRegistry: cannot read {yaml_path}: {exc}"
                ) from exc

            try:
                data = yaml.safe_load(raw_text)
            except yaml.YAMLError as exc:
                raise PromptStoreError(
                    f"PromptRegistry: malformed YAML in {yaml_path}: {exc}"
                ) from exc

            if data is None:
                raise PromptStoreError(
                    f"PromptRegistry: {yaml_path} is empty (parsed as None). "
                    "Remove the file or add valid prompt definitions."
                )

            fname = yaml_path.name

            # persona_overlays.yaml has a different top-level schema
            if fname == "persona_overlays.yaml":
                new_overlays = _parse_overlays(data, str(yaml_path))
            else:
                # Expect: {"prompts": {id: {...}, ...}}
                if not isinstance(data, dict):
                    raise PromptStoreError(
                        f"PromptRegistry: {yaml_path}: top-level must be a YAML mapping, "
                        f"got {type(data).__name__!r}."
                    )
                prompts_block = data.get("prompts")
                if prompts_block is None:
                    raise PromptStoreError(
                        f"PromptRegistry: {yaml_path}: missing top-level 'prompts' key."
                    )
                if not isinstance(prompts_block, dict):
                    raise PromptStoreError(
                        f"PromptRegistry: {yaml_path}: 'prompts' must be a mapping "
                        f"(dict), got {type(prompts_block).__name__!r}."
                    )
                for entry_key, raw_entry in prompts_block.items():
                    if not isinstance(raw_entry, dict):
                        raise PromptStoreError(
                            f"PromptRegistry: {yaml_path}: entry '{entry_key}' must be a "
                            f"mapping, got {type(raw_entry).__name__!r}."
                        )
                    record = _parse_prompt_record(raw_entry, str(yaml_path))
                    if record.prompt_id in new_cache:
                        raise PromptStoreError(
                            f"PromptRegistry: duplicate prompt_id '{record.prompt_id}' "
                            f"found in {yaml_path} (already loaded from another file). "
                            "Each prompt_id must be unique across the entire prompts_dir."
                        )
                    new_cache[record.prompt_id] = record

            try:
                new_mtimes[str(yaml_path)] = os.stat(yaml_path).st_mtime
            except OSError:
                new_mtimes[str(yaml_path)] = 0.0

        # Atomically swap state (only reached if no exceptions above)
        self._cache = new_cache
        self._overlays = new_overlays
        self._mtimes = new_mtimes

        log.info(
            "PromptRegistry: loaded %d prompt(s) from %s — ids: %s",
            len(self._cache),
            self._prompts_dir,
            sorted(self._cache.keys()),
        )

    def _check_mtime(self) -> None:
        """Check whether any YAML file's mtime has changed; reload if so.

        If the triggered reload fails (PromptStoreError), the exception is
        re-raised so the caller (get_prompt) propagates it.  The mtime snapshot
        is updated by ``reload()`` on failure so this method does not trigger a
        new reload on the next call with the same bad file on disk.
        """
        for yaml_path in sorted(self._prompts_dir.glob("*.yaml")):
            path_str = str(yaml_path)
            try:
                current_mtime = os.stat(yaml_path).st_mtime
            except OSError:
                continue
            if current_mtime != self._mtimes.get(path_str, -1.0):
                log.info(
                    "PromptRegistry: mtime change detected in %s — hot-reloading.",
                    yaml_path.name,
                )
                self.reload()  # reload() handles lock + mtime update on failure
                return  # reload() rescans all files; no need to check further


# ---------------------------------------------------------------------------
# Startup validation helper
# ---------------------------------------------------------------------------


def validate_registry(registry: PromptRegistry) -> None:
    """Verify every prompt loads and every required_var placeholder is present.

    Callable at server startup and in tests.  Raises PromptStoreError on the
    first validation failure found.  On success, returns None.

    Validation checks:
      1. Every prompt can be resolved (prompt_id exists in cache).
      2. Every declared required_var appears in the template's {placeholder} set.
         (This is also done per-prompt at load time, but validate_registry provides
         a single explicit call-point for startup harnesses and tests.)
    """
    for meta in registry.list_prompts():
        pid = meta.prompt_id
        # Retrieve raw template — raises PromptNotFoundError on any corruption
        raw = registry._raw_template(pid)
        # Verify required_vars are in the template
        # (already checked at load, but double-check here for the validation hook contract)
        record = registry._cache.get(pid)
        if record is None:
            raise PromptStoreError(
                f"validate_registry: prompt '{pid}' disappeared from cache during validation."
            )
        template_vars = _extract_template_vars(raw)
        for var in record.required_vars:
            if var not in template_vars:
                raise PromptStoreError(
                    f"validate_registry: prompt '{pid}': required_var '{var}' is declared "
                    f"but not present as a {{placeholder}} in the template. "
                    f"Template vars: {sorted(template_vars)}"
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Optional[PromptRegistry] = None
_registry_lock = threading.Lock()

# Default prompts directory (relative to this file → framework/config/prompts/)
# prompt_registry.py lives at framework/skill_builder/prompt_registry.py
#   parents[0] = framework/skill_builder/
#   parents[1] = framework/
# So parents[1] / "config" / "prompts" = framework/config/prompts/ (correct)
_DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "config" / "prompts"


def get_registry(prompts_dir: Optional[Path] = None) -> PromptRegistry:
    """Return (or construct) the module-level singleton PromptRegistry.

    The singleton is initialised on first call with *prompts_dir* (defaults to
    ``framework/config/prompts/``).  Subsequent calls with ``prompts_dir=None``
    return the existing singleton unchanged.

    Passing a non-None *prompts_dir* after initialisation is supported in tests
    (creates a new registry for the given directory — useful for isolating test
    fixtures).  Call sites in production should always pass ``prompts_dir=None``.

    Raises PromptStoreError if the YAML files cannot be loaded.
    """
    global _registry
    if prompts_dir is not None:
        # Test or explicit override path — return a fresh registry (do NOT
        # replace the module-level singleton, to avoid polluting other tests).
        return PromptRegistry(prompts_dir)
    with _registry_lock:
        if _registry is None:
            _registry = PromptRegistry(_DEFAULT_PROMPTS_DIR)
    return _registry
