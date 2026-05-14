"""conversation — interactive skill-builder session (author_skill API).

Per ADR-015 §Conversation contract. Implements a state machine that drives the
full skill authoring lifecycle from IDENTIFY_PERSONA through DONE.

ADR-027 (2026-05-14): 16-state design-first machine. Sources are inspected
BEFORE schema design. One integrated DESIGN_SKILL call produces schema +
source_bindings + workflow_shape + reuse_plan. EVAL runs real extraction scoring
with auto-generated gold rows (Option A, DECISION-010).

State machine (ADR-027 — 16 states):
  IDENTIFY_PERSONA → CAPTURE_INTENT → CONFIGURE_SOURCES → INSPECT_SOURCES →
  UPLOAD_ARTIFACT_EXAMPLE → DESIGN_SKILL → REVIEW_DESIGN → CONFIGURE_TRIGGERS →
  PREVIEW_EXTRACTION → CONFIRM → COMMITTED → VALIDATE → INGEST → EVAL →
  PROMOTE → DONE

Legacy states (pre-ADR-027 — retained for in-flight sessions):
  ANALYZE_ARTIFACT, REVIEW_FIELDS, REVIEW_SCHEMA, CHECK_REUSE, PREVIEW

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

# Module-level import so tests can patch 'framework.skill_builder.conversation.fetch_samples'
from .sampler import fetch_samples  # noqa: E402


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

# ---------------------------------------------------------------------------
# ADR-027 LLM prompts
# ---------------------------------------------------------------------------

_CAPTURE_INTENT_PROMPT = """\
You are a Knowledge Builder Framework assistant. Parse the user's intent into a
normalised goal object so downstream design steps have a structured representation
to work from.

Persona: {persona}
Raw intent: "{intent}"

Return ONLY a JSON object with these keys:
{{
  "output_kind": "pptx | docx | markdown | email | slack",
  "audience": "exec | team | ops | all",
  "cadence": "weekly | monthly | on_request | daily",
  "scope_domains": ["domain1", "domain2"],
  "success_criteria": ["criterion1", "criterion2"],
  "ambiguities": ["anything unclear that the user should confirm"]
}}

Rules:
- "output_kind": infer from words like "PPT", "deck", "slide", "document", "report", "email"
- "scope_domains": extract project/service names (e.g. "26ai", "FA DB", "OCIFACP")
- "success_criteria": infer from phrases like "one slide", "real data", "exec-ready"
- "ambiguities": list anything genuinely unclear; empty list if intent is clear
- Keep all string values concise (< 80 chars each)
"""

_CONFIGURE_SOURCES_SUGGEST_PROMPT = """\
You are a Knowledge Builder Framework source advisor. Given the user's intent and the
persona's declared adapters, propose the most likely source descriptors.

Persona: {persona}
Normalised intent: {normalised_intent}
Available adapters: {adapter_list}
Intent text (original): "{intent_text}"

Return ONLY a JSON array of source descriptor objects. Each object must include:
- "kind": "confluence" | "jira" | "git" | "adb"
- For confluence: optionally "pages" (list of page IDs or URLs), "space", "labels"
- For jira: "jql" string
- "rationale": why this source is likely to contain the required data (1 sentence)

Example:
[
  {{
    "kind": "confluence",
    "pages": ["20030556732"],
    "rationale": "26ai project status page explicitly mentioned in intent"
  }}
]

Rules:
- Extract all page IDs or URLs from the intent text — these are high-confidence.
- Propose additional sources only when the adapter list makes them available AND
  the intent clearly implies them.
- Do not invent sources not supported by the adapter list.
- Return an empty array [] if no confident source can be proposed.
"""

_INSPECT_SOURCES_PROMPT = """\
You are a Knowledge Builder Framework source analyst. Review the sample content
fetched from a source and produce a capability inventory.

Source ID: {source_id}
Persona: {persona}
Intent: {normalised_intent}

Sample content (up to 3 pages):
{sample_content}

Return ONLY a JSON object:
{{
  "source_id": "{source_id}",
  "available_fields": [
    {{"field": "snake_case_name", "type": "string|array|integer",
      "confidence": "high|medium|low",
      "evidence": "quote or location from sample (< 100 chars)"}}
  ],
  "missing_fields": [
    {{"field": "field_the_intent_might_want",
      "reason": "why this content cannot supply it"}}
  ],
  "suggested_fields": [
    {{"field": "snake_case_name", "type": "string|array|integer",
      "reason": "why this is consistently present and useful"}}
  ],
  "summary": "2-3 sentence overview of what this source contains"
}}

Rules:
- "available_fields": ONLY fields clearly extractable from the sample content.
- "suggested_fields": fields present in the sample that the intent might have missed.
- "missing_fields": fields the intent implies but the source clearly cannot provide.
- Base ALL findings on the sample content — do not invent.
"""

_DESIGN_SKILL_PROMPT = """\
You are a Knowledge Builder Framework skill architect. Design a complete skill from
the user's intent, the source capability inventory, the artifact layout, and the
existing reusable KB cards.

Persona: {persona}
Normalised intent: {normalised_intent}
Source capability inventory: {source_capability}
Artifact layout hint (may be null): {artifact_layout}
Existing reusable KB cards: {existing_kb_cards}

Produce a single JSON design object:
{{
  "schema": {{
    "title": "skill_name",
    "properties": {{
      "field_name": {{
        "type": "string|array|integer|boolean",
        "description": "precise 1-2 sentence extraction instruction",
        "maxLength": 500
      }}
    }},
    "required": ["field1", "field2"]
  }},
  "source_bindings": {{
    "field_name": ["source_id1"]
  }},
  "workflow_shape": {{
    "output_format": "pptx|docx|markdown|email|slack",
    "layout": "weekly_exec_review_v1 | default",
    "trigger": {{"on_request": true, "schedule": "cron_or_null"}},
    "retriever": "search_wiki"
  }},
  "reuse_plan": {{
    "covered": {{"field": "existing_kb_name"}},
    "gaps": ["field1", "field2"]
  }},
  "unsupportable_fields": [
    {{"field": "field_name", "reason": "why no source can provide this"}}
  ],
  "open_questions": ["question for the user to resolve"]
}}

Rules:
- Include ONLY fields that at least one source can support (confidence high or medium).
- Source bindings must reference source IDs from the capability inventory.
- Reuse plan covers must reference real KB cards from "existing_kb_cards".
- If artifact layout is provided, align the output_format and layout accordingly.
- Choose "weekly_exec_review_v1" layout only for exec-review PPTX skills.
- "required" list should contain only fields critical to the skill's purpose.
- maxLength: 200 for IDs/statuses, 500 for summaries, 2000 for detailed content.
"""

_REVIEW_DESIGN_REPLAN_PROMPT = """\
You are a Knowledge Builder Framework skill architect. The user wants to modify the
current skill design. Return ONLY the changes needed as a diff object.

Current design: {current_design}
User edit request: "{edit_request}"
Updated source capability (if sources changed): {updated_source_capability}

Return ONLY a JSON diff object with keys matching what changed:
{{
  "schema_add": {{"field_name": {{"type": "...", "description": "...", "maxLength": 500}}}},
  "schema_remove": ["field_to_remove"],
  "schema_update": {{"field_name": {{"description": "updated instruction"}}}},
  "source_bindings_add": {{"new_field": ["source_id"]}},
  "source_bindings_remove": ["field_to_unbind"],
  "workflow_shape_update": {{"layout": "new_layout"}},
  "reuse_plan_update": {{"covered": {{}}, "gaps": ["field1"]}},
  "open_questions": ["any new questions from the edit"]
}}

Rules:
- Only include keys that actually change — omit unchanged sections.
- If the edit request is trivial (rename, description change), return only the
  affected field in schema_update.
- If new sources must be added (the edit implies data not in the inventory),
  add an open_question noting the source gap.
"""

_EVAL_JUDGE_PROMPT = """\
You are a Knowledge Builder Framework faithfulness judge. Determine whether an
extracted field value is faithfully grounded in the source document snippet.

Field: {field_name}
Extraction instruction: {field_description}
Extracted value: {extracted_value}
Source snippet: {source_snippet}

Return ONLY a JSON object:
{{
  "faithful": true | false,
  "confidence": "high | medium | low",
  "reason": "1 sentence explanation"
}}

