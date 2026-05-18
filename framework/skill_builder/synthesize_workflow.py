"""synthesize_workflow — generate a complete workflow skill YAML dict.

Per ADR-015 + ADR-016. Phase 3 addition. Takes intent + fields + optional
template path and produces a workflow skill YAML structure that matches
the ADR-016 schema.

ADR-032 addition: when source_binding_mode == "ask_parameterized", the
synthesizer emits a full source_binding block and replaces the generic
trigger input with a typed page_id input.  author_fixed mode produces
byte-identical output to the pre-ADR-032 synthesizer (no source_binding
block, generic trigger input).

DECISION-019 RC1 addition: when source_binding_mode == "author_fixed" AND a
fixed external source exists (i.e., source_samples has at least one entry),
the synthesizer now emits a source_binding block with mode: author_fixed and
a pinned_ref derived from source_samples.  This allows the runtime executor to
resolve the exact page the skill was authored against, preventing silent wrong-
page retrieval.  Skills with no external fixed source (pure in-KB) continue to
emit NO source_binding block — unchanged pre-ADR-032 behavior.
"""
from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# space_allow_list derivation (ADR-032 P1 synthesizer gap fix)
# ---------------------------------------------------------------------------

def derive_space_allow_list(
    sources: list[dict],
    source_samples: dict,
) -> list[str]:
    """Derive the Confluence space_allow_list from session state.

    Derivation priority (highest first):

    1. ``source_samples`` — the INSPECT_SOURCES step fetched live pages and
       stored their metadata in ``source_samples`` keyed by
       ``"confluence:{source_id}"``.  Each sample dict carries a ``space``
       field extracted from the page metadata by ``sampler.fetch_samples()``.
       This is the most reliable source: it reflects the *actual* space of
       the pages the author configured, confirmed by a live API call during
       authoring.

    2. ``sources[*].page_url`` / ``sources[*].pages`` (URL form) — Confluence
       page URLs encode the space key in ``/spaces/{SPACE}/`` or
       ``/display/{SPACE}/`` path segments.  This handles sessions where the
       author supplied URLs but INSPECT_SOURCES did not populate source_samples
       (e.g. the session was restored from a checkpoint after inspection).

    3. ``sources[*].space`` — explicit space key stored on the source dict when
       the author configured a Confluence space (e.g. "confluence OCIFACP").

    Returns a deduplicated, sorted list of space keys (uppercase).  Empty list
    when the space cannot be determined from session state.  The caller
    (``_synthesize_preview`` in conversation.py) must treat an empty return as
    an underivable case — see ADR-032 synthesizer wiring note.

    Design decision (ADR-032 implementation note): a wrong space_allow_list is
    MORE dangerous than an empty one.  The P1-E bug (hardcoded [FA, PROJ])
    caused a hard "space not in allow-list" failure at runtime for the OCIFACP
    user.  We NEVER guess or hardcode.  If we cannot derive, we return [] and
    the caller surfaces an actionable error to the author.
    """
    spaces: set[str] = set()

    # --- Priority 1: space field from live-fetched source_samples -----------
    # source_samples: dict[str, list[dict]] keyed "confluence:{source_id}".
    # Each sample dict has a "space" field populated by sampler.fetch_samples()
    # from the adapter metadata (meta.get("space") or space.key in payload).
    for _key, samples in (source_samples or {}).items():
        if not isinstance(samples, list):
            continue
        for s in samples:
            sp = s.get("space", "")
            if sp and isinstance(sp, str) and sp.strip():
                spaces.add(sp.strip().upper())

    if spaces:
        return sorted(spaces)

    # --- Priority 2: extract space from Confluence URLs in sources ----------
    _URL_SPACE_PATTERNS = [
        re.compile(r"/wiki/spaces/([A-Z0-9_\-]+)/", re.IGNORECASE),
        re.compile(r"/spaces/([A-Z0-9_\-]+)/", re.IGNORECASE),
        re.compile(r"/display/([A-Z0-9_\-]+)/", re.IGNORECASE),
    ]
    for src in (sources or []):
        candidates: list[str] = []
        page_url = src.get("page_url", "")
        if page_url:
            candidates.append(page_url)
        for pg in (src.get("pages") or []):
            if isinstance(pg, str) and pg.startswith("http"):
                candidates.append(pg)
        for url in candidates:
            for pat in _URL_SPACE_PATTERNS:
                m = pat.search(url)
                if m:
                    spaces.add(m.group(1).upper())

    if spaces:
        return sorted(spaces)

    # --- Priority 3: explicit space key on the source dict ------------------
    for src in (sources or []):
        sp = src.get("space", "")
        if sp and isinstance(sp, str) and sp.strip():
            spaces.add(sp.strip().upper())

    return sorted(spaces)


