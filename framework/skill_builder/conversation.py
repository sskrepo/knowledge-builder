"""conversation — interactive skill-builder session (author_skill API).

Per ADR-015 §Conversation contract. Implements a state machine that drives the
full skill authoring lifecycle from IDENTIFY_PERSONA through DONE. Works in
stub LLM mode with template-based responses — no external services required.

State machine (14 states):
  IDENTIFY_PERSONA → ANALYZE_ARTIFACT → REVIEW_FIELDS → REVIEW_SCHEMA →
  CHECK_REUSE → CONFIGURE_SOURCES → CONFIGURE_TRIGGERS → PREVIEW → CONFIRM →
  COMMITTED → VALIDATE → INGEST → EVAL → PROMOTE → DONE

Session persistence: sessions are serializable via to_dict()/from_dict() for
storage in ADB (keyed by synth_id + user_id).  This enables resume across
client restarts and listing all in-progress authoring sessions per user.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_confluence_adapter(kbf_env: str, repo_root: "Path"):
    """Build the Confluence adapter from config.

    Merges base framework/config/adapters/confluence.yaml with env-specific
    adapters_overrides.confluence from {kbf_env}.yaml (e.g. laptop.yaml sets
    mode: codex_proxy for eMCP via Codex CLI).  Returns None only when no
    Confluence config exists at all (falls back to fixture HTML).
    """
    try:
        import yaml as _yaml

        # Load base adapter config
        base_path = repo_root / "framework" / "config" / "adapters" / "confluence.yaml"
        base_cfg: dict = {}
        if base_path.exists():
            base_cfg = _yaml.safe_load(base_path.read_text()) or {}

        # Load env-specific overrides (laptop.yaml, staging.yaml, prod.yaml)
        env_path = repo_root / "framework" / "config" / f"{kbf_env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        overrides = env_cfg.get("adapters_overrides", {}).get("confluence", {})

        # Merge: base first, env overrides on top
        merged = {**base_cfg, **overrides}
        mode = merged.get("mode", "")

        if not mode:
            log.info("Confluence mode not configured — using fixture mode")
            return None

        if mode == "codex_proxy":
            from ..adapters.confluence.codex_proxy import ConfluenceCodexProxyAdapter
            cp_cfg = {**merged.get("codex_proxy", {}), **overrides.get("codex_proxy", {})}
            log.info("Confluence adapter: codex_proxy server_name=%s", cp_cfg.get("server_name"))
            return ConfluenceCodexProxyAdapter(cp_cfg)

        if mode == "codex_cli":
            from ..adapters.confluence.codex_cli import ConfluenceCodexCLIAdapter
            cc_cfg = {**merged.get("codex_cli", {}), **overrides.get("codex_cli", {})}
            log.info("Confluence adapter: codex_cli server_name=%s", cc_cfg.get("server_name"))
            return ConfluenceCodexCLIAdapter(cc_cfg)

        if mode == "emcp_direct":
            # Direct HTTPS+OAuth to the emcp.oracle.com Confluence MCP server.
            # Uses the bearer token codex stored in the macOS Keychain (after
            # `codex mcp login central_confluence`). ~10s/page versus the 180s
            # timeout we saw with codex_proxy (BUG-queue-d3ec0).
            from ..adapters.confluence.emcp_direct import ConfluenceEmcpDirectAdapter
            ed_cfg = {**merged.get("emcp_direct", {}), **overrides.get("emcp_direct", {})}
            log.info(
                "Confluence adapter: emcp_direct server_name=%s",
                ed_cfg.get("server_name"),
            )
            return ConfluenceEmcpDirectAdapter(ed_cfg)

        if mode == "mcp":
            from ..adapters.confluence.mcp import ConfluenceMcpAdapter
            log.info("Confluence adapter: mcp endpoint=%s", merged.get("mcp", {}).get("endpoint"))
            return ConfluenceMcpAdapter(merged.get("mcp", {}))

        if mode == "native":
            from ..adapters.confluence.native import ConfluenceNativeAdapter
            log.info("Confluence adapter: native base_url=%s", merged.get("native", {}).get("base_url"))
            return ConfluenceNativeAdapter(merged.get("native", {}))

        log.info("Confluence mode=%r not recognised — using fixture mode", mode)
        return None
    except Exception as exc:
        log.warning("could not build Confluence adapter (%s) — using fixture mode", exc)
        return None


_ANALYZE_ARTIFACT_PROMPT = """\
You are a Knowledge Builder Framework schema engineer. An artifact has been parsed \
and its structural sections identified.

Persona: {persona}
Intent: "{intent}"
Artifact type: {artifact_type}

Sections / slides found in the artifact:
{field_contexts}

For EACH field listed above, decide:
1. The most appropriate JSON Schema type ("string", "integer", "boolean", "array").
   Use "array" only for genuinely multi-valued list fields. Default to "string".
2. A precise 1–2 sentence extraction instruction that tells the LLM parser exactly
   what content to look for and how to format the output.