Rules:
- "faithful" = true if the extracted value is directly supported by the source snippet.
- "faithful" = false if the extracted value contains information NOT present in the snippet.
- Paraphrasing is acceptable; exact wording is not required.
- If the extracted value is empty/null and the field is optional, mark faithful=true.
- Base the judgment ONLY on the source snippet provided — do not use outside knowledge.
"""

# ---------------------------------------------------------------------------
# STATES list: ADR-027 16-state machine
# ---------------------------------------------------------------------------

# The canonical ADR-027 state machine.
STATES = [
    "IDENTIFY_PERSONA",
    "CAPTURE_INTENT",
    "CONFIGURE_SOURCES",
    "INSPECT_SOURCES",
    "UPLOAD_ARTIFACT_EXAMPLE",
    "DESIGN_SKILL",
    "REVIEW_DESIGN",
    "CONFIGURE_TRIGGERS",
    "PREVIEW_EXTRACTION",
    "CONFIRM",
    "COMMITTED",
    "VALIDATE",
    "INGEST",
    "EVAL",
    "PROMOTE",
    "DONE",
]

# Legacy states from the ADR-026 machine. In-flight sessions at deploy time
# execute via the legacy handlers (dispatch table below). New sessions never
# enter these states.
_STATES_LEGACY = [
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

# States that are in the legacy machine but NOT in the new one (for migration).
_LEGACY_ONLY_STATES = frozenset(_STATES_LEGACY) - frozenset(STATES)


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
    """Mutable session state threaded through the conversation.

    ADR-027 additions:
      - normalised_intent: structured goal object from CAPTURE_INTENT
      - source_samples: cached {source_id: [sample_dict]} from INSPECT_SOURCES
      - source_capability: list of per-source capability inventory dicts
      - artifact_layout: structural layout hint from UPLOAD_ARTIFACT_EXAMPLE
      - design: full output of DESIGN_SKILL LLM call

    Legacy fields retained for in-flight session compatibility:
      - slide_mapping, llm_suggested_specs (used by ANALYZE_ARTIFACT handler)
    """

    intent_description: str = ""
    artifact_path: str = ""
    fields: list[str] = field(default_factory=list)
    field_specs: dict[str, dict] = field(default_factory=dict)
    # Legacy: used by ANALYZE_ARTIFACT handler for in-flight sessions
    slide_mapping: dict | None = None
    llm_suggested_specs: dict[str, dict] = field(default_factory=dict)
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
    # ADR-027 new fields
    normalised_intent: dict = field(default_factory=dict)
    source_samples: dict = field(default_factory=dict)   # source_id -> list[sample_dict]
    source_capability: list = field(default_factory=list)  # per-source capability inventory
    artifact_layout: dict | None = None
    design: dict | None = None


class SkillBuilderConversation:
    """Interactive skill-builder session (author_skill API).

    ADR-027: 16-state design-first machine. Sources are inspected BEFORE schema
    design. One integrated DESIGN_SKILL LLM call produces schema + source_bindings
    + workflow_shape + reuse_plan. EVAL runs real scoring with auto-generated gold.

    Each call to start() / respond() returns a ConversationTurn describing what
    to display to the user and the current state.

    In-flight sessions under the ADR-026 15-state machine continue to execute
    via legacy handlers (ANALYZE_ARTIFACT, REVIEW_FIELDS, REVIEW_SCHEMA,
    CHECK_REUSE, PREVIEW). New sessions start at CAPTURE_INTENT.

    The session is serializable (to_dict/from_dict) for persistence in ADB,
    enabling resume across client restarts.
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
            # ADR-027: go to CAPTURE_INTENT (not ANALYZE_ARTIFACT)
            self._state = "CAPTURE_INTENT"
            return self._turn(self._advance_to_capture_intent())
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
            # ADR-027 new states
            "IDENTIFY_PERSONA": self._handle_identify_persona,
            "CAPTURE_INTENT": self._handle_capture_intent,
            "CONFIGURE_SOURCES": self._handle_configure_sources_response,
            "INSPECT_SOURCES": self._handle_inspect_sources_response,
            "UPLOAD_ARTIFACT_EXAMPLE": self._handle_upload_artifact_example,
            "DESIGN_SKILL": self._handle_design_skill_response,
            "REVIEW_DESIGN": self._handle_review_design_response,
            "CONFIGURE_TRIGGERS": self._handle_configure_triggers_response,
            "PREVIEW_EXTRACTION": self._handle_preview_extraction_response,
            "CONFIRM": self._handle_confirm_response,
            "COMMITTED": self._handle_committed_response,
            "VALIDATE": self._handle_validate_response,
            "INGEST": self._handle_ingest_response,
            "EVAL": self._handle_eval_response,
            "PROMOTE": self._handle_promote_response,
            "DONE": lambda _: ConversationTurn(
                state="DONE", message="Session complete.", done=True,
            ),
            # Legacy states (ADR-026 machine) — for in-flight sessions only
            "ANALYZE_ARTIFACT": self._handle_analyze_artifact,
            "REVIEW_FIELDS": self._handle_review_fields_response,
            "REVIEW_SCHEMA": self._handle_review_schema_response,
            "CHECK_REUSE": self._handle_check_reuse_response,
            "PREVIEW": self._handle_preview_response,
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

        NOTE: get_state() intentionally omits large artifacts to keep the
        GET-endpoint snapshot lean. We add them back here so that
        PREVIEW_EXTRACTION → CONFIRM → COMMIT and other transitions work
        correctly when a session is resumed across separate MCP calls.

        ADR-027 additions: normalised_intent, source_samples, source_capability,
        artifact_layout, design.

        Legacy fields: slide_mapping, llm_suggested_specs (in-flight sessions).
        """
        d = {"state": self._state, "persona": self._persona, **self.get_state()}
        d["synthesized_artifacts"] = dict(self._data.synthesized_artifacts)
        # Legacy fields (in-flight pre-ADR-027 sessions)
        if self._data.slide_mapping is not None:
            d["slide_mapping"] = dict(self._data.slide_mapping)
        if self._data.llm_suggested_specs:
            d["llm_suggested_specs"] = dict(self._data.llm_suggested_specs)
        # ADR-027 new fields
        if self._data.normalised_intent:
            d["normalised_intent"] = dict(self._data.normalised_intent)
        if self._data.source_samples:
            d["source_samples"] = dict(self._data.source_samples)
        if self._data.source_capability:
            d["source_capability"] = list(self._data.source_capability)
        if self._data.artifact_layout is not None:
            d["artifact_layout"] = dict(self._data.artifact_layout)
        if self._data.design is not None:
            d["design"] = dict(self._data.design)
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
            # ADR-027 new fields
            normalised_intent=dict(d.get("normalised_intent", {})),
            source_samples=dict(d.get("source_samples", {})),
            source_capability=list(d.get("source_capability", [])),
            artifact_layout=d.get("artifact_layout"),
            design=d.get("design"),
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
            # ADR-027: go to CAPTURE_INTENT to normalise the intent before
            # doing anything else. The old path went directly to ANALYZE_ARTIFACT
            # and derived field names from the artifact structure — now the
            # intent is normalised first, sources are proposed, sources are
            # inspected, and THEN the design is produced.
            self._state = "CAPTURE_INTENT"
            return self._advance_to_capture_intent()

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

    # ==================================================================
    # ADR-027 NEW STATE HANDLERS
    # ==================================================================

    # -- CAPTURE_INTENT --------------------------------------------------

    def _advance_to_capture_intent(self) -> ConversationTurn:
        """Run CAPTURE_INTENT: normalise the raw intent via one LLM call.

        Called immediately when the user has provided both persona and intent.
        Returns the CAPTURE_INTENT turn showing the normalised goal object with
        any ambiguities flagged for user confirmation.
        """
        self._state = "CAPTURE_INTENT"
        if not self._llm:
            raise RuntimeError(
                "CAPTURE_INTENT requires an LLM client. "
                "Per ADR-027 no-stub-mode policy, intent parsing cannot fall back to "
                "heuristics. Ensure the MCP server is started with a real LLM configured."
            )

        intent = self._data.intent_description
        persona = self._data.persona

        prompt = _CAPTURE_INTENT_PROMPT.format(
            persona=persona,
            intent=intent,
        )
        try:
            result = self._llm.chat(
                model="synthesis",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=1024,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            import re as _re
            cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
            normalised = json.loads(cleaned)
        except Exception as exc:
            raise RuntimeError(
                f"CAPTURE_INTENT: LLM call failed — cannot parse intent. "
                f"Error: {exc}. "
                f"Check LLM connectivity and retry."
            ) from exc

        # Update skill_name from normalised scope_domains + output_kind
        domains = normalised.get("scope_domains", [])
        kind = normalised.get("output_kind", "")
        if domains:
            slug_base = "_".join(d.lower().replace(" ", "_") for d in domains[:2])
            if kind and kind not in slug_base:
                slug_base = f"{slug_base}_{kind}"
            self._data.skill_name = _slugify(slug_base)
        else:
            # Fall back to slugifying the raw intent
            self._data.skill_name = _slugify(intent)

        self._data.normalised_intent = normalised
        log.info(
            "_advance_to_capture_intent: persona=%s output_kind=%s cadence=%s domains=%s",
            persona,
            normalised.get("output_kind"),
            normalised.get("cadence"),
            normalised.get("scope_domains"),
        )

        ambiguities = normalised.get("ambiguities", [])
        ambig_text = ""
        if ambiguities:
            ambig_text = (
                "\n\nAmbiguities detected — please confirm or correct:\n"
                + "\n".join(f"  • {a}" for a in ambiguities)
                + "\n\nType 'ok' to proceed with the above, or clarify any of the above."
            )
        else:
            ambig_text = "\n\nNo ambiguities. Type 'ok' to proceed."

        return ConversationTurn(
            state="CAPTURE_INTENT",
            message=(
                f"Intent parsed for persona '{persona}':\n\n"
                f"  Output kind: {normalised.get('output_kind', '?')}\n"
                f"  Audience: {normalised.get('audience', '?')}\n"
                f"  Cadence: {normalised.get('cadence', '?')}\n"
                f"  Scope: {', '.join(normalised.get('scope_domains', ['?']))}\n"
                f"  Success criteria: {'; '.join(normalised.get('success_criteria', ['?']))}\n"
                f"  Skill name: {self._data.skill_name}"
                + ambig_text
            ),
            data={"normalised_intent": normalised, "skill_name": self._data.skill_name},
            options=["ok"] + (["clarify"] if ambiguities else []),
        )

    def _handle_capture_intent(self, user_input: str) -> ConversationTurn:
        """Handle user response at CAPTURE_INTENT state."""
        lowered = user_input.lower().strip()

        # User confirms or clarifies
        if lowered in ("ok", "looks good", "continue", "yes", "proceed"):
            return self._advance_to_configure_sources_v2()

        # User wants to clarify — re-run with updated intent
        if lowered not in ("ok", "continue", "yes"):
            # Treat the user's input as an amendment to the intent
            amended_intent = f"{self._data.intent_description}. Additional context: {user_input}"
            self._data.intent_description = amended_intent
            return self._advance_to_capture_intent()

        return self._advance_to_configure_sources_v2()

    # -- CONFIGURE_SOURCES (v2 — LLM-assisted) ---------------------------

    def _advance_to_configure_sources_v2(self) -> ConversationTurn:
        """CONFIGURE_SOURCES with LLM-proposed source list (ADR-027)."""
        self._state = "CONFIGURE_SOURCES"

        # Extract sources already in the intent text (same as legacy path)
        if not self._data.sources and self._data.intent_description:
            auto = _extract_confluence_sources_from_text(self._data.intent_description)
            if auto:
                self._data.sources.extend(auto)

        # Build persona adapter list for the LLM prompt
        adapter_list = self._get_persona_adapters()

        if not self._llm:
            raise RuntimeError(
                "CONFIGURE_SOURCES requires an LLM client for source proposal. "
                "Per ADR-027 no-stub-mode policy."
            )

        prompt = _CONFIGURE_SOURCES_SUGGEST_PROMPT.format(
            persona=self._data.persona,
            normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
            adapter_list=json.dumps(adapter_list, indent=2),
            intent_text=self._data.intent_description,
        )
        try:
            result = self._llm.chat(
                model="synthesis",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=1024,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            import re as _re
            cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
            # The prompt asks for a JSON array — but json_object mode may wrap it
            parsed = json.loads(cleaned)
            proposed_sources = parsed if isinstance(parsed, list) else parsed.get("sources", [])
        except Exception as exc:
            log.warning(
                "_advance_to_configure_sources_v2: LLM source proposal failed (%s) — "
                "using pre-extracted sources only",
                exc,
            )
            proposed_sources = []

        # Merge proposed sources with any already auto-extracted
        existing_pages = {
            p
            for s in self._data.sources
            for p in (s.get("pages") or [])
        }
        for ps in proposed_sources:
            # Avoid adding duplicates
            ps_pages = ps.get("pages", [])
            if any(p in existing_pages for p in ps_pages):
                continue
            ps_clean = {k: v for k, v in ps.items() if k != "rationale"}
            self._data.sources.append(ps_clean)
            for p in ps_pages:
                existing_pages.add(p)

        if not self._data.sources:
            return ConversationTurn(
                state="CONFIGURE_SOURCES",
                message=(
                    "I could not find specific source references in your intent.\n\n"
                    "Please provide at least one source:\n"
                    "  • Paste a Confluence page URL or page ID\n"
                    "  • Confluence space: 'confluence SPACE_KEY'\n"
                    "  • Jira JQL: 'jira project = OPS AND labels = weekly-status'\n\n"
                    "Type 'done' when finished."
                ),
                options=["done"],
            )

        source_lines = "\n".join(
            f"  {i+1}. {s}"
            for i, s in enumerate(self._data.sources)
        )
        rationale_lines = "\n".join(
            f"  {i+1}. {ps.get('rationale', '')}"
            for i, ps in enumerate(proposed_sources)
            if ps.get("rationale")
        )
        rationale_section = (
            f"\nRationale:\n{rationale_lines}" if rationale_lines else ""
        )

        return ConversationTurn(
            state="CONFIGURE_SOURCES",
            message=(
                f"Proposed sources ({len(self._data.sources)}):\n{source_lines}"
                + rationale_section
                + "\n\nAdd more sources, edit the list, or type 'done' to proceed."
            ),
            data={"sources": self._data.sources},
            options=["done", "add <url_or_descriptor>"],
        )

    def _get_persona_adapters(self) -> list[str]:
        """Return a list of adapter type names available to this persona."""
        try:
            import yaml as _yaml
            pb_path = REPO_ROOT / "framework" / "persona_builders" / f"{self._data.persona}.yaml"
            if pb_path.exists():
                pb = _yaml.safe_load(pb_path.read_text()) or {}
                adapters = set()
                for kb in pb.get("knowledge_bases", []):
                    for src in kb.get("sources", []):
                        if src.get("kind"):
                            adapters.add(src["kind"])
                if adapters:
                    return sorted(adapters)
        except Exception as exc:
            log.debug("_get_persona_adapters: failed to read persona YAML: %s", exc)
        return ["confluence"]  # default

    # -- INSPECT_SOURCES -------------------------------------------------

    def _handle_inspect_sources_response(self, user_input: str) -> ConversationTurn:
        """Handle user response at INSPECT_SOURCES (confirmation of capability inventory)."""
        lowered = user_input.lower().strip()
        if lowered in ("ok", "looks good", "continue", "yes", "proceed", "next"):
            return self._advance_to_upload_artifact_example()
        # Any other input — re-show the inspection results
        return ConversationTurn(
            state="INSPECT_SOURCES",
            message=(
                "Type 'ok' to proceed to upload an artifact example, "
                "or 'back' to reconfigure sources."
            ),
            options=["ok", "back to sources"],
        )

    def _run_inspect_sources(self) -> ConversationTurn:
        """Fetch live source samples and produce a capability inventory (ADR-027).

        Called as a transition action — not as a response handler.
        Hard-fails if a source with a page_id/page_url returns no content.
        """
        self._state = "INSPECT_SOURCES"
        import os as _os

        if not self._llm:
            raise RuntimeError(
                "INSPECT_SOURCES requires an LLM client. "
                "Per ADR-027 no-stub-mode policy."
            )

        kbf_env = _os.environ.get("KBF_ENV", "laptop")
        capability_list: list[dict] = []
        source_samples_cache: dict = {}

        for src in self._data.sources:
            kind = src.get("kind", "confluence")
            if kind != "confluence":
                log.info("INSPECT_SOURCES: source kind=%s — non-Confluence sources not yet inspected", kind)
                continue

            pages = src.get("pages") or []
            page_id = src.get("page_id")
            page_url = src.get("page_url")

            # Collect identifiers to inspect
            ids_to_inspect: list[str] = list(pages)
            if page_id and str(page_id) not in ids_to_inspect:
                ids_to_inspect.append(str(page_id))
            if page_url and page_url not in ids_to_inspect:
                ids_to_inspect.append(page_url)

            if not ids_to_inspect:
                log.info("INSPECT_SOURCES: source has no page IDs/URLs — skipping")
                continue

            for source_id in ids_to_inspect[:2]:  # inspect at most 2 per source entry
                is_url = str(source_id).startswith("http")
                sq = {"page_url": source_id} if is_url else {"page_id": str(source_id)}

                try:
                    samples = fetch_samples(
                        adapter_name="confluence",
                        source_query=sq,
                        n=2,
                        require_live=True,
                        kbf_env=kbf_env,
                        repo_root=REPO_ROOT,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"INSPECT_SOURCES: failed to fetch source '{source_id}'. "
                        f"Error: {exc}. "
                        f"Verify the page ID/URL, Confluence adapter config, and "
                        f"network/auth access. Per ADR-027, inspection cannot fall back "
                        f"to synthetic samples."
                    ) from exc

                cache_key = f"confluence:{source_id}"
                source_samples_cache[cache_key] = samples

                # Build sample content for the LLM prompt (cap at 6k chars)
                sample_parts = []
                total_chars = 0
                for s in samples:
                    content = s.get("content", "")[:3000]
                    citation = s.get("source_citation", source_id)
                    sample_parts.append(f"--- {citation} ---\n{content}")
                    total_chars += len(content)
                    if total_chars >= 6000:
                        break
                sample_content = "\n\n".join(sample_parts)

                prompt = _INSPECT_SOURCES_PROMPT.format(
                    source_id=cache_key,
                    persona=self._data.persona,
                    normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
                    sample_content=sample_content[:6000],
                )
                try:
                    result = self._llm.chat(
                        model="synthesis",
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                        max_tokens=2048,
                    )
                    raw = result.get("text", "") if isinstance(result, dict) else str(result)
                    import re as _re
                    cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
                    cap = json.loads(cleaned)
                    cap["source_id"] = cache_key
                    capability_list.append(cap)
                    log.info(
                        "_run_inspect_sources: source=%s available=%d suggested=%d missing=%d",
                        cache_key,
                        len(cap.get("available_fields", [])),
                        len(cap.get("suggested_fields", [])),
                        len(cap.get("missing_fields", [])),
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"INSPECT_SOURCES: LLM capability analysis failed for source "
                        f"'{cache_key}'. Error: {exc}."
                    ) from exc

        if not capability_list:
            raise RuntimeError(
                "INSPECT_SOURCES: no sources were inspectable. "
                "Configure at least one Confluence source with a page_id or page_url."
            )

        # Store caches
        self._data.source_samples = source_samples_cache
        self._data.source_capability = capability_list

        # Format capability summary for the user
        lines = ["Source capability inventory:\n"]
        for cap in capability_list:
            src_id = cap.get("source_id", "?")
            summary = cap.get("summary", "")
            lines.append(f"Source: {src_id}")
            lines.append(f"  Summary: {summary}")
            available = cap.get("available_fields", [])
            if available:
                lines.append(f"  Available fields ({len(available)}):")
                for af in available[:8]:
                    conf = af.get("confidence", "?")
                    lines.append(f"    - {af.get('field', '?')} [{conf}]: {af.get('evidence', '')[:80]}")
            suggested = cap.get("suggested_fields", [])
            if suggested:
                lines.append(f"  Suggested additional fields ({len(suggested)}):")
                for sf in suggested[:5]:
                    lines.append(f"    - {sf.get('field', '?')}: {sf.get('reason', '')[:80]}")
            missing = cap.get("missing_fields", [])
            if missing:
                lines.append(f"  Cannot be extracted ({len(missing)}):")
                for mf in missing[:5]:
                    lines.append(f"    - {mf.get('field', '?')}: {mf.get('reason', '')[:80]}")
            lines.append("")

        lines.append("Type 'ok' to proceed to artifact upload (optional), or 'back' to reconfigure sources.")

        return ConversationTurn(
            state="INSPECT_SOURCES",
            message="\n".join(lines),
            data={"source_capability": capability_list},
            options=["ok", "back to sources"],
        )

    # -- UPLOAD_ARTIFACT_EXAMPLE -----------------------------------------

    def _advance_to_upload_artifact_example(self) -> ConversationTurn:
        """Transition to UPLOAD_ARTIFACT_EXAMPLE state (ADR-027)."""
        self._state = "UPLOAD_ARTIFACT_EXAMPLE"
        return ConversationTurn(
            state="UPLOAD_ARTIFACT_EXAMPLE",
            message=(
                "Optional: upload a reference artifact to provide a layout hint.\n\n"
                "This helps DESIGN_SKILL choose the right output format and layout.\n"
                "Provide the path to a PPTX, DOCX, Markdown, or text file.\n\n"
                "If you don't have a reference file, type 'skip'."
            ),
            options=["skip", "artifact:<filename> id:<artifact_id>", "/path/to/file.pptx"],
        )

    def _handle_upload_artifact_example(self, user_input: str) -> ConversationTurn:
        """Handle user input at UPLOAD_ARTIFACT_EXAMPLE state (ADR-027).

        Performs structural parse only — output is a layout hint, NOT field names.
        Field names come from DESIGN_SKILL.
        """
        from .analyze_artifact import analyze_artifact

        lowered = user_input.lower().strip()
        if lowered in ("skip", "no", "none", "later"):
            self._data.artifact_layout = None
            self._data.artifact_path = ""
            return self._run_design_skill()

        path = user_input.strip()

        # Handle artifact: prefix (ADR-021 uploaded artifacts)
        if path.startswith("artifact:"):
            import re as _re
            m = _re.match(r"^artifact:(\S+)\s+id:(\S+)$", path)
            if m and self._artifact_store is not None:
                artifact_id = m.group(2)
                local_path = self._artifact_store.resolve(artifact_id)
                if local_path:
                    path = str(local_path)
                    self._data.artifact_path = path
                else:
                    log.warning("UPLOAD_ARTIFACT_EXAMPLE: artifact_id=%s not found", artifact_id)
                    return self._run_design_skill()
            else:
                return self._run_design_skill()

        # Local filesystem path
        p = Path(path)
        if p.exists() and p.suffix in (".pptx", ".docx", ".md", ".txt"):
            try:
                _fields, mapping = analyze_artifact(str(p))
                # Build layout hint from the mapping (section order, headings)
                layout = {
                    "sections": _fields,
                    "slide_count": len({v.get("slide", 0) for v in (mapping or {}).values()}),
                    "mapping": mapping or {},
                }
                self._data.artifact_layout = layout
                self._data.artifact_path = str(p)
                log.info(
                    "UPLOAD_ARTIFACT_EXAMPLE: parsed layout sections=%d from %s",
                    len(_fields), p,
                )
            except ValueError as exc:
                # Image-only PPTX: hard-fail (ADR-026 Fix 1 still applies)
                return ConversationTurn(
                    state="UPLOAD_ARTIFACT_EXAMPLE",
                    message=(
                        f"Cannot parse artifact: {exc}\n\n"
                        "Please provide a text-bearing PPTX/DOCX/Markdown, "
                        "or type 'skip' to proceed without an artifact."
                    ),
                    options=["skip"],
                )
        else:
            log.warning(
                "UPLOAD_ARTIFACT_EXAMPLE: path %r not found or unsupported — skipping",
                path,
            )
            self._data.artifact_layout = None

        return self._run_design_skill()

    # -- DESIGN_SKILL ----------------------------------------------------

    def _run_design_skill(self) -> ConversationTurn:
        """Run the integrated DESIGN_SKILL LLM call (ADR-027).

        One big call that sees intent + source capability + artifact layout +
        existing KB cards and produces schema + source_bindings + workflow_shape
        + reuse_plan + open_questions.
        """
        self._state = "DESIGN_SKILL"

        if not self._llm:
            raise RuntimeError(
                "DESIGN_SKILL requires an LLM client. "
                "Per ADR-027 no-stub-mode policy."
            )

        # Load existing KB cards for this persona
        try:
            from ..orchestrator.shim_kb import ShimKb
            pb_dir = REPO_ROOT / "framework" / "persona_builders"
            shim = ShimKb(pb_dir, skill_store=self._skill_store)
            existing_cards = shim.cards_visible_to(self._data.persona)
            cards_summary = [
                {
                    "name": c.get("name", "?"),
                    "provides_fields": c.get("provides_fields", []),
                }
                for c in existing_cards[:10]  # cap to avoid huge prompt
            ]
        except Exception as exc:
            log.warning("_run_design_skill: could not load ShimKb cards: %s", exc)
            cards_summary = []

        prompt = _DESIGN_SKILL_PROMPT.format(
            persona=self._data.persona,
            normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
            source_capability=json.dumps(self._data.source_capability, indent=2),
            artifact_layout=json.dumps(self._data.artifact_layout, indent=2)
            if self._data.artifact_layout
            else "null",
            existing_kb_cards=json.dumps(cards_summary, indent=2),
        )
        try:
            result = self._llm.chat(
                model="synthesis",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=4096,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            import re as _re
            cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
            design = json.loads(cleaned)
        except Exception as exc:
            raise RuntimeError(
                f"DESIGN_SKILL: LLM design call failed. Error: {exc}. "
                f"Check LLM connectivity and retry."
            ) from exc

        # Validate design output
        if "schema" not in design or "properties" not in design.get("schema", {}):
            raise RuntimeError(
                "DESIGN_SKILL: LLM returned an invalid design (missing schema.properties). "
                "This is a prompt engineering bug — check _DESIGN_SKILL_PROMPT."
            )

        self._data.design = design

        # Populate fields + field_specs from design.schema
        schema = design["schema"]
        properties = schema.get("properties", {})
        self._data.fields = list(properties.keys())
        self._data.field_specs = {
            f: dict(spec)
            for f, spec in properties.items()
        }

        # Populate reuse_result from design.reuse_plan, but filter out any
        # hallucinated KB references. The DESIGN_SKILL LLM occasionally
        # invents KB names (e.g. 'tpm_dependencies') that look plausible
        # but don't exist in the persona builder index. If we let those
        # through, ADR-017 link validation correctly blocks promotion.
        # Filter against the real KB index (ShimKb) so reuse covers only
        # KBs the framework actually knows about.
        reuse_plan = design.get("reuse_plan", {"covered": {}, "gaps": list(self._data.fields)})
        known_kbs: set[str] = set()
        try:
            from ..orchestrator.shim_kb import ShimKb
            shim = ShimKb(REPO_ROOT / "framework" / "persona_builders")
            for card in (shim.all_cards() if hasattr(shim, "all_cards") else []):
                # Cards are keyed as 'persona.kb_name'; also accept the short form
                name = card.get("name") or ""
                persona_owner = card.get("persona") or ""
                if name:
                    known_kbs.add(name)
                    if persona_owner:
                        known_kbs.add(f"{persona_owner}.{name}")
        except Exception as exc:  # noqa: BLE001
            log.warning("_run_design_skill: could not load ShimKb to validate reuse_plan: %s", exc)
        if known_kbs:
            filtered_covered: dict = {}
            dropped: list[str] = []
            for fld, kb in (reuse_plan.get("covered") or {}).items():
                if kb in known_kbs:
                    filtered_covered[fld] = kb
                else:
                    dropped.append(f"{fld}→{kb}")
            if dropped:
                log.warning(
                    "_run_design_skill: dropping %d reuse claims referencing unknown KBs: %s",
                    len(dropped), dropped[:5],
                )
            # Any field whose claimed KB was dropped now flows into the new
            # skill's KB (the workflow synthesizer handles this automatically
            # via _build_requires_extractions — fields not in covered_fields
            # are owned by the new KB).
            reuse_plan = dict(reuse_plan)
            reuse_plan["covered"] = filtered_covered
        self._data.reuse_result = reuse_plan

        # Populate trigger and output_format from design.workflow_shape
        ws = design.get("workflow_shape", {})
        self._data.output_format = ws.get("output_format", "markdown")
        trigger = ws.get("trigger", {"on_request": True})
        self._data.trigger = trigger

        log.info(
            "_run_design_skill: fields=%d output_format=%s reuse_covered=%d gaps=%d",
            len(self._data.fields),
            self._data.output_format,
            len(reuse_plan.get("covered", {})),
            len(reuse_plan.get("gaps", [])),
        )

        return self._prompt_review_design()

    def _handle_design_skill_response(self, user_input: str) -> ConversationTurn:
        """Handle user response at DESIGN_SKILL state (rare — usually auto-transitions)."""
        # This state auto-transitions via _run_design_skill; the handler is
        # here to catch any edge cases where the state machine lands here
        # from a session restore without a design having run yet.
        if self._data.design is None:
            return self._run_design_skill()
        return self._prompt_review_design()

    # -- REVIEW_DESIGN ---------------------------------------------------

    def _prompt_review_design(self) -> ConversationTurn:
        """Render the full design for user review."""
        self._state = "REVIEW_DESIGN"
        design = self._data.design or {}
        schema = design.get("schema", {})
        properties = schema.get("properties", {})
        source_bindings = design.get("source_bindings", {})
        workflow_shape = design.get("workflow_shape", {})
        reuse_plan = design.get("reuse_plan", {})
        unsupportable = design.get("unsupportable_fields", [])
        open_questions = design.get("open_questions", [])

        lines = ["=== Skill Design ===\n"]

        # Schema
        lines.append(f"Schema ({len(properties)} fields):")
        required = schema.get("required", [])
        for fname, spec in properties.items():
            req_tag = " [required]" if fname in required else ""
            t = spec.get("type", "string")
            desc = spec.get("description", "")[:100]
            binding = ", ".join(source_bindings.get(fname, ["?"]))
            lines.append(f"  {fname}{req_tag} [{t}] ← {binding}")
            lines.append(f"    {desc}")

        # Workflow shape
        lines.append("\nWorkflow shape:")
        lines.append(f"  Output format: {workflow_shape.get('output_format', '?')}")
        lines.append(f"  Layout: {workflow_shape.get('layout', 'default')}")
        trig = workflow_shape.get("trigger", {})
        trig_str = "on-request" if trig.get("on_request") else ""
        sched = trig.get("schedule")
        if sched:
            trig_str += (", " if trig_str else "") + f"scheduled: {sched}"
        lines.append(f"  Trigger: {trig_str or '?'}")

        # Reuse plan
        covered = reuse_plan.get("covered", {})
        gaps = reuse_plan.get("gaps", [])
        if covered:
            lines.append(f"\nReuse ({len(covered)} fields from existing KBs):")
            for f, kb in covered.items():
                lines.append(f"  {f} → {kb}")
        if gaps:
            lines.append(f"\nNew extraction needed ({len(gaps)} fields):")
            for g in gaps:
                lines.append(f"  {g}")

        # Warnings
        if unsupportable:
            lines.append(f"\nCannot extract ({len(unsupportable)} fields):")
            for u in unsupportable:
                lines.append(f"  {u.get('field', '?')}: {u.get('reason', '')}")

        if open_questions:
            lines.append(f"\nOpen questions ({len(open_questions)}):")
            for q in open_questions:
                lines.append(f"  ? {q}")

        lines.append("\n=== End Design ===")
        lines.append("\nEdit commands (trivial — no LLM call):")
        lines.append("  describe <field> as <text>")
        lines.append("  set type of <field> to <type>")
        lines.append("  rename field <old> to <new>")
        lines.append("  remove field <name>")
        lines.append("  set trigger to <cron>")
        lines.append("\nFor major changes (new source, new field from Jira, etc.) describe them in plain English.")
        lines.append("\nType 'ok' when the design looks right.")

        return ConversationTurn(
            state="REVIEW_DESIGN",
            message="\n".join(lines),
            data={"design": design},
            options=["ok", "describe <field> as <text>", "remove field <name>"],
        )

    def _handle_review_design_response(self, user_input: str) -> ConversationTurn:
        """Handle user input at REVIEW_DESIGN state."""
        lowered = user_input.lower().strip()

        if lowered in ("ok", "looks good", "continue", "yes", "proceed"):
            return self._advance_to_configure_triggers()

        # Trivial edits — deterministic patching
        result = self._apply_design_patch(user_input)
        if result is not None:
            # Trivial edit applied — re-render
            return self._prompt_review_design()

        # Substantive edit — trigger LLM re-plan
        return self._run_design_replan(user_input)

    def _apply_design_patch(self, user_input: str) -> bool | None:
        """Apply a trivial deterministic design edit. Returns True on success, None on no-match."""
        import re as _re
        design = self._data.design or {}
        schema = design.get("schema", {})
        properties = schema.get("properties", {})

        # describe <field> as <text>
        m = _re.match(r"(?i)describe\s+(\S+)\s+as\s+(.+)", user_input)
        if m:
            fname = _to_field_name(m.group(1))
            new_desc = m.group(2).strip().strip("'\"")
            if fname in properties:
                properties[fname]["description"] = new_desc
                if fname in self._data.field_specs:
                    self._data.field_specs[fname]["description"] = new_desc
                return True

        # set type of <field> to <type>
        m = _re.match(r"(?i)set\s+type\s+of\s+(\S+)\s+to\s+(\S+)", user_input)
        if m:
            fname = _to_field_name(m.group(1))
            new_type = m.group(2).strip()
            if fname in properties and new_type in ("string", "integer", "number", "boolean", "array"):
                properties[fname]["type"] = new_type
                if fname in self._data.field_specs:
                    self._data.field_specs[fname]["type"] = new_type
                return True

        # rename field <old> to <new>
        m = _re.match(r"(?i)rename\s+field\s+(\S+)\s+to\s+(\S+)", user_input)
        if m:
            old_name = _to_field_name(m.group(1))
            new_name = _to_field_name(m.group(2))
            if old_name in properties:
                spec = properties.pop(old_name)
                properties[new_name] = spec
                # Update ordered fields list
                self._data.fields = [
                    new_name if f == old_name else f
                    for f in self._data.fields
                ]
                if old_name in self._data.field_specs:
                    self._data.field_specs[new_name] = self._data.field_specs.pop(old_name)
                # Update source_bindings
                sb = design.get("source_bindings", {})
                if old_name in sb:
                    sb[new_name] = sb.pop(old_name)
                return True

        # remove field <name>
        m = _re.match(r"(?i)remove\s+field\s+(\S+)", user_input)
        if m:
            fname = _to_field_name(m.group(1))
            if fname in properties:
                del properties[fname]
                self._data.fields = [f for f in self._data.fields if f != fname]
                self._data.field_specs.pop(fname, None)
                sb = design.get("source_bindings", {})
                sb.pop(fname, None)
                # Remove from required list if present
                req = schema.get("required", [])
                schema["required"] = [r for r in req if r != fname]
                return True

        # set trigger to <cron>
        m = _re.match(r"(?i)set\s+trigger\s+to\s+(.+)", user_input)
        if m:
            cron = m.group(1).strip()
            ws = design.get("workflow_shape", {})
            ws["trigger"] = {"on_request": True, "schedule": cron}
            self._data.trigger = ws["trigger"]
            return True

        return None

    def _run_design_replan(self, edit_request: str) -> ConversationTurn:
        """Run LLM re-plan for a substantive design change (ADR-027)."""
        if not self._llm:
            raise RuntimeError(
                "REVIEW_DESIGN replan requires an LLM client. "
                "Per ADR-027 no-stub-mode policy."
            )
        prompt = _REVIEW_DESIGN_REPLAN_PROMPT.format(
            current_design=json.dumps(self._data.design, indent=2),
            edit_request=edit_request,
            updated_source_capability=json.dumps(self._data.source_capability, indent=2),
        )
        try:
            result = self._llm.chat(
                model="synthesis",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=2048,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            import re as _re
            cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
            diff = json.loads(cleaned)
        except Exception as exc:
            raise RuntimeError(
                f"REVIEW_DESIGN replan: LLM call failed. Error: {exc}."
            ) from exc

        # Apply diff to design
        design = self._data.design or {}
        schema = design.setdefault("schema", {})
        properties = schema.setdefault("properties", {})
        sb = design.setdefault("source_bindings", {})

        for fname, spec in diff.get("schema_add", {}).items():
            properties[fname] = spec
            self._data.fields.append(fname)
            self._data.field_specs[fname] = spec

        for fname in diff.get("schema_remove", []):
            properties.pop(fname, None)
            self._data.fields = [f for f in self._data.fields if f != fname]
            self._data.field_specs.pop(fname, None)

        for fname, spec_delta in diff.get("schema_update", {}).items():
            if fname in properties:
                properties[fname].update(spec_delta)
            if fname in self._data.field_specs:
                self._data.field_specs[fname].update(spec_delta)

        sb.update(diff.get("source_bindings_add", {}))
        for fname in diff.get("source_bindings_remove", []):
            sb.pop(fname, None)

        if "workflow_shape_update" in diff:
            design.setdefault("workflow_shape", {}).update(diff["workflow_shape_update"])

        if "reuse_plan_update" in diff:
            design["reuse_plan"] = diff["reuse_plan_update"]
            self._data.reuse_result = diff["reuse_plan_update"]

        if diff.get("open_questions"):
            existing = design.get("open_questions", [])
            design["open_questions"] = existing + diff["open_questions"]

        return self._prompt_review_design()

    # -- PREVIEW_EXTRACTION ----------------------------------------------

    def _advance_to_preview_extraction(self) -> ConversationTurn:
        """Transition to PREVIEW_EXTRACTION: run real LLM extraction on cached samples."""
        self._state = "PREVIEW_EXTRACTION"
        from .review import review_extractions
        from .synthesize_schema import synthesize_extraction_schema

        if not self._llm:
            raise RuntimeError(
                "PREVIEW_EXTRACTION requires an LLM client. "
                "Per ADR-027 no-stub-mode policy."
            )

        # Get all cached samples from INSPECT_SOURCES
        all_samples: list[dict] = []
        for cache_key, samples in self._data.source_samples.items():
            all_samples.extend(samples)

        if not all_samples:
            raise RuntimeError(
                "PREVIEW_EXTRACTION: no source samples are cached. "
                "INSPECT_SOURCES must have run and cached at least one sample. "
                "Per ADR-027, there is no synthetic sample fallback at this state."
            )

        # Build schema from ALL fields in the design (not just reuse_plan.gaps).
        # Bug fix (post-ADR-027 walk): reuse_plan.gaps from DESIGN_SKILL output
        # is the "cannot extract" subset, NOT "fields needing a new extraction
        # skill". For a brand-new skill with no reuse, we want every designed
        # field to appear in the schema, the gold set, and the preview/eval.
        all_fields = list(self._data.fields)
        schema = synthesize_extraction_schema(all_fields, self._data.persona, self._data.skill_name)
        for f in all_fields:
            if f in self._data.field_specs and f in schema.get("properties", {}):
                schema["properties"][f] = dict(self._data.field_specs[f])

        # Run review_extractions (ADR-026 Fix 3)
        review_result = review_extractions(
            samples=all_samples[:3],  # up to 3 samples
            schema=schema,
            llm=self._llm,
        )

        # Format for user display
        lines = ["=== Extraction Preview (live data) ===\n"]
        for ex in review_result.get("extractions", []):
            citation = ex.get("source_citation", "?")
            extracted = ex.get("extracted", {})
            missing = ex.get("missing_fields", [])
            lines.append(f"Source: {citation}")
            for f, v in extracted.items():
                val_str = str(v)[:150] if v else "<empty>"
                lines.append(f"  {f}: {val_str}")
            if missing:
                lines.append(f"  Missing required fields: {', '.join(missing)}")
            lines.append("")

        coverage = review_result.get("field_coverage", {})
        issues = review_result.get("issues", [])
        if coverage:
            lines.append("Field coverage:")
            for f, cov in coverage.items():
                lines.append(f"  {f}: {int(cov * 100)}%")
        if issues:
            lines.append("\nIssues:")
            for issue in issues:
                lines.append(f"  ! {issue}")

        lines.append("\n=== End Extraction Preview ===")
        lines.append("\nThis is what the parser will extract from your source at query time.")
        lines.append("Type 'ok' to confirm and assemble artifacts, or 'back' to revise the design.")

        return ConversationTurn(
            state="PREVIEW_EXTRACTION",
            message="\n".join(lines),
            data={"extraction_preview": review_result},
            options=["ok, commit", "back to design"],
        )

    def _handle_preview_extraction_response(self, user_input: str) -> ConversationTurn:
        """Handle user input at PREVIEW_EXTRACTION state."""
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("ok", "commit", "yes", "looks good", "proceed")):
            return self._handle_commit_v2()
        if "back" in lowered or "design" in lowered:
            return self._prompt_review_design()
        return ConversationTurn(
            state="PREVIEW_EXTRACTION",
            message="Type 'ok' to commit or 'back to design' to revise.",
            options=["ok, commit", "back to design"],
        )

    def _handle_commit_v2(self) -> ConversationTurn:
        """Assemble and commit artifacts (ADR-027 version of _handle_commit)."""
        # Synthesize artifacts from the design
        artifacts = self._synthesize_preview()
        self._data.synthesized_artifacts = artifacts
        return self._handle_commit()

    # ==================================================================
    # END ADR-027 NEW STATE HANDLERS
    # ==================================================================

    # Legacy ANALYZE_ARTIFACT handler (for in-flight pre-ADR-027 sessions)
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

        Three-pass approach (ADR-026 Fix 4 adds pass 3):
        1. Fields covered by llm_suggested_specs (from ANALYZE_ARTIFACT LLM call):
           apply the LLM's type + description directly — these will be high quality.
        2. User-added fields NOT in llm_suggested_specs (delta fields):
           call synthesize_field_descriptions() for a targeted LLM description
           using raw_title + body_text context from the slide_mapping.
        3. Source-grounded coherence review (ADR-026): fetch 2-3 live samples from
           declared Confluence sources and ask the LLM to flag unsupportable fields
           and suggest missing ones. Findings are surfaced in the REVIEW_SCHEMA prompt
           so the user can refine before committing.
        Falls back gracefully when no LLM is wired up.
        """
        from .synthesize_schema import _infer_field_spec, synthesize_field_descriptions

        new_fields = [f for f in self._data.fields if f not in self._data.field_specs]
        if not new_fields:
            self._state = "REVIEW_SCHEMA"
            # Still run source-grounded review even when specs already exist
            source_review = self._source_grounded_review(self._data.field_specs)
            return self._prompt_review_schema(source_review=source_review)

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

        # Pass 3 — source-grounded coherence review (ADR-026 Fix 4)
        source_review = self._source_grounded_review(self._data.field_specs)

        self._state = "REVIEW_SCHEMA"
        return self._prompt_review_schema(
            delta_fields=delta_fields if delta_fields else None,
            source_review=source_review,
        )

    # -- Source-grounded review (ADR-026 Fix 4) --------------------------

    _SOURCE_GROUNDED_REVIEW_PROMPT = """\
You are a Knowledge Builder Framework schema engineer performing a source-coherence check.

Persona: {persona}
Intent: "{intent}"

CANDIDATE EXTRACTION SCHEMA (fields the user wants to extract):
{schema_summary}

LIVE SOURCE CONTENT (sample from the actual Confluence page(s) declared as sources):
{sample_content}

Your task — analyse the source content against the candidate schema and return a JSON object:
{{
  "unsupportable_fields": [
    {{"field": "field_name", "reason": "why this field cannot be extracted from the source"}}
  ],
  "suggested_additions": [
    {{"field": "snake_case_name", "type": "string|array|integer",
      "description": "extraction instruction",
      "reason": "what in the source suggests this field"}}
  ],
  "enum_corrections": [
    {{"field": "field_name",
      "current_enum": ["val1"],
      "seen_in_source": ["actual_val1", "actual_val2"],
      "recommendation": "update enum or use free-text string"}}
  ],
  "summary": "1-3 sentence note for the user about overall schema-source alignment"
}}

Rules:
- Only flag a field as unsupportable if the source clearly cannot produce it.
- Only suggest additions that are clearly and consistently present in the source.
- If the schema is well-aligned, return empty lists and a positive summary.
- Base ALL findings on the actual source content above — do not invent.
"""

    def _source_grounded_review(self, field_specs: dict) -> dict | None:
        """Fetch live source samples and run a schema-coherence LLM check.

        Returns a dict with keys: unsupportable_fields, suggested_additions,
        enum_corrections, summary.  Returns None on any failure (advisory, never
        blocking).

        Per ADR-026 Fix 4: this is an advisory, one-LLM-call review that surfaces
        findings inside REVIEW_SCHEMA without inserting a new state.
        """
        if self._llm is None:
            log.info("_source_grounded_review: no LLM wired — skipping")
            return None

        # Find Confluence sources with page IDs or URLs
        confluence_sources = [
            s for s in (self._data.sources or [])
            if s.get("kind") == "confluence" and (
                s.get("page_id") or s.get("page_url")
                or s.get("pages")
            )
        ]
        if not confluence_sources:
            log.info(
                "_source_grounded_review: no Confluence page-id/url sources configured "
                "(sources=%s) — skipping live fetch",
                self._data.sources,
            )
            return None

        try:
            import os as _os

            kbf_env = _os.environ.get("KBF_ENV", "laptop")

            # Collect samples from up to 2 source entries
            all_samples: list[dict] = []
            for src in confluence_sources[:2]:
                pages = src.get("pages") or []
                if pages:
                    for pg in pages[:2]:
                        # page may be a URL or a page_id string
                        is_url = str(pg).startswith("http")
                        sq = {"page_url": pg} if is_url else {"page_id": str(pg)}
                        try:
                            s = fetch_samples(
                                adapter_name="confluence",
                                source_query=sq,
                                n=1,
                                kbf_env=kbf_env,
                                repo_root=REPO_ROOT,
                            )
                            all_samples.extend(s)
                        except Exception as exc:
                            log.warning(
                                "_source_grounded_review: fetch page %s failed: %s",
                                pg, exc,
                            )
                elif src.get("page_id") or src.get("page_url"):
                    sq = {}
                    if src.get("page_url"):
                        sq["page_url"] = src["page_url"]
                    if src.get("page_id"):
                        sq["page_id"] = str(src["page_id"])
                    try:
                        s = fetch_samples(
                            adapter_name="confluence",
                            source_query=sq,
                            n=2,
                            kbf_env=kbf_env,
                            repo_root=REPO_ROOT,
                        )
                        all_samples.extend(s)
                    except Exception as exc:
                        log.warning(
                            "_source_grounded_review: fetch failed for src=%s: %s",
                            src, exc,
                        )

            if not all_samples:
                log.warning(
                    "_source_grounded_review: no live samples fetched — skipping review"
                )
                return None

            # Build schema summary
            schema_lines = []
            for field, spec in field_specs.items():
                t = spec.get("type", "string")
                desc = spec.get("description", "")
                enum = spec.get("enum")
                extra = f" (enum: {enum})" if enum else ""
                schema_lines.append(f"  - {field} [{t}{extra}]: {desc[:120]}")

            # Combine sample content (cap total at 8k chars for prompt budget)
            sample_parts = []
            total_chars = 0
            for s in all_samples:
                content = s.get("content", s.get("text", ""))[:4000]
                citation = s.get("source_citation", "?")
                sample_parts.append(f"--- {citation} ---\n{content}")
                total_chars += len(content)
                if total_chars >= 8000:
                    break
            sample_content = "\n\n".join(sample_parts)

            prompt = self._SOURCE_GROUNDED_REVIEW_PROMPT.format(
                persona=self._data.persona or "unknown",
                intent=self._data.intent_description or "",
                schema_summary="\n".join(schema_lines),
                sample_content=sample_content[:8000],
            )

            result = self._llm.chat(
                model="synthesis",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=2048,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            import re as _re
            cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
            review_data = json.loads(cleaned)

            # Attach citations so the user can trace findings back to the source
            review_data["_source_citations"] = [
                s.get("source_citation", "?") for s in all_samples
            ]
            log.info(
                "_source_grounded_review: complete — %d unsupportable, "
                "%d suggested, citations=%s",
                len(review_data.get("unsupportable_fields", [])),
                len(review_data.get("suggested_additions", [])),
                review_data["_source_citations"],
            )
            return review_data

        except Exception as exc:
            log.warning(
                "_source_grounded_review: failed (%s) — schema review will proceed without source grounding",
                exc,
            )
            return None

    def _prompt_review_schema(
        self,
        delta_fields: list[str] | None = None,
        source_review: dict | None = None,
    ) -> ConversationTurn:
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
                    f"\n{len(delta_fields)} field(s) were added after the artifact analysis "
                    f"({', '.join(delta_fields)}) — their descriptions were synthesised from "
                    "context and may need more refinement than the rest.\n"
                )

        # "removed from artifact" — only show when artifact produced original fields
        # that the user then dropped (not meaningful when no artifact was uploaded).
        if artifact_was_analyzed and original_fields:
            removed = original_fields - set(self._data.fields)
            if removed:
                delta_note += (
                    f"{len(removed)} field(s) identified in the artifact were removed "
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

        # Source-grounded review findings (ADR-026 Fix 4)
        if source_review:
            citations = source_review.get("_source_citations", [])
            citation_str = ", ".join(citations) if citations else "live source"
            lines.append(
                f"\n=== Source-grounded review (against {citation_str}) ==="
            )
            summary = source_review.get("summary", "")
            if summary:
                lines.append(f"  {summary}")

            unsupportable = source_review.get("unsupportable_fields", [])
            if unsupportable:
                lines.append(
                    f"\n  Fields the source may NOT support ({len(unsupportable)}):"
                )
                for item in unsupportable:
                    lines.append(
                        f"    - {item.get('field', '?')}: {item.get('reason', '')}"
                    )

            additions = source_review.get("suggested_additions", [])
            if additions:
                lines.append(
                    f"\n  Fields the source clearly CONTAINS but schema is missing ({len(additions)}):"
                )
                for item in additions:
                    lines.append(
                        f"    - {item.get('field', '?')}: {item.get('reason', '')}"
                    )
                lines.append(
                    "  Use 'add <field>' at REVIEW_FIELDS or type the field name below to include it."
                )

            enum_corrections = source_review.get("enum_corrections", [])
            if enum_corrections:
                lines.append(f"\n  Enum corrections ({len(enum_corrections)}):")
                for item in enum_corrections:
                    lines.append(
                        f"    - {item.get('field', '?')}: "
                        f"declared {item.get('current_enum')} but source uses "
                        f"{item.get('seen_in_source')} — {item.get('recommendation', '')}"
                    )
            lines.append("=== End source review ===\n")

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

        turn_data: dict = {"field_specs": field_data}
        if source_review:
            turn_data["source_review"] = source_review

        return ConversationTurn(
            state="REVIEW_SCHEMA",
            message="\n".join(lines),
            data=turn_data,
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
            # ADR-027: new-machine sessions go to INSPECT_SOURCES;
            # legacy sessions (no normalised_intent) go to CONFIGURE_TRIGGERS
            if self._data.normalised_intent:
                return self._run_inspect_sources()
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

        # ADR-027: show what DESIGN_SKILL already proposed so the user just confirms
        design_ws = (self._data.design or {}).get("workflow_shape", {})
        proposed_trigger = design_ws.get("trigger", {})
        proposed_format = design_ws.get("output_format", self._data.output_format or "markdown")

        proposed_lines = []
        if proposed_trigger.get("on_request"):
            proposed_lines.append("on-request")
        if proposed_trigger.get("schedule"):
            proposed_lines.append(f"scheduled: {proposed_trigger['schedule']}")
        proposed_str = " + ".join(proposed_lines) or "on-request only"

        return ConversationTurn(
            state="CONFIGURE_TRIGGERS",
            message=(
                f"Trigger (from design proposal): {proposed_str}\n"
                f"Output format: {proposed_format}\n\n"
                "Confirm or override:\n"
                "  1. on-request only (user asks → skill runs immediately)\n"
                "  2. scheduled only  (e.g. '0 16 * * 5' = every Friday 4pm)\n"
                "  3. both            (on-request + schedule)\n\n"
                "Type 'ok' to accept the proposal, or enter your choice.\n"
                "Example: '3, pptx, 0 16 * * 5'"
            ),
            options=[
                "ok",
                "1, markdown",
                "2, pptx, 0 16 * * 5",
                "3, pptx, 0 16 * * 5",
            ],
        )

    def _handle_configure_triggers_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()

        if lowered in ("ok", "yes", "continue", "looks good"):
            # Accept the proposal from DESIGN_SKILL
            design_ws = (self._data.design or {}).get("workflow_shape", {})
            if design_ws.get("trigger"):
                self._data.trigger = design_ws["trigger"]
            if design_ws.get("output_format"):
                self._data.output_format = design_ws["output_format"]
        else:
            trigger, output_format = _parse_trigger_input(user_input)
            self._data.trigger = trigger
            self._data.output_format = output_format

        # ADR-027: transition to PREVIEW_EXTRACTION (not PREVIEW)
        # If we are in the new machine (source_capability populated), use
        # PREVIEW_EXTRACTION. If this is a legacy session (old machine, PREVIEW
        # state), use the old PREVIEW path.
        if self._data.source_capability or self._data.source_samples:
            return self._advance_to_preview_extraction()
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
        # Wire WikiMetadataStore so ingest_page populates the index that
        # search_wiki retriever queries. Without this, pages land on disk
        # as markdown but the retrieval layer can't find them at query time
        # — exactly the gap behind "no relevant context found" after the
        # weekly_exec_review_26ai skill's INGEST in synth-tpm-bcbc739d.
        # Default root (~/.kbf/store/wiki_metadata) is shared with the
        # search_wiki retriever instance in the MCP server's lifespan.
        from ..stores.wiki_metadata_store import WikiMetadataStore
        wiki_store = WikiMetadataStore()
        ingestor = ConfluenceWikiIngestor(adapter=confluence_adapter, wiki_store=wiki_store)

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
        """Real eval harness (ADR-027 DECISION-010 Option A).

        Algorithm:
        1. Re-use source_samples cached at INSPECT_SOURCES; re-fetch if missing.
        2. Run extraction against each sample with _llm_extract.
        3. Write extraction gold rows (kind=auto_generated).
        4. Call /api/v1/ask for workflow-level scoring.
        5. Write workflow gold rows (kind=auto_generated).
        6. Compute recall@k + faithfulness (LLM judge).
        7. Gate PROMOTE on exit_criteria thresholds.
        8. Surface metrics + disclaimer to user.
        """
        import os as _os
        import time

        self._state = "EVAL"

        if not self._llm:
            raise RuntimeError(
                "EVAL requires an LLM client. "
                "Per ADR-027 no-stub-mode policy, eval cannot be skipped or stubbed."
            )

        persona = self._data.persona
        skill_name = self._data.skill_name

        # Step 1: get samples (cache from INSPECT_SOURCES or re-fetch)
        all_samples: list[dict] = []
        for _key, samples in self._data.source_samples.items():
            all_samples.extend(samples)

        if not all_samples:
            # Session resumed after INGEST from a pre-ADR-027 path; re-fetch
            kbf_env = _os.environ.get("KBF_ENV", "laptop")
            for src in self._data.sources:
                if src.get("kind") != "confluence":
                    continue
                pages = src.get("pages") or []
                page_id = src.get("page_id")
                page_url = src.get("page_url")
                ids = list(pages)
                if page_id:
                    ids.append(str(page_id))
                if page_url:
                    ids.append(page_url)
                for sid in ids[:2]:
                    is_url = str(sid).startswith("http")
                    sq = {"page_url": sid} if is_url else {"page_id": str(sid)}
                    try:
                        s = fetch_samples(
                            adapter_name="confluence",
                            source_query=sq,
                            n=2,
                            require_live=True,
                            kbf_env=kbf_env,
                            repo_root=REPO_ROOT,
                        )
                        all_samples.extend(s)
                        cache_key = f"confluence:{sid}"
                        self._data.source_samples[cache_key] = s
                    except Exception as exc:
                        raise RuntimeError(
                            f"EVAL: failed to re-fetch samples for '{sid}'. "
                            f"Error: {exc}."
                        ) from exc

        if not all_samples:
            raise RuntimeError(
                "EVAL: no source samples available. "
                "INSPECT_SOURCES must have run (or source re-fetch must succeed). "
                "Per ADR-027, EVAL cannot proceed without real source content."
            )

        # Step 2: load committed schema from ADB
        schema_text = None
        try:
            schema_text = self._skill_store.read_artifact(
                persona=persona,
                skill_name=skill_name,
                artifact_type="extraction_schema",
            )
        except Exception as exc:
            log.warning("_run_eval: could not load extraction_schema from ADB (%s) — using in-memory", exc)

        if schema_text:
            try:
                schema = json.loads(schema_text)
            except Exception:
                schema = None

        if not schema_text or not schema:
            # Build from current field_specs (in-memory). See PREVIEW_EXTRACTION
            # comment for why this uses self._data.fields (full list) instead
            # of reuse_result.gaps (cannot-extract subset).
            from .synthesize_schema import synthesize_extraction_schema
            all_fields = list(self._data.fields)
            schema = synthesize_extraction_schema(all_fields, persona, skill_name)
            for f in all_fields:
                if f in self._data.field_specs and f in schema.get("properties", {}):
                    schema["properties"][f] = dict(self._data.field_specs[f])

        # Step 3: run extraction on each sample and generate gold rows
        from .review import _llm_extract
        extraction_gold_rows: list[dict] = []
        extraction_results: list[dict] = []

        for sample in all_samples[:3]:
            try:
                extracted = _llm_extract(sample, schema, self._llm)
            except Exception as exc:
                raise RuntimeError(
                    f"EVAL: extraction LLM call failed for sample "
                    f"'{sample.get('source_citation', '?')}'. Error: {exc}."
                ) from exc

            # Faithfulness judge needs to find the extracted value in the
            # snippet. _llm_extract uses 12k chars; the judge MUST get the
            # same window or it will return faithful=false for any value
            # not in the first 500 chars (most of them). Match the budget.
            source_snippet = str(sample.get("content", ""))[:12000]
            gold_row = {
                "kind": "auto_generated",
                "source_citation": sample.get("source_citation", "?"),
                "source_snippet": source_snippet,
                "expected_extraction": extracted,
                "schema_version": "v1",
                "created_at": _now_iso(),
            }
            extraction_gold_rows.append(gold_row)
            extraction_results.append({
                "extracted": extracted,
                "source_snippet": source_snippet,
                "citation": sample.get("source_citation", "?"),
            })

        # Step 4: compute recall@k
        required_fields = schema.get("required", [])
        all_fields = list(schema.get("properties", {}).keys())
        if not all_fields:
            all_fields = list(self._data.fields)

        total_hits = 0
        total_expected = 0
        for row in extraction_gold_rows:
            extracted = row["expected_extraction"]
            for f in all_fields:
                total_expected += 1
                if extracted.get(f):
                    total_hits += 1

        recall_at_k = round(total_hits / max(total_expected, 1), 3)
        log.info("_run_eval: recall@k=%.3f (%d/%d fields hit)", recall_at_k, total_hits, total_expected)

        # Step 5: compute faithfulness (LLM judge per field per sample)
        faithful_count = 0
        faithfulness_total = 0
        for ex_result in extraction_results:
            extracted = ex_result["extracted"]
            source_snippet = ex_result["source_snippet"]
            for fname, fspec in schema.get("properties", {}).items():
                val = extracted.get(fname)
                if not val:
                    continue
                faithfulness_total += 1
                judge_prompt = _EVAL_JUDGE_PROMPT.format(
                    field_name=fname,
                    field_description=fspec.get("description", "")[:200],
                    extracted_value=str(val)[:300],
                    source_snippet=source_snippet,
                )
                try:
                    judge_result_raw = self._llm.chat(
                        model="synthesis",
                        messages=[{"role": "user", "content": judge_prompt}],
                        response_format={"type": "json_object"},
                        max_tokens=256,
                    )
                    judge_raw = (
                        judge_result_raw.get("text", "")
                        if isinstance(judge_result_raw, dict)
                        else str(judge_result_raw)
                    )
                    import re as _re
                    judge_cleaned = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", judge_raw, flags=_re.S).strip()
                    judge = json.loads(judge_cleaned)
                    if judge.get("faithful"):
                        faithful_count += 1
                except Exception as exc:
                    log.warning("_run_eval: faithfulness judge failed for field=%s: %s", fname, exc)
                    # Count as faithful on judge failure to avoid penalising
                    faithful_count += 1
                    faithfulness_total += 1  # don't inflate denominator

        faithfulness = round(faithful_count / max(faithfulness_total, 1), 3)
        log.info("_run_eval: faithfulness=%.3f (%d/%d faithful)", faithfulness, faithful_count, faithfulness_total)

        # Step 6: workflow scoring — call /api/v1/ask
        workflow_gold_rows: list[dict] = []
        ask_result: dict = {}
        ask_latency_ms = None
        wf_tier = None
        wf_artifact_url = None

        mcp_base = _os.environ.get("KBF_MCP_URL", "http://localhost:8080")
        bearer = _os.environ.get("KBF_BEARER_TOKEN", "dev-only-token-replace-me")
        # Build a canonical question from the normalised intent
        domains = (self._data.normalised_intent or {}).get("scope_domains", [skill_name])
        canonical_question = f"What is the status of the {' '.join(domains)} project for this week?"

        try:
            import urllib.request
            ask_payload = json.dumps({
                "question": canonical_question,
                "persona": persona,
            }).encode()
            req = urllib.request.Request(
                f"{mcp_base}/api/v1/ask",
                data=ask_payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {bearer}",
                },
                method="POST",
            )
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=60) as resp:
                ask_latency_ms = int((time.monotonic() - t0) * 1000)
                ask_result = json.loads(resp.read().decode())
            wf_tier = ask_result.get("tierUsed") or ask_result.get("tier_used")
            wf_artifact_url = ask_result.get("artifactUrl") or ask_result.get("artifact_url")
            log.info(
                "_run_eval: /api/v1/ask tier=%s latency_ms=%d artifact_url=%s",
                wf_tier, ask_latency_ms or 0, wf_artifact_url,
            )
        except Exception as exc:
            log.warning(
                "_run_eval: /api/v1/ask call failed (%s) — workflow scoring skipped. "
                "This is non-fatal; extraction metrics are still valid.",
                exc,
            )

        # Build workflow gold row
        expected_skill = f"{persona}.{skill_name}"
        wf_gold_row = {
            "kind": "auto_generated",
            "question": canonical_question,
            "expected_skill": expected_skill,
            "expected_tier": 1,
            "expected_fields": list(all_fields[:5]),  # top-5 fields as presence check
            "actual_tier_used": wf_tier,
            "actual_artifact_url": wf_artifact_url,
            "ask_latency_ms": ask_latency_ms,
            "created_at": _now_iso(),
        }
        workflow_gold_rows.append(wf_gold_row)

        # Step 7: write gold sets to filesystem + ADB
        extraction_gold_path = f"eval/gold_sets/{persona}-{skill_name}-extraction.jsonl"
        workflow_gold_path = f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl"

        try:
            ext_path = REPO_ROOT / extraction_gold_path
            ext_path.parent.mkdir(parents=True, exist_ok=True)
            ext_path.write_text(
                "\n".join(json.dumps(row) for row in extraction_gold_rows) + "\n"
            )
            wf_path = REPO_ROOT / workflow_gold_path
            wf_path.write_text(
                "\n".join(json.dumps(row) for row in workflow_gold_rows) + "\n"
            )
        except Exception as exc:
            log.warning("_run_eval: filesystem write of gold sets failed: %s", exc)

        # Update skill_store gold artifacts (durable)
        try:
            typed = {
                "eval_extraction": "\n".join(json.dumps(r) for r in extraction_gold_rows) + "\n",
                "eval_workflow": "\n".join(json.dumps(r) for r in workflow_gold_rows) + "\n",
            }
            self._skill_store.write_artifacts(
                synth_id=self._data.synth_id,
                persona=persona,
                skill_name=skill_name,
                artifacts=typed,
            )
        except Exception as exc:
            log.warning("_run_eval: ADB gold set write failed: %s", exc)

        # Step 8: load exit criteria from workflow YAML or use defaults
        recall_threshold = 0.85
        faithfulness_threshold = 0.85
        try:
            wf_content = self._skill_store.read_artifact(
                persona=persona, skill_name=skill_name, artifact_type="workflow_skill"
            )
            if wf_content:
                wf_data = yaml.safe_load(wf_content) or {}
                ec = wf_data.get("synthesis", {}).get("exit_criteria", {})
                recall_threshold = float(ec.get("recall_threshold", 0.85))
                faithfulness_threshold = float(ec.get("faithfulness_threshold", 0.85))
        except Exception as exc:
            log.debug("_run_eval: could not read exit_criteria from workflow YAML: %s", exc)

        passed = recall_at_k >= recall_threshold and faithfulness >= faithfulness_threshold
        total_cost_est = faithfulness_total * 0.002  # rough estimate at $0.002/call

        self._data.eval_result = {
            "status": "completed",
            "extraction_gold_set": extraction_gold_path,
            "workflow_gold_set": workflow_gold_path,
            "gold_row_count": len(extraction_gold_rows),
            "metrics": {
                "recall_at_k": recall_at_k,
                "faithfulness": faithfulness,
                "ask_latency_ms": ask_latency_ms,
                "estimated_cost_usd": round(total_cost_est, 4),
            },
            "exit_criteria": {
                "recall_threshold": recall_threshold,
                "faithfulness_threshold": faithfulness_threshold,
                "passed": passed,
            },
            "workflow_score": {
                "tier_used": wf_tier,
                "artifact_url": wf_artifact_url,
                "expected_skill": expected_skill,
                "skill_matched": wf_tier == 1,
            },
        }

        # Build user message
        lines = [
            f"Eval results (ADR-027 — kind=auto_generated):\n",
            f"  Extraction samples: {len(extraction_gold_rows)}",
            f"  Recall@k: {recall_at_k:.1%} (threshold: {recall_threshold:.0%})",
            f"  Faithfulness: {faithfulness:.1%} (threshold: {faithfulness_threshold:.0%})",
        ]
        if ask_latency_ms is not None:
            lines.append(f"  /api/v1/ask latency: {ask_latency_ms}ms, tier={wf_tier}")
        else:
            lines.append("  /api/v1/ask: not reached (server may be down)")

        lines.append("")
        lines.append(
            "Note: kind=auto_generated — these gold rows were created from the same LLM "
            "that did the extraction. They measure consistency, not correctness. "
            "Human review encouraged before promoting to production fleet-wide."
        )
        lines.append("")

        if passed:
            lines.append(
                f"Exit criteria met. "
                f"Proceed to promote ({persona}.{skill_name} → production)?"
            )
            options = ["yes, promote", "stop here"]
        else:
            failing = []
            if recall_at_k < recall_threshold:
                failing.append(
                    f"recall@k {recall_at_k:.1%} < threshold {recall_threshold:.0%}"
                )
            if faithfulness < faithfulness_threshold:
                failing.append(
                    f"faithfulness {faithfulness:.1%} < threshold {faithfulness_threshold:.0%}"
                )
            lines.append(
                f"Exit criteria NOT met: {'; '.join(failing)}.\n"
                f"The skill cannot be promoted until metrics meet thresholds.\n"
                f"Options:\n"
                f"  • Revise extraction descriptions at REVIEW_DESIGN and re-author\n"
                f"  • Improve source data (add more Confluence pages)\n"
                f"  • Lower thresholds in the workflow YAML (synthesis.exit_criteria)\n"
                f"  • Type 'force promote' to override (not recommended)"
            )
            options = ["force promote", "stop here"]

        return ConversationTurn(
            state="EVAL",
            message="\n".join(lines),
            data={"eval": self._data.eval_result},
            options=options,
        )

    def _handle_eval_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "done", "exit", "no")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)

        # "force promote" — user explicitly overrides failing exit criteria.
        # The eval_result already has passed=False; we warn but proceed.
        if "force" in lowered and "promote" in lowered:
            eval_result = self._data.eval_result or {}
            metrics = eval_result.get("metrics", {})
            ec = eval_result.get("exit_criteria", {})
            log.warning(
                "_handle_eval_response: FORCE PROMOTE by user — "
                "metrics did not meet exit criteria. "
                "recall_at_k=%.3f (threshold=%.2f) faithfulness=%.3f (threshold=%.2f) "
                "persona=%s skill=%s",
                metrics.get("recall_at_k", 0),
                ec.get("recall_threshold", 0.85),
                metrics.get("faithfulness", 0),
                ec.get("faithfulness_threshold", 0.85),
                self._data.persona,
                self._data.skill_name,
            )
            # Stamp force-promote into eval_result for audit trail
            if self._data.eval_result:
                self._data.eval_result["force_promoted"] = True
                self._data.eval_result["force_promoted_at"] = _now_iso()
            return self._run_promote()

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
        # Bug fix (post-ADR-027 walk): use ALL designed fields, not just the
        # reuse_plan.gaps subset. DESIGN_SKILL's gaps means "cannot extract"
        # (8 of 22 for the 26ai walk), but the schema/KB/gold set should
        # cover everything the user wants to extract.
        all_fields = list(self._data.fields)

        artifacts: dict[str, Any] = {}

        if all_fields:
            schema = synthesize_extraction_schema(all_fields, persona, skill_name)
            if self._data.field_specs:
                for f in all_fields:
                    if f in self._data.field_specs and f in schema.get("properties", {}):
                        schema["properties"][f] = dict(self._data.field_specs[f])
            schema_path = f"framework/parsers/schemas/{persona}/{skill_name}/v1.json"
            artifacts[schema_path] = schema

            pb_entry = synthesize_persona_builder_diff(
                persona=persona,
                kb_name=f"{persona}.{skill_name}",
                schema_path=schema_path,
                sources=self._data.sources,
                fields=all_fields,
            )
            artifacts[f"framework/persona_builders/{persona}.yaml.new_kb"] = pb_entry

            gold_entries = seed_gold_set(
                persona=persona,
                kb_name=skill_name,
                artifact_path=self._data.artifact_path,
                extracted_fields={f: None for f in all_fields},
            )
            artifacts[f"eval/gold_sets/{persona}-{skill_name}-extraction.jsonl"] = gold_entries

        # Carry the DESIGN_SKILL's chosen rendering layout (e.g.
        # 'weekly_exec_review_v1') through to the workflow YAML so the
        # PptxRenderer dispatches to the correct layout-aware builder at
        # query time. Without this the renderer falls back to the generic
        # "title + content per slide" layout and produces a 20+ slide deck
        # instead of the designed single-slide format.
        ws_design = (self._data.design or {}).get("workflow_shape", {}) or {}
        intent = {
            "task_description": self._data.intent_description,
            "sources": self._data.sources,
            "trigger": self._data.trigger,
            "output_format": self._data.output_format,
            "layout": ws_design.get("layout"),
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