# ---------------------------------------------------------------------------
# pinned_source derivation (DECISION-019 RC1 — author_fixed source binding)
# ---------------------------------------------------------------------------

def derive_pinned_source(
    sources: list[dict],
    source_samples: dict,
) -> dict | None:
    """Derive the pinned external source for an author_fixed skill.

    Returns a dict with keys:
      - ``pinned_ref``       — the canonical Confluence URL or page ID
      - ``source_type``      — "confluence_page" (default)
      - ``space_allow_list`` — list[str] of space keys (may be empty if
                               space cannot be derived — caller responsibility
                               to validate)

    Returns None when no external fixed source can be identified
    (e.g., purely in-KB skills with no live Confluence source in session state).
    A None return means NO source_binding block should be emitted for
    author_fixed skills — unchanged pre-DECISION-019 behavior.

    Derivation priority (mirrors derive_space_allow_list — same session state):

    1. ``source_samples`` — the INSPECT_SOURCES step stores fetched page
       metadata in ``source_samples[key]`` where ``key`` is
       ``"confluence:{source_id}"``.  The source_id is a URL or page ID.
       This is the most reliable source: it reflects the actual page the
       author configured, confirmed by a live API call during authoring.

    2. ``sources[*].page_url`` / ``sources[*].pages`` (URL form) — used as
       fallback when source_samples is absent.

    3. ``sources[*].source_id`` or ``sources[*].page_id`` — bare page IDs as
       last resort.

    Design principle (DECISION-019 RC1 + ADR-031 no-silent-degradation):
      A wrong pinned_ref is LESS dangerous than no binding — a wrong ref
      causes a deterministic lookup failure at runtime (actionable), whereas
      no binding causes silent wrong-page retrieval (the original bug).
      We prefer a deterministic source even if it may not resolve, because
      the executor will hard-fail loudly on a bad ref rather than silently
      substituting.
    """
    # --- Priority 1: source from source_samples (most reliable) -------------
    # source_samples: dict[str, list[dict]] keyed "confluence:{source_id}".
    # The source_id part (after "confluence:") is the URL or page ID.
    for key in (source_samples or {}):
        if not key.startswith("confluence:"):
            continue
        source_id = key[len("confluence:"):]
        if source_id:
            space_allow_list = _spaces_from_source_samples(source_samples)
            source_type = "confluence_page"
            return {
                "pinned_ref": source_id,
                "source_type": source_type,
                "space_allow_list": space_allow_list,
            }

    # --- Priority 2: URL-form source from sources ----------------------------
    _URL_SPACE_PATTERNS = [
        re.compile(r"/wiki/spaces/([A-Z0-9_\-]+)/", re.IGNORECASE),
        re.compile(r"/spaces/([A-Z0-9_\-]+)/", re.IGNORECASE),
        re.compile(r"/display/([A-Z0-9_\-]+)/", re.IGNORECASE),
    ]
    for src in (sources or []):
        page_url = src.get("page_url", "")
        if page_url and page_url.startswith("http"):
            space_allow_list = derive_space_allow_list(sources, {})
            return {
                "pinned_ref": page_url,
                "source_type": "confluence_page",
                "space_allow_list": space_allow_list,
            }
        # pages list
        for pg in (src.get("pages") or []):
            if isinstance(pg, str) and pg.startswith("http"):
                space_allow_list = derive_space_allow_list(sources, {})
                return {
                    "pinned_ref": pg,
                    "source_type": "confluence_page",
                    "space_allow_list": space_allow_list,
                }

    # --- Priority 3: bare page_id or source_id on source dict ---------------
    for src in (sources or []):
        pid = src.get("page_id") or src.get("source_id") or src.get("id")
        if pid and isinstance(pid, (str, int)) and str(pid).strip().isdigit():
            space_allow_list = derive_space_allow_list(sources, source_samples)
            return {
                "pinned_ref": str(pid).strip(),
                "source_type": "confluence_page",
                "space_allow_list": space_allow_list,
            }

    # No external fixed source found — pure in-KB skill, emit no binding.
    return None