Return ONLY a JSON object mapping field_name → object with "type" and "description":
{{
  "schedule_health": {{
    "type": "string",
    "description": "RAG status (Red/Amber/Green) for the schedule, with a 1–2 sentence \
justification citing specific milestone dates or blockers from the slide."
  }}
}}
"""

STATES = [
    "IDENTIFY_PERSONA",
    "ANALYZE_ARTIFACT",
    "REVIEW_FIELDS",
    "REVIEW_SCHEMA",
    "CHECK_REUSE",
    "CONFIGURE_SOURCES",
    "CONFIGURE_TRIGGERS",
    "PREVIEW",
    "CONFIRM",
    "COMMITTED",
    "VALIDATE",
    "INGEST",
    "EVAL",
    "PROMOTE",
    "DONE",
]


@dataclass
class ConversationTurn:
    """Return value from every state handler."""

    synth_id: str = ""
    state: str = ""
    message: str = ""
    data: dict | None = None
    options: list[str] | None = None
    artifacts_preview: dict | None = None
    progress: dict | None = None
    done: bool = False


def _progress(state: str) -> dict:
    """Return a progress dict for the given state."""
    try:
        step = STATES.index(state) + 1
    except ValueError:
        step = 0
    return {"step": step, "total": len(STATES), "label": state.replace("_", " ").title()}


@dataclass
class _SessionData:
    """Mutable session state threaded through the conversation."""

    intent_description: str = ""
    artifact_path: str = ""
    fields: list[str] = field(default_factory=list)
    field_specs: dict[str, dict] = field(default_factory=dict)
    slide_mapping: dict | None = None
    llm_suggested_specs: dict[str, dict] = field(default_factory=dict)  # LLM read at ANALYZE_ARTIFACT
    reuse_result: dict = field(default_factory=lambda: {"covered": {}, "gaps": []})
    sources: list[dict] = field(default_factory=list)
    trigger: dict = field(default_factory=lambda: {"on_request": True})
    output_format: str = "markdown"
    persona: str = ""
    skill_name: str = ""
    user_id: str = ""
    synth_id: str = ""
    synthesized_artifacts: dict = field(default_factory=dict)
    committed_paths: list[str] = field(default_factory=list)
    validation_result: dict | None = None
    ingest_result: dict | None = None
    eval_result: dict | None = None
    created_at: str = ""
    updated_at: str = ""


class SkillBuilderConversation:
    """Interactive skill-builder session (author_skill API).

    Drives the full authoring lifecycle through a 14-state conversation.
    Each call to start() / respond() returns a ConversationTurn describing
    what to display to the user and the current state.

    The session is serializable (to_dict/from_dict) for persistence in ADB,
    enabling resume across client restarts.

    In stub LLM mode responses are template-based; real LLM integration would
    replace the _* handlers while keeping the state machine intact.
    """

    def __init__(self, persona: str = "", user_id: str = "", llm=None, artifact_store=None, skill_store=None):
        # skill_store is REQUIRED. ADB is the source of truth — there is no
        # filesystem-only / stub-mode operating mode. Passing skill_store=None
        # silently dropped ADB writes in older code (see synth-tpm-14a54555 /
        # BUG-queue-e8298), so we now fail fast at construction. Tests must
        # supply a MagicMock or a real SkillStore.
        if skill_store is None:
            raise ValueError(
                "SkillBuilderConversation: skill_store is required. "
                "ADB is the source of truth — there is no filesystem-only "
                "mode. If you reached this from a real request, the route "
                "handler forgot to pass app.state.skill_store (this was the "
                "exact bug behind synth-tpm-14a54555). In tests, pass a "
                "MagicMock())."
            )
        self._persona = persona
        self._llm = llm
        self._artifact_store = artifact_store
        self._skill_store = skill_store
        self._state = "IDENTIFY_PERSONA"
        now = _now_iso()
        synth_id = _make_synth_id(persona, now)
        self._data = _SessionData(
            persona=persona,
            user_id=user_id,
            synth_id=synth_id,
            created_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, intent_description: str = "") -> ConversationTurn:
        """Begin the session. Call once before any respond() calls."""
        self._data.intent_description = intent_description.strip()
        self._data.updated_at = _now_iso()

        if self._data.persona and self._data.intent_description:
            self._data.skill_name = _slugify(self._data.intent_description)
            self._state = "ANALYZE_ARTIFACT"
            return self._turn(self._handle_analyze_artifact_prompt())
        if self._data.persona:
            self._state = "IDENTIFY_PERSONA"
            return self._turn(ConversationTurn(
                state="IDENTIFY_PERSONA",
                message=(
                    f"Skill Builder — persona: {self._data.persona}\n\n"
                    "What task do you want automated? Describe it in plain English.\n"
                    "Example: 'Produce a weekly project status PPT for exec review every Friday'"
                ),
            ))
        self._state = "IDENTIFY_PERSONA"
        return self._turn(self._prompt_identify_persona())

    def respond(self, user_input: str) -> ConversationTurn:
        """Submit a user response in the current state."""
        user_input = user_input.strip()
        self._data.updated_at = _now_iso()

        # Cross-cutting command: "rename skill to <name>" — valid before COMMIT
        _PRE_COMMIT_STATES = frozenset({
            "IDENTIFY_PERSONA", "ANALYZE_ARTIFACT", "REVIEW_FIELDS",
            "REVIEW_SCHEMA", "CHECK_REUSE", "CONFIGURE_SOURCES",
            "CONFIGURE_TRIGGERS", "PREVIEW", "CONFIRM",
        })
        if self._state in _PRE_COMMIT_STATES:
            _rename_m = re.match(r"(?i)rename\s+skill\s+to\s+(\S+)", user_input)
            if _rename_m:
                return self._turn(self._handle_rename_skill(_rename_m.group(1)))

        handler = {
            "IDENTIFY_PERSONA": self._handle_identify_persona,
            "ANALYZE_ARTIFACT": self._handle_analyze_artifact,
            "REVIEW_FIELDS": self._handle_review_fields_response,
            "REVIEW_SCHEMA": self._handle_review_schema_response,
            "CHECK_REUSE": self._handle_check_reuse_response,
            "CONFIGURE_SOURCES": self._handle_configure_sources_response,
            "CONFIGURE_TRIGGERS": self._handle_configure_triggers_response,
            "PREVIEW": self._handle_preview_response,
            "CONFIRM": self._handle_confirm_response,
            "COMMITTED": self._handle_committed_response,
            "VALIDATE": self._handle_validate_response,
            "INGEST": self._handle_ingest_response,
            "EVAL": self._handle_eval_response,
            "PROMOTE": self._handle_promote_response,
            "DONE": lambda _: ConversationTurn(
                state="DONE", message="Session complete.", done=True,
            ),
        }.get(self._state)

        if handler is None:
            return self._turn(ConversationTurn(
                state=self._state,
                message=f"Unknown state {self._state!r}. This is a bug.",
                done=True,
            ))
        return self._turn(handler(user_input))

    def get_state(self) -> dict:
        """Return a snapshot of the current session state (for GET endpoint)."""
        return {
            "synth_id": self._data.synth_id,
            "state": self._state,
            "persona": self._data.persona,
            "skill_name": self._data.skill_name,
            "intent_description": self._data.intent_description,
            "user_id": self._data.user_id,
            "artifact_path": self._data.artifact_path,
            "fields": list(self._data.fields),
            "field_specs": dict(self._data.field_specs),
            "reuse": dict(self._data.reuse_result),
            "sources": list(self._data.sources),
            "trigger": dict(self._data.trigger),
            "output_format": self._data.output_format,
            "committed_paths": list(self._data.committed_paths),
            "validation_result": self._data.validation_result,
            "ingest_result": self._data.ingest_result,
            "eval_result": self._data.eval_result,
            "created_at": self._data.created_at,
            "updated_at": self._data.updated_at,
            "progress": _progress(self._state),
        }

    def to_dict(self) -> dict:
        """Serialize entire session for DB persistence.

        NOTE: get_state() intentionally omits synthesized_artifacts and
        slide_mapping to keep the GET-endpoint snapshot lean (artifact content
        can be several KB of YAML/JSON).  We add them back here so that the
        PREVIEW → COMMIT and ANALYZE_ARTIFACT → REVIEW_SCHEMA transitions work
        correctly when a session is saved and resumed across separate MCP calls.
        Without this, _write_artifacts() iterates an empty dict and commits 0
        artifacts (BUG-004).
        """
        d = {"state": self._state, "persona": self._persona, **self.get_state()}
        d["synthesized_artifacts"] = dict(self._data.synthesized_artifacts)
        if self._data.slide_mapping is not None:
            d["slide_mapping"] = dict(self._data.slide_mapping)
        if self._data.llm_suggested_specs:
            d["llm_suggested_specs"] = dict(self._data.llm_suggested_specs)
        return d

    @classmethod
    def from_dict(cls, d: dict, llm=None, artifact_store=None, skill_store=None) -> "SkillBuilderConversation":
        """Restore a session from a persisted dict.

        skill_store is REQUIRED. See __init__ docstring for rationale.
        Restoring a session without skill_store would mean any subsequent
        COMMIT in this session would silently lose the ADB write — exactly
        the synth-tpm-14a54555 bug.
        """
        if skill_store is None:
            raise ValueError(
                "SkillBuilderConversation.from_dict: skill_store is required. "
                "Restoring a session without it would let the next COMMIT "
                "silently lose ADB writes (the synth-tpm-14a54555 bug)."
            )
        obj = cls.__new__(cls)
        obj._persona = d.get("persona", "")
        obj._llm = llm
        obj._artifact_store = artifact_store
        obj._skill_store = skill_store
        obj._state = d.get("state", "IDENTIFY_PERSONA")
        obj._data = _SessionData(
            intent_description=d.get("intent_description", ""),
            artifact_path=d.get("artifact_path", ""),
            fields=list(d.get("fields", [])),
            field_specs=dict(d.get("field_specs", {})),
            slide_mapping=d.get("slide_mapping"),
            llm_suggested_specs=dict(d.get("llm_suggested_specs", {})),
            reuse_result=dict(d.get("reuse", {"covered": {}, "gaps": []})),
            sources=list(d.get("sources", [])),
            trigger=dict(d.get("trigger", {"on_request": True})),
            output_format=d.get("output_format", "markdown"),
            persona=d.get("persona", ""),
            skill_name=d.get("skill_name", ""),
            user_id=d.get("user_id", ""),
            synth_id=d.get("synth_id", ""),
            synthesized_artifacts=dict(d.get("synthesized_artifacts", {})),
            committed_paths=list(d.get("committed_paths", [])),
            validation_result=d.get("validation_result"),
            ingest_result=d.get("ingest_result"),
            eval_result=d.get("eval_result"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )
        return obj

    def _turn(self, turn: ConversationTurn) -> ConversationTurn:
        """Stamp synth_id and progress on every outgoing turn."""
        turn.synth_id = self._data.synth_id
        turn.progress = _progress(self._state)
        return turn

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _prompt_identify_persona(self) -> ConversationTurn:
        personas = _list_available_personas()
        persona_lines = "\n".join(
            f"  • {p['name']} — {p['display_name']} ({p['skill_count']} skills)"
            for p in personas
        )
        return ConversationTurn(
            state="IDENTIFY_PERSONA",
            message=(
                "Which persona is this skill for?\n\n"
                f"{persona_lines}\n\n"
                "Type the persona name and describe the task.\n"
                "Example: 'ops_eng — automate weekly ADB-S migration status updates'"
            ),
            data={"personas": personas},
            options=[p["name"] for p in personas],
        )

    def _handle_identify_persona(self, user_input: str) -> ConversationTurn:
        if not user_input:
            return self._prompt_identify_persona()

        parts = re.split(r"\s*[—–\-:]\s*", user_input, maxsplit=1)
        persona_candidate = _to_field_name(parts[0])
        intent = parts[1].strip() if len(parts) > 1 else ""

        known = [p["name"] for p in _list_available_personas()]
        if persona_candidate not in known and known:
            return ConversationTurn(
                state="IDENTIFY_PERSONA",
                message=f"Unknown persona '{persona_candidate}'. Available: {', '.join(known)}",
                options=known,
            )

        self._persona = persona_candidate
        self._data.persona = persona_candidate
        self._data.synth_id = _make_synth_id(persona_candidate, self._data.created_at)

        if intent:
            self._data.intent_description = intent
            self._data.skill_name = _slugify(intent)
            self._state = "ANALYZE_ARTIFACT"
            return self._handle_analyze_artifact_prompt()

        self._state = "IDENTIFY_PERSONA"
        return ConversationTurn(
            state="IDENTIFY_PERSONA",
            message=(
                f"Persona: {persona_candidate}\n\n"
                "What task do you want automated? Describe it in plain English.\n"
                "Example: 'Produce a weekly project status PPT for exec review every Friday'"
            ),
        )

    def _handle_rename_skill(self, new_name: str) -> ConversationTurn:
        """Apply 'rename skill to <name>' command — valid at any pre-COMMIT state."""
        old_name = self._data.skill_name
        new_slug = _slugify(new_name)
        if not new_slug or new_slug == "unnamed_skill":
            return ConversationTurn(
                state=self._state,
                message=(
                    f"'{new_name}' is not a valid skill name. "
                    "Use a short descriptive snake_case name (e.g. 'weekly_exec_review')."
                ),
            )
        self._data.skill_name = new_slug
        return ConversationTurn(
            state=self._state,
            message=(
                f"✓ Skill renamed: '{old_name}' → '{new_slug}'.\n\n"
                "Now continue — re-send your previous input to proceed."
            ),
        )

    def _handle_analyze_artifact_prompt(self) -> ConversationTurn:
        slug = self._data.skill_name
        slug_notice = ""
        if len(slug) >= 48:
            log.warning(
                "skill name slug is near or at the length limit: %r (%d chars)",
                slug, len(slug),
            )
            slug_notice = (
                f"\n\nNote: your skill has been auto-named '{slug}'. "
                "You can type 'rename skill to <shorter_name>' at any point before COMMIT "
                "to use a shorter, more descriptive name."
            )
        return ConversationTurn(
            state="ANALYZE_ARTIFACT",
            message=(
                f"Great. You want to automate: '{self._data.intent_description}'.{slug_notice}\n\n"
                "Now show me an example outcome. Provide the path to a PPT, DOCX, Markdown, "
                "or text file that represents the kind of output this skill should produce.\n\n"
                "If you don't have a file, type the field names separated by commas "
                "(e.g. 'week_id, rag_status, blockers, exec_asks')."
            ),
            options=[
                "framework/_dev_fixtures/skill_builder_intents/example_workflow.yaml",
                "Enter field names manually",
            ],
        )

    def _llm_analyze_artifact(
        self,
        fields: list[str],
        mapping: dict | None,
    ) -> dict[str, dict]:
        """Call the LLM to suggest type + extraction description for every field.

        Called immediately after analyze_artifact() at ANALYZE_ARTIFACT state.
        Returns {field_name: {"type": ..., "description": ...}}.
        Returns {} when no LLM is wired or on any failure (graceful degradation).
        """
        if self._llm is None or not fields:
            return {}

        try:
            # Determine artifact type label from mapping kinds
            artifact_type = "document"
            if mapping:
                kinds = {v.get("kind", "") for v in mapping.values()}
                if "slide_title" in kinds:
                    artifact_type = "PowerPoint presentation"

            # Build per-field context lines (raw section title + sample body text)
            context_lines: list[str] = []
            for f in fields:
                m = (mapping or {}).get(f, {})
                raw_label = m.get("raw_title") or m.get("raw_heading") or f
                body = m.get("body_text", "").strip()
                location = ""
                if "slide" in m:
                    location = f"slide {m['slide'] + 1}"
                elif "line_number" in m:
                    location = f"line {m['line_number']}"
                ctx = f"- {f}: section/slide titled '{raw_label}'"
                if location:
                    ctx += f" ({location})"
                if body:
                    ctx += f"\n  Sample content: {body[:200]}"
                context_lines.append(ctx)

            prompt = _ANALYZE_ARTIFACT_PROMPT.format(
                persona=self._data.persona or "unknown",
                intent=self._data.intent_description or "",
                artifact_type=artifact_type,
                field_contexts="\n".join(context_lines),
            )

            result = self._llm.chat(
                model="synthesis",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=2048,
            )
            raw = result["text"] if isinstance(result, dict) else str(result)

            import re as _re
            raw_clean = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
            data = json.loads(raw_clean)

            # Validate and normalise — only keep entries with a usable description
            specs: dict[str, dict] = {}
            valid_types = {"string", "integer", "number", "boolean", "array"}
            for f in fields:
                entry = data.get(f)
                if not isinstance(entry, dict):
                    continue
                desc = str(entry.get("description", "")).strip()
                if not desc:
                    continue
                t = str(entry.get("type", "string"))
                if t not in valid_types:
                    t = "string"
                specs[f] = {"type": t, "description": desc}

            log.info(
                "_llm_analyze_artifact: got specs for %d/%d fields",
                len(specs),
                len(fields),
            )
            return specs

        except Exception as exc:
            log.warning("_llm_analyze_artifact: LLM call failed (%s) — skipping", exc)
            return {}

    def _handle_analyze_artifact(self, user_input: str) -> ConversationTurn:
        from .analyze_artifact import analyze_artifact

        path = user_input.strip()

        # --- artifact: prefix path (uploadArtifact flow, ADR-021) ---
        if path.startswith("artifact:"):
            return self._handle_uploaded_artifact(path)

        # --- Local filesystem path (laptop direct-path) ---
        if Path(path).exists() and Path(path).suffix in (".pptx", ".docx", ".md", ".txt"):
            fields, mapping = analyze_artifact(path)
            self._data.artifact_path = path
            self._data.fields = fields
            self._data.slide_mapping = mapping
            self._data.llm_suggested_specs = self._llm_analyze_artifact(fields, mapping)
            source = f"artifact at {path!r}"
        else:
            fields, mapping = self._parse_fields_from_input(user_input)
            self._data.fields = fields
            self._data.slide_mapping = mapping
            # LLM analysis even for manual field lists — uses field names + intent as context
            self._data.llm_suggested_specs = self._llm_analyze_artifact(fields, mapping)
            source = "your field list"

        self._state = "REVIEW_FIELDS"
        return self._handle_review_fields_prompt(source)

    def _handle_uploaded_artifact(self, user_input: str) -> ConversationTurn:
        """Handle 'artifact:<filename> id:<artifact_id>' input (ADR-021).

        Parses the artifact:/<filename> id:<artifact_id> syntax, resolves the
        artifact via ArtifactStore, and calls analyze_artifact on the local path.

        Falls back to generic field list if the artifact_store is not wired or
        the artifact_id is not found.
        """
        from .analyze_artifact import analyze_artifact
        import re

        # Parse "artifact:<filename> id:<artifact_id>"
        m = re.match(r"^artifact:(\S+)\s+id:(\S+)$", user_input.strip())
        if not m:
            # Malformed — treat as field-name input
            log.warning(
                "_handle_uploaded_artifact: could not parse %r — falling back to field list",
                user_input,
            )
            fields, mapping = self._parse_fields_from_input(user_input)
            self._data.fields = fields
            self._data.slide_mapping = mapping
            self._state = "REVIEW_FIELDS"
            return self._handle_review_fields_prompt("your field list")

        filename, artifact_id = m.group(1), m.group(2)

        if self._artifact_store is None:
            log.warning(
                "_handle_uploaded_artifact: artifact_store not wired — fallback for %s",
                artifact_id,
            )
            fields, mapping = self._parse_fields_from_input(filename)
            self._data.fields = fields
            self._data.slide_mapping = mapping
            self._state = "REVIEW_FIELDS"
            return self._handle_review_fields_prompt(f"filename '{filename}'")

        local_path = self._artifact_store.resolve(artifact_id)

        if local_path is None:
            log.warning(
                "_handle_uploaded_artifact: artifact_id=%s not found in store",
                artifact_id,
            )
            self._state = "REVIEW_FIELDS"
            fields, mapping = self._parse_fields_from_input(filename)
            self._data.fields = fields
            self._data.slide_mapping = mapping
            return ConversationTurn(
                state="REVIEW_FIELDS",
                message=(
                    f"⚠️ Could not find uploaded artifact '{artifact_id}' on the server. "
                    "I'll derive fields from the filename instead — "
                    "you can correct them in the next step.\n\n"
                ) + self._handle_review_fields_prompt(f"filename '{filename}'").message,
                options=["ok", "add <field>", "remove <field>", "rename <old> to <new>"],
            )

        fields, mapping = analyze_artifact(str(local_path))
        self._data.artifact_path = str(local_path)
        self._data.fields = fields
        self._data.slide_mapping = mapping
        self._data.llm_suggested_specs = self._llm_analyze_artifact(fields, mapping)

        self._state = "REVIEW_FIELDS"
        return self._handle_review_fields_prompt(f"artifact '{filename}'")

    def _handle_review_fields_prompt(self, source: str) -> ConversationTurn:
        fields = self._data.fields
        field_list = "\n".join(f"  • {f}" for f in fields)
        return ConversationTurn(
            state="REVIEW_FIELDS",
            message=(
                f"From {source} I found these fields:\n{field_list}\n\n"
                "Would you like to add, remove, or rename any? "
                "Type changes (e.g. 'add priority_score', 'remove details', "
                "'rename summary to executive_summary') or 'ok' to continue."
            ),
            options=["ok", "add <field>", "remove <field>", "rename <old> to <new>"],
        )

    def _handle_review_fields_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()

        if lowered in ("ok", "looks good", "continue", "done", "yes"):
            return self._advance_to_review_schema()

        edits = _parse_field_edits(user_input)
        for edit in edits:
            if edit[0] == "add":
                f = _to_field_name(edit[1])
                if f and f not in self._data.fields:
                    self._data.fields.append(f)
            elif edit[0] == "remove":
                f = _to_field_name(edit[1])
                self._data.fields = [x for x in self._data.fields if x != f]
            elif edit[0] == "rename":
                old, new = _to_field_name(edit[1]), _to_field_name(edit[2])
                self._data.fields = [new if x == old else x for x in self._data.fields]

        if not edits:
            return ConversationTurn(
                state="REVIEW_FIELDS",
                message=(
                    "I didn't understand that edit. Try 'add <field>', "
                    "'remove <field>', 'rename <old> to <new>', or 'ok'."
                ),
                options=["ok", "add <field>", "remove <field>"],
            )

        return self._handle_review_fields_prompt("updated list")

    # -- REVIEW_SCHEMA ---------------------------------------------------

    def _advance_to_review_schema(self) -> ConversationTurn:
        """Generate field specs (type + description) and present for review.

        Two-pass approach:
        1. Fields covered by llm_suggested_specs (from ANALYZE_ARTIFACT LLM call):
           apply the LLM's type + description directly — these will be high quality.
        2. User-added fields NOT in llm_suggested_specs (delta fields):
           call synthesize_field_descriptions() for a targeted LLM description
           using raw_title + body_text context from the slide_mapping.
        Falls back to heuristic when no LLM is wired up.
        """
        from .synthesize_schema import _infer_field_spec, synthesize_field_descriptions

        new_fields = [f for f in self._data.fields if f not in self._data.field_specs]
        if not new_fields:
            self._state = "REVIEW_SCHEMA"
            return self._prompt_review_schema()

        # Pass 1 — LLM suggestions from ANALYZE_ARTIFACT (high-quality, no extra call)
        llm_specs = self._data.llm_suggested_specs
        delta_fields: list[str] = []
        for f in new_fields:
            if f in llm_specs:
                base_spec = _infer_field_spec(f)
                # Override type and description from LLM suggestion
                base_spec["type"] = llm_specs[f].get("type", base_spec.get("type", "string"))
                base_spec["description"] = llm_specs[f]["description"]
                self._data.field_specs[f] = base_spec
            else:
                delta_fields.append(f)

        # Pass 2 — delta fields (user added after ANALYZE_ARTIFACT, or LLM missed them)
        if delta_fields:
            descriptions = synthesize_field_descriptions(
                fields=delta_fields,
                mapping=self._data.slide_mapping,
                intent=self._data.intent_description or "",
                persona=self._data.persona or "",
                llm=self._llm,
            )
            for f in delta_fields:
                base_spec = _infer_field_spec(f)
                base_spec["description"] = descriptions.get(f, base_spec["description"])
                self._data.field_specs[f] = base_spec

        self._state = "REVIEW_SCHEMA"
        return self._prompt_review_schema(delta_fields=delta_fields if delta_fields else None)

    def _prompt_review_schema(self, delta_fields: list[str] | None = None) -> ConversationTurn:
        delta_note = ""
        original_fields = set(self._data.llm_suggested_specs.keys())
        artifact_was_analyzed = bool(self._data.artifact_path)

        # "added after artifact analysis" — only meaningful when a real artifact was
        # uploaded AND the original LLM-suggested fields overlap with the final set.
        # Without an artifact, delta_fields just means the LLM didn't pre-spec them,
        # not that they were added "after" anything (BUG-938f0, BUG-9c3d9).
        if delta_fields and artifact_was_analyzed:
            retained_original = original_fields & set(self._data.fields)
            if retained_original:
                delta_note = (
                    f"\n⚠️ {len(delta_fields)} field(s) were added after the artifact analysis "
                    f"({', '.join(delta_fields)}) — their descriptions were synthesised from "
                    "context and may need more refinement than the rest.\n"
                )

        # "removed from artifact" — only show when artifact produced original fields
        # that the user then dropped (not meaningful when no artifact was uploaded).
        if artifact_was_analyzed and original_fields:
            removed = original_fields - set(self._data.fields)
            if removed:
                delta_note += (
                    f"ℹ️ {len(removed)} field(s) identified in the artifact were removed "
                    f"({', '.join(sorted(removed))}) — remove them only if they're truly not needed.\n"
                )

        lines = [
            "These extraction instructions tell the parser what to look for in each field.",
            "The description is the most important part — it controls extraction quality.",
        ]
        if delta_note:
            lines.append(delta_note)
        else:
            lines.append("")
        for f in self._data.fields:
            spec = self._data.field_specs.get(f, {})
            t = spec.get("type", "string")
            desc = spec.get("description", "")
            lines.append(f"  {f} [{t}]: {desc}")

        lines.append("")
        lines.append("Edit any field's extraction instructions:")
        lines.append("  describe <field> as <extraction instruction>")
        lines.append("  set type of <field> to <type>")
        lines.append("  set maxLength of <field> to <number>")
        lines.append("  set enum of <field> to <val1>, <val2>, ...")
        lines.append("")
        lines.append("Type 'ok' when the descriptions accurately capture what to extract.")

        field_data = [
            {"name": f, **self._data.field_specs.get(f, {})}
            for f in self._data.fields
        ]

        return ConversationTurn(
            state="REVIEW_SCHEMA",
            message="\n".join(lines),
            data={"field_specs": field_data},
            options=["ok", "describe <field> as <text>", "set type of <field> to <type>"],
        )

    def _handle_review_schema_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()

        if lowered in ("ok", "looks good", "continue", "done", "yes"):
            return self._advance_to_check_reuse()

        # Multi-command support: split on newlines, execute each line in order.
        # A single-line input goes through the same path (list of one).
        lines = [ln.strip() for ln in user_input.splitlines() if ln.strip()]
        if len(lines) > 1:
            return self._handle_bulk_schema_edits(lines)

        return self._apply_single_schema_command(user_input)

    def _handle_bulk_schema_edits(self, lines: list[str]) -> ConversationTurn:
        """Apply multiple schema edit commands in one turn.

        Executes each line as an independent command (describe / set type /
        set maxLength / set enum).  Collects errors and reports them alongside
        the count of successful edits, then re-renders the schema prompt.
        """
        applied: list[str] = []
        errors: list[str] = []

        for line in lines:
            result = self._apply_single_schema_command(line, _bulk=True)
            if result is None:
                # Successfully applied
                applied.append(line)
            else:
                errors.append(f"  ✗ {line!r} — {result}")

        # Prepend a summary banner to the schema re-render.
        summary_parts = []
        if applied:
            summary_parts.append(f"✓ Applied {len(applied)} edit(s).")
        if errors:
            summary_parts.append(
                f"⚠ {len(errors)} line(s) not recognised (skipped):\n" + "\n".join(errors)
            )

        base_turn = self._prompt_review_schema()
        base_turn.message = "\n".join(summary_parts) + "\n\n" + base_turn.message
        return base_turn

    def _apply_single_schema_command(
        self, user_input: str, _bulk: bool = False
    ) -> "ConversationTurn | None":
        """Apply one schema edit command.

        When called from _handle_bulk_schema_edits (_bulk=True):
          - Returns None on success (caller accumulates).
          - Returns the error message string on failure (caller collects).
        When called directly (_bulk=False):
          - Returns a ConversationTurn (prompt or error) as before.
        """

        def _err(msg: str):
            if _bulk:
                return msg  # type: ignore[return-value]
            return ConversationTurn(state="REVIEW_SCHEMA", message=msg)

        def _ok():
            if _bulk:
                return None
            return self._prompt_review_schema()

        # describe <field> as <text>
        m = re.match(r"(?i)describe\s+(\S+)\s+as\s+(.+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            new_desc = m.group(2).strip().strip("'\"")
            if field_name in self._data.field_specs:
                self._data.field_specs[field_name]["description"] = new_desc
            elif field_name in self._data.fields:
                self._data.field_specs[field_name] = {
                    "type": "string", "description": new_desc, "maxLength": 500,
                }
            else:
                return _err(
                    f"Unknown field '{field_name}'. Available: {', '.join(self._data.fields)}"
                )
            return _ok()

        # set type of <field> to <type>
        m = re.match(r"(?i)set\s+type\s+of\s+(\S+)\s+to\s+(\S+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            new_type = m.group(2).strip()
            if new_type in ("string", "integer", "number", "boolean", "array", "object"):
                if field_name in self._data.field_specs:
                    self._data.field_specs[field_name]["type"] = new_type
                    return _ok()
            return _err(
                f"Invalid type '{new_type}'. Valid: string, integer, number, boolean, array, object"
            )

        # set maxLength of <field> to <number>
        m = re.match(r"(?i)set\s+maxLength\s+of\s+(\S+)\s+to\s+(\d+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            if field_name in self._data.field_specs:
                self._data.field_specs[field_name]["maxLength"] = int(m.group(2))
                return _ok()
            return _err(f"Unknown field '{field_name}'.")

        # set enum of <field> to <val1>, <val2>, ...
        m = re.match(r"(?i)set\s+enum\s+of\s+(\S+)\s+to\s+(.+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            vals = [v.strip().strip("'\"") for v in m.group(2).split(",") if v.strip()]
            if field_name in self._data.field_specs and vals:
                self._data.field_specs[field_name]["enum"] = vals
                return _ok()
            return _err(f"Unknown field '{field_name}' or empty enum list.")

        return _err(
            "I didn't understand that edit. Try:\n"
            "  'describe <field> as <extraction instruction>'\n"
            "  'set type of <field> to <type>'\n"
            "  'ok' to continue"
        )

    # -- CHECK_REUSE -----------------------------------------------------

    def _advance_to_check_reuse(self) -> ConversationTurn:
        from .reuse_detector import detect_reuse
        from ..orchestrator.shim_kb import ShimKb

        pb_dir = REPO_ROOT / "framework" / "persona_builders"
        try:
            shim = ShimKb(pb_dir)
            result = detect_reuse(self._data.fields, self._data.persona, shim)
        except Exception as e:
            log.warning("reuse detection failed: %s; assuming no reuse", e)
            result = {"covered": {}, "gaps": list(self._data.fields)}

        self._data.reuse_result = result
        self._state = "CHECK_REUSE"

        covered = result.get("covered", {})
        gaps = result.get("gaps", [])

        if not covered and not gaps:
            return self._advance_to_configure_sources()

        covered_lines = "\n".join(
            f"  • {f} → reusing from {kb}" for f, kb in covered.items()
        )
        gap_lines = "\n".join(f"  • {f}" for f in gaps)

        msg_parts = []
        if covered:
            msg_parts.append(f"Already covered by existing KBs ({len(covered)} fields):\n{covered_lines}")
        if gaps:
            msg_parts.append(f"New extraction needed for ({len(gaps)} fields):\n{gap_lines}")

        return ConversationTurn(
            state="CHECK_REUSE",
            message=(
                "\n\n".join(msg_parts)
                + "\n\nReady to configure sources for the new extraction. Continue?"
            ),
            options=["yes, continue", "no, let me revise fields"],
        )

    def _handle_check_reuse_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("yes", "continue", "ok", "go")):
            return self._advance_to_configure_sources()
        if any(kw in lowered for kw in ("no", "revise", "back")):
            self._state = "REVIEW_FIELDS"
            return self._handle_review_fields_prompt("current list")
        return ConversationTurn(
            state="CHECK_REUSE",
            message="Please respond with 'yes' to continue or 'no' to revise fields.",
            options=["yes, continue", "no, let me revise fields"],
        )

    def _advance_to_configure_sources(self) -> ConversationTurn:
        self._state = "CONFIGURE_SOURCES"

        # Pre-populate sources from anything we can salvage out of the intent
        # text — Confluence URLs and bare 'pageId=N' references. Client LLMs
        # often compress a pasted URL into 'pageId=N' before the tool-call,
        # losing the URL but keeping the id. We catch both here so the user
        # doesn't have to re-state what's already in their intent.
        # (See session synth-tpm-3bda58fe.) Only auto-add if no sources yet —
        # don't clobber what the user has explicitly entered.
        if not self._data.sources and self._data.intent_description:
            auto = _extract_confluence_sources_from_text(self._data.intent_description)
            if auto:
                self._data.sources.extend(auto)
                summary = "\n".join(f"  • {s}" for s in auto)
                return ConversationTurn(
                    state="CONFIGURE_SOURCES",
                    message=(
                        "I extracted these Confluence references from your intent:\n\n"
                        f"{summary}\n\n"
                        "These are pre-populated as sources. You can add more "
                        "(URLs, page-ids, label filters, Jira, Git) or type 'done'."
                    ),
                    options=["done", "add another source"],
                )

        return ConversationTurn(
            state="CONFIGURE_SOURCES",
            message=(
                "Where does the source data live?\n"
                "Describe one or more sources (you can add multiple):\n\n"
                "  • Confluence specific page (recommended when you have a link):\n"
                "      paste the page URL, e.g.\n"
                "      'https://confluence.example.com/display/OCIFACP/26AI+Weekly+Status'\n"
                "  • Confluence space + label filter:\n"
                "      'confluence SPACE_KEY with labels: label1, label2'\n"
                "  • Confluence by page id:\n"
                "      'confluence page-id: 12345678'\n"
                "  • Jira: 'jira JQL: project = OPS AND labels = weekly-status'\n"
                "  • Git: 'git repo org/my-repo paths: **/*.md'\n\n"
                "Type 'done' when finished adding sources."
            ),
            options=[
                "https://confluence.example.com/display/SPACE/Page+Title",
                "confluence SPACE labels: weekly-status",
                "jira project = OPS AND labels = weekly-ops",
                "done",
            ],
        )

    def _handle_configure_sources_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if lowered == "done":
            if not self._data.sources:
                self._data.sources.append({"kind": "confluence", "space": "REPLACE_ME"})
            return self._advance_to_configure_triggers()

        source = _parse_source_descriptor(user_input)
        self._data.sources.append(source)

        return ConversationTurn(
            state="CONFIGURE_SOURCES",
            message=(
                f"Added source: {source}.\n"
                f"Current sources ({len(self._data.sources)}):\n"
                + "\n".join(f"  • {s}" for s in self._data.sources)
                + "\n\nAdd another source or type 'done'."
            ),
            options=["done"],
        )

    def _advance_to_configure_triggers(self) -> ConversationTurn:
        self._state = "CONFIGURE_TRIGGERS"
        return ConversationTurn(
            state="CONFIGURE_TRIGGERS",
            message=(
                "How should this skill be triggered?\n\n"
                "  1. on-request only (user asks → skill runs immediately)\n"
                "  2. scheduled only  (e.g. '0 16 * * 5' = every Friday 4pm)\n"
                "  3. both            (on-request + schedule)\n\n"
                "Also, what format should the output be? "
                "(markdown / pptx / docx / email / slack)\n\n"
                "Example: '3, pptx, 0 16 * * 5'"
            ),
            options=[
                "1, markdown",
                "2, pptx, 0 16 * * 5",
                "3, pptx, 0 16 * * 5",
            ],
        )

    def _handle_configure_triggers_response(self, user_input: str) -> ConversationTurn:
        trigger, output_format = _parse_trigger_input(user_input)
        self._data.trigger = trigger
        self._data.output_format = output_format
        return self._advance_to_preview()

    def _advance_to_preview(self) -> ConversationTurn:
        self._state = "PREVIEW"
        artifacts = self._synthesize_preview()
        self._data.synthesized_artifacts = artifacts

        artifact_summary: dict = {}
        for path, content in artifacts.items():
            if isinstance(content, (dict, list)):
                artifact_summary[path] = f"({type(content).__name__} with {len(content)} entries)"
            else:
                snippet = str(content)[:120].replace("\n", " ")
                artifact_summary[path] = snippet

        return ConversationTurn(
            state="PREVIEW",
            message=(
                "Here's what I'll commit:\n\n"
                + "\n".join(f"  • {path}: {summary}" for path, summary in artifact_summary.items())
                + "\n\nReview the artifacts above. Commit them?"
            ),
            options=["yes, commit", "no, let me adjust"],
            artifacts_preview=artifact_summary,
        )

    def _handle_preview_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("yes", "commit", "ok", "go", "looks good")):
            return self._handle_commit()
        return ConversationTurn(
            state="PREVIEW",
            message="What would you like to adjust? (type 'yes' to commit when ready)",
            options=["yes, commit"],
        )

    def _handle_confirm_response(self, user_input: str) -> ConversationTurn:
        return self._handle_commit()

    def _handle_commit(self) -> ConversationTurn:
        # ADB write must succeed before we advance to COMMITTED state. If the
        # skill_store raises (e.g. pool unavailable, schema mismatch, network),
        # we stay at PREVIEW so the user can fix and retry — never advance on a
        # filesystem-only "commit" because downstream states (VALIDATE/INGEST/
        # PROMOTE) all assume the row is durable in ADB.
        try:
            committed_paths = self._write_artifacts()
        except Exception as exc:
            log.error(
                "_handle_commit: write_artifacts failed — staying at PREVIEW. "
                "synth_id=%s persona=%s skill=%s err=%s",
                self._data.synth_id, self._data.persona, self._data.skill_name, exc,
            )
            return ConversationTurn(
                state="PREVIEW",
                message=(
                    f"❌ Commit failed — skill was NOT saved to the durable store.\n\n"
                    f"  {type(exc).__name__}: {exc}\n\n"
                    f"Filesystem files may or may not have been written; ADB rejected the write.\n"
                    f"Fix the underlying issue (ADB connectivity, schema, etc.) and retry."
                ),
                options=["retry commit", "stop here"],
            )

        self._data.committed_paths = committed_paths
        self._state = "COMMITTED"
        return ConversationTurn(
            state="COMMITTED",
            message=(
                f"Committed {len(committed_paths)} artifact(s):\n"
                + "\n".join(f"  • {p}" for p in committed_paths)
                + "\n\nReady to validate, ingest, run eval, and promote?\n"
                "Type 'yes' to run the full pipeline, or 'stop' to finish here."
            ),
            options=["yes, run full pipeline", "just validate", "stop here"],
        )

    def _handle_committed_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "later", "no", "done", "exit")):
            self._state = "DONE"
            return ConversationTurn(
                state="DONE",
                message=(
                    "Session paused after commit. Resume anytime to validate, ingest, and promote.\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )
        if "just validate" in lowered or "validate only" in lowered:
            return self._run_validate()
        return self._run_validate()

    # -- VALIDATE --------------------------------------------------------

    def _run_validate(self) -> ConversationTurn:
        from .validate_links import validate_workflow_links
        import shutil
        import tempfile
        import os

        self._state = "VALIDATE"
        pb_dir = REPO_ROOT / "framework" / "persona_builders"

        # Load workflow_skill content from skill_store if available (ADB-backed).
        # Fallback to filesystem path when skill_store is None (laptop/dev mode).
        wf_path_str: str | None = None
        _tmp_wf_file = None  # keep reference so tempfile survives the try block
        _tmp_pb_dir: str | None = None  # merged persona-builders dir (may include in-session delta)

        if self._skill_store is not None:
            try:
                wf_content = self._skill_store.read_artifact(
                    persona=self._data.persona,
                    skill_name=self._data.skill_name,
                    artifact_type="workflow_skill",
                )
                if wf_content is not None:
                    # Write to a named tempfile so validate_workflow_links can open it
                    _tmp_wf_file = tempfile.NamedTemporaryFile(
                        mode="w",
                        suffix=".yaml",
                        delete=False,
                        encoding="utf-8",
                    )
                    _tmp_wf_file.write(wf_content)
                    _tmp_wf_file.flush()
                    _tmp_wf_file.close()
                    wf_path_str = _tmp_wf_file.name
                    log.debug(
                        "_run_validate: loaded workflow_skill from skill_store → %s",
                        wf_path_str,
                    )
            except Exception as exc:
                log.warning(
                    "_run_validate: skill_store.read_artifact failed (%s) — falling back to filesystem",
                    exc,
                )

        if wf_path_str is None:
            # Filesystem fallback
            wf_path_str = str(
                REPO_ROOT / "framework" / "workflow_skills"
                / self._data.persona / f"{self._data.skill_name}.yaml"
            )

        # Build a merged persona-builders directory that supplements the
        # filesystem builders with any in-session persona_builder_delta stored
        # in ADB.  Without this, the validator fails with "unknown KB" for any
        # KB that was authored in this session but not yet promoted to disk.
        # (BUG-queue-6c173 — fixes validate step for new-KB skills.)
        merged_pb_dir_str = str(pb_dir)

        def _make_merged_pb_dir(delta_entry: dict) -> str:
            """Wrap a raw KB entry dict in a full persona-builder YAML, copy
            existing *.yaml builders alongside it, and return the temp dir path."""
            synthetic_pb = {
                "persona": self._data.persona,
                "knowledge_bases": [delta_entry],
            }
            tmp = tempfile.mkdtemp(prefix="kbf_validate_pb_")
            if pb_dir.exists():
                for fs_yaml in pb_dir.glob("*.yaml"):
                    shutil.copy2(str(fs_yaml), tmp)
            synth_path = os.path.join(tmp, f"insession_{self._data.persona}.yaml")
            with open(synth_path, "w", encoding="utf-8") as fp:
                yaml.safe_dump(synthetic_pb, fp, sort_keys=False, allow_unicode=True)
            return tmp

        # Path 1 — ADB skill_store: read persona_builder_delta artifact.
        # Without this, the validator fails with "unknown KB" for any KB that
        # was authored in this session but not yet promoted to disk.
        # (BUG-queue-6c173 — fixes validate step for new-KB skills.)
        if self._skill_store is not None:
            try:
                delta_content = self._skill_store.read_artifact(
                    persona=self._data.persona,
                    skill_name=self._data.skill_name,
                    artifact_type="persona_builder_delta",
                )
                if delta_content is not None:
                    delta_entry = yaml.safe_load(delta_content) or {}
                    _tmp_pb_dir = _make_merged_pb_dir(delta_entry)
                    merged_pb_dir_str = _tmp_pb_dir
                    log.debug(
                        "_run_validate: augmented pb_dir with ADB delta → %s",
                        _tmp_pb_dir,
                    )
            except Exception as exc:
                log.warning(
                    "_run_validate: could not load persona_builder_delta from ADB (%s) — "
                    "falling through to filesystem .new_kb check",
                    exc,
                )

        # Path 2 — Filesystem fallback: read {persona}.yaml.new_kb from disk.
        # *.yaml.new_kb files are raw KB entry dicts (not full persona builders),
        # so they must be wrapped before being passed to _build_kb_index().
        # (BUG-queue-51dd3 / 3d13e / 1b0c0 / 30b34 — belt-and-suspenders fix
        # for when skill_store is unavailable or returned no delta.)
        if _tmp_pb_dir is None:
            new_kb_path = pb_dir / f"{self._data.persona}.yaml.new_kb"
            if new_kb_path.exists():
                try:
                    delta_entry = yaml.safe_load(new_kb_path.read_text()) or {}
                    if isinstance(delta_entry, dict) and delta_entry.get("name"):
                        _tmp_pb_dir = _make_merged_pb_dir(delta_entry)
                        merged_pb_dir_str = _tmp_pb_dir
                        log.debug(
                            "_run_validate: augmented pb_dir with filesystem .new_kb → %s",
                            _tmp_pb_dir,
                        )
                except Exception as exc:
                    log.warning(
                        "_run_validate: could not load .new_kb from filesystem (%s) — "
                        "validation will use base persona builders only",
                        exc,
                    )

        try:
            errors = validate_workflow_links(wf_path_str, merged_pb_dir_str)
            result = {"passed": len(errors) == 0, "errors": errors}
        except Exception as e:
            log.warning("validation failed: %s", e)
            result = {"passed": False, "errors": [str(e)]}
        finally:
            # Clean up temp files/dirs
            if _tmp_wf_file is not None:
                try:
                    os.unlink(_tmp_wf_file.name)
                except OSError:
                    pass
            if _tmp_pb_dir is not None:
                try:
                    shutil.rmtree(_tmp_pb_dir, ignore_errors=True)
                except OSError:
                    pass

        self._data.validation_result = result
        passed = result.get("passed", False)

        if not passed:
            errors = result.get("errors", [])
            error_lines = "\n".join(f"  • {e}" for e in errors[:5])
            return ConversationTurn(
                state="VALIDATE",
                message=(
                    f"Validation FAILED:\n{error_lines}\n\n"
                    "Fix the issues and type 'retry', or 'skip' to continue anyway."
                ),
                data={"validation": result},
                options=["retry", "skip", "stop here"],
            )

        return ConversationTurn(
            state="VALIDATE",
            message=(
                "Validation PASSED — ADR-017 link check OK.\n\n"
                "Proceed to ingestion? This will run the extraction pipeline."
            ),
            data={"validation": result},
            options=["yes, ingest", "skip to eval", "stop here"],
        )

    def _handle_validate_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if "retry" in lowered:
            return self._run_validate()
        if any(kw in lowered for kw in ("stop", "done", "exit")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)
        if "skip" in lowered and "eval" in lowered:
            return self._run_eval()
        if "skip" in lowered:
            return self._run_ingest()
        return self._run_ingest()

    # -- INGEST ----------------------------------------------------------

    def _run_ingest(self) -> ConversationTurn:
        import os as _os
        from ..ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor

        self._state = "INGEST"

        kbf_env = _os.environ.get("KBF_ENV", "laptop")
        confluence_sources = [s for s in self._data.sources if s.get("kind") == "confluence"]

        if not confluence_sources:
            # No Confluence sources — nothing to ingest; other kinds are query-time or Phase 2
            for src in self._data.sources:
                kind = src.get("kind")
                if kind == "adb":
                    log.info("source kind=adb table=%s — query-time retrieval, no ingest needed",
                             src.get("table", "?"))
                elif kind in ("jira", "git"):
                    log.info("source kind=%s — Phase 2, not yet wired", kind)
            self._data.ingest_result = {
                "status": "completed",
                "items_processed": 0,
                "items_upserted": 0,
                "mode": "stub",
                "message": "No Confluence sources configured — nothing to ingest.",
            }
            return ConversationTurn(
                state="INGEST",
                message=(
                    "Ingestion complete (no Confluence sources configured).\n\n"
                    "Proceed to eval?"
                ),
                data={"ingest": self._data.ingest_result},
                options=["yes, run eval", "stop here"],
            )

        confluence_adapter = _build_confluence_adapter(kbf_env, REPO_ROOT)
        mode = "live" if confluence_adapter is not None else "fixture"
        ingestor = ConfluenceWikiIngestor(adapter=confluence_adapter)

        total_new = 0
        total_updated = 0
        total_unchanged = 0
        failures: list[tuple[str, str]] = []  # (space, error_message)

        for src in self._data.sources:
            kind = src.get("kind")
            if kind == "confluence":
                space = src.get("space", "")
                labels = src.get("include_labels") or src.get("labels") or []
                pages_explicit = src.get("pages") or []
                # Label/source descriptor for the log + failure message
                if pages_explicit:
                    source_label = f"pages={pages_explicit}"
                else:
                    source_label = f"space '{space}' labels={labels or '(none)'}"
                try:
                    if pages_explicit:
                        # Specific page(s) by URL or page-id — fetch each directly.
                        stats = ingestor.ingest_pages(pages_explicit)
                    else:
                        stats = ingestor.ingest_space(space, labels or None)
                    pages_total = (
                        stats["pages_new"]
                        + stats["pages_updated"]
                        + stats["pages_unchanged"]
                    )
                    log.info(
                        "_run_ingest: Confluence %s new=%d updated=%d unchanged=%d total=%d",
                        source_label,
                        stats["pages_new"],
                        stats["pages_updated"],
                        stats["pages_unchanged"],
                        pages_total,
                    )
                    # Zero pages back from the adapter is an extraction failure,
                    # not a success. Codex returning {"results": []} silently
                    # advanced the synth-tpm-14a54555 session through INGEST →
                    # PROMOTE with an empty KB. Possible causes:
                    #   - label filters match no real pages
                    #   - space key wrong / not accessible
                    #   - page URL/ID wrong or not accessible
                    #   - codex MCP layer returned empty results without error
                    #   - fixture dir missing for the space (laptop fixture mode)
                    # Whatever the cause, advancing on an empty extraction is
                    # never correct — the user must fix the input and retry.
                    if pages_total == 0:
                        if pages_explicit:
                            msg = (
                                f"adapter returned no content for pages "
                                f"{pages_explicit} (mode={mode}). "
                                f"KB extraction yielded nothing — treating as failed. "
                                f"Verify the page URLs/IDs and codex/Confluence access."
                            )
                            failures.append((str(pages_explicit), msg))
                        else:
                            labels_desc = labels if labels else "(no label filter)"
                            msg = (
                                f"adapter returned 0 pages for space '{space}' "
                                f"with labels {labels_desc} (mode={mode}). "
                                f"KB extraction yielded nothing — treating as failed. "
                                f"Verify the space key, label filters, and codex/Confluence access."
                            )
                            failures.append((space, msg))
                        log.error("_run_ingest: %s", msg)
                    else:
                        total_new += stats["pages_new"]
                        total_updated += stats["pages_updated"]
                        total_unchanged += stats["pages_unchanged"]
                except Exception as exc:
                    log.error(
                        "_run_ingest: Confluence %s failed: %s", source_label, exc,
                    )
                    failures.append((source_label, str(exc)))
            elif kind == "adb":
                log.info("_run_ingest: source kind=adb table=%s — query-time retrieval, no ingest needed",
                         src.get("table", "?"))
            elif kind in ("jira", "git"):
                log.info("_run_ingest: source kind=%s — Phase 2, not yet wired", kind)

        items_upserted = total_new + total_updated
        items_processed = total_new + total_updated + total_unchanged

        # Hard-fail policy: if ANY Confluence source failed, do NOT advance.
        # Session stays at INGEST; user can fix the upstream (e.g. codex proxy,
        # Confluence creds, network) and retry. Promotion is blocked so the
        # skill remains in its previous state (draft).
        if failures:
            self._data.ingest_result = {
                "status": "failed",
                "items_processed": items_processed,
                "items_upserted": items_upserted,
                "pages_new": total_new,
                "pages_updated": total_updated,
                "pages_unchanged": total_unchanged,
                "mode": mode,
                "failures": [{"space": sp, "error": err} for sp, err in failures],
            }
            failure_lines = "\n".join(f"  • {sp}: {err}" for sp, err in failures)
            return ConversationTurn(
                state="INGEST",
                message=(
                    f"❌ Ingestion failed for {len(failures)} source(s) — skill will NOT be promoted.\n\n"
                    f"{failure_lines}\n\n"
                    f"The skill remains in its previous state. Fix the upstream issue "
                    f"(e.g. restart codex proxy, check Confluence access, network) "
                    f"and retry, or stop the session and resume later."
                ),
                data={"ingest": self._data.ingest_result},
                options=["retry ingestion", "stop here"],
            )

        self._data.ingest_result = {
            "status": "completed",
            "items_processed": items_processed,
            "items_upserted": items_upserted,
            "pages_new": total_new,
            "pages_updated": total_updated,
            "pages_unchanged": total_unchanged,
            "mode": mode,
        }
        return ConversationTurn(
            state="INGEST",
            message=(
                f"Ingestion complete ({mode} mode).\n\n"
                f"Processed {items_processed} pages: "
                f"{total_new} new, {total_updated} updated, {total_unchanged} unchanged.\n\n"
                "Proceed to eval?"
            ),
            data={"ingest": self._data.ingest_result},
            options=["yes, run eval", "stop here"],
        )

    def _handle_ingest_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "done", "exit")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)

        # If the previous INGEST run failed, only "retry" advances us — anything
        # else loops back to the failure message. Promotion is blocked until
        # ingestion succeeds.
        last = self._data.ingest_result or {}
        if last.get("status") == "failed":
            if "retry" in lowered or "again" in lowered:
                return self._run_ingest()
            return ConversationTurn(
                state="INGEST",
                message=(
                    "Ingestion is still in a failed state — cannot proceed to eval.\n"
                    "Type 'retry ingestion' to re-run, or 'stop here' to pause."
                ),
                options=["retry ingestion", "stop here"],
            )

        return self._run_eval()

    # -- EVAL ------------------------------------------------------------

    def _run_eval(self) -> ConversationTurn:
        import tempfile
        import os

        self._state = "EVAL"
        # In stub mode, report gold set counts
        from ..eval.gold_set_feeder import count_entries
        gold_count = count_entries(self._data.persona)
        skill_gold = f"eval/gold_sets/{self._data.persona}-{self._data.skill_name}-extraction.jsonl"
        wf_gold = f"eval/gold_sets/{self._data.persona}-{self._data.skill_name}-workflow.jsonl"

        # If skill_store is available, load eval gold set CLOBs from ADB and write
        # to tempfiles so the eval harness can consume them via file paths.
        # Fallback to filesystem paths when skill_store is None.
        _tmp_files: list[str] = []
        if self._skill_store is not None:
            for artifact_type, gold_path_attr in (
                ("eval_extraction", "extraction_gold_path"),
                ("eval_workflow",   "workflow_gold_path"),
            ):
                try:
                    content = self._skill_store.read_artifact(
                        persona=self._data.persona,
                        skill_name=self._data.skill_name,
                        artifact_type=artifact_type,
                    )
                    if content is not None:
                        tmp = tempfile.NamedTemporaryFile(
                            mode="w",
                            suffix=".jsonl",
                            delete=False,
                            encoding="utf-8",
                        )
                        tmp.write(content)
                        tmp.flush()
                        tmp.close()
                        _tmp_files.append(tmp.name)
                        log.debug(
                            "_run_eval: loaded %s from skill_store → %s",
                            artifact_type, tmp.name,
                        )
                except Exception as exc:
                    log.warning(
                        "_run_eval: skill_store.read_artifact(%s) failed (%s) — using filesystem",
                        artifact_type, exc,
                    )

        # Clean up any tempfiles created above (eval harness has already seen them)
        for tmp_path in _tmp_files:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        self._data.eval_result = {
            "status": "stub",
            "persona_gold_count": gold_count,
            "extraction_gold_set": skill_gold,
            "workflow_gold_set": wf_gold,
            "metrics": {
                "recall_at_k": None,
                "faithfulness": None,
            },
            "exit_criteria": {
                "recall_threshold": 0.80,
                "faithfulness_threshold": 0.85,
                "passed": None,
            },
        }

        return ConversationTurn(
            state="EVAL",
            message=(
                f"Eval harness (stub mode):\n"
                f"  Gold set entries for {self._data.persona}: {gold_count}\n"
                f"  Extraction gold set: {skill_gold}\n"
                f"  Workflow gold set: {wf_gold}\n\n"
                "In production this would run queries against the KB and measure "
                "recall@5, faithfulness, latency, and cost.\n\n"
                "Exit criteria: recall ≥0.80, faithfulness ≥0.85\n\n"
                "Proceed to promote (draft → production)?"
            ),
            data={"eval": self._data.eval_result},
            options=["yes, promote", "stop here"],
        )

    def _handle_eval_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "done", "exit", "no")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)
        return self._run_promote()

    # -- PROMOTE ---------------------------------------------------------

    def _run_promote(self) -> ConversationTurn:
        # Belt-and-suspenders: refuse to enter PROMOTE if ingestion did not complete
        # successfully. State stays at INGEST so the skill remains in its previous
        # state until the upstream issue (codex proxy, Confluence access, etc.) is fixed.
        ingest = self._data.ingest_result or {}
        if ingest.get("status") == "failed":
            self._state = "INGEST"
            failures = ingest.get("failures") or []
            failure_lines = "\n".join(
                f"  • {f.get('space','?')}: {f.get('error','?')}" for f in failures
            ) or "  (no detail recorded)"
            return ConversationTurn(
                state="INGEST",
                message=(
                    "❌ Cannot promote — ingestion failed and the KB is empty.\n\n"
                    f"{failure_lines}\n\n"
                    "Fix the upstream issue and type 'retry ingestion', or 'stop here' to pause."
                ),
                data={"ingest": ingest},
                options=["retry ingestion", "stop here"],
            )

        self._state = "PROMOTE"
        # Notify the skill_store that we are about to promote (user still confirms)
        # The actual promotion DB update happens in _handle_promote_response when
        # the user types 'yes'.
        return ConversationTurn(
            state="PROMOTE",
            message=(
                f"Promote {self._data.persona}.{self._data.skill_name} from draft → production?\n\n"
                "This makes the skill live — the consumption flow will start routing to it.\n"
                "Type 'yes' to promote or 'no' to keep as draft."
            ),
            options=["yes, promote", "no, keep as draft"],
        )

    def _handle_promote_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        is_yes = any(kw in lowered for kw in ("yes", "promote", "ok", "go"))

        if is_yes:
            # ADB is the source of truth. Promote MUST succeed against ADB.
            # Previously a try/except turned every failure (skill missing,
            # ADB unreachable, constraint) into log.warning while the session
            # advanced to DONE — letting users believe the promotion landed
            # when ADB had nothing (synth-tpm-14a54555). Now: any failure
            # raises out, the state stays at PROMOTE, and the user sees the
            # real error in the turn message.
            try:
                self._skill_store.promote(self._data.persona, self._data.skill_name)
                log.info(
                    "skill_store.promote: persona=%s skill=%s",
                    self._data.persona, self._data.skill_name,
                )

                # Option B: write the promoted KB delta into KBF_PERSONA_BUILDERS
                delta_text = self._skill_store.read_artifact(
                    self._data.persona,
                    self._data.skill_name,
                    "persona_builder_delta",
                )
                if delta_text:
                    self._skill_store.upsert_persona_builder_kb(
                        persona=self._data.persona,
                        kb_name=self._data.skill_name,
                        content_yaml=delta_text,
                        status="production",
                    )
                    log.info(
                        "_handle_promote_response: upserted KB entry "
                        "persona=%s kb_name=%s",
                        self._data.persona, self._data.skill_name,
                    )
                    # Clean up stray .new_kb file if it still exists on disk
                    new_kb_path = (
                        REPO_ROOT
                        / "framework"
                        / "persona_builders"
                        / f"{self._data.persona}.yaml.new_kb"
                    )
                    if new_kb_path.exists():
                        new_kb_path.unlink()
                        log.info(
                            "_handle_promote_response: removed stale %s",
                            new_kb_path.name,
                        )
            except Exception as exc:
                log.error(
                    "_handle_promote_response: promotion FAILED — session stays "
                    "at PROMOTE. synth_id=%s persona=%s skill=%s err=%s",
                    self._data.synth_id, self._data.persona,
                    self._data.skill_name, exc,
                )
                # Leave self._state at PROMOTE so the user can retry.
                return ConversationTurn(
                    state="PROMOTE",
                    message=(
                        f"❌ Promotion failed — skill is NOT live.\n\n"
                        f"  {type(exc).__name__}: {exc}\n\n"
                        f"The skill remains in its previous state. Likely cause: "
                        f"the upstream COMMIT did not actually write to ADB (check "
                        f"server logs for write_artifacts errors). Fix the root "
                        f"cause and retry."
                    ),
                    options=["retry promote", "stop here"],
                )

        self._state = "DONE"

        if is_yes:
            # Check whether ingestion produced any content — warn if KB is empty.
            ingest_result = self._data.ingest_result or {}
            items = ingest_result.get("items_processed", 0)
            ingest_mode = ingest_result.get("mode", "stub")
            sources_list = self._data.sources or []
            conf_sources = [s for s in sources_list if s.get("kind") == "confluence"]

            if items > 0:
                kb_status_note = (
                    f"KB populated: {items} pages ingested "
                    f"(mode: {ingest_mode}). Routing will return real content."
                )
            elif conf_sources:
                spaces = ", ".join(s.get("space", "?") for s in conf_sources)
                kb_status_note = (
                    f"⚠️  KB is empty — no content was ingested yet.\n"
                    f"The skill routes correctly but askKnowledgeBase will return "
                    f"'no relevant context found' until the Confluence space(s) "
                    f"({spaces}) have been crawled.\n\n"
                    f"To populate the KB on this instance:\n"
                    f"  python -m framework.deploy.ingestion_worker  "
                    f"  # or trigger via webhook after a Confluence page update\n\n"
                    f"On laptop with dev fixtures: check "
                    f"framework/_dev_fixtures/ for fixture data that matches your skill's KB name."
                )
            elif any(s.get("kind") == "adb" for s in sources_list):
                kb_status_note = (
                    "⚠️  KB is empty — ADB sources are registered but not yet crawled.\n"
                    "Run the ingestion worker to populate: "
                    "python -m framework.deploy.ingestion_worker"
                )
            else:
                kb_status_note = (
                    "⚠️  No sources configured. KB is empty.\n"
                    "Configure sources and re-run ingestion before querying this skill."
                )

            return ConversationTurn(
                state="DONE",
                message=(
                    f"Skill {self._data.persona}.{self._data.skill_name} promoted to production.\n\n"
                    "The consumption flow will now route matching queries to this skill.\n\n"
                    f"{kb_status_note}\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )
        return ConversationTurn(
            state="DONE",
            message=(
                f"Skill {self._data.persona}.{self._data.skill_name} remains as draft.\n"
                "Promote later via: resume this session or kb-cli promote.\n"
                f"Session ID: {self._data.synth_id}"
            ),
            done=True,
        )

    # ------------------------------------------------------------------
    # Synthesis helpers
    # ------------------------------------------------------------------

    def _synthesize_preview(self) -> dict[str, Any]:
        from .synthesize_schema import synthesize_extraction_schema
        from .synthesize_builder import synthesize_persona_builder_diff
        from .synthesize_workflow import synthesize_workflow_skill
        from .gold_seed import seed_gold_set, seed_workflow_gold

        persona = self._data.persona
        skill_name = self._data.skill_name
        gaps = self._data.reuse_result.get("gaps", list(self._data.fields))

        artifacts: dict[str, Any] = {}

        if gaps:
            schema = synthesize_extraction_schema(gaps, persona, skill_name)
            if self._data.field_specs:
                for f in gaps:
                    if f in self._data.field_specs and f in schema.get("properties", {}):
                        schema["properties"][f] = dict(self._data.field_specs[f])
            schema_path = f"framework/parsers/schemas/{persona}/{skill_name}/v1.json"
            artifacts[schema_path] = schema

            pb_entry = synthesize_persona_builder_diff(
                persona=persona,
                kb_name=f"{persona}.{skill_name}",
                schema_path=schema_path,
                sources=self._data.sources,
                fields=gaps,
            )
            artifacts[f"framework/persona_builders/{persona}.yaml.new_kb"] = pb_entry

            gold_entries = seed_gold_set(
                persona=persona,
                kb_name=skill_name,
                artifact_path=self._data.artifact_path,
                extracted_fields={f: None for f in gaps},
            )
            artifacts[f"eval/gold_sets/{persona}-{skill_name}-extraction.jsonl"] = gold_entries

        intent = {
            "task_description": self._data.intent_description,
            "sources": self._data.sources,
            "trigger": self._data.trigger,
            "output_format": self._data.output_format,
            "reuse": self._data.reuse_result,
        }
        wf_struct = synthesize_workflow_skill(
            persona=persona,
            skill_name=skill_name,
            intent=intent,
            fields=self._data.fields,
            template_path=None,
        )
        artifacts[f"framework/workflow_skills/{persona}/{skill_name}.yaml"] = wf_struct

        wf_gold = seed_workflow_gold(
            persona=persona,
            skill_name=skill_name,
            task_description=self._data.intent_description,
            example_fields={f: None for f in self._data.fields},
        )
        artifacts[f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl"] = wf_gold

        return artifacts

    def _write_artifacts(self) -> list[str]:
        import json as _json

        committed: list[str] = []

        # Build a {artifact_type: text} mapping alongside the {rel_path: content} map
        # so we can pass clean artifact_type keys to skill_store.write_artifacts().
        # Must match framework/deploy/skill_store/_base.py ARTIFACT_TYPES and the
        # KBF_SKILL_ARTIFACTS check constraint added in migration 006. Forgetting
        # one here means it gets silently dropped from typed_artifacts and never
        # reaches ADB (see BUG: extraction_schema regression).
        _KNOWN_ARTIFACT_TYPES = {
            "workflow_skill",
            "persona_builder_delta",
            "eval_extraction",
            "eval_workflow",
            "extraction_schema",
        }

        # Map rel_path → (artifact_type, text) for every synthesized artifact
        typed_artifacts: dict[str, str] = {}  # artifact_type → text

        for rel_path, content in self._data.synthesized_artifacts.items():
            if isinstance(content, dict):
                text = yaml.safe_dump(content, sort_keys=False, allow_unicode=True)
                if "schema" in rel_path or rel_path.endswith(".json"):
                    text = _json.dumps(content, indent=2)
            elif isinstance(content, list):
                text = "\n".join(_json.dumps(item) for item in content) + "\n"
            else:
                text = str(content)

            # Infer artifact_type from rel_path
            if "workflow_skills" in rel_path and rel_path.endswith(".yaml"):
                artifact_type = "workflow_skill"
            elif ".yaml.new_kb" in rel_path or "persona_builders" in rel_path:
                artifact_type = "persona_builder_delta"
            elif rel_path.endswith("-extraction.jsonl"):
                artifact_type = "eval_extraction"
            elif rel_path.endswith("-workflow.jsonl"):
                artifact_type = "eval_workflow"
            elif "parsers/schemas" in rel_path and rel_path.endswith(".json"):
                artifact_type = "extraction_schema"
            else:
                artifact_type = None

            if artifact_type in _KNOWN_ARTIFACT_TYPES:
                typed_artifacts[artifact_type] = text

            # Always write to filesystem (fallback path or primary for laptop mode)
            full = REPO_ROOT / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
            log.info("wrote %s", full)
            committed.append(rel_path)

        # Write to skill_store (ADB). HARD-FAIL on failure: ADB is the source
        # of truth. Caller (_handle_commit) catches the exception and keeps
        # the session at PREVIEW state — never advancing past PREVIEW on a
        # phantom commit. (See synth-tpm-6523a9c4 / synth-tpm-14a54555.)
        # _skill_store cannot be None — __init__/from_dict already rejected that.
        if typed_artifacts:
            self._skill_store.write_artifacts(
                synth_id=self._data.synth_id,
                persona=self._data.persona,
                skill_name=self._data.skill_name,
                artifacts=typed_artifacts,
            )
            log.info(
                "skill_store.write_artifacts: synth_id=%s persona=%s skill=%s types=%s",
                self._data.synth_id,
                self._data.persona,
                self._data.skill_name,
                sorted(typed_artifacts.keys()),
            )

        return committed

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_fields_from_input(self, user_input: str) -> tuple[list[str], dict | None]:
        parts = re.split(r"[,\n]+", user_input)
        fields: list[str] = []
        for p in parts:
            f = _to_field_name(p.strip())
            if f and f not in fields:
                fields.append(f)
        if not fields:
            fields = ["title", "summary", "details"]
        return fields, None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    # Cap at 64 chars (matches DB column width; well within filesystem limits).
    # Avoid truncating mid-word: walk back to the last underscore if possible.
    if len(s) > 64:
        truncated = s[:64]
        last_sep = truncated.rfind("_")
        s = truncated[:last_sep] if last_sep > 0 else truncated
        s = s.strip("_")
    return s or "unnamed_skill"


def _to_field_name(text: str) -> str:
    t = text.lower().replace(" ", "_").replace("-", "_")
    t = re.sub(r"[^a-z0-9_]", "", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t[:60]


def _parse_field_edits(user_input: str) -> list[tuple]:
    edits: list[tuple] = []
    for line in re.split(r"[,\n;]+", user_input):
        line = line.strip()
        m_add = re.match(r"(?i)add\s+(\S+)", line)
        m_remove = re.match(r"(?i)remove\s+(\S+)", line)
        m_rename = re.match(r"(?i)rename\s+(\S+)\s+to\s+(\S+)", line)
        if m_add:
            edits.append(("add", m_add.group(1)))
        elif m_remove:
            edits.append(("remove", m_remove.group(1)))
        elif m_rename:
            edits.append(("rename", m_rename.group(1), m_rename.group(2)))
    return edits


def _extract_confluence_page_id(url: str) -> str | None:
    """Extract numeric page-id from common Confluence URL formats; else None.

    Recognises:
      .../pages/12345/Title             (DC + Cloud)
      .../viewpage.action?pageId=12345  (legacy)
      .../wiki/spaces/SPACE/pages/12345 (Cloud)
    """
    m = re.search(r"/pages/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]pageId=(\d+)", url)
    if m:
        return m.group(1)
    return None


def _extract_confluence_sources_from_text(text: str) -> list[dict]:
    """Find Confluence URLs / pageId references in arbitrary text.

    Returns a list of source descriptors of the form
      {"kind": "confluence", "pages": [<id_or_url>], "page_urls": [<url>]}
    one per discovered reference. Used to pre-populate sources from a free-form
    intent description when the user pasted a link (often the MCP client LLM
    compresses pasted URLs into 'pageId=N' before sending — we catch both).

    Returns [] when nothing matches.
    """
    out: list[dict] = []

    # 1. Full Confluence-looking URLs
    for url_match in re.finditer(r"https?://\S+", text):
        url = url_match.group(0).rstrip(".,;)\"'")
        is_confluence = (
            "confluence" in url.lower()
            or "atlassian" in url.lower()
            or "/display/" in url
            or "/spaces/" in url
            or "pageId=" in url
            or "pageid=" in url.lower()
        )
        if not is_confluence:
            continue
        page_id = _extract_confluence_page_id(url)
        out.append({
            "kind": "confluence",
            "pages": [page_id or url],
            "page_urls": [url],
        })

    # 2. Bare 'pageId=NNN' / 'page-id: NNN' / 'pageIds=N,M' references that
    #    are NOT already part of a URL we just captured. Common when the
    #    client LLM compressed a URL.
    captured_ids = {p for s in out for p in s["pages"] if p.isdigit()}
    for pid_match in re.finditer(
        r"page[\-\s_]?ids?\s*[:=]\s*([\d,\s]+)", text, re.IGNORECASE
    ):
        for pid in pid_match.group(1).split(","):
            pid = pid.strip()
            if not pid.isdigit() or pid in captured_ids:
                continue
            captured_ids.add(pid)
            out.append({"kind": "confluence", "pages": [pid]})

    return out


def _parse_source_descriptor(user_input: str) -> dict:
    lowered = user_input.lower()

    # ── Confluence specific page (URL or page-id) ───────────────────────────
    # User pastes a URL like 'https://confluence.example.com/display/SPACE/Page'
    # or 'https://.../wiki/spaces/SPACE/pages/12345/Title'. We ingest THAT page
    # via adapter.fetch() — no label search, no space crawl. The user's intent
    # is "this specific page", not "search the space by labels".
    url_m = re.search(r"https?://\S+", user_input)
    is_confluence_url = bool(url_m) and (
        "confluence" in lowered
        or "atlassian" in lowered
        or "/display/" in (url_m.group(0) if url_m else "")
        or "/spaces/" in (url_m.group(0) if url_m else "")
        or "pageid=" in (url_m.group(0).lower() if url_m else "")
    )
    if is_confluence_url:
        url = url_m.group(0).rstrip(".,;)")
        page_id = _extract_confluence_page_id(url)
        # Pass the extracted numeric id when we have one (more reliable for
        # adapter.fetch); otherwise pass the URL itself — the codex_proxy
        # adapter prompt accepts either form.
        ref = page_id or url
        return {
            "kind": "confluence",
            "pages": [ref],
            # Keep URL too for human-readable display and so the ingestor can
            # record source_url if the adapter response doesn't include one.
            "page_urls": [url],
        }

    # ── Confluence "pageId=NNN" / "page-id: NNN" / "pageIds=N,M,O" ──────────
    # Recognise the LLM-summarised form clients often emit when they compress
    # a pasted Confluence URL into a tool-call argument. Examples we now match:
    #   "confluence pageId=20030556732"
    #   "confluence OCIFACP pageId=20030556732"
    #   "confluence page-id: 12345"
    #   "confluence pageIds=20030556732, 20030556733"
    #   "confluence pageIds: 12345,67890"
    # If "confluence" appears in the input AND one or more page-ids are found,
    # treat it as a page-fetch source — the user's intent is "ingest THESE
    # pages", not a label search.
    if "confluence" in lowered:
        pid_list_m = re.search(
            r"page[\-\s_]?ids?\s*[:=]\s*([\d,\s]+)",
            user_input,
            re.IGNORECASE,
        )
        if pid_list_m:
            ids = [
                pid.strip()
                for pid in pid_list_m.group(1).split(",")
                if pid.strip().isdigit()
            ]
            if ids:
                return {"kind": "confluence", "pages": ids}

    # ── Confluence space + labels (existing form) ───────────────────────────
    if "confluence" in lowered:
        space_m = re.search(r"confluence\s+([A-Z0-9_\-]+)", user_input, re.IGNORECASE)
        labels_m = re.search(r"labels?:\s*([\w,\s\-]+)", user_input, re.IGNORECASE)
        source: dict = {"kind": "confluence"}
        if space_m:
            source["space"] = space_m.group(1).upper()
        if labels_m:
            source["include_labels"] = [
                l.strip() for l in labels_m.group(1).split(",") if l.strip()
            ]
        return source
    if "jira" in lowered:
        jql_m = re.search(r"jira\s*:?\s*(.+)", user_input, re.IGNORECASE)
        return {"kind": "jira", "jql": jql_m.group(1).strip() if jql_m else "project = ?"}
    if "git" in lowered:
        repo_m = re.search(r"repo\s+(\S+)", user_input, re.IGNORECASE)
        paths_m = re.search(r"paths?:\s*([\w/*.*\s,]+)", user_input, re.IGNORECASE)
        source = {"kind": "git"}
        if repo_m:
            source["repo"] = repo_m.group(1)
        if paths_m:
            source["paths"] = [p.strip() for p in paths_m.group(1).split(",") if p.strip()]
        return source
    return {"kind": "unknown", "raw": user_input}


def _parse_trigger_input(user_input: str) -> tuple[dict, str]:
    output_formats = ["pptx", "docx", "email", "slack", "markdown"]
    output_format = "markdown"
    for fmt in output_formats:
        if fmt in user_input.lower():
            output_format = fmt
            break

    trigger: dict = {}
    if "1" in user_input or "on-request" in user_input.lower() or "request" in user_input.lower():
        trigger["on_request"] = True
    if "2" in user_input or "schedule" in user_input.lower() or "cron" in user_input.lower():
        cron_m = re.search(r"(\d+\s+\d+\s+\S+\s+\S+\s+\S+)", user_input)
        trigger["on_schedule"] = cron_m.group(1) if cron_m else "0 9 * * 1"
    if "3" in user_input and not trigger:
        trigger["on_request"] = True
        trigger["on_schedule"] = "0 9 * * 1"
    if not trigger:
        trigger["on_request"] = True

    return trigger, output_format


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_synth_id(persona: str, timestamp: str) -> str:
    import uuid
    unique = uuid.uuid4().hex[:8]
    return f"synth-{persona or 'new'}-{unique}"


def _list_available_personas() -> list[dict]:
    """List personas from persona_builders/*.yaml on disk."""
    pb_dir = REPO_ROOT / "framework" / "persona_builders"
    personas: list[dict] = []
    if not pb_dir.exists():
        return personas
    for p in sorted(pb_dir.glob("*.yaml")):
        name = p.stem
        try:
            with open(p) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
        display = cfg.get("display_name", name.replace("-", " ").replace("_", " ").title())
        kbs = cfg.get("knowledge_bases", [])
        skills_dir = REPO_ROOT / "framework" / "workflow_skills" / name
        skill_count = len(list(skills_dir.glob("*.yaml"))) if skills_dir.exists() else 0
        personas.append({
            "name": name,
            "display_name": display,
            "kb_count": len(kbs),
            "skill_count": skill_count,
        })
    return personas