def _spaces_from_source_samples(source_samples: dict) -> list[str]:
    """Extract space keys from source_samples (mirror of derive_space_allow_list P1)."""
    spaces: set[str] = set()
    for _key, samples in (source_samples or {}).items():
        if not isinstance(samples, list):
            continue
        for s in samples:
            sp = s.get("space", "")
            if sp and isinstance(sp, str) and sp.strip():
                spaces.add(sp.strip().upper())
    return sorted(spaces)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def synthesize_workflow_skill(
    persona: str,
    skill_name: str,
    intent: dict,
    fields: list[str],
    template_path: str | None = None,
    # ADR-032: ask_parameterized source_binding emission
    source_binding_mode: str = "author_fixed",
    space_allow_list: list[str] | None = None,
    input_param: str = "page_id",
    ephemeral_ttl_seconds: int = 300,
    source_type: str = "confluence_page",
    # DECISION-019 RC1: author_fixed pinned source binding
    pinned_source: dict | None = None,
) -> dict:
    """Generate a workflow skill YAML structure as a Python dict.

    Args:
        persona: persona id (e.g. "tpm").
        skill_name: snake_case skill name (e.g. "weekly_exec_review").
        intent: dict from the intent file (task_description, sources, trigger,
                output_format, delivery, requires_extractions etc.).
        fields: list of required field names for the synthesis mapping.
        template_path: optional path to an existing synthesis template; used
                       to populate synthesis.template.
        source_binding_mode: "author_fixed" (default) | "ask_parameterized".
                             When "ask_parameterized", emits a full
                             source_binding block and a typed page_id trigger
                             input per ADR-032 §D.1.
        space_allow_list: Confluence space keys for the source_binding block.
                          Required (non-empty) when source_binding_mode ==
                          "ask_parameterized".  Ignored for author_fixed.
        input_param: name of the trigger input that carries the page reference.
                     Default "page_id" per ADR-032 §D.1.
        ephemeral_ttl_seconds: TTL for the in-process ephemeral cache.
                               Default 300 per ADR-032 §E.5.
        source_type: type of source for the source_binding block.
                     Default "confluence_page".

        pinned_source: optional dict returned by derive_pinned_source().
                      When provided AND source_binding_mode == "author_fixed",
                      emits a source_binding block with mode: author_fixed and
                      the pinned_ref so the runtime executor resolves the exact
                      page the skill was authored against (DECISION-019 RC1).
                      When None (default) AND source_binding_mode ==
                      "author_fixed": NO source_binding block emitted —
                      byte-identical to pre-DECISION-019 behavior for pure in-KB
                      skills.

    Returns:
        A dict that can be round-tripped through yaml.safe_dump to produce a
        valid workflow_skills/{persona}/{skill_name}.yaml file.

    Behavior contract (ADR-032 + DECISION-019 RC1):
        author_fixed with external fixed source (pinned_source not None):
            - Emits a source_binding block: {mode: author_fixed,
              source_type: <type>, pinned_ref: <url_or_id>,
              space_allow_list: [...], ingest_on_demand: <bool>}
            - trigger.on_request.inputs unchanged (generic string input).
            - The runtime executor uses pinned_ref to resolve the specific
              page before/instead of generic KB retrieval.

        author_fixed without external fixed source (pinned_source is None):
            - NO source_binding block emitted (unchanged pre-DECISION-019
              behavior for pure in-KB skills).
            - trigger.on_request.inputs unchanged.

        ask_parameterized:
            - A full source_binding block is emitted with all 6 required fields:
                mode, input_param, ingest_on_demand, source_type,
                space_allow_list, ephemeral_ttl_seconds.
            - trigger.on_request.inputs is replaced with a single typed entry:
                {name: <input_param>, type: confluence_page_ref,
                 description: "Confluence pageId or full page URL ...",
                 required: true}
              so that source_binding.input_param matches a declared trigger input
              (the P1-D contract check asserts this).
            - space_allow_list must be non-empty; the caller is responsible for
              deriving it (derive_space_allow_list) or surfacing an error.
    """
    task = intent.get("task_description", skill_name.replace("_", " "))
    output_format = intent.get("output_format", "markdown")
    delivery = intent.get("delivery", {"kind": "filesystem",
                                        "path": f"~/.kbf/outputs/{skill_name}.{output_format}"})
    trigger_cfg = intent.get("trigger", {"on_request": True})
    requires_extractions = intent.get("requires_extractions", [])

    result: dict = {
        "workflow_skill": skill_name,
        "persona": persona,
        "status": "draft",
        "trigger": _build_trigger(
            trigger_cfg,
            output_format,
            skill_name,
            source_binding_mode=source_binding_mode,
            input_param=input_param,
        ),
        "skill_card": _build_skill_card(task, skill_name, output_format),
        "requires_extractions": _build_requires_extractions(
            requires_extractions, fields, persona, skill_name, intent
        ),
        "synthesis": _build_synthesis(skill_name, output_format, fields, template_path, intent.get("layout")),
        "delivery": delivery,
        "eval": {
            "gold_set": f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl",
            "exit_criteria": {
                "field_accuracy": 0.85,
                "delivery_success_rate": 0.99,
            },
        },
    }

    # ADR-032 / DECISION-019 RC1: emit source_binding block.
    #
    # ask_parameterized: full 6-field block (ADR-032 §D.1).
    #
    # author_fixed + pinned_source: emit pinned-ref block so the runtime
    # executor resolves the exact page this skill was authored against
    # (DECISION-019 RC1 Option A).  The block is SYMMETRIC with
    # ask_parameterized except: no input_param (source is fixed, not
    # parameterized), and pinned_ref carries the URL/ID.
    #
    # author_fixed + no pinned_source: NO block (unchanged pre-DECISION-019
    # behavior; absent == author_fixed per ADR-032 §H migration rule).
    if source_binding_mode == "ask_parameterized":
        result["source_binding"] = {
            "mode": "ask_parameterized",
            "input_param": input_param,
            "ingest_on_demand": True,
            "source_type": source_type,
            "space_allow_list": list(space_allow_list) if space_allow_list else [],
            "ephemeral_ttl_seconds": ephemeral_ttl_seconds,
        }
    elif source_binding_mode == "author_fixed" and pinned_source is not None:
        # DECISION-019 RC1: pinned source binding for author_fixed with
        # an external fixed source (Confluence page identified at author time).
        # ingest_on_demand defaults False: executor looks up the pinned page
        # from the KB (already ingested at authorSkill time).
        result["source_binding"] = {
            "mode": "author_fixed",
            "source_type": pinned_source.get("source_type", "confluence_page"),
            "pinned_ref": pinned_source["pinned_ref"],
            "space_allow_list": list(pinned_source.get("space_allow_list") or []),
            "ingest_on_demand": False,
        }

    return result


# ---------------------------------------------------------------------------
# private helpers
# ---------------------------------------------------------------------------

def _build_trigger(
    trigger_cfg: dict,
    output_format: str,
    skill_name: str,
    source_binding_mode: str = "author_fixed",
    input_param: str = "page_id",
) -> dict:
    trigger: dict = {}

    if trigger_cfg.get("on_request", False):
        if source_binding_mode == "ask_parameterized":
            # ADR-032 §D.1: replace generic string input with a typed
            # confluence_page_ref input whose name matches source_binding.input_param.
            # The P1-D contract check asserts input_param is in declared inputs.
            inputs = [
                {
                    "name": input_param,
                    "type": "confluence_page_ref",
                    "description": (
                        "Confluence pageId or full page URL of the page to use"
                    ),
                    "required": True,
                }
            ]
        else:
            # author_fixed: use whatever the trigger_cfg provides (preserves
            # pre-ADR-032 byte-identical output for existing skills).
            inputs = trigger_cfg.get("inputs", [{"name": "input", "type": "string",
                                                  "description": "Query or filter input"}])
        trigger["on_request"] = {
            "enabled": True,
            "inputs": inputs,
            "output_format": output_format,
            "response_mode": "artifact_url",
        }

    if trigger_cfg.get("on_schedule"):
        cron = trigger_cfg["on_schedule"]
        trigger["on_schedule"] = {
            "cron": cron,
            "delivery": trigger_cfg.get("delivery", {
                "kind": "filesystem",
                "path": f"~/.kbf/outputs/{skill_name}.{output_format}",
            }),
        }

    return trigger or {"on_request": {"enabled": True, "output_format": output_format,
                                       "response_mode": "artifact_url"}}


def _build_skill_card(task: str, skill_name: str, output_format: str = "markdown") -> dict:
    summary = task[:200]
    # Include the output format token in the example invocation so the Tier-1
    # LLM router can distinguish skill cards by their artifact type (e.g. 'pptx'
    # vs 'eml') even when the task descriptions are similar.
    # BUG-queue-2ad9a FIX 2: task[:100] was too short to carry the output_format
    # context, causing the router to pick the wrong skill for .eml vs .pptx.
    example_invocation = f"{task[:300]} Output: {output_format}."
    return {
        "summary": summary,
        "use_when": f"User asks for: {summary} (produces {output_format} output)",
        "example_invocations": [example_invocation],
        "do_not_use_for": (
            "Single-fact lookups (use vector_search). "
            "Live operational data (use query_fleet)."
        ),
    }


def _build_requires_extractions(
    explicit_requires: list[dict],
    fields: list[str],
    persona: str,
    skill_name: str,
    intent: dict,
) -> list[dict]:
    if explicit_requires:
        return explicit_requires

    # Derive from intent: look for new_kb / reuse_kbs in intent.
    # Note: under ADR-027, intent["reuse"]["gaps"] from DESIGN_SKILL is the
    # "fields the source cannot supply" list (advisory), NOT the
    # "fields needing a new extraction skill" list. So we cannot use gaps
    # to populate required_fields directly — that would put unsupportable
    # fields into the workflow contract, which then fails ADR-017 link check
    # because the schema doesn't list them. The correct semantic: for a
    # brand-new skill, required_fields = ALL designed fields not already
    # covered by an existing (reused) KB.
    reuse = intent.get("reuse", {})
    covered = reuse.get("covered", {})

    entries: list[dict] = []

    # Fields not covered by any existing KB → go into the new KB for this skill
    covered_fields = set(covered.keys())
    new_kb_fields = [f for f in fields if f not in covered_fields]
    if new_kb_fields:
        entries.append({
            "kb": f"{persona}.{skill_name}",
            "required_fields": new_kb_fields,
        })

    # Group covered fields by their reused KB
    seen_kbs: dict[str, list[str]] = {}
    for field, kb in covered.items():
        seen_kbs.setdefault(kb, []).append(field)
    for kb, kb_fields in seen_kbs.items():
        # Persona-qualify reused KB refs. DESIGN_SKILL's reuse_plan.covered
        # emits bare KB names (e.g. 'tpm_weekly_ops'); the validator's
        # kb_index and ShimKb are keyed '{persona}.{kb}'. The new-KB entry
        # above is already qualified — keep reused entries consistent or
        # ADR-017 validate fails with "references unknown KB".
        qualified_kb = kb if "." in kb else f"{persona}.{kb}"
        entries.append({
            "kb": qualified_kb,
            "required_fields": kb_fields,
        })

    if not entries and fields:
        entries.append({
            "kb": f"{persona}.{skill_name}",
            "required_fields": list(fields),
        })

    return entries


def _build_synthesis(
    skill_name: str,
    output_format: str,
    fields: list[str],
    template_path: str | None,
    layout: str | None = None,
) -> dict:
    template = template_path or f"synthesis/templates/{skill_name}.{output_format}"
    slide_mapping_path = f"synthesis/mappings/{skill_name}.yaml"

    synthesis: dict = {
        "output_format": output_format,
        "template": template,
        "slide_mapping": slide_mapping_path,
    }

    # Layout (e.g. 'weekly_exec_review_v1') tells the renderer to dispatch
    # to a layout-aware builder instead of the generic title+content per
    # slide fallback. Set by DESIGN_SKILL based on the user's intent.
    if layout:
        synthesis["layout"] = layout

    if fields:
        synthesis["field_mapping"] = {
            f: {"section": f.replace("_", " ").title(), "source_field": f}
            for f in fields
        }

    return synthesis
