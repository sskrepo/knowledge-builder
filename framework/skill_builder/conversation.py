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
# ADR-029 Phase 2 (S6): shared JSON-parse helper — same as S5/review.py path
from .review import _parse_llm_json_response  # noqa: E402
# ADR-030 C1: prompt registry — replaces hard-coded constants + persona_prompts.yaml wiring
from .prompt_registry import get_registry  # noqa: E402


# ADR-032 P2-Infra: factory relocated to framework/adapters/confluence/factory.py.
# Re-exported here as a private alias so existing callers within this module
# (INGEST state) and existing tests that import
#   `from framework.skill_builder.conversation import _build_confluence_adapter`
# continue to work without modification.
from ..adapters.confluence.factory import build_confluence_adapter as _build_confluence_adapter  # noqa: E402
# ADR-034: layout catalog — provides plain-language preset descriptions injected
# into design_skill prompt so LLM reasons over descriptions, never over hardcoded ids.
from ..renderers.layout_catalog import catalog_for_prompt as _layout_catalog_for_prompt  # noqa: E402
# ADR-036: Connector Registry — type-level capability gate for CONFIGURE_SOURCES.
# Aliased to avoid shadowing prompt_registry.get_registry imported above.
from ..connectors.registry import (  # noqa: E402
    get_registry as _get_connector_registry,
    HARD_STOP as _CONNECTOR_HARD_STOP,
)


# ADR-030 C1: All prompt constants moved to framework/config/prompts/skill_builder.yaml.
# Use get_registry().get_prompt(prompt_id, ...) at each call site.


# ---------------------------------------------------------------------------
# ADR-032 P1-D: source_binding contract validation helpers
# ---------------------------------------------------------------------------

def _check_confluence_adapter_available(env: str, repo_root: "Path") -> bool:
    """Return True if a Confluence adapter mode is configured for the given env.

    Config-only check — does NOT make a live HTTP call.  Reads:
      1. framework/config/adapters/confluence.yaml (base config)
      2. framework/config/{env}.yaml → adapters_overrides.confluence (env override)

    Returns True when a non-empty "mode" key is present in the merged config.
    Returns False when mode is absent, empty, or the config files are missing.

    This is used by the VALIDATE state (ADR-032 P1-D) to gate ask_parameterized
    + ingest_on_demand:true skills — a skill that requires live Confluence access
    cannot be promoted to a deployment environment with no adapter configured.
    """
    try:
        import yaml as _yaml
        base_path = repo_root / "framework" / "config" / "adapters" / "confluence.yaml"
        base_cfg: dict = {}
        if base_path.exists():
            base_cfg = _yaml.safe_load(base_path.read_text()) or {}
        env_path = repo_root / "framework" / "config" / f"{env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        overrides = env_cfg.get("adapters_overrides", {}).get("confluence", {})
        merged = {**base_cfg, **overrides}
        return bool(merged.get("mode", ""))
    except Exception as exc:
        log.warning(
            "_check_confluence_adapter_available: could not read adapter config "
            "for env=%r (%s) — treating adapter as unavailable",
            env, exc,
        )
        return False


def _validate_source_binding_contract(
    synthesized_yaml: dict,
    session_binding_mode: str,
) -> list[str]:
    """Validate the source_binding contract for ADR-032 P1-D.

    Returns a list of validation error strings.  An empty list means the
    contract is satisfied (VALIDATE should pass for source_binding concerns).

    Rules (from ADR-032 §D.4 and user task description):
    - If session_binding_mode == "ask_parameterized":
        * The YAML MUST have source_binding.mode == "ask_parameterized"
        * source_binding.input_param must be present and non-empty
        * input_param must match a declared trigger.on_request.inputs name
        * source_binding.ingest_on_demand must be present (True or False)
        * source_binding.source_type must be present and non-empty
        * source_binding.space_allow_list must be a non-empty list
        * source_binding.ephemeral_ttl_seconds must be present
        * source_binding MUST NOT pin fixed page IDs (source_bindings dict
          must not contain page_id references in the asks context — specifically
          the YAML source_bindings dict values must not be a list of page IDs
          when ask_parameterized; they should be dynamic)
    - If session_binding_mode == "author_fixed":
        * The YAML MUST NOT have source_binding.mode == "ask_parameterized"
          (author_fixed skills must not declare ask_parameterized source binding)
    - If the YAML has no source_binding block and session mode is "author_fixed":
        * This is the correct default — no error.
    """
    errors: list[str] = []
    sb = synthesized_yaml.get("source_binding") or {}
    yaml_sb_mode = sb.get("mode", "author_fixed")

    if session_binding_mode == "ask_parameterized":
        # The committed YAML must declare ask_parameterized
        if yaml_sb_mode != "ask_parameterized":
            errors.append(
                "source_binding.mode must be 'ask_parameterized' in the workflow skill YAML "
                f"(session resolved mode is ask_parameterized, but YAML has mode={yaml_sb_mode!r}). "
                "Re-author the skill or add the source_binding block to the YAML."
            )
            # Cannot validate further fields if mode is wrong
            return errors

        # input_param must be present and non-empty
        input_param = sb.get("input_param", "")
        if not input_param:
            errors.append(
                "source_binding.input_param is missing or empty. "
                "It must name the trigger input that carries the consumer-supplied page reference "
                "(e.g. input_param: page_id)."
            )
        else:
            # input_param must match a declared trigger.on_request.inputs name
            trigger_inputs = (
                synthesized_yaml.get("trigger", {})
                .get("on_request", {})
                .get("inputs", [])
            )
            declared_names = [inp.get("name") for inp in trigger_inputs if inp.get("name")]
            if declared_names and input_param not in declared_names:
                errors.append(
                    f"source_binding.input_param={input_param!r} does not match any declared "
                    f"trigger.on_request.inputs name. Declared: {declared_names}. "
                    "Add an input entry with the matching name to trigger.on_request.inputs."
                )

        # ingest_on_demand must be present
        if "ingest_on_demand" not in sb:
            errors.append(
                "source_binding.ingest_on_demand is missing. "
                "Set ingest_on_demand: true to enable ephemeral fetch "
                "or ingest_on_demand: false to hard-fail on un-ingested pages."
            )

        # source_type must be present and non-empty
        if not sb.get("source_type", ""):
            errors.append(
                "source_binding.source_type is missing or empty. "
                "Set source_type: confluence_page (or confluence_space, jira_filter, git_ref)."
            )

        # space_allow_list must be a non-empty list
        sal = sb.get("space_allow_list")
        if not sal or not isinstance(sal, list) or len(sal) == 0:
            errors.append(
                "source_binding.space_allow_list is missing or empty. "
                "Provide at least one Confluence space key to restrict ephemeral fetch "
                "(e.g. space_allow_list: [FA, PROJ])."
            )

        # ephemeral_ttl_seconds must be present
        if "ephemeral_ttl_seconds" not in sb:
            errors.append(
                "source_binding.ephemeral_ttl_seconds is missing. "
                "Set ephemeral_ttl_seconds: 300 (default) or another positive integer."
            )

    elif session_binding_mode == "author_fixed":
        # author_fixed skills must NOT have ask_parameterized source_binding
        if yaml_sb_mode == "ask_parameterized":
            errors.append(
                "This session was resolved as author_fixed but the workflow skill YAML "
                "declares source_binding.mode: ask_parameterized. "
                "Either re-author the skill with the correct mode or remove the "
                "source_binding block (absent = author_fixed per ADR-032 §H)."
            )

    return errors


# ---------------------------------------------------------------------------
# ADR-029 Phase 2 (S6): constrained routing map + guardrail constants
# ---------------------------------------------------------------------------
#
# This map is CODE, not LLM-decided. The classifier diagnoses; this dict routes.
# An unknown/garbled failure_class must NOT be used to free-route — see guardrail 1.
#
# The sentinel value "DONE_DRAFT" means: exit to DONE with draft status + explanation.
# It is NOT a state in the STATES list — it is handled inline in _classify_and_route.
_ROUTING_MAP: dict[str, str] = {
    "MISSING_FIELDS": "REVIEW_DESIGN",
    "THIN_FIELDS":    "REVIEW_DESIGN",
    "WRONG_LAYOUT":   "REVIEW_DESIGN",
    "SOURCE_COVERAGE": "CONFIGURE_SOURCES",
    "WRONG_SOURCE":   "INSPECT_SOURCES",
    "UNSUPPORTABLE":  "DONE_DRAFT",
}

# Guardrail thresholds — may be overridden per workflow YAML in future but
# hardcoded here per ADR-029 §C.3 until a workflow-YAML override is wired.
_EVAL_MAX_ITERATIONS: int = 3
_EVAL_COST_CEILING_USD: float = 2.00

# ---------------------------------------------------------------------------
# STATES list: ADR-027 + ADR-028 state machine
# ---------------------------------------------------------------------------

# The canonical ADR-027 + ADR-028 state machine.
# ADR-028 Item 3 (S3): adds CLARIFY as the 17th state.
# CLARIFY sits after CAPTURE_INTENT (and optionally after DESIGN_SKILL)
# to handle blocking ambiguities before proceeding to CONFIGURE_SOURCES
# or REVIEW_DESIGN respectively.
STATES = [
    "IDENTIFY_PERSONA",
    "CAPTURE_INTENT",
    "CLARIFY",                 # ADR-028 Item 3: conversational clarification loop
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
# ADR-029 S6 internal transient state — NOT in STATES (not user-facing or API-facing).
# Used only by the handler dispatch table to route _handle_eval_route_confirm.
_EVAL_ROUTE_PENDING = "EVAL_ROUTE_PENDING"

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
    """Return value from every state handler.

    ADR-028 Item 2 additions:
      awaiting_user:  True on every turn that requires a human response. False only
                      for deterministic auto-transitions where no human input is needed
                      (e.g. DESIGN_SKILL auto-starting after UPLOAD_ARTIFACT_EXAMPLE).
      must_show_human: True for turns the client MUST NEVER auto-answer. The authorSkill
                      tool description enforces this at the MCP level. Set True for:
                      CAPTURE_INTENT (when blocking_ambiguities is non-empty),
                      CLARIFY (all turns), REVIEW_DESIGN (always), PREVIEW_EXTRACTION
                      (always), and any EVAL turn that carries a gap report or change
                      proposal.
    """

    synth_id: str = ""
    state: str = ""
    message: str = ""
    data: dict | None = None
    options: list[str] | None = None
    artifacts_preview: dict | None = None
    progress: dict | None = None
    done: bool = False
    awaiting_user: bool = True
    must_show_human: bool = False


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
    # ADR-028 Item 3 (CLARIFY state) fields
    clarification_log: list = field(default_factory=list)
    # {"question": str, "answer": str, "resolved_at": str (ISO)}
    # Internal: pending blocking questions for CLARIFY handler.
    # NOW persisted across ADB round-trips (BUG-queue-f0591 fix).
    # Without persistence, the DESIGN_SKILL→CLARIFY→REVIEW_DESIGN path entered an
    # infinite loop because _clarify_next_state reset to "CONFIGURE_SOURCES" on every
    # session resume, rewinding the flow instead of advancing to REVIEW_DESIGN.
    _clarify_questions: list = field(default_factory=list)
    # Context: which state CLARIFY should transition to after all questions are resolved.
    # Persisted across ADB round-trips (BUG-queue-f0591 fix); backward-compat default
    # "CONFIGURE_SOURCES" is applied in from_dict() for pre-fix persisted sessions.
    _clarify_next_state: str = field(default="CONFIGURE_SOURCES")
    # ADR-029 Phase 1 (S5): reference artifact retention
    # artifact_reference_id: ArtifactStore key for the uploaded reference artifact.
    # Retained from UPLOAD_ARTIFACT_EXAMPLE through EVAL for comparator.compare().
    # None when the user skipped artifact upload or upload was image-only rejected.
    artifact_reference_id: str | None = None
    # artifact_reference_type: "pptx" | "docx" | "md" | "txt" — needed to dispatch
    # the right ArtifactComparator extractor at EVAL time.
    artifact_reference_type: str | None = None
    # ADR-035 (DECISION-015): single-source-of-truth artifact binding name.
    # Set when a reference artifact is successfully bound (alongside artifact_reference_id).
    # REVIEW_DESIGN reads this, not design.workflow_shape.layout text.
    # Cleared only by an explicit deliberate user action at UPLOAD_ARTIFACT_EXAMPLE.
    # Backward-compat default None for pre-ADR-035 sessions.
    artifact_reference_name: str | None = None
    # ADR-035 (DECISION-015): source access-verification status produced by INSPECT_SOURCES.
    # Keyed by item_id (source, reference artifact, output destination).
    # Must be complete (all required items verified) before DESIGN is permitted.
    source_access_status: dict = field(default_factory=dict)
    # ADR-035 (DECISION-015): reference artifact is conditionally required.
    # True when output is structured/templated (pptx/docx) OR intent referenced a template.
    # None = not yet determined (pre-CONFIGURE_SOURCES). False = not required.
    artifact_required: bool | None = None
    # ADR-035 (DECISION-015): declared output destination (delivery kind + config).
    # Set by CONFIGURE_SOURCES; verified accessible by INSPECT_SOURCES.
    declared_output_destination: dict | None = field(default=None)
    # ADR-029 Phase 2 (S6): loop guardrail fields.
    # eval_iteration_count: incremented every time the reject path runs the classifier.
    # eval_cumulative_cost_usd: accumulates classifier LLM call cost across iterations.
    # last_eval_failure_class: the failure_class returned on the PREVIOUS iteration;
    #   used by the consecutive-same-class pathological-loop detector.
    # All have backward-compat defaults for pre-S6 sessions.
    eval_iteration_count: int = 0
    eval_cumulative_cost_usd: float = 0.0
    last_eval_failure_class: str | None = None
    # Internal transient: target state after the user confirms the routing turn.
    # Set when state machine enters EVAL_ROUTE_PENDING.
    # Not persisted (reconstructed from the routing turn if needed).
    _eval_pending_route: str = field(default="REVIEW_DESIGN")
    # ADR-032 P1-C: source_binding_mode resolved from capture_intent LLM output
    # and optionally confirmed via CLARIFY.
    # "author_fixed"    — source pages fixed at author time (default for all pre-ADR-032
    #                     sessions; absent = author_fixed per ADR-032 §H migration rule).
    # "ask_parameterized" — consumer supplies source page at query time.
    # "ambiguous"       — transient; cleared to author_fixed|ask_parameterized by CLARIFY.
    # Persisted across ADB round-trips; backward-compat default "author_fixed" applied
    # in from_dict() so pre-ADR-032 sessions load without error.
    source_binding_mode: str = "author_fixed"
    # One-line evidence text from the intent that drove the source_binding_mode
    # classification.  Empty string when mode is "author_fixed" and no signal present.
    source_binding_signal: str = ""
    # BUG-queue-f4987: artifact stash — when the user supplies an
    # "artifact:<filename> id:<artifact_id>" reference at CLARIFY (or any other
    # pre-UPLOAD_ARTIFACT_EXAMPLE state that accepts free-text), we stash the
    # filename + artifact_id here rather than silently swallowing it as clarify
    # answer text.  The stash is auto-applied when the FSM reaches
    # UPLOAD_ARTIFACT_EXAMPLE.  None when no stash is pending.
    # Schema: {"filename": str, "artifact_id": str}
    # Persisted across ADB round-trips so resume across client restarts works.
    _pending_artifact_stash: dict | None = field(default=None)
    # ADR-038: consumer-facing skill_card produced by DESIGN_SKILL LLM call.
    # Contains summary, use_when, example_invocations, routing_queries.
    # Persisted so _synthesize_preview can carry it through to the committed
    # workflow_skill artifact (synthesize_workflow.py must NOT overwrite it).
    # None for pre-ADR-038 sessions (backward-compat).
    design_skill_card: dict | None = field(default=None)
    # ADR-038 Path B: routing self-test results stored in eval_result.
    # True when routing self-test passed (all positives route to this skill,
    # no negatives route to it). None = self-test not yet run.
    routing_self_test_passed: bool | None = field(default=None)


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
            "CLARIFY": self._handle_clarify_response,  # ADR-028 Item 3
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
            "EVAL_ROUTE_PENDING": self._handle_eval_route_confirm,  # ADR-029 S6
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
        # ADR-028 Item 3: clarification_log persisted for session resumability.
        # BUG-queue-f0591 fix: also persist _clarify_questions and _clarify_next_state
        # so that the DESIGN_SKILL→CLARIFY→REVIEW_DESIGN path survives ADB round-trips.
        d["clarification_log"] = list(self._data.clarification_log)
        d["clarify_questions"] = list(self._data._clarify_questions)
        d["clarify_next_state"] = self._data._clarify_next_state
        # ADR-029 Phase 1 (S5): reference artifact retention — default None for
        # backward-compat; pre-S5 sessions will simply have no reference artifact.
        d["artifact_reference_id"] = self._data.artifact_reference_id
        d["artifact_reference_type"] = self._data.artifact_reference_type
        # ADR-035 (DECISION-015): single-source-of-truth binding name + access-status.
        # Backward-compat defaults None / {} / None for pre-ADR-035 sessions.
        d["artifact_reference_name"] = self._data.artifact_reference_name
        d["source_access_status"] = dict(self._data.source_access_status)
        d["artifact_required"] = self._data.artifact_required
        d["declared_output_destination"] = self._data.declared_output_destination
        # ADR-029 Phase 2 (S6): loop guardrail fields — default 0/None for
        # backward-compat; pre-S6 sessions have no iteration history.
        d["eval_iteration_count"] = self._data.eval_iteration_count
        d["eval_cumulative_cost_usd"] = self._data.eval_cumulative_cost_usd
        d["last_eval_failure_class"] = self._data.last_eval_failure_class
        # ADR-032 P1-C: source_binding_mode resolved from capture_intent / CLARIFY.
        # Backward-compat default "author_fixed" applied in from_dict() for pre-ADR-032
        # sessions that lack this key (ADR-032 §H migration rule: absent = author_fixed).
        d["source_binding_mode"] = self._data.source_binding_mode
        d["source_binding_signal"] = self._data.source_binding_signal
        # BUG-queue-f4987: artifact stash — None when nothing pending; dict when
        # user supplied "artifact:<filename> id:<artifact_id>" at a pre-upload state.
        d["pending_artifact_stash"] = self._data._pending_artifact_stash
        # ADR-038: consumer-facing skill card produced at DESIGN_SKILL.
        # Backward-compat default None for pre-ADR-038 sessions.
        d["design_skill_card"] = self._data.design_skill_card
        d["routing_self_test_passed"] = self._data.routing_self_test_passed
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
            # ADR-028 Item 3: clarification_log for audit trail.
            # BUG-queue-f0591 fix: restore _clarify_questions and _clarify_next_state
            # so sessions resumed from ADB continue the CLARIFY flow correctly.
            # Backward-compat defaults apply for pre-fix persisted sessions.
            clarification_log=list(d.get("clarification_log", [])),
            _clarify_questions=list(d.get("clarify_questions", [])),
            _clarify_next_state=d.get("clarify_next_state", "CONFIGURE_SOURCES"),
            artifact_layout=d.get("artifact_layout"),
            design=d.get("design"),
            # ADR-029 Phase 1 (S5): reference artifact retention.
            # Backward-compat default None — pre-S5 sessions have no reference.
            artifact_reference_id=d.get("artifact_reference_id"),
            artifact_reference_type=d.get("artifact_reference_type"),
            # ADR-035 (DECISION-015): single-source-of-truth binding name + access-status.
            # Backward-compat defaults None / {} / None for pre-ADR-035 sessions.
            artifact_reference_name=d.get("artifact_reference_name"),
            source_access_status=dict(d.get("source_access_status") or {}),
            artifact_required=d.get("artifact_required"),
            declared_output_destination=d.get("declared_output_destination"),
            # ADR-029 Phase 2 (S6): loop guardrail fields.
            # Backward-compat defaults: 0 / 0.0 / None for pre-S6 sessions.
            eval_iteration_count=int(d.get("eval_iteration_count", 0)),
            eval_cumulative_cost_usd=float(d.get("eval_cumulative_cost_usd", 0.0)),
            last_eval_failure_class=d.get("last_eval_failure_class"),
            # ADR-032 P1-C: source_binding_mode.
            # Backward-compat default "author_fixed" for pre-ADR-032 sessions
            # (absent key = author_fixed per ADR-032 §H migration rule).
            source_binding_mode=d.get("source_binding_mode", "author_fixed"),
            source_binding_signal=d.get("source_binding_signal", ""),
            # BUG-queue-f4987: artifact stash.
            # Backward-compat default None — pre-fix sessions have no stash.
            _pending_artifact_stash=d.get("pending_artifact_stash"),
            # ADR-038: consumer-facing skill card produced at DESIGN_SKILL.
            # Backward-compat default None for pre-ADR-038 sessions.
            design_skill_card=d.get("design_skill_card"),
            routing_self_test_passed=d.get("routing_self_test_passed"),
        )
        return obj

    def _turn(self, turn: ConversationTurn) -> ConversationTurn:
        """Stamp synth_id and progress on every outgoing turn."""
        turn.synth_id = self._data.synth_id
        turn.progress = _progress(self._state)
        return turn

    # ------------------------------------------------------------------
    # ADR-035 (DECISION-015): Single-source-of-truth helpers
    # ------------------------------------------------------------------

    def has_bound_reference_artifact(self) -> bool:
        """Single source of truth: is a reference artifact successfully bound?

        ADR-035 (DECISION-015): REVIEW_DESIGN and _run_eval MUST both call this
        method rather than reading separate fields independently.  The invariant
        is: has_bound_reference_artifact() is True IFF both artifact_reference_id
        and artifact_reference_name are non-None and non-empty.  They are set
        atomically in _bind_reference_artifact() and cleared atomically in
        _clear_reference_artifact().  Reading design.workflow_shape.layout text
        is explicitly prohibited as an artifact-bound signal.
        """
        return bool(
            self._data.artifact_reference_id
            and self._data.artifact_reference_name
        )

    def _bind_reference_artifact(
        self,
        artifact_id: str,
        artifact_type: str,
        artifact_name: str,
        artifact_layout: dict | None,
        artifact_path: str = "",
    ) -> None:
        """Atomically bind a reference artifact — single write path.

        ADR-035 (DECISION-015): all three retention fields are set together so
        has_bound_reference_artifact() is always consistent.
        """
        self._data.artifact_reference_id = artifact_id
        self._data.artifact_reference_type = artifact_type
        self._data.artifact_reference_name = artifact_name
        if artifact_layout is not None:
            self._data.artifact_layout = artifact_layout
        if artifact_path:
            self._data.artifact_path = artifact_path
        log.info(
            "_bind_reference_artifact: id=%r type=%s name=%r — binding complete",
            artifact_id, artifact_type, artifact_name,
        )

    def _clear_reference_artifact(self, reason: str = "") -> None:
        """Atomically clear reference artifact binding.

        ADR-035 (DECISION-015): clearing requires explicit deliberate action;
        must never be called silently on re-entry.  Callers must pass a reason
        for audit-trail logging.
        """
        log.info(
            "_clear_reference_artifact: clearing binding "
            "(id=%r name=%r) — reason: %s",
            self._data.artifact_reference_id,
            self._data.artifact_reference_name,
            reason or "(no reason given)",
        )
        self._data.artifact_reference_id = None
        self._data.artifact_reference_type = None
        self._data.artifact_reference_name = None
        self._data.artifact_layout = None

    @staticmethod
    def _is_artifact_required(normalised_intent: dict, output_format: str = "") -> bool:
        """Conditional-required rule (ADR-035 DECISION-015 §3).

        Returns True (artifact required, no skip) when:
          - output_kind or output_format is "pptx" or "docx" (structured/templated output), OR
          - the intent text explicitly references a template, reference, or example artifact.

        Returns False (artifact not required) for pure text/email/markdown skills
        where no reference was ever declared.
        """
        import re as _re
        structured_kinds = {"pptx", "docx", "powerpoint", "word"}
        output_kind = (normalised_intent.get("output_kind") or "").lower()
        fmt = (output_format or "").lower()
        if output_kind in structured_kinds or fmt in structured_kinds:
            return True
        # Check intent text for template/reference keywords
        task_desc = (normalised_intent.get("task_description") or "").lower()
        if _re.search(r"\b(template|reference|example|like the|same format|mirror)\b", task_desc):
            return True
        return False

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

        # ADR-030 C1: capture_intent prompt via registry.
        # persona= triggers overlay resolution for persona_key_fields from persona_overlays.yaml.
        # For unknown personas not in persona_overlays.yaml, the overlay falls through (WARNING)
        # and we supply empty-string defaults to preserve the old graceful-degradation behavior.
        from .prompt_registry import MissingVarsError as _MissingVarsError
        try:
            spec = get_registry().get_prompt(
                "capture_intent",
                persona=persona,
                intent=intent,
            )
        except _MissingVarsError:
            # Unknown persona: overlay supplied no persona_key_fields — use empty defaults
            log.warning(
                "_advance_to_capture_intent: unknown persona %r has no overlay — "
                "degrading to empty persona_key_fields",
                persona,
            )
            spec = get_registry().get_prompt(
                "capture_intent",
                persona=persona,
                intent=intent,
                persona_key_fields="(none specified)",
            )
        try:
            result = self._llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
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

        # ADR-032 P1-C: persist source_binding_mode + source_binding_signal from the
        # capture_intent LLM output (v1.1 prompt emits both fields).
        # Backward-compat default "author_fixed" when the key is absent (pre-v1.1 LLM
        # output or sessions restored from pre-ADR-032 ADB state).
        sb_mode = normalised.get("source_binding_mode", "author_fixed")
        sb_signal = normalised.get("source_binding_signal", "")
        self._data.source_binding_mode = sb_mode
        self._data.source_binding_signal = sb_signal
        log.info(
            "_advance_to_capture_intent: source_binding_mode=%s signal=%r",
            sb_mode, sb_signal[:60] if sb_signal else "",
        )

        ambiguities = normalised.get("ambiguities", [])
        # ADR-028 S3: distinguish blocking from nice-to-know
        blocking_ambiguities = normalised.get("blocking_ambiguities", [])
        nice_to_know_ambiguities = normalised.get("nice_to_know_ambiguities", [])

        # If LLM returned the old 'ambiguities' key (pre-S3 prompts or old LLM output),
        # treat all of them as blocking (safer default — prevent silent steamrolling).
        if ambiguities and not blocking_ambiguities and not nice_to_know_ambiguities:
            blocking_ambiguities = ambiguities

        # ADR-028 S3: route to CLARIFY when blocking ambiguities exist.
        # ADR-032 P1-C: when source_binding_mode is ask_parameterized or ambiguous,
        # the v1.1 prompt already adds the source-binding question to blocking_ambiguities
        # ("Is the source page fixed at authoring time or supplied by the consumer at query
        # time?").  We annotate that specific question with context="source_binding_mode"
        # so _handle_clarify_response can resolve it to a definitive mode.
        # We do NOT add the question again — the prompt already emitted it.
        if blocking_ambiguities:
            # Build question dicts; annotate the source-binding question if present.
            _SB_QUESTION_FRAGMENT = "source page fixed at authoring time or supplied"
            clarify_questions = []
            for q in blocking_ambiguities:
                q_dict: dict = {"question": q, "resolved": False}
                # ADR-032 P1-C: annotate source-binding question so the clarify handler
                # can resolve it to a definitive author_fixed | ask_parameterized mode.
                if _SB_QUESTION_FRAGMENT in q:
                    q_dict["context"] = "source_binding_mode"
                    q_dict["options"] = {"A": "author_fixed", "B": "ask_parameterized"}
                clarify_questions.append(q_dict)
            self._data._clarify_questions = clarify_questions
            return self._advance_to_clarify(self._data._clarify_questions)

        # ADR-028 S3: nice_to_know_ambiguities are advisory — proceed with assumptions.
        # Do NOT block at CAPTURE_INTENT or CLARIFY; log the assumptions and auto-advance.
        # NOTE: only auto-advance when there ARE nice-to-know notes; zero ambiguities
        #       still returns a CAPTURE_INTENT confirmation turn (legacy S2 contract).
        all_ambiguities = list(ambiguities) + list(nice_to_know_ambiguities)
        if nice_to_know_ambiguities:
            log.info(
                "_advance_to_capture_intent: %d nice-to-know advisory notes — "
                "auto-advancing with assumptions (no blocking questions)",
                len(nice_to_know_ambiguities),
            )
            # Store advisory notes in normalised_intent for downstream reference
            normalised["nice_to_know_assumptions"] = all_ambiguities
            return self._advance_to_configure_sources_v2()

        # Zero ambiguities — show CAPTURE_INTENT confirmation turn; user types 'ok' to advance.
        return ConversationTurn(
            state="CAPTURE_INTENT",
            message=(
                f"Intent parsed for persona '{persona}':\n\n"
                f"  Output kind: {normalised.get('output_kind', '?')}\n"
                f"  Audience: {normalised.get('audience', '?')}\n"
                f"  Cadence: {normalised.get('cadence', '?')}\n"
                f"  Scope: {', '.join(normalised.get('scope_domains', ['?']))}\n"
                f"  Success criteria: {'; '.join(normalised.get('success_criteria', ['?']))}\n"
                f"  Skill name: {self._data.skill_name}\n\n"
                "No ambiguities. Type 'ok' to proceed."
            ),
            data={"normalised_intent": normalised, "skill_name": self._data.skill_name},
            options=["ok"],
            must_show_human=False,
            awaiting_user=True,
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

    # -- CLARIFY state (ADR-028 Item 3) ----------------------------------

    @staticmethod
    def _sanitize_clarify_question(question: str) -> str:
        """ADR-034: Strip any internal layout preset identifiers from clarify question text.

        The LLM may inadvertently include an internal_id (e.g. 'weekly_exec_review_v1')
        in a generated clarify question.  This guard replaces every known internal_id
        with the corresponding human_label so the user never sees a machine identifier.

        This is a defense-in-depth guard — the prompt already instructs the LLM not to
        emit internal ids.  Here we enforce it at the surface.
        """
        from ..renderers.layout_catalog import all_presets as _all_presets
        sanitized = question
        for preset in _all_presets():
            if preset.internal_id in sanitized:
                log.warning(
                    "_sanitize_clarify_question: internal preset id %r found in clarify "
                    "question — replacing with human label %r (ADR-034 guard)",
                    preset.internal_id,
                    preset.human_label,
                )
                sanitized = sanitized.replace(preset.internal_id, preset.human_label)
        return sanitized

    def _advance_to_clarify(self, blocking_questions: list, next_state: str = "CONFIGURE_SOURCES") -> ConversationTurn:
        """Transition to CLARIFY state with a list of blocking questions.

        ADR-028 Item 3: The CLARIFY state asks one blocking question per turn.
        It must NOT advance while any blocking questions remain unresolved.
        Sets must_show_human=True on every turn — the human must see and answer.

        Args:
            blocking_questions: List of dicts {"question": str, "resolved": bool}
            next_state: The state to transition to when all questions are resolved.
                        Defaults to CONFIGURE_SOURCES (after CAPTURE_INTENT).
                        Can be "REVIEW_DESIGN" (after DESIGN_SKILL blocking_questions).
        """
        self._state = "CLARIFY"
        # ADR-034: sanitize all question texts before storing — ensure no internal ids
        sanitized_questions = [
            {**q, "question": self._sanitize_clarify_question(q.get("question", ""))}
            for q in blocking_questions
        ]
        self._data._clarify_questions = sanitized_questions
        self._data._clarify_next_state = next_state

        # Find the first unresolved question
        pending = [q for q in sanitized_questions if not q.get("resolved")]
        if not pending:
            # All resolved — advance to next state
            return self._clarify_advance()

        question = pending[0]["question"]
        total = len(sanitized_questions)
        resolved_count = total - len(pending)

        progress_note = f"({resolved_count + 1}/{total})" if total > 1 else ""

        # ADR-030 C1: clarify prompt via registry (model=none; only .text is used)
        message = get_registry().get_prompt("clarify", question=question).text
        if progress_note:
            message = f"Clarification {progress_note}:\n\n{question}\n\nPlease answer so I can proceed."

        log.info(
            "_advance_to_clarify: pending=%d total=%d next_state=%s",
            len(pending), total, next_state,
        )

        return ConversationTurn(
            state="CLARIFY",
            message=message,
            data={
                "question": question,
                "pending_count": len(pending),
                "total_count": total,
            },
            options=["<your answer>", "skip"],
            must_show_human=True,   # ADR-028 Item 2: ALWAYS show CLARIFY turns to human
            awaiting_user=True,
        )

    # Non-substantive single-word replies that do NOT answer a clarification question.
    # Sending any of these alone must keep the session at CLARIFY and re-display the question.
    _NON_ANSWERS: frozenset[str] = frozenset({
        "ok", "yes", "no", "continue", "proceed", "next", "sure", "okay", "got it",
        "noted", "done", "fine", "alright", "right",
    })

    def _handle_clarify_response(self, user_input: str) -> ConversationTurn:
        """Handle user response at CLARIFY state (ADR-028 Item 3).

        Records the answer, marks the question resolved, and either:
        - Asks the next unresolved question (if any remain), or
        - Advances to the next state (when all are resolved).

        Refuses to advance if the user sends a non-substantive response (e.g. 'ok', 'yes').
        The user must provide a real answer or type 'skip' to proceed with an assumption.
        """
        lowered = user_input.strip().lower()

        questions = getattr(self._data, "_clarify_questions", [])
        next_state = getattr(self._data, "_clarify_next_state", "CONFIGURE_SOURCES")

        # Reject non-substantive responses — 'ok'/'yes'/'no' don't answer the question
        if lowered in self._NON_ANSWERS:
            pending = [q for q in questions if not q.get("resolved")]
            question_text = pending[0]["question"] if pending else "the question above"
            log.info(
                "_handle_clarify_response: non-substantive reply %r — re-asking question",
                user_input.strip()[:40],
            )
            return ConversationTurn(
                state="CLARIFY",
                message=(
                    f"I need a specific answer to proceed.\n\n"
                    f"{question_text}\n\n"
                    f"Please type your answer, or type 'skip' to proceed with my best assumption.\n"
                    f"(Typing '{user_input.strip()}' alone doesn't answer the question.)"
                ),
                data={
                    "question": question_text,
                    "pending_count": len(pending),
                    "total_count": len(questions),
                },
                options=["<your answer>", "skip"],
                must_show_human=True,
                awaiting_user=True,
            )

        # BUG-queue-f4987 fix: detect early artifact: reference supplied at CLARIFY.
        # "artifact:<filename> id:<artifact_id>" is the uploadArtifact syntax (ADR-021).
        # The binding handler (_handle_upload_artifact_example) only runs at
        # UPLOAD_ARTIFACT_EXAMPLE, so supplying it here would normally be silently
        # swallowed as free-text clarify answer — a silent loss of user intent.
        # Instead: stash the ref in session state, re-ask the pending clarify question,
        # and confirm to the user that the artifact will be auto-applied at the upload step.
        _stripped = user_input.strip()
        if _stripped.startswith("artifact:"):
            # Filename may contain spaces; artifact_id is always the last whitespace-free token
            # after the literal " id:" marker.  Use a non-greedy filename capture up to " id:".
            _m = re.match(r"^artifact:(.+?)\s+id:(\S+)$", _stripped)
            if _m:
                _filename, _artifact_id = _m.group(1), _m.group(2)
                self._data._pending_artifact_stash = {
                    "filename": _filename,
                    "artifact_id": _artifact_id,
                }
                pending = [q for q in questions if not q.get("resolved")]
                question_text = pending[0]["question"] if pending else "the question above"
                log.info(
                    "_handle_clarify_response: stashed early artifact ref "
                    "filename=%r artifact_id=%r — re-asking clarify question",
                    _filename, _artifact_id,
                )
                return ConversationTurn(
                    state="CLARIFY",
                    message=(
                        f"Got it — I've noted your reference artifact "
                        f"'{_filename}' (id: {_artifact_id}). "
                        f"It will be automatically applied when we reach the "
                        f"artifact upload step.\n\n"
                        f"I still need your answer to the current question first:\n\n"
                        f"{question_text}\n\n"
                        f"Please answer the question above (or type 'skip' to proceed "
                        f"with my best assumption)."
                    ),
                    data={
                        "question": question_text,
                        "pending_count": len(pending),
                        "total_count": len(questions),
                        "artifact_stashed": {"filename": _filename, "artifact_id": _artifact_id},
                    },
                    options=["<your answer>", "skip"],
                    must_show_human=True,
                    awaiting_user=True,
                )
            # artifact: prefix but not the expected format — let it fall through
            # as a normal answer (it won't bind anything, but it's also not the
            # upload syntax; treat it as regular clarify text).

        # Find the first unresolved question and mark it answered
        resolved_any = False
        for q in questions:
            if not q.get("resolved"):
                answer = user_input.strip()

                # "skip" is accepted but flagged — we log it and proceed
                if lowered == "skip":
                    answer = "[SKIPPED — proceeding with best assumption]"
                    log.warning(
                        "_handle_clarify_response: user skipped question=%r — schema quality may suffer",
                        q["question"][:80],
                    )

                q["resolved"] = True
                q["answer"] = answer

                # ADR-032 P1-C: if this is the source-binding clarification question,
                # resolve source_binding_mode to a definitive author_fixed | ask_parameterized.
                # Resolution rule (impl-plan P1-C): answer maps as follows —
                #   "A", "fixed", "author_fixed", "same page every time",
                #   "yes" (in context "always same") → author_fixed
                #   "B", "parameterized", "ask_parameterized", "different page",
                #   "dynamic", "at query time" → ask_parameterized
                #   "skip" → default to author_fixed (safer; user acknowledged quality risk)
                if q.get("context") == "source_binding_mode" and not lowered == "skip":
                    _answer_lower = answer.strip().lower()
                    if (
                        _answer_lower in ("a", "author_fixed", "fixed", "author fixed")
                        or "same page" in _answer_lower
                        or "fixed" in _answer_lower
                        or "always" in _answer_lower
                        or "specific" in _answer_lower
                    ):
                        self._data.source_binding_mode = "author_fixed"
                    elif (
                        _answer_lower in ("b", "ask_parameterized", "parameterized",
                                          "ask parameterized", "dynamic")
                        or "different page" in _answer_lower
                        or "query time" in _answer_lower
                        or "at query" in _answer_lower
                        or "consumer" in _answer_lower
                        or "user passes" in _answer_lower
                        or "supplied" in _answer_lower
                        or "on request" in _answer_lower
                    ):
                        self._data.source_binding_mode = "ask_parameterized"
                    else:
                        # Ambiguous answer — default to ask_parameterized (the intent
                        # already signalled parameterization; the user's ambiguous reply
                        # doesn't override that signal).
                        self._data.source_binding_mode = "ask_parameterized"
                    log.info(
                        "_handle_clarify_response: source_binding_mode resolved to %r "
                        "from answer %r",
                        self._data.source_binding_mode, answer[:80],
                    )
                elif q.get("context") == "source_binding_mode" and lowered == "skip":
                    # User skipped — default to author_fixed (safer; no page-fetching
                    # at query time without explicit confirmation).
                    self._data.source_binding_mode = "author_fixed"
                    log.warning(
                        "_handle_clarify_response: source_binding_mode defaulted to "
                        "author_fixed (user skipped clarification)"
                    )

                # Record in clarification_log for audit trail
                self._data.clarification_log.append({
                    "question": q["question"],
                    "answer": answer,
                    "resolved_at": _now_iso(),
                })
                log.info(
                    "_handle_clarify_response: resolved question=%r answer=%r",
                    q["question"][:80], answer[:80],
                )
                resolved_any = True
                break

        if not resolved_any:
            # No unresolved questions — this is unexpected.
            # BUG-queue-f0591 hardening: if we are on the DESIGN_SKILL clarify path
            # (design is set) and _clarify_questions is empty, this signals a
            # persistence regression — the questions were lost between sessions.
            # Surface a must_show_human error rather than silently misrouting to
            # CONFIGURE_SOURCES (which would rewind the flow to the start).
            if self._data.design is not None and not questions:
                log.error(
                    "_handle_clarify_response: CLARIFY state has no pending questions "
                    "but design is set — likely a persistence regression. "
                    "Surfacing error rather than silently rewinding to CONFIGURE_SOURCES."
                )
                return ConversationTurn(
                    state="CLARIFY",
                    message=(
                        "I've lost track of the clarifying questions for this design. "
                        "This is an internal inconsistency — please start the skill "
                        "design step again by typing 'redesign' or 'continue'."
                    ),
                    data={"error": "clarify_questions_lost", "design_present": True},
                    must_show_human=True,
                    awaiting_user=True,
                )
            log.warning("_handle_clarify_response: no unresolved questions found — advancing")
            return self._clarify_advance()

        # Check if any questions remain unresolved
        pending = [q for q in questions if not q.get("resolved")]
        if pending:
            # More questions to ask — stay in CLARIFY
            return self._advance_to_clarify(questions, next_state)

        # All resolved — advance
        return self._clarify_advance()

    def _clarify_advance(self) -> ConversationTurn:
        """Advance from CLARIFY to the configured next state.

        Called when all blocking questions are resolved.
        """
        next_state = getattr(self._data, "_clarify_next_state", "CONFIGURE_SOURCES")
        log.info(
            "_clarify_advance: all questions resolved, advancing to %s. "
            "clarification_log=%d entries",
            next_state, len(self._data.clarification_log),
        )

        if next_state == "CONFIGURE_SOURCES":
            return self._advance_to_configure_sources_v2()
        elif next_state == "REVIEW_DESIGN":
            return self._prompt_review_design()
        else:
            # Unexpected next_state — fall back to CONFIGURE_SOURCES
            log.warning(
                "_clarify_advance: unexpected next_state=%r — falling back to CONFIGURE_SOURCES",
                next_state,
            )
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

        # ADR-030 C1: configure_sources prompt via registry
        spec = get_registry().get_prompt(
            "configure_sources",
            persona=self._data.persona,
            normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
            adapter_list=json.dumps(adapter_list, indent=2),
            intent_text=self._data.intent_description,
        )
        try:
            result = self._llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
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

                # Build sample content for the LLM prompt.
                # C6/ADR-031: raise per-sample cap 3000→20000, total cap 6000→40000.
                # gpt-4o input is ~128k tokens; the old 3k/6k caps discarded source
                # content that the capability analysis needs (e.g. WBS table rows).
                sample_parts = []
                total_chars = 0
                for s in samples:
                    content = s.get("content", "")[:20000]
                    citation = s.get("source_citation", source_id)
                    sample_parts.append(f"--- {citation} ---\n{content}")
                    total_chars += len(content)
                    if total_chars >= 40000:
                        break
                sample_content = "\n\n".join(sample_parts)

                # ADR-030 C1: inspect_sources prompt via registry
                spec = get_registry().get_prompt(
                    "inspect_sources",
                    source_id=cache_key,
                    persona=self._data.persona,
                    normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
                    sample_content=sample_content[:40000],
                )
                try:
                    result = self._llm.chat(
                        model=spec.model,
                        messages=[{"role": "user", "content": spec.text}],
                        response_format=spec.response_format,
                        max_tokens=spec.max_tokens,
                    )
                    raw = result.get("text", "") if isinstance(result, dict) else str(result)
                    # C3/ADR-031: parse via _parse_llm_json_response for truncation detection.
                    tokens_out = result.get("tokens_out") if isinstance(result, dict) else None
                    cap = _parse_llm_json_response(
                        raw,
                        tokens_out=tokens_out,
                        max_tokens=spec.max_tokens,
                    )
                    cap["source_id"] = cache_key
                    capability_list.append(cap)
                    log.info(
                        "_run_inspect_sources: source=%s available=%d suggested=%d missing=%d",
                        cache_key,
                        len(cap.get("available_fields", [])),
                        len(cap.get("suggested_fields", [])),
                        len(cap.get("missing_fields", [])),
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        f"INSPECT_SOURCES: LLM response could not be parsed for source "
                        f"'{cache_key}'. Possible truncation (BUG-queue-44364). "
                        f"Error: {exc}."
                    ) from exc
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
        """Transition to UPLOAD_ARTIFACT_EXAMPLE state (ADR-027 + ADR-035).

        ADR-035 (DECISION-015): computes artifact_required and suppresses the
        'skip' affordance when the artifact is mandatory.  If a binding is already
        established, surfaces the current binding name prominently so the user can
        confirm or replace it.
        """
        self._state = "UPLOAD_ARTIFACT_EXAMPLE"
        artifact_required = self._is_artifact_required(
            self._data.normalised_intent or {},
            self._data.output_format,
        )
        self._data.artifact_required = artifact_required

        stash = self._data._pending_artifact_stash
        already_bound = self.has_bound_reference_artifact()

        if already_bound:
            # ADR-035 RE-ENTRY: surface existing binding; skip = keep it, new ref = replace.
            bound_name = self._data.artifact_reference_name
            bound_id = self._data.artifact_reference_id
            msg = (
                f"Reference artifact already bound: '{bound_name}' (id: {bound_id}).\n\n"
                + ("Type 'skip' to keep this binding, or provide a new artifact to replace it.\n\n"
                   if not artifact_required else
                   "Provide a new artifact reference to replace it "
                   "(a reference is REQUIRED for this skill — cannot be cleared).\n\n")
                + "This reference informs DESIGN_SKILL's output format and layout."
            )
            base_options = [f"artifact:{bound_name} id:{bound_id}"]
            if not artifact_required:
                base_options = ["skip"] + base_options
            options = base_options + ["/path/to/other.pptx"]
        elif stash:
            # BUG-queue-f4987: surface stash.
            msg = (
                f"Reference artifact noted: '{stash['filename']}' (id: {stash['artifact_id']}).\n\n"
                + ("Type 'skip' to apply this reference artifact automatically, "
                   if not artifact_required else
                   "A reference artifact is REQUIRED for this skill. "
                   "Type 'skip' to apply this stashed reference, ")
                + "or provide a different artifact reference to override it.\n\n"
                "This reference helps DESIGN_SKILL choose the right output format and layout."
            )
            options = ["skip", f"artifact:{stash['filename']} id:{stash['artifact_id']}", "/path/to/other.pptx"]
        elif artifact_required:
            # ADR-035 CONDITIONAL-REQUIRED: no 'skip' option offered.
            msg = (
                "A reference artifact is REQUIRED for this skill "
                f"(output format: {self._data.output_format or 'structured'}).\n\n"
                "Please upload the reference template to help DESIGN_SKILL match "
                "the expected output structure.\n\n"
                "Provide: artifact:<filename> id:<artifact_id>  or a local file path."
            )
            options = ["artifact:<filename> id:<artifact_id>", "/path/to/template.pptx"]
        else:
            msg = (
                "Optional: upload a reference artifact to provide a layout hint.\n\n"
                "This helps DESIGN_SKILL choose the right output format and layout.\n"
                "Provide the path to a PPTX, DOCX, Markdown, or text file.\n\n"
                "If you don't have a reference file, type 'skip'."
            )
            options = ["skip", "artifact:<filename> id:<artifact_id>", "/path/to/file.pptx"]
        return ConversationTurn(
            state="UPLOAD_ARTIFACT_EXAMPLE",
            message=msg,
            options=options,
        )

    def _handle_upload_artifact_example(self, user_input: str) -> ConversationTurn:
        """Handle user input at UPLOAD_ARTIFACT_EXAMPLE state (ADR-027 + ADR-029 S5).

        ADR-035 (DECISION-015) changes:
          - RE-ENTRY GUARD: if a binding is already established (has_bound_reference_artifact()),
            re-entering this state does NOT silently clear it.  A bare 'skip' at re-entry
            preserves the existing binding.  Clearing/replacing requires an explicit new
            artifact reference.
          - CONDITIONAL-REQUIRED GATE: when the skill is REQUIRED to have a reference
            artifact (_is_artifact_required()), the 'skip' option is suppressed — no
            progression to DESIGN_SKILL until an artifact is bound.  For text/email/markdown
            skills with no declared reference, the gate is not imposed.
          - SINGLE-SOURCE-OF-TRUTH: all artifact field writes go through
            _bind_reference_artifact() / _clear_reference_artifact() atomically.
            has_bound_reference_artifact() is the authoritative "is bound" check for
            both REVIEW_DESIGN display and _run_eval.

        Retains from ADR-029 Phase 1 (S5):
          1. IMAGE HARD-REJECT: image-only artifacts are hard-rejected before parse.
          2. ARTIFACT RETENTION: artifact_reference_id / artifact_reference_name persisted.

        Retains from BUG-queue-f4987: stash auto-apply on skip.
        """
        from .analyze_artifact import analyze_artifact
        from .comparator import ArtifactComparator, IMAGE_ONLY_MESSAGE, SUPPORTED_TYPES

        artifact_required = self._is_artifact_required(
            self._data.normalised_intent or {},
            self._data.output_format,
        )
        # Cache the decision so INSPECT_SOURCES and the REVIEW_DESIGN display can read it.
        self._data.artifact_required = artifact_required

        # BUG-queue-f4987: auto-apply stashed artifact if user skips and stash exists.
        stash = self._data._pending_artifact_stash
        lowered = user_input.lower().strip()
        if (
            stash
            and lowered in ("skip", "no", "none", "later")
        ):
            log.info(
                "_handle_upload_artifact_example: auto-applying stashed artifact "
                "filename=%r artifact_id=%r (user skipped at upload step)",
                stash.get("filename"), stash.get("artifact_id"),
            )
            # Rewrite user_input to the stashed artifact: reference so the rest of
            # the handler processes it normally.
            user_input = f"artifact:{stash['filename']} id:{stash['artifact_id']}"
            lowered = user_input.lower()
            # Clear the stash — it has been consumed.
            self._data._pending_artifact_stash = None
        elif stash and user_input.strip().startswith("artifact:"):
            # User supplied a new artifact: reference — clear the stash (new one wins).
            log.info(
                "_handle_upload_artifact_example: new artifact: ref supplied — "
                "discarding stash (stash=%r)",
                stash,
            )
            self._data._pending_artifact_stash = None

        if lowered in ("skip", "no", "none", "later"):
            # ADR-035 RE-ENTRY GUARD: if already bound, preserve binding on bare skip.
            if self.has_bound_reference_artifact():
                log.info(
                    "_handle_upload_artifact_example: re-entry skip received but "
                    "artifact already bound (id=%r name=%r) — preserving binding "
                    "(ADR-035 re-entry guard)",
                    self._data.artifact_reference_id,
                    self._data.artifact_reference_name,
                )
                return self._run_design_skill()
            # ADR-035 CONDITIONAL-REQUIRED GATE: suppress skip when required.
            if artifact_required:
                log.warning(
                    "_handle_upload_artifact_example: skip attempted but artifact "
                    "is REQUIRED for this skill (output=%r) — blocking skip",
                    self._data.output_format,
                )
                return ConversationTurn(
                    state="UPLOAD_ARTIFACT_EXAMPLE",
                    message=(
                        "A reference artifact is REQUIRED for this skill "
                        f"(output format: {self._data.output_format or 'structured'}).\n\n"
                        "Please provide the path to a PPTX or DOCX reference template.\n"
                        "This is needed so DESIGN_SKILL can match the expected output structure.\n\n"
                        "Provide: artifact:<filename> id:<artifact_id>  "
                        "or a local file path."
                    ),
                    options=["artifact:<filename> id:<artifact_id>", "/path/to/template.pptx"],
                    must_show_human=True,
                    awaiting_user=True,
                )
            # Not required, no existing binding — explicit skip, clear any stale state.
            self._clear_reference_artifact(reason="explicit skip at UPLOAD_ARTIFACT_EXAMPLE")
            self._data.artifact_path = ""
            return self._run_design_skill()

        path = user_input.strip()
        resolved_path: Path | None = None
        artifact_id_for_retention: str | None = None
        artifact_filename: str = ""

        # Handle artifact: prefix (ADR-021 uploaded artifacts)
        if path.startswith("artifact:"):
            import re as _re
            m = _re.match(r"^artifact:(.+?)\s+id:(\S+)$", path)
            if m and self._artifact_store is not None:
                artifact_filename = m.group(1).strip()
                artifact_id_for_retention = m.group(2)
                local_path = self._artifact_store.resolve(artifact_id_for_retention)
                if local_path:
                    resolved_path = Path(local_path)
                    self._data.artifact_path = str(resolved_path)
                else:
                    log.warning(
                        "UPLOAD_ARTIFACT_EXAMPLE: artifact_id=%s not found in store — "
                        "proceeding without reference artifact",
                        artifact_id_for_retention,
                    )
                    if artifact_required:
                        return ConversationTurn(
                            state="UPLOAD_ARTIFACT_EXAMPLE",
                            message=(
                                f"Artifact id '{artifact_id_for_retention}' was not found "
                                "in the artifact store.\n\n"
                                "A reference artifact is REQUIRED for this skill. "
                                "Please re-upload the file."
                            ),
                            options=["artifact:<filename> id:<artifact_id>"],
                            must_show_human=True,
                            awaiting_user=True,
                        )
                    self._clear_reference_artifact(reason="artifact_id not found in store")
                    return self._run_design_skill()
            else:
                if artifact_required:
                    return ConversationTurn(
                        state="UPLOAD_ARTIFACT_EXAMPLE",
                        message=(
                            "A reference artifact is REQUIRED for this skill.\n\n"
                            "Please re-upload using: artifact:<filename> id:<artifact_id>"
                        ),
                        options=["artifact:<filename> id:<artifact_id>"],
                        must_show_human=True,
                        awaiting_user=True,
                    )
                self._clear_reference_artifact(reason="artifact: prefix but no store or no match")
                return self._run_design_skill()
        else:
            # Local filesystem path
            p = Path(path)
            if p.exists() and p.suffix in (".pptx", ".docx", ".md", ".txt"):
                resolved_path = p
                artifact_filename = p.name
                # For local filesystem paths there is no artifact_id — we store
                # the absolute path as the reference identifier so _run_eval can
                # read the bytes back. Format: "file:<abs_path>"
                artifact_id_for_retention = f"file:{p.resolve()}"
            else:
                log.warning(
                    "UPLOAD_ARTIFACT_EXAMPLE: path %r not found or unsupported — skipping",
                    path,
                )
                if artifact_required:
                    return ConversationTurn(
                        state="UPLOAD_ARTIFACT_EXAMPLE",
                        message=(
                            f"File not found or unsupported type: {path!r}\n\n"
                            "A reference artifact is REQUIRED for this skill. "
                            "Please provide a valid PPTX or DOCX file."
                        ),
                        options=["artifact:<filename> id:<artifact_id>", "/path/to/template.pptx"],
                        must_show_human=True,
                        awaiting_user=True,
                    )
                self._clear_reference_artifact(reason="path not found or unsupported")
                return self._run_design_skill()

        # We have a resolved local path — read bytes for image-only check.
        try:
            artifact_bytes = resolved_path.read_bytes()
        except Exception as exc:
            log.warning(
                "UPLOAD_ARTIFACT_EXAMPLE: could not read artifact bytes from %s: %s — skipping",
                resolved_path, exc,
            )
            if artifact_required:
                return ConversationTurn(
                    state="UPLOAD_ARTIFACT_EXAMPLE",
                    message=(
                        f"Could not read artifact bytes: {exc}\n\n"
                        "A reference artifact is REQUIRED for this skill. "
                        "Please provide a readable file."
                    ),
                    options=["artifact:<filename> id:<artifact_id>", "/path/to/template.pptx"],
                    must_show_human=True,
                    awaiting_user=True,
                )
            self._clear_reference_artifact(reason=f"could not read bytes: {exc}")
            return self._run_design_skill()

        artifact_type = resolved_path.suffix.lstrip(".").lower()

        # --- ADR-029 Phase 1 (S5): IMAGE HARD-REJECT ---
        # Check image-only BEFORE calling analyze_artifact. If image-only, stop here.
        comparator = ArtifactComparator(llm=None)
        if artifact_type not in SUPPORTED_TYPES:
            # Unsupported type (e.g. PDF) — surface as type-unsupported error.
            log.warning(
                "UPLOAD_ARTIFACT_EXAMPLE: unsupported artifact_type=%r — hard-reject",
                artifact_type,
            )
            return ConversationTurn(
                state="UPLOAD_ARTIFACT_EXAMPLE",
                message=(
                    f"Artifact type '.{artifact_type}' is not supported for comparison. "
                    f"Supported types: {sorted(SUPPORTED_TYPES)}. "
                    "Please upload a text-bearing PPTX, DOCX, or Markdown file, "
                    "or type 'skip' to proceed without a reference artifact."
                ),
                options=["skip"] if not artifact_required else [],
                must_show_human=True,
            )

        try:
            is_image = comparator.is_image_only(artifact_bytes, artifact_type)
        except Exception as exc:
            log.warning(
                "UPLOAD_ARTIFACT_EXAMPLE: is_image_only check failed (%s) — "
                "treating as non-image-only (conservative)",
                exc,
            )
            is_image = False

        if is_image:
            # ADR-029 hard-reject: surface verbatim IMAGE_ONLY_MESSAGE.
            # Do NOT advance state — user must re-upload a text-bearing artifact.
            log.warning(
                "UPLOAD_ARTIFACT_EXAMPLE: image-only artifact detected — "
                "hard-rejecting, state stays at UPLOAD_ARTIFACT_EXAMPLE. path=%s",
                resolved_path,
            )
            return ConversationTurn(
                state="UPLOAD_ARTIFACT_EXAMPLE",
                message=IMAGE_ONLY_MESSAGE,
                options=["skip", "upload a text-bearing PPTX/DOCX/MD"] if not artifact_required
                       else ["upload a text-bearing PPTX/DOCX/MD"],
                must_show_human=True,
                awaiting_user=True,
            )

        # --- Text-bearing artifact: structural parse + atomic binding ---
        try:
            _fields, mapping = analyze_artifact(str(resolved_path))
            layout = {
                "sections": _fields,
                "slide_count": len({v.get("slide", 0) for v in (mapping or {}).values()}),
                "mapping": mapping or {},
            }
            # ADR-035: use atomic bind method — single source of truth.
            self._bind_reference_artifact(
                artifact_id=artifact_id_for_retention,
                artifact_type=artifact_type,
                artifact_name=artifact_filename or resolved_path.name,
                artifact_layout=layout,
                artifact_path=str(resolved_path),
            )
            log.info(
                "UPLOAD_ARTIFACT_EXAMPLE: parsed layout sections=%d type=%s ref_id=%s name=%r",
                len(_fields), artifact_type, artifact_id_for_retention, artifact_filename,
            )
        except ValueError as exc:
            # analyze_artifact may raise for malformed files; surface clearly.
            log.warning(
                "UPLOAD_ARTIFACT_EXAMPLE: analyze_artifact failed (%s) — "
                "clearing artifact reference",
                exc,
            )
            if artifact_required:
                return ConversationTurn(
                    state="UPLOAD_ARTIFACT_EXAMPLE",
                    message=(
                        f"Cannot parse artifact: {exc}\n\n"
                        "A reference artifact is REQUIRED for this skill. "
                        "Please provide a valid text-bearing PPTX/DOCX/Markdown."
                    ),
                    options=["artifact:<filename> id:<artifact_id>", "/path/to/template.pptx"],
                    must_show_human=True,
                    awaiting_user=True,
                )
            self._clear_reference_artifact(reason=f"analyze_artifact failed: {exc}")
            return ConversationTurn(
                state="UPLOAD_ARTIFACT_EXAMPLE",
                message=(
                    f"Cannot parse artifact: {exc}\n\n"
                    "Please provide a text-bearing PPTX/DOCX/Markdown, "
                    "or type 'skip' to proceed without an artifact."
                ),
                options=["skip"],
            )

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

        # ADR-030 C1: design_skill prompt via registry.
        # persona= triggers overlay resolution for persona_key_fields,
        # persona_extraction_style, persona_few_shot_example from persona_overlays.yaml.
        # For unknown personas not in persona_overlays.yaml, the overlay falls through (WARNING)
        # and we supply empty-string defaults to preserve the old graceful-degradation behavior.
        # ADR-034: layout_preset_catalog is injected here so the LLM reasons over
        # plain-language descriptions, never over hardcoded preset identifiers.
        # DECISION-019 RC2: layout_valid_ids injected into OUTPUT SCHEMA section ONLY
        # (not into reasoning rules) — DECISION-014 mitigation: IDs appear as constrained
        # enum in the output schema, not as reasoning instructions or examples.
        from .prompt_registry import MissingVarsError as _MissingVarsError
        from ..renderers.layout_catalog import internal_ids as _layout_internal_ids
        # Derive output_format hint from normalised_intent for catalog filtering
        _output_fmt_hint = (self._data.normalised_intent or {}).get("output_kind")
        _valid_layout_ids = _layout_internal_ids()
        _layout_valid_ids_str = ", ".join(f'"{i}"' for i in _valid_layout_ids) + (", null" if _valid_layout_ids else "null")
        _design_base_kwargs = dict(
            normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
            source_capability=json.dumps(self._data.source_capability, indent=2),
            artifact_layout=json.dumps(self._data.artifact_layout, indent=2)
            if self._data.artifact_layout
            else "null",
            existing_kb_cards=json.dumps(cards_summary, indent=2),
            layout_preset_catalog=_layout_catalog_for_prompt(_output_fmt_hint),
            layout_valid_ids=_layout_valid_ids_str,
        )
        try:
            spec = get_registry().get_prompt(
                "design_skill",
                persona=self._data.persona,
                **_design_base_kwargs,
            )
        except _MissingVarsError:
            # Unknown persona: overlay supplied no persona fragment vars — use empty defaults
            log.warning(
                "_run_design_skill: unknown persona %r has no overlay — "
                "degrading to empty persona fragment vars",
                self._data.persona,
            )
            spec = get_registry().get_prompt(
                "design_skill",
                persona=self._data.persona,
                **_design_base_kwargs,
                persona_key_fields="(none specified — use intent-driven fields)",
                persona_extraction_style="",
                persona_few_shot_example="",
            )
        try:
            result = self._llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            # C2/ADR-031: capture tokens_out and parse via _parse_llm_json_response
            # — HIGHEST-risk silent-loss site: a truncated design = silently
            # incomplete deployed skill schema. Hard-fail loudly on truncation.
            tokens_out = result.get("tokens_out") if isinstance(result, dict) else None
            design = _parse_llm_json_response(
                raw,
                tokens_out=tokens_out,
                max_tokens=spec.max_tokens,
            )
        except ValueError as exc:
            raise RuntimeError(
                f"DESIGN_SKILL: LLM response could not be parsed. "
                f"If this is a truncation error (BUG-queue-44364), increase "
                f"design_skill max_tokens in skill_builder.yaml or reduce schema size. "
                f"Error: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"DESIGN_SKILL: LLM design call failed. Error: {exc}. "
                f"Check LLM connectivity and retry."
            ) from exc

        # Validate design output
        if "schema" not in design or "properties" not in design.get("schema", {}):
            raise RuntimeError(
                "DESIGN_SKILL: LLM returned an invalid design (missing schema.properties). "
                "This is a prompt engineering bug — check design_skill in skill_builder.yaml."
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
        # Map every resolvable KB key → its provides_fields set, so we can
        # reject reuse claims where the LLM attributes a field to a KB that
        # does NOT actually provide it (ADR-017 validate would otherwise fail
        # with "workflow requires fields not provided by '<kb>'"). Keyed by
        # both bare name and persona.name.
        kb_provides: dict[str, set] = {}
        try:
            from ..orchestrator.shim_kb import ShimKb
            shim = ShimKb(REPO_ROOT / "framework" / "persona_builders")
            for card in (shim.all_cards() if hasattr(shim, "all_cards") else []):
                # Cards are keyed as 'persona.kb_name'; also accept the short form
                name = card.get("name") or ""
                persona_owner = card.get("persona") or ""
                pf = set(card.get("provides_fields") or [])
                if name:
                    known_kbs.add(name)
                    kb_provides.setdefault(name, set()).update(pf)
                    if persona_owner:
                        q = f"{persona_owner}.{name}"
                        known_kbs.add(q)
                        kb_provides.setdefault(q, set()).update(pf)
        except Exception as exc:  # noqa: BLE001
            log.warning("_run_design_skill: could not load ShimKb to validate reuse_plan: %s", exc)
        if known_kbs:
            filtered_covered: dict = {}
            dropped: list[str] = []
            for fld, kb in (reuse_plan.get("covered") or {}).items():
                # Keep the reuse claim only if the KB exists AND actually
                # provides this field. Bare/qualified both checked. A claim
                # that survives existence but fails field-provision is a
                # DESIGN_SKILL hallucination — drop it so the field flows
                # into the new skill's own KB (which provides it via the
                # synthesized extraction schema).
                provides = (
                    kb_provides.get(kb)
                    or kb_provides.get(kb.split(".")[-1])
                    or set()
                )
                if kb in known_kbs and fld in provides:
                    filtered_covered[fld] = kb
                elif kb in known_kbs:
                    dropped.append(f"{fld}→{kb}(field-not-provided)")
                else:
                    dropped.append(f"{fld}→{kb}(unknown-kb)")
            if dropped:
                log.warning(
                    "_run_design_skill: dropping %d invalid reuse claims "
                    "(unknown KB or field-not-provided): %s",
                    len(dropped), dropped[:8],
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

        # DECISION-019 RC2: validate workflow_shape.layout against the catalog.
        # The design_skill prompt v1.3 (OUTPUT SCHEMA CONSTRAINT) requires the LLM
        # to emit exactly one of the registered catalog internal_ids or null.
        # If the LLM emits a prose string or an unrecognised ID, fail LOUD here —
        # a design-time error that prevents a skill with an unresolvable layout from
        # ever being committed.  Do NOT silently accept prose, do NOT auto-map.
        # (DECISION-019 RC2 Option A — Option B/prose-resolver was rejected.)
        _designed_layout = ws.get("layout")
        if _designed_layout is not None and _designed_layout != "":
            from ..renderers.layout_catalog import internal_ids as _layout_ids_check
            _valid_ids = _layout_ids_check()
            if _designed_layout not in _valid_ids:
                raise RuntimeError(
                    f"DESIGN_SKILL: workflow_shape.layout={_designed_layout!r} is not a "
                    f"registered catalog internal_id. "
                    f"Valid ids: {_valid_ids}. "
                    "The LLM must emit exactly one of these ids (or null) for layout. "
                    "This is a design-time error (DECISION-019 RC2) — the skill cannot "
                    "be committed with a prose or unrecognised layout value. "
                    "Retry DESIGN_SKILL or manually set workflow_shape.layout to a valid id."
                )

        # ADR-038 §A: generate the consumer-facing skill_card + routing_queries
        # AFTER the main design LLM call and AFTER self._data.output_format is set
        # from design["workflow_shape"]["output_format"].  This is the correct
        # (78307d1-original) ordering: output_format in the card reflects the
        # design-authoritative value, not the user's pre-design guess
        # (normalised_intent.output_kind).  The b1adf33 workaround moved this call
        # to before the design LLM call solely so the design_skill call was last in
        # mock_llm.chat.call_args — that brittleness is removed; the ADR-028 test
        # now locates the design_skill call by content (exec-safe / persona_key_fields),
        # not call order.
        _card_generated = self._generate_design_skill_card()
        log.info(
            "_run_design_skill: consumer-facing card generated (has routing_queries=%s). "
            "persona=%s skill=%s",
            bool((_card_generated or {}).get("routing_queries")),
            self._data.persona, self._data.skill_name,
        )

        # ADR-028 Item 3: route to CLARIFY if DESIGN_SKILL has blocking_questions
        blocking_questions_from_design = design.get("blocking_questions", [])
        log.info(
            "_run_design_skill: fields=%d output_format=%s reuse_covered=%d gaps=%d "
            "blocking_questions=%d",
            len(self._data.fields),
            self._data.output_format,
            len(reuse_plan.get("covered", {})),
            len(reuse_plan.get("gaps", [])),
            len(blocking_questions_from_design),
        )

        if blocking_questions_from_design:
            log.info(
                "_run_design_skill: routing to CLARIFY — %d blocking questions before REVIEW_DESIGN",
                len(blocking_questions_from_design),
            )
            bq_dicts = [{"question": q, "resolved": False} for q in blocking_questions_from_design]
            return self._advance_to_clarify(bq_dicts, next_state="REVIEW_DESIGN")

        # ADR-038 §C: must_show_human gate — author must review/confirm the
        # generated skill card (incl. routing_queries) before proceeding.
        return self._prompt_review_skill_card()

    def _generate_design_skill_card(self) -> dict:
        """Generate the consumer-facing skill card at DESIGN_SKILL time (ADR-038 §A).

        Calls the design_skill_card prompt (ADR-030 externalized, hot-reload-safe).
        Returns the card dict and stores it on self._data.design_skill_card.
        Falls back to a minimal card if the LLM call fails so the FSM never halts
        silently — the fallback card has no routing_queries (detected at EVAL).
        """
        persona = self._data.persona
        skill_name = self._data.skill_name
        task_description = self._data.intent_description or skill_name
        # output_format: use the design-authoritative value (self._data.output_format is
        # set from design["workflow_shape"]["output_format"] before this call, per the
        # restored 78307d1 ordering).  The defensive fallback to normalised_intent.output_kind
        # then "markdown" covers only unexpected empty-string cases (should not occur in
        # normal flow).
        output_format = (
            self._data.output_format
            or (self._data.normalised_intent or {}).get("output_kind")
            or "markdown"
        )
        intent_summary = json.dumps(self._data.normalised_intent or {}, indent=2)

        if not self._llm:
            log.warning(
                "_generate_design_skill_card: no LLM — using minimal fallback card. "
                "persona=%s skill=%s",
                persona, skill_name,
            )
            fallback = {
                "summary": task_description[:200],
                "use_when": f"User asks for: {task_description[:200]} (produces {output_format} output)",
                "example_invocations": [
                    f"{task_description[:300]} Output: {output_format}."
                ],
                "routing_queries": {"positive": [], "negative": []},
            }
            self._data.design_skill_card = fallback
            return fallback

        try:
            card_spec = get_registry().get_prompt(
                "design_skill_card",
                skill_name=skill_name,
                persona=persona,
                task_description=task_description[:500],
                output_format=output_format,
                intent_summary=intent_summary[:1000],
            )
            result = self._llm.chat(
                model=card_spec.model,
                messages=[{"role": "user", "content": card_spec.text}],
                response_format=card_spec.response_format,
                max_tokens=card_spec.max_tokens,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            tokens_out = result.get("tokens_out") if isinstance(result, dict) else None
            card = _parse_llm_json_response(raw, tokens_out=tokens_out, max_tokens=card_spec.max_tokens)

            # Validate card structure — must have routing_queries
            if not isinstance(card.get("routing_queries"), dict):
                log.warning(
                    "_generate_design_skill_card: LLM returned card without routing_queries "
                    "— injecting empty structure. persona=%s skill=%s card_keys=%s",
                    persona, skill_name, list(card.keys()),
                )
                card["routing_queries"] = {"positive": [], "negative": []}

            # Ensure example_invocations always includes output_format token
            # (BUG-queue-2ad9a regression guard)
            if card.get("example_invocations"):
                first_ex = card["example_invocations"][0]
                if output_format and output_format.lower() not in first_ex.lower():
                    card["example_invocations"][0] = f"{first_ex} (Output: {output_format})"

            self._data.design_skill_card = card
            log.info(
                "_generate_design_skill_card: card stored. positives=%d negatives=%d "
                "persona=%s skill=%s",
                len(card["routing_queries"].get("positive", [])),
                len(card["routing_queries"].get("negative", [])),
                persona, skill_name,
            )
            return card

        except Exception as exc:
            log.warning(
                "_generate_design_skill_card: LLM call failed (%s) — using fallback card. "
                "persona=%s skill=%s",
                exc, persona, skill_name,
            )
            fallback = {
                "summary": task_description[:200],
                "use_when": f"User asks for: {task_description[:200]} (produces {output_format} output)",
                "example_invocations": [
                    f"{task_description[:300]} Output: {output_format}."
                ],
                "routing_queries": {"positive": [], "negative": []},
            }
            self._data.design_skill_card = fallback
            return fallback

    def _prompt_review_skill_card(self) -> ConversationTurn:
        """ADR-038 §C: must_show_human gate — surface generated card to author.

        The author must review/edit/confirm the consumer-facing skill card
        (incl. routing_queries) before proceeding to REVIEW_DESIGN. This is a
        blocking human review turn per the locked design.
        """
        card = self._data.design_skill_card or {}
        rq = card.get("routing_queries", {})
        positives = rq.get("positive", [])
        negatives = rq.get("negative", [])

        lines = [
            "=== Consumer-Facing Skill Card (review before proceeding) ===\n",
            f"Summary: {card.get('summary', '(not generated)')}",
            f"Use when: {card.get('use_when', '(not generated)')}",
        ]
        if card.get("example_invocations"):
            lines.append("Example invocations:")
            for ex in card["example_invocations"]:
                lines.append(f"  - {ex}")
        lines.append("")
        lines.append("Routing queries (used for EVAL self-test + runtime classifier signal):")
        lines.append("  POSITIVE (should route to this skill):")
        for q in positives:
            lines.append(f"    + {q}")
        if not positives:
            lines.append("    (none generated — EVAL Path-B self-test will not run)")
        lines.append("  NEGATIVE (should NOT route to this skill):")
        for q in negatives:
            lines.append(f"    - {q}")
        if not negatives:
            lines.append("    (none generated — no negative routing guard at EVAL)")
        lines.append("")
        lines.append(
            "Review the card above. Type 'ok' to confirm, or provide edits as JSON:\n"
            '  e.g. {"summary": "Updated summary text"}\n'
            '  or   {"routing_queries": {"positive": ["query 1", "query 2"], "negative": ["query 3"]}}'
        )

        return ConversationTurn(
            state="DESIGN_SKILL",
            message="\n".join(lines),
            data={"skill_card": card},
            options=["ok"],
            must_show_human=True,   # ADR-038 §C: author MUST review this turn
            awaiting_user=True,
        )

    def _handle_design_skill_response(self, user_input: str) -> ConversationTurn:
        """Handle user response at DESIGN_SKILL state.

        ADR-038 §C: covers the skill-card review turn that follows card generation.
        The author can confirm ('ok') or provide JSON edits to the card before
        proceeding to REVIEW_DESIGN.

        Also handles edge cases where the state machine lands here from a session
        restore without a design having run yet.
        """
        # Edge case: session restored without design yet — re-run design
        if self._data.design is None:
            return self._run_design_skill()

        lowered = user_input.lower().strip()

        # If no card yet (backward compat restore), skip card review
        if self._data.design_skill_card is None:
            return self._prompt_review_design()

        # Check if user is confirming ('ok', 'yes', 'looks good', 'confirm')
        if any(kw in lowered for kw in ("ok", "yes", "looks good", "confirm", "proceed")):
            log.info(
                "_handle_design_skill_response: author confirmed skill card. "
                "persona=%s skill=%s routing_queries_positive=%d",
                self._data.persona, self._data.skill_name,
                len((self._data.design_skill_card.get("routing_queries") or {}).get("positive", [])),
            )
            return self._prompt_review_design()

        # Try to apply JSON edits to the card
        try:
            edits = json.loads(user_input)
            if isinstance(edits, dict):
                card = dict(self._data.design_skill_card or {})
                for key, val in edits.items():
                    if key == "routing_queries" and isinstance(val, dict):
                        rq = dict(card.get("routing_queries") or {})
                        rq.update(val)
                        card["routing_queries"] = rq
                    else:
                        card[key] = val
                self._data.design_skill_card = card
                log.info(
                    "_handle_design_skill_response: author edited card fields=%s. "
                    "persona=%s skill=%s",
                    list(edits.keys()), self._data.persona, self._data.skill_name,
                )
                # Re-show the updated card for confirmation
                return self._prompt_review_skill_card()
        except (json.JSONDecodeError, TypeError):
            pass

        # Non-JSON, non-confirm input — re-show the card with guidance
        return self._prompt_review_skill_card()

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

        # ADR-035 (DECISION-015): reference artifact binding status — read from
        # single-source-of-truth method, NOT from design.workflow_shape.layout text.
        if self.has_bound_reference_artifact():
            lines.append(
                f"  Reference artifact: {self._data.artifact_reference_name!r} "
                f"(id: {self._data.artifact_reference_id})"
            )
        else:
            lines.append("  Reference artifact: none bound")

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
            must_show_human=True,   # ADR-028 Item 2: client must show full design to human
            awaiting_user=True,
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
        # ADR-030 C1: review_design_replan prompt via registry
        spec = get_registry().get_prompt(
            "review_design_replan",
            current_design=json.dumps(self._data.design, indent=2),
            edit_request=edit_request,
            updated_source_capability=json.dumps(self._data.source_capability, indent=2),
        )
        try:
            result = self._llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            # C4/ADR-031: parse via _parse_llm_json_response for truncation detection.
            tokens_out = result.get("tokens_out") if isinstance(result, dict) else None
            diff = _parse_llm_json_response(
                raw,
                tokens_out=tokens_out,
                max_tokens=spec.max_tokens,
            )
        except ValueError as exc:
            raise RuntimeError(
                f"REVIEW_DESIGN replan: LLM response could not be parsed. "
                f"Possible truncation (BUG-queue-44364). Error: {exc}."
            ) from exc
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

    def _content_filter_turn(self, state: str, exc) -> ConversationTurn:
        """Build a clean, actionable must_show_human turn for a content-filter
        rejection (ContentFilterRejection). No provider internals leaked —
        mirrors the tier-4 content-filter discipline. State is NOT advanced;
        the session is preserved so the operator can choose a different source.
        """
        rid = getattr(exc, "request_id", "KBF-UNKNOWN")
        hint = getattr(exc, "source_hint", "") or ""
        msg = (
            "The source content was rejected by the LLM provider's "
            "content-safety filter and cannot be used for automated "
            f"extraction.\n\nRequest ID: {rid}"
            + (f"\nSource: {hint}" if hint else "")
            + "\n\nThis is not a framework error — the inference provider "
            "blocked the request. Options:\n"
            "  • 'change sources' — go back and use a different / sanitised "
            "source\n"
            "  • 'stop here' — abandon this session"
        )
        log.warning(
            "%s: content-filter rejection surfaced to operator (requestId=%s)",
            state, rid,
        )
        return ConversationTurn(
            synth_id=self._data.synth_id,
            state=state,
            message=msg,
            data={"error": "content_filter_rejection", "request_id": rid},
            options=["change sources", "stop here"],
            must_show_human=True,
            awaiting_user=True,
        )

    def _advance_to_preview_extraction(self) -> ConversationTurn:
        """Transition to PREVIEW_EXTRACTION: run real LLM extraction on cached samples."""
        self._state = "PREVIEW_EXTRACTION"
        from .review import review_extractions, ContentFilterRejection
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

        # Run review_extractions (ADR-026 Fix 3). A content-filter rejection
        # from the inference provider is surfaced as a clean must_show_human
        # turn (no 500, no provider internals, state NOT advanced).
        try:
            review_result = review_extractions(
                samples=all_samples[:3],  # up to 3 samples
                schema=schema,
                llm=self._llm,
            )
        except ContentFilterRejection as exc:
            return self._content_filter_turn("PREVIEW_EXTRACTION", exc)

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
            must_show_human=True,   # ADR-028 Item 2: human must review extraction before commit
            awaiting_user=True,
        )

    def _handle_preview_extraction_response(self, user_input: str) -> ConversationTurn:
        """Handle user input at PREVIEW_EXTRACTION state."""
        lowered = user_input.lower().strip()
        # Content-filter recovery: 'change sources' → back to CONFIGURE_SOURCES
        if "change source" in lowered or "different source" in lowered:
            return self._advance_to_configure_sources_v2()
        if "stop" in lowered:
            return ConversationTurn(
                state="DONE", message="Session abandoned.", done=True,
            )
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

            # ADR-030 C1: analyze_artifact prompt via registry (legacy path — in-flight sessions only)
            spec = get_registry().get_prompt(
                "analyze_artifact",
                persona=self._data.persona or "unknown",
                intent=self._data.intent_description or "",
                artifact_type=artifact_type,
                field_contexts="\n".join(context_lines),
            )

            result = self._llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
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
    # ADR-031 C1: _SOURCE_GROUNDED_REVIEW_PROMPT deleted — prompt is now served
    # by PromptRegistry (id="source_grounded_review" in skill_builder.yaml).
    # Use get_registry().get_prompt("source_grounded_review", ...) at call sites.

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

            # Build schema summary.
            # C7: pass the full description — no [:120] slice (descriptions are
            # never huge; slicing silently discards precision from the LLM).
            schema_lines = []
            for field, spec in field_specs.items():
                t = spec.get("type", "string")
                desc = spec.get("description", "")
                enum = spec.get("enum")
                extra = f" (enum: {enum})" if enum else ""
                schema_lines.append(f"  - {field} [{t}{extra}]: {desc}")

            # Combine sample content.
            # C6/ADR-031: raise per-sample cap 4000→20000, total cap 8000→40000.
            # gpt-4o input is ~128k tokens; the old 4k/8k caps silently discarded
            # source structure that the coherence check needed to see.
            sample_parts = []
            total_chars = 0
            for s in all_samples:
                content = s.get("content", s.get("text", ""))[:20000]
                citation = s.get("source_citation", "?")
                sample_parts.append(f"--- {citation} ---\n{content}")
                total_chars += len(content)
                if total_chars >= 40000:
                    break
            sample_content = "\n\n".join(sample_parts)

            # ADR-031 C1: prompt via PromptRegistry — last hard-coded prompt migrated.
            spec = get_registry().get_prompt(
                "source_grounded_review",
                persona=self._data.persona or "unknown",
                intent=self._data.intent_description or "",
                schema_summary="\n".join(schema_lines),
                sample_content=sample_content[:40000],
            )

            result = self._llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
            )
            raw = result.get("text", "") if isinstance(result, dict) else str(result)
            tokens_out = result.get("tokens_out") if isinstance(result, dict) else None
            # ADR-031 C1: parse via shared helper — truncation detection active.
            review_data = _parse_llm_json_response(
                raw,
                tokens_out=tokens_out,
                max_tokens=spec.max_tokens,
            )

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
                # ADR-031 C9: no maxLength — consistent with Group B policy.
                self._data.field_specs[field_name] = {
                    "type": "string", "description": new_desc,
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

    # -- ADR-036: Connector Registry type-check gate -------------------------

    def _gate_source_connector(
        self,
        source: dict,
        user_input: str = "",
        operation: str | None = None,
    ) -> "ConversationTurn | None":
        """ADR-036 §D.1 Step 2: connector type-check against the registry.

        Returns a HARD_STOP ConversationTurn when the connector_id in
        ``source["kind"]`` is not registered or does not support
        ``operation``.  Returns None when the source passes the gate.

        Also auto-logs a CONNECTOR-REQ record (ADR-036 Amendment 1) when
        a hard stop is triggered.

        Args:
            source:     Parsed source dict with at minimum a ``"kind"`` key.
            user_input: Original user text (stored in connector-request record).
            operation:  Operation to validate (default None — type-check only).
        """
        connector_id = source.get("kind", "")
        if not connector_id:
            return None

        if connector_id == "unknown":
            # _parse_source_descriptor could not classify the input.
            # Treat the first word of the raw input as the attempted connector_id
            # and check it against the registry.  If it's not registered, fire
            # HARD_STOP with the honest message — the user used an unknown descriptor
            # that does not match any recognized connector or URL pattern.
            raw = source.get("raw", user_input or "").strip()
            first_word = raw.split()[0] if raw.split() else ""
            if not first_word:
                return None
            connector_id = first_word.lower()

        registry = _get_connector_registry()
        result = registry.gate_connector_type(connector_id, operation)

        if result.status == _CONNECTOR_HARD_STOP:
            # ADR-036 Amendment 1: log a New Connector Request demand record
            self._log_connector_request(
                connector_id=connector_id,
                operation=operation or "unknown",
                user_input=user_input,
                gating_result=result,
                registry=registry,
            )
            return ConversationTurn(
                state="CONFIGURE_SOURCES",
                message=result.message,
                awaiting_user=True,
                must_show_human=True,
            )
        return None

    def _log_connector_request(
        self,
        connector_id: str,
        operation: str,
        user_input: str,
        gating_result: Any,
        registry: Any,
    ) -> None:
        """ADR-036 Amendment 1: log a CONNECTOR-REQ demand record to ADB.

        Uses AdbErrorStore.record_user_bug with ``record_kind: "connector_request"``
        in extra_json (discriminator per Amendment 1 §L.2).

        Per §L.5: write failure is logged at ERROR, never silently swallowed,
        and never suppresses the hard stop that has already been issued.
        """
        import uuid as _uuid
        from datetime import datetime as _dt, timezone as _tz
        queue_id = f"CONNECTOR-REQ-{_uuid.uuid4().hex[:5]}"
        now_iso = _dt.now(tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        supported_set = [m.connector_id for m in registry.list_connectors()]

        entry = {
            "queue_id": queue_id,
            "timestamp": now_iso,
            "tool": "authorSkill",
            "description": (
                f"New Connector Request: unsupported connector \"{connector_id}\" "
                f"requested (operation={operation!r}) during CONFIGURE_SOURCES."
            ),
            # ADR-036 Amendment 1 §L.3 extra_json fields:
            "record_kind": "connector_request",
            "requested_connector_id": connector_id,
            "inferred_operation": operation,
            "supported_set_at_rejection": supported_set,
            "user_request_text": user_input[:2000] if user_input else "",
            "persona": self._data.persona or None,
            "session_id": self._data.synth_id or None,
            "request_id": "",
        }

        # Try skill_store's error_store if available; fall back to a filestore
        # ErrorStore so the demand signal is never silently dropped (ADR-036 §L.5).
        error_store = None
        try:
            if self._skill_store is not None:
                error_store = getattr(self._skill_store, "error_store", None)
        except Exception:
            pass

        if error_store is None:
            # Fallback: write to filesystem JSONL under ~/.kbf/store/
            try:
                from ..deploy.error_store import ErrorStore as _ErrorStore
                import os as _os
                store_root = _os.path.expanduser("~/.kbf/store")
                error_store = _ErrorStore(store_root)
            except Exception as fe:
                log.error(
                    "ADR-036 connector-request log: could not initialise fallback "
                    "ErrorStore: %s — demand record LOST: %s",
                    fe, entry,
                )
                return

        try:
            error_store.record_user_bug(entry)
            log.info(
                "ADR-036 connector-request logged: queue_id=%s connector=%s",
                queue_id, connector_id,
            )
        except Exception as exc:
            # §L.5: failure MUST be logged at ERROR, never silently swallowed.
            log.error(
                "ADR-036 connector-request: ADB write failed — demand record "
                "queue_id=%s connector=%s LOST: %s",
                queue_id, connector_id, exc,
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

        # ADR-036 §D.1 Step 2: registry type-check before accepting the source.
        hard_stop = self._gate_source_connector(source, user_input=user_input)
        if hard_stop is not None:
            # HARD_STOP — connector not in registry.  Do NOT add to sources.
            # No partial state saved.  Message already formatted per §D.2.
            return hard_stop

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

        # ADR-032 P1-D: source_binding contract validation.
        # Hard-fail discipline: an ask_parameterized skill that fails the contract
        # MUST fail VALIDATE with a clear actionable message — never silently pass
        # or downgrade to author_fixed.  Same discipline as the ADR-017 link check above.
        import os as _os_validate
        kbf_env_for_validate = _os_validate.environ.get("KBF_ENV", "laptop")
        session_sb_mode = self._data.source_binding_mode

        # Read the committed workflow skill YAML for source_binding inspection.
        # Use synthesized_artifacts in-session dict when available (avoids re-reading
        # a tempfile that may have already been cleaned up), then fall back to
        # filesystem path.
        _synthesized_wf: dict = {}
        _wf_key = f"framework/workflow_skills/{self._data.persona}/{self._data.skill_name}.yaml"
        _cached = self._data.synthesized_artifacts.get(_wf_key)
        if isinstance(_cached, dict):
            _synthesized_wf = _cached
        else:
            # Fall back: try the filesystem path
            _fs_wf_path = (
                REPO_ROOT / "framework" / "workflow_skills"
                / self._data.persona / f"{self._data.skill_name}.yaml"
            )
            if _fs_wf_path.exists():
                try:
                    _synthesized_wf = yaml.safe_load(_fs_wf_path.read_text()) or {}
                except Exception as _wf_exc:
                    log.warning(
                        "_run_validate: could not read workflow YAML for source_binding check (%s)",
                        _wf_exc,
                    )

        sb_errors = _validate_source_binding_contract(_synthesized_wf, session_sb_mode)

        # Additional check: adapter availability for ask_parameterized + ingest_on_demand.
        # Per ADR-032 §D.4: if the skill requires live Confluence access at consumption
        # time, the target environment MUST have a Confluence adapter configured.
        _sb_block = _synthesized_wf.get("source_binding") or {}
        if (
            _sb_block.get("mode") == "ask_parameterized"
            and _sb_block.get("ingest_on_demand", False)
            and not sb_errors  # only check adapter if structural contract passes
        ):
            target_env = self._data.normalised_intent.get("target_env") or kbf_env_for_validate
            adapter_ok = _check_confluence_adapter_available(target_env, REPO_ROOT)
            if not adapter_ok:
                sb_errors.append(
                    f"This skill requires live Confluence access at consumption time "
                    f"(source_binding.ingest_on_demand: true). The target deployment "
                    f"environment '{target_env}' has no Confluence adapter configured. "
                    f"Configure a Confluence adapter in "
                    f"framework/config/adapters/confluence.yaml "
                    f"for that environment, or set ingest_on_demand: false."
                )

        if sb_errors:
            # Merge source_binding errors into the validation result — hard-fail.
            all_errors = result.get("errors", []) + sb_errors
            result = {"passed": False, "errors": all_errors}
            log.warning(
                "_run_validate: source_binding contract failed for skill=%s "
                "session_mode=%s errors=%d",
                self._data.skill_name, session_sb_mode, len(sb_errors),
            )

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
        # A3 (BUG-queue-990fe): pass the session persona so pages ingested here
        # carry the correct persona in wiki_metadata (RC1 fix — raw wins, this
        # is the fallback for pages with no raw persona field).
        ingestor = ConfluenceWikiIngestor(
            adapter=confluence_adapter,
            wiki_store=wiki_store,
            persona=self._data.persona or None,
        )

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

        # Capture entering state BEFORE mutating — used for INGEST-or-later gate below.
        _entering_state = self._state
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
        from .review import _llm_extract, ContentFilterRejection
        extraction_gold_rows: list[dict] = []
        extraction_results: list[dict] = []

        for sample in all_samples[:3]:
            try:
                extracted = _llm_extract(sample, schema, self._llm)
            except ContentFilterRejection as exc:
                # Provider blocked this source — surface a clean
                # must_show_human turn; do NOT 500, do NOT advance state.
                return self._content_filter_turn("EVAL", exc)
            except Exception as exc:
                raise RuntimeError(
                    f"EVAL: extraction LLM call failed for sample "
                    f"'{sample.get('source_citation', '?')}'. Error: {exc}."
                ) from exc

            # Faithfulness judge needs to find the extracted value in the
            # snippet. _llm_extract now uses 80k chars (raised in review.py
            # Group D / ADR-031). Keep parity so the judge sees the same
            # window as the extractor — mismatches cause false unfaithful verdicts.
            # C6/ADR-031: raise 12000→80000 (sync with review.py _llm_extract).
            source_snippet = str(sample.get("content", ""))[:80000]
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
                # ADR-030 C1: eval_judge prompt via registry.
                # C8/ADR-031: pass full field_description (no [:200] slice — never
                # huge); extracted_value raised [:300]→[:2000] so the judge sees
                # the real value, not a clip that would give false "unfaithful"
                # verdicts on longer extracted strings.
                judge_spec = get_registry().get_prompt(
                    "eval_judge",
                    field_name=fname,
                    field_description=fspec.get("description", ""),
                    extracted_value=str(val)[:2000],
                    source_snippet=source_snippet,
                )
                try:
                    judge_result_raw = self._llm.chat(
                        model=judge_spec.model,
                        messages=[{"role": "user", "content": judge_spec.text}],
                        response_format=judge_spec.response_format,
                        max_tokens=judge_spec.max_tokens,
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

        # Step 6: ADR-038 — Path B routing self-test + Path A in-process execution.
        # Replaces the old /api/v1/ask HTTP call (chicken-and-egg defect, BUG-queue-2ad9a).
        # See ADR-038 §B.2 (Path A) and §B.3 (Path B) for full design.
        workflow_gold_rows: list[dict] = []
        expected_skill = f"{persona}.{skill_name}"
        wf_artifact_url = None
        ask_latency_ms = None

        # --- ADR-038 §B.3: Path B — routing self-test (resolve-only mode) ---
        # INGEST-or-later gate (ADR-038 §B.4): the FSM naturally ensures this
        # at EVAL state, but check explicitly to guard against future FSM changes.
        # _entering_state is captured before self._state was mutated to "EVAL".
        _pre_ingest_states = {"COMMITTED", "VALIDATE"}
        if _entering_state in _pre_ingest_states:
            # Hard fail — do NOT silently skip (no-silent-degradation rule)
            raise RuntimeError(
                f"EVAL: INGEST-or-later gate failed. Skill is at state={_entering_state!r}. "
                f"EVAL cannot run before INGEST — the KB does not exist yet. "
                f"Per ADR-038 §B.4, this is a hard failure (not a silent skip)."
            )

        # Path B routing self-test: use routing_queries from the design_skill_card.
        # These are curated consumer queries from the author's review at DESIGN_SKILL.
        routing_results: list[dict] = []
        routing_self_test_passed: bool = True  # default true; set False on any failure

        skill_card = self._data.design_skill_card or {}
        rq = skill_card.get("routing_queries") or {}
        positive_queries = rq.get("positive") or []
        negative_queries = rq.get("negative") or []

        # Load the ShimWorkflows for resolve-only routing
        try:
            from ..orchestrator.shim_workflows import ShimWorkflows
            wf_dir = REPO_ROOT / "framework" / "workflow_skills"
            _shim = ShimWorkflows(wf_dir, skill_store=self._skill_store)
        except Exception as exc:
            log.warning("_run_eval: could not load ShimWorkflows for Path-B: %s", exc)
            _shim = None

        this_skill_id = f"{persona}.{skill_name}"

        if _shim and positive_queries:
            log.info(
                "_run_eval: Path-B routing self-test — %d positive, %d negative queries. "
                "persona=%s skill=%s",
                len(positive_queries), len(negative_queries), persona, skill_name,
            )
            for q in positive_queries:
                resolved = _shim.resolve_only(q, scope="ingest_or_later")
                resolved_id = resolved.get("skill_id")
                resolved_name = resolved.get("skill_name")
                passed = (
                    resolved.get("matched")
                    and resolved.get("tier") == 1
                    and resolved_id == this_skill_id
                )
                if not passed:
                    routing_self_test_passed = False
                routing_results.append({
                    "type": "positive",
                    "query": q,
                    "passed": passed,
                    "resolved_skill_id": resolved_id,
                    "resolved_skill_name": resolved_name,
                    "tier": resolved.get("tier"),
                })
                log.info(
                    "_run_eval: Path-B positive q=%r → %s tier=%s pass=%s",
                    q, resolved_id, resolved.get("tier"), passed,
                )
            for q in negative_queries:
                resolved = _shim.resolve_only(q, scope="ingest_or_later")
                resolved_id = resolved.get("skill_id")
                # Negative: should NOT route to this skill
                passed = (not resolved.get("matched")) or (resolved_id != this_skill_id)
                if not passed:
                    routing_self_test_passed = False
                routing_results.append({
                    "type": "negative",
                    "query": q,
                    "passed": passed,
                    "resolved_skill_id": resolved_id,
                    "resolved_skill_name": resolved.get("skill_name"),
                    "tier": resolved.get("tier"),
                })
                log.info(
                    "_run_eval: Path-B negative q=%r → %s tier=%s pass=%s",
                    q, resolved_id, resolved.get("tier"), passed,
                )
        elif not positive_queries:
            log.info(
                "_run_eval: Path-B skipped — no routing_queries.positive in skill card. "
                "routing_self_test_passed defaults True (no queries to fail). "
                "persona=%s skill=%s",
                persona, skill_name,
            )
        else:
            log.warning("_run_eval: Path-B skipped — ShimWorkflows failed to load.")

        # Store routing self-test result on session
        self._data.routing_self_test_passed = routing_self_test_passed

        # --- ADR-038 §B.2: Path A — in-process execution via execute_from_config ---
        # Uses the session's committed/design config + real inputs, bypassing the
        # promoted-only router. Never exposed as a public HTTP flag or endpoint.
        execution_status: str = "not_run"  # "success" | "failure" | "not_run"
        execution_error: str | None = None
        produced_artifact_bytes: bytes | None = None

        try:
            from ..workflow_runtime.executor import WorkflowExecutor
            wf_cfg_text = self._skill_store.read_artifact(
                persona=persona, skill_name=skill_name, artifact_type="workflow_skill"
            )
            if wf_cfg_text:
                wf_cfg = yaml.safe_load(wf_cfg_text) or {}
                # Build inputs from session state
                domains = (self._data.normalised_intent or {}).get("scope_domains", [skill_name])
                canonical_question = f"What is the status of the {' '.join(domains)} project for this week?"
                exec_inputs = {"input": canonical_question, "persona": persona}
                t0 = time.monotonic()
                _executor = WorkflowExecutor(llm=self._llm)
                exec_result = _executor.execute_from_config(wf_cfg, exec_inputs)
                ask_latency_ms = int((time.monotonic() - t0) * 1000)
                execution_status = "success"
                wf_artifact_url = (
                    exec_result.get("artifact_url")
                    or exec_result.get("artifact_path")
                    or exec_result.get("output_path")
                )
                log.info(
                    "_run_eval: Path-A execution SUCCESS latency_ms=%d artifact_url=%s",
                    ask_latency_ms, wf_artifact_url,
                )
            else:
                execution_status = "failure"
                execution_error = (
                    f"workflow_skill artifact not found in ADB for {persona}.{skill_name}. "
                    f"Re-commit the skill before running EVAL."
                )
                log.warning("_run_eval: Path-A skipped — no workflow_skill artifact in ADB.")
        except Exception as exc:
            # ADR-038 §B.2: execution failure is HIGH-severity; NOT collapsed to soft note.
            execution_status = "failure"
            execution_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "_run_eval: Path-A EXECUTION FAILURE [HIGH] — persona=%s skill=%s error=%s",
                persona, skill_name, exc, exc_info=True,
            )

        # Try to read produced artifact bytes from the delivered path
        if wf_artifact_url:
            try:
                produced_path = Path(wf_artifact_url) if not wf_artifact_url.startswith("http") else None
                if produced_path and produced_path.exists():
                    produced_artifact_bytes = produced_path.read_bytes()
                    log.info(
                        "_run_eval: read produced artifact bytes (%d bytes) from %s",
                        len(produced_artifact_bytes), produced_path,
                    )
                else:
                    log.info(
                        "_run_eval: produced artifact URL %r is remote or not found "
                        "on filesystem — comparator skipped",
                        wf_artifact_url,
                    )
            except Exception as exc:
                log.warning("_run_eval: could not read produced artifact bytes: %s", exc)

        # Build workflow gold row (updated to reflect ADR-038 Path-A/B results)
        domains_for_gold = (self._data.normalised_intent or {}).get("scope_domains", [skill_name])
        canonical_question_for_gold = (
            f"What is the status of the {' '.join(domains_for_gold)} project for this week?"
        )
        wf_gold_row = {
            "kind": "auto_generated",
            "question": canonical_question_for_gold,
            "expected_skill": expected_skill,
            "expected_tier": 1,
            # C10/ADR-031: no cap — eval quality gate must cover the whole schema
            "expected_fields": list(all_fields),
            "path_a_status": execution_status,
            "path_a_artifact_url": wf_artifact_url,
            "path_b_routing_results": routing_results,
            "path_b_passed": routing_self_test_passed,
            "ask_latency_ms": ask_latency_ms,
            "created_at": _now_iso(),
        }
        workflow_gold_rows.append(wf_gold_row)

        # Step 7: write gold sets to filesystem + ADB
        # Guard: persona/skill_name must be non-None strings for safe path construction.
        # _data defaults to "" so this is defensive against out-of-band None assignment.
        _safe_persona = persona or "unknown"
        _safe_skill = skill_name or "unknown"
        extraction_gold_path = f"eval/gold_sets/{_safe_persona}-{_safe_skill}-extraction.jsonl"
        workflow_gold_path = f"eval/gold_sets/{_safe_persona}-{_safe_skill}-workflow.jsonl"

        try:
            ext_path = REPO_ROOT / extraction_gold_path
            ext_path.parent.mkdir(parents=True, exist_ok=True)
            ext_path.write_text(
                "\n".join(json.dumps(row) for row in extraction_gold_rows) + "\n"
            )
            wf_path = REPO_ROOT / workflow_gold_path
            # mkdir must be called on wf_path.parent too — ext_path.parent.mkdir() only
            # covers the first path; in mocked / unusual environments the two path
            # objects may differ even if the parent dir is the same on a real filesystem.
            wf_path.parent.mkdir(parents=True, exist_ok=True)
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

        # Step 8: load exit criteria from workflow YAML or use defaults.
        # ADR-029 Phase 1 (S5): exit_criteria.passed is NOW DIAGNOSTIC ONLY —
        # it is no longer the gate for PROMOTE. The gate is explicit user
        # acceptance ("accept") at the EVAL gap-report turn.
        # DECISION-010 (auto-gold gate) is superseded; the comparator gap-report
        # + user-accept is the new terminal signal.
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

        # Diagnostic-only: still computed for audit trail, but does NOT gate PROMOTE.
        passed_diagnostic = (
            recall_at_k >= recall_threshold and faithfulness >= faithfulness_threshold
        )
        total_cost_est = faithfulness_total * 0.002  # rough estimate at $0.002/call

        # Step 9: ADR-029 Phase 1 + ADR-038 §B.2 — run ArtifactComparator.
        # produced_artifact_bytes is set above by Path-A execution (or None on failure).
        # The comparator gate requires BOTH produced_artifact_bytes AND ref_bytes.
        comparator_result = None
        comparator_error: str | None = None

        # ADR-035 (DECISION-015): use single-source-of-truth method for artifact bound check.
        # EVAL and REVIEW_DESIGN now agree by construction — both call has_bound_reference_artifact().
        ref_id = self._data.artifact_reference_id
        ref_type = self._data.artifact_reference_type
        ref_bytes: bytes | None = None

        if self.has_bound_reference_artifact():
            # Retrieve reference artifact bytes from the store
            try:
                if ref_id.startswith("file:"):
                    ref_path = Path(ref_id[len("file:"):])
                    if ref_path.exists():
                        ref_bytes = ref_path.read_bytes()
                    else:
                        log.warning(
                            "_run_eval: reference artifact path %s not found — "
                            "comparator skipped",
                            ref_path,
                        )
                elif self._artifact_store is not None:
                    local = self._artifact_store.resolve(ref_id)
                    if local:
                        ref_bytes = Path(local).read_bytes()
                    else:
                        log.warning(
                            "_run_eval: artifact_store.resolve(%r) returned None — "
                            "comparator skipped",
                            ref_id,
                        )
                else:
                    log.warning(
                        "_run_eval: artifact_reference_id=%r but artifact_store is None — "
                        "comparator skipped (no artifact store wired)",
                        ref_id,
                    )
            except Exception as exc:
                log.warning(
                    "_run_eval: could not retrieve reference artifact bytes: %s", exc
                )

        if ref_bytes is not None and produced_artifact_bytes is not None and ref_type:
            try:
                from .comparator import ArtifactComparator
                _cmp = ArtifactComparator(llm=None)
                comparator_result = _cmp.compare(ref_bytes, produced_artifact_bytes, ref_type)
                log.info(
                    "_run_eval: comparator structure_score=%.3f density_score=%.3f "
                    "missing=%d thin=%d",
                    comparator_result.structure_score,
                    comparator_result.density_score,
                    len(comparator_result.missing_sections),
                    len(comparator_result.thin_sections),
                )
            except Exception as exc:
                comparator_error = str(exc)
                log.warning("_run_eval: comparator.compare() failed: %s", exc)
        elif ref_bytes is not None and ref_type:
            log.info(
                "_run_eval: reference artifact available but no produced artifact bytes "
                "(wf_artifact_url=%r) — comparator skipped for this run",
                wf_artifact_url,
            )
        elif self.has_bound_reference_artifact():
            log.info(
                "_run_eval: reference artifact bound (id=%r name=%r) but bytes unavailable — "
                "comparator skipped",
                self._data.artifact_reference_id,
                self._data.artifact_reference_name,
            )
        else:
            log.info(
                "_run_eval: has_bound_reference_artifact()=False — comparator skipped "
                "(ADR-035: single-source-of-truth; no artifact bound at UPLOAD_ARTIFACT_EXAMPLE)"
            )

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
            # ADR-029 S5: exit_criteria.passed is DIAGNOSTIC ONLY — not the PROMOTE gate.
            # The gate is user's explicit "accept" response at the EVAL gap-report turn.
            # DECISION-010 auto-gold gate is superseded.
            "exit_criteria": {
                "recall_threshold": recall_threshold,
                "faithfulness_threshold": faithfulness_threshold,
                "passed": passed_diagnostic,
                "_note": (
                    "diagnostic-only (ADR-029 Phase 1): exit_criteria.passed no longer "
                    "gates PROMOTE. User explicit accept at EVAL gap-report is the gate."
                ),
            },
            # ADR-038: Path-A execution result (replaces old workflow_score/wf_tier dict)
            "path_a_execution": {
                "status": execution_status,
                "artifact_url": wf_artifact_url,
                "latency_ms": ask_latency_ms,
                "error": execution_error,
            },
            # ADR-038: Path-B routing self-test results
            "path_b_routing": {
                "passed": routing_self_test_passed,
                "results": routing_results,
                "positive_count": len(positive_queries),
                "negative_count": len(negative_queries),
            },
            # ADR-029 Phase 1: comparator result (None when no reference was uploaded)
            "comparator": comparator_result.to_dict() if comparator_result else None,
            # ADR-038 §F: routing_self_test_passed is a HARD BLOCKER on PROMOTE.
            # If False, PROMOTE is refused with an actionable message.
            "routing_self_test_passed": routing_self_test_passed,
        }

        # Build user message — ADR-038 §B.6: THREE mandatory sections.
        # Each section is ALWAYS present; silent omission of any section is prohibited.
        lines: list[str] = []

        # ----------------------------------------------------------------
        # SECTION 1 — ROUTING ASSERTIONS (Path B)
        # ----------------------------------------------------------------
        lines.append("=== SECTION 1: ROUTING ASSERTIONS (Path B) ===\n")
        if positive_queries or negative_queries:
            pass_count = sum(1 for r in routing_results if r["passed"])
            fail_count = len(routing_results) - pass_count
            lines.append(
                f"Positive queries tested: {len(positive_queries)}  "
                f"Negative queries tested: {len(negative_queries)}"
            )
            for r in routing_results:
                status_tag = "PASS" if r["passed"] else "FAIL  [HIGH]"
                q_type = r["type"].upper()
                resolved = r.get("resolved_skill_name") or r.get("resolved_skill_id") or "(none)"
                tier_str = f" tier {r.get('tier', '?')}" if r.get("tier") else ""
                lines.append(f"  {q_type} {status_tag}: {r['query']!r} → {resolved}{tier_str}")
            if not routing_self_test_passed:
                lines.append(
                    f"\n  [HIGH] Routing self-test FAILED: {fail_count} assertion(s) failed. "
                    "PROMOTE is BLOCKED until all routing assertions pass. "
                    "Fix the skill card routing_queries or the skill's summary/use_when text, "
                    "then re-run EVAL."
                )
            else:
                lines.append(f"\n  Routing self-test PASSED ({pass_count}/{len(routing_results)} assertions passed).")
        else:
            lines.append(
                "  N/A — no routing_queries in skill card (DESIGN_SKILL did not generate them "
                "or they were cleared during review). Routing self-test did not run.\n"
                "  Note: without routing_queries, routing correctness is UNVERIFIED. "
                "Consider re-running DESIGN_SKILL to generate a consumer-facing card."
            )
        lines.append("")

        # ----------------------------------------------------------------
        # SECTION 2 — EXECUTION RESULT (Path A)
        # ----------------------------------------------------------------
        lines.append("=== SECTION 2: EXECUTION (Path A) ===\n")
        if execution_status == "success":
            lines.append(f"  Status: SUCCESS")
            if wf_artifact_url:
                lines.append(f"  Artifact produced: {wf_artifact_url}")
            if ask_latency_ms is not None:
                lines.append(f"  Latency: {ask_latency_ms}ms")
        elif execution_status == "failure":
            lines.append(f"  Status: FAILURE  [HIGH]")
            lines.append(f"  Error: {execution_error}")
            lines.append(
                "  The skill execution failed. PROMOTE requires execution success. "
                "Review the error above and fix the skill configuration."
            )
        else:
            lines.append("  Status: NOT RUN (no workflow_skill config available in ADB)")
        lines.append("")

        # ----------------------------------------------------------------
        # SECTION 3 — COMPARATOR SCORES (ADR-029)
        # ----------------------------------------------------------------
        lines.append("=== SECTION 3: COMPARATOR (ADR-029) ===\n")
        if comparator_result:
            lines.append(f"  Status: RAN")
            lines.append(
                f"  structure_score: {comparator_result.structure_score:.0%}  "
                f"density_score: {comparator_result.density_score:.0%}"
            )
            if comparator_result.missing_sections:
                lines.append(f"  Missing: {', '.join(comparator_result.missing_sections)}")
            if comparator_result.thin_sections:
                lines.append(f"  Thin:    {', '.join(comparator_result.thin_sections)}")
            lines.append("")
            lines.append(comparator_result.gap_report)
        elif comparator_error:
            lines.append(f"  Status: SKIPPED — comparator.compare() raised: {comparator_error}")
        elif execution_status == "failure":
            lines.append("  Status: SKIPPED — reason: execution failed in Section 2")
        elif self.has_bound_reference_artifact() and not produced_artifact_bytes:
            lines.append(
                f"  Status: SKIPPED — reference artifact "
                f"'{self._data.artifact_reference_name}' was bound "
                "but execution did not deliver a local artifact file this run"
            )
        else:
            # ADR-035: has_bound_reference_artifact() is False
            lines.append(
                "  Status: SKIPPED — reason: no bound reference artifact "
                "(ADR-035 has_bound_reference_artifact()=False; "
                "upload a reference at UPLOAD_ARTIFACT_EXAMPLE to enable comparator)"
            )
        lines.append("")

        # ----------------------------------------------------------------
        # Diagnostic Metrics (informational — not the PROMOTE gate)
        # ----------------------------------------------------------------
        lines.append(
            "=== Diagnostic Metrics (informational — not the PROMOTE gate) ==="
        )
        lines.append(f"  Extraction samples: {len(extraction_gold_rows)}")
        lines.append(
            f"  Recall@k: {recall_at_k:.1%} (threshold: {recall_threshold:.0%}) "
            f"[DIAGNOSTIC ONLY — ADR-029 superseded DECISION-010]"
        )
        lines.append(
            f"  Faithfulness: {faithfulness:.1%} (threshold: {faithfulness_threshold:.0%}) "
            f"[DIAGNOSTIC ONLY]"
        )
        if ask_latency_ms is not None:
            lines.append(f"  Path-A execution latency: {ask_latency_ms}ms")
        else:
            lines.append("  Path-A execution: not run")

        lines.append("")
        lines.append(
            "Note: kind=auto_generated — diagnostic gold rows were created from the same "
            "LLM that did the extraction. They measure consistency, not correctness."
        )
        lines.append("")

        # Options — ADR-038 §F: if routing self-test failed, PROMOTE is blocked.
        if not routing_self_test_passed and (positive_queries or negative_queries):
            lines.append(
                "Options (PROMOTE IS BLOCKED — routing self-test failed):\n"
                "  'ship as draft'    — save as draft without promoting\n"
                "  'review design'    — go back to REVIEW_DESIGN to update skill card\n"
                "  'stop here'        — pause session\n\n"
                "To unblock PROMOTE: fix the routing_queries in the skill card at DESIGN_SKILL, "
                "re-commit, re-ingest, and re-run EVAL. No override is provided."
            )
        else:
            lines.append(
                "Options:\n"
                "  'accept'           — the produced artifact meets your quality bar → PROMOTE\n"
                "  'ship as draft'    — save as draft without promoting\n"
                "  'review design'    — go back to REVIEW_DESIGN (S6 routing — not yet active)\n"
                "  'configure sources'— go back to CONFIGURE_SOURCES (S6 routing — not yet active)\n"
                "  'stop here'        — pause session"
            )

        # ADR-029 S5 + ADR-038: the EVAL turn ALWAYS has must_show_human=True.
        eval_turn_data = {
            "structure_score": comparator_result.structure_score if comparator_result else None,
            "density_score": comparator_result.density_score if comparator_result else None,
            "missing_sections": comparator_result.missing_sections if comparator_result else [],
            "thin_sections": comparator_result.thin_sections if comparator_result else [],
            # ADR-038: Path-A/B results
            "path_a_status": execution_status,
            "path_b_passed": routing_self_test_passed,
            "routing_results": routing_results,
            # Diagnostic-only fields — present for observability but do not gate PROMOTE
            "intrinsic_recall": recall_at_k,
            "intrinsic_faithfulness": faithfulness,
        }

        return ConversationTurn(
            state="EVAL",
            message="\n".join(lines),
            data={"eval": self._data.eval_result, "gap_report": eval_turn_data},
            options=["accept", "ship as draft", "review design", "configure sources", "stop here"],
            must_show_human=True,   # ADR-029 S5: user MUST read gap report before accepting
            awaiting_user=True,
        )

    def _handle_eval_response(self, user_input: str) -> ConversationTurn:
        """Handle user response at EVAL state.

        ADR-029 Phase 1 (S5) gate:
          "accept" or "looks good" → PROMOTE (the only numeric-independent gate)
          "ship as draft"          → DONE with draft status
          "stop here"              → DONE (session paused)
          "review design" / "configure sources" / "retry" → S6 seam (not yet active)

        NOTE: "force promote" is intentionally retained for operator escape-hatch
        but no longer needed as the primary path (any user who sees the gap report
        and types "accept" proceeds to PROMOTE without needing to force).

        S6 routing seam: when the user rejects (not "accept" / "ship as draft" /
        "stop here"), S5 surfaces the gap report again with guidance but does NOT
        auto-route. Auto-routing requires the classifier validation gate (between
        S5 and S6 per the blueprint). The reject path is a labeled seam here.
        """
        lowered = user_input.lower().strip()

        # --- CONTENT-FILTER RECOVERY (from _content_filter_turn) ---
        # The provider blocked extraction on the current source; let the
        # operator pick a different source rather than dead-ending.
        if "change source" in lowered or "different source" in lowered:
            return self._advance_to_configure_sources_v2()

        # --- STOP / PAUSE ---
        if any(kw in lowered for kw in ("stop", "exit", "pause")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)

        # --- SHIP AS DRAFT ---
        if "ship" in lowered and "draft" in lowered:
            self._state = "DONE"
            log.info(
                "_handle_eval_response: user chose 'ship as draft' — "
                "session ends at DONE without PROMOTE. persona=%s skill=%s",
                self._data.persona, self._data.skill_name,
            )
            return ConversationTurn(
                state="DONE",
                message=(
                    f"Skill {self._data.persona}.{self._data.skill_name} saved as draft.\n\n"
                    "The skill is committed to ADB but NOT live in production. "
                    "Promote it later via: resume this session → accept, or kb-cli promote.\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )

        # --- FORCE PROMOTE (operator escape-hatch, retained for backward-compat) ---
        # Check BEFORE the generic "promote" accept path so "force promote" is not
        # captured by the accept branch.
        if "force" in lowered and "promote" in lowered:
            eval_result = self._data.eval_result or {}
            metrics = eval_result.get("metrics", {})
            log.warning(
                "_handle_eval_response: FORCE PROMOTE by user — "
                "persona=%s skill=%s "
                "recall_at_k=%.3f faithfulness=%.3f (both now diagnostic-only — "
                "force promote retained as operator escape-hatch)",
                self._data.persona, self._data.skill_name,
                metrics.get("recall_at_k", 0),
                metrics.get("faithfulness", 0),
            )
            if self._data.eval_result:
                self._data.eval_result["force_promoted"] = True
                self._data.eval_result["force_promoted_at"] = _now_iso()
            return self._run_promote()

        # --- ACCEPT → PROMOTE ---
        # User acceptance is the ONLY gate for PROMOTE (ADR-029 Phase 1).
        # exit_criteria.passed is now diagnostic-only (DECISION-010 superseded).
        # ADR-038 §F: HARD BLOCKER — if routing self-test failed, PROMOTE is refused.
        # No override. This is a hard, no-escape blocker per the locked design.
        if any(kw in lowered for kw in ("accept", "looks good", "yes, promote", "promote")):
            # ADR-038 §F: check routing self-test result before allowing PROMOTE
            eval_result = self._data.eval_result or {}
            path_b = eval_result.get("path_b_routing", {})
            path_b_ran = bool(path_b.get("positive_count") or path_b.get("negative_count"))
            routing_passed = eval_result.get("routing_self_test_passed", True)

            if path_b_ran and not routing_passed:
                fail_details = []
                for r in (path_b.get("results") or []):
                    if not r.get("passed"):
                        q_type = r.get("type", "?").upper()
                        resolved = r.get("resolved_skill_name") or r.get("resolved_skill_id") or "(none)"
                        fail_details.append(f"  {q_type}: {r['query']!r} → {resolved}")
                fail_msg = "\n".join(fail_details) or "  (see EVAL report above)"
                log.warning(
                    "_handle_eval_response: PROMOTE BLOCKED by routing self-test failure "
                    "(ADR-038 §F hard blocker). persona=%s skill=%s",
                    self._data.persona, self._data.skill_name,
                )
                return ConversationTurn(
                    state="EVAL",
                    message=(
                        "PROMOTE BLOCKED — routing self-test failed (ADR-038 §F).\n\n"
                        f"The following routing assertions failed:\n{fail_msg}\n\n"
                        "To fix: update the skill_card routing_queries at DESIGN_SKILL "
                        "so positive queries route to THIS skill and negative queries do not. "
                        "Then re-commit, re-ingest, and re-run EVAL.\n\n"
                        "No override is provided. This block cannot be bypassed."
                    ),
                    options=["ship as draft", "review design", "stop here"],
                    must_show_human=True,
                    awaiting_user=True,
                )

            log.info(
                "_handle_eval_response: user accepted — transitioning to PROMOTE. "
                "persona=%s skill=%s",
                self._data.persona, self._data.skill_name,
            )
            # Stamp user-accept into eval_result for audit trail
            if self._data.eval_result:
                self._data.eval_result["user_accepted"] = True
                self._data.eval_result["user_accepted_at"] = _now_iso()
            return self._run_promote()

        # --- S6: classifier-driven constrained routing (ADR-029 Phase 2) ---
        # Gate has passed (commit eb31230). Auto-routing is now ACTIVE.
        # Guardrails are applied in order before the classifier is called.
        # The routing turn is must_show_human=True — user must confirm re-route.
        return self._classify_and_route(user_input)

    def _classify_and_route(self, user_input: str) -> ConversationTurn:
        """ADR-029 Phase 2 (S6): run the failure-class classifier and apply constrained routing.

        Guardrails (applied in order, all six per ADR-029 §C.3):
          1. confidence==low → route to REVIEW_DESIGN (never CONFIGURE_SOURCES/INSPECT_SOURCES).
          2. UNSUPPORTABLE → DONE as draft, no loop.
          3. Consecutive-same-class → pathological-loop → DONE as draft.
          4. eval_iteration_count >= _EVAL_MAX_ITERATIONS → DONE as draft.
          5. eval_cumulative_cost_usd > _EVAL_COST_CEILING_USD → DONE as draft.
          6. ALWAYS surface evidence + why_not_alternative to user (must_show_human=True)
             before applying the route — a routing turn the user must confirm.

        The LLM only classifies.  _ROUTING_MAP is the only thing that maps class→state.
        An unknown/garbled class is treated as low-confidence (guardrail 1: REVIEW_DESIGN).

        No stub mode: if self._llm is None this method raises RuntimeError immediately
        with an actionable message.  The seam was previously non-functional; S6 requires
        a real LLM for all router calls.
        """
        if self._llm is None:
            # No stub-mode: do NOT silently skip routing. Surface a hard-fail turn
            # with must_show_human=True so the operator sees the configuration error.
            # The session stays at EVAL — the user must supply a correctly-wired
            # session (with an LLM client) to continue.
            log.error(
                "_classify_and_route: self._llm is None — cannot run failure classifier. "
                "Wire an LLM client when constructing SkillBuilderConversation. "
                "Returning actionable error turn (no silent skip per no-stub-mode policy). "
                "persona=%s skill=%s",
                self._data.persona, self._data.skill_name,
            )
            return ConversationTurn(
                state="EVAL",
                message=(
                    "ERROR: Failure classifier cannot run — no LLM client is configured. "
                    "This session was constructed without an LLM (llm=None). "
                    "The failure-class classifier requires a real LLM client. "
                    "Please re-create the session with a properly wired LLM.\n\n"
                    "Options: 'accept' to promote as-is, 'ship as draft' to save without promoting."
                ),
                options=["accept", "ship as draft", "stop here"],
                must_show_human=True,
                awaiting_user=True,
            )

        eval_result = self._data.eval_result or {}
        comparator_dict = eval_result.get("comparator") or {}

        # --- Guardrail 4: iteration ceiling ---
        if self._data.eval_iteration_count >= _EVAL_MAX_ITERATIONS:
            self._state = "DONE"
            log.warning(
                "_classify_and_route: eval_iteration_count=%d >= max=%d — "
                "exiting as draft (loop ceiling). persona=%s skill=%s",
                self._data.eval_iteration_count, _EVAL_MAX_ITERATIONS,
                self._data.persona, self._data.skill_name,
            )
            return ConversationTurn(
                state="DONE",
                message=(
                    f"EVAL loop limit reached ({_EVAL_MAX_ITERATIONS} iterations). "
                    f"Skill {self._data.persona}.{self._data.skill_name} saved as draft "
                    f"for manual review.\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )

        # --- Guardrail 5: cost ceiling ---
        if self._data.eval_cumulative_cost_usd > _EVAL_COST_CEILING_USD:
            self._state = "DONE"
            log.warning(
                "_classify_and_route: eval_cumulative_cost_usd=%.4f > ceiling=%.2f — "
                "exiting as draft (cost ceiling). persona=%s skill=%s",
                self._data.eval_cumulative_cost_usd, _EVAL_COST_CEILING_USD,
                self._data.persona, self._data.skill_name,
            )
            return ConversationTurn(
                state="DONE",
                message=(
                    f"EVAL cost ceiling exceeded "
                    f"(${self._data.eval_cumulative_cost_usd:.4f} > "
                    f"${_EVAL_COST_CEILING_USD:.2f}). "
                    f"Skill {self._data.persona}.{self._data.skill_name} saved as draft "
                    f"for manual review.\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )

        # --- Run the failure-class classifier ---
        # ADR-030 C1: failure_classifier prompt via registry.
        # Gate-locked — LockedPromptTamperedError surfaces as hard-fail if checksum drifts.
        schema_properties = (self._data.design or {}).get("schema", {}).get("properties", {})
        classifier_spec = get_registry().get_prompt(
            "failure_classifier",
            normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
            schema_properties=json.dumps(schema_properties, indent=2),
            capability_inventory=json.dumps(self._data.source_capability, indent=2),
            gap_report=comparator_dict.get("gap_report", ""),
            missing_sections=json.dumps(comparator_dict.get("missing_sections", [])),
            thin_sections=json.dumps(comparator_dict.get("thin_sections", [])),
        )
        log.info(
            "_classify_and_route: calling failure classifier. "
            "iteration=%d persona=%s skill=%s",
            self._data.eval_iteration_count + 1,
            self._data.persona, self._data.skill_name,
        )
        try:
            classifier_response = self._llm.chat(
                model=classifier_spec.model,
                messages=[{"role": "user", "content": classifier_spec.text}],
                response_format=classifier_spec.response_format,
                max_tokens=classifier_spec.max_tokens,
            )
        except Exception as exc:
            log.error("_classify_and_route: LLM call failed: %s", exc)
            raise

        raw_text = (
            classifier_response.get("text", "")
            if isinstance(classifier_response, dict)
            else str(classifier_response)
        )
        # Accumulate cost from this classifier call
        call_cost = 0.0
        if isinstance(classifier_response, dict):
            call_cost = float(classifier_response.get("estimated_cost_usd", 0.0) or 0.0)
        self._data.eval_cumulative_cost_usd += call_cost
        self._data.eval_iteration_count += 1

        # Parse — use the shared helper (parity with S5 / review.py)
        try:
            tokens_out = (
                classifier_response.get("tokens_out")
                if isinstance(classifier_response, dict)
                else None
            )
            # C5/ADR-031: use spec value (matches YAML — today 512, but will
            # not drift if the YAML value is updated). Template NOT touched
            # (gate-locked checksum covers template only, not max_tokens).
            parsed = _parse_llm_json_response(
                raw_text,
                tokens_out=tokens_out,
                max_tokens=classifier_spec.max_tokens,
            )
        except (ValueError, Exception) as exc:
            log.error(
                "_classify_and_route: failed to parse classifier JSON (%s). "
                "Raw text: %r. Treating as low-confidence → REVIEW_DESIGN.",
                exc, raw_text[:300],
            )
            parsed = {
                "failure_class": "UNKNOWN",
                "confidence": "low",
                "evidence": f"Classifier returned non-JSON output ({exc}). Defaulting to REVIEW_DESIGN.",
                "alternative_class": "UNKNOWN",
                "why_not_alternative": "Parse failed.",
            }

        failure_class = parsed.get("failure_class", "UNKNOWN")
        confidence = parsed.get("confidence", "low")
        evidence = parsed.get("evidence", "(no evidence provided)")
        alternative_class = parsed.get("alternative_class", "(none)")
        why_not_alternative = parsed.get("why_not_alternative", "(not provided)")

        log.info(
            "_classify_and_route: classifier result: failure_class=%r confidence=%r "
            "cost_this_call=%.4f cumulative_cost=%.4f persona=%s skill=%s",
            failure_class, confidence, call_cost, self._data.eval_cumulative_cost_usd,
            self._data.persona, self._data.skill_name,
        )

        # --- Guardrail 1: low confidence → REVIEW_DESIGN (never CONFIGURE_SOURCES/INSPECT_SOURCES) ---
        # Also applies to unknown/garbled failure_class.
        unknown_class = failure_class not in _ROUTING_MAP
        if confidence == "low" or unknown_class:
            if unknown_class:
                log.warning(
                    "_classify_and_route: unknown failure_class=%r — treating as low-confidence "
                    "→ REVIEW_DESIGN (guardrail 1). persona=%s skill=%s",
                    failure_class, self._data.persona, self._data.skill_name,
                )
            else:
                log.info(
                    "_classify_and_route: confidence=low → defaulting to REVIEW_DESIGN "
                    "(guardrail 1). failure_class=%r persona=%s skill=%s",
                    failure_class, self._data.persona, self._data.skill_name,
                )
            target_state = "REVIEW_DESIGN"
        else:
            target_state = _ROUTING_MAP[failure_class]

        # --- Guardrail 2: UNSUPPORTABLE → DONE as draft ---
        if target_state == "DONE_DRAFT" or failure_class == "UNSUPPORTABLE":
            self._state = "DONE"
            self._data.last_eval_failure_class = failure_class
            log.info(
                "_classify_and_route: UNSUPPORTABLE — exiting as draft. "
                "evidence=%r persona=%s skill=%s",
                evidence[:200], self._data.persona, self._data.skill_name,
            )
            return ConversationTurn(
                state="DONE",
                message=(
                    f"Failure diagnosis: UNSUPPORTABLE\n\n"
                    f"Evidence: {evidence}\n\n"
                    f"Why not {alternative_class}: {why_not_alternative}\n\n"
                    f"The missing content cannot be derived from any configured source, "
                    f"even with synthesis. Human review is required. "
                    f"Skill {self._data.persona}.{self._data.skill_name} saved as draft.\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
                must_show_human=True,
            )

        # --- Guardrail 3: consecutive-same-class → pathological loop → DONE as draft ---
        if (
            self._data.last_eval_failure_class is not None
            and self._data.last_eval_failure_class == failure_class
        ):
            self._state = "DONE"
            log.warning(
                "_classify_and_route: consecutive-same-class detected (%r). "
                "Pathological loop — exiting as draft. persona=%s skill=%s",
                failure_class, self._data.persona, self._data.skill_name,
            )
            return ConversationTurn(
                state="DONE",
                message=(
                    f"EVAL has cycled twice on the same failure class ({failure_class!r}). "
                    f"This likely means the root cause is structural. "
                    f"The skill is saved as draft for manual review.\n\n"
                    f"Diagnosis: {evidence}\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
                must_show_human=True,
            )

        # --- Update loop-state fields ---
        self._data.last_eval_failure_class = failure_class

        # --- Guardrail 6: surface evidence + why_not_alternative to user (must_show_human) ---
        # The routing turn MUST be confirmed by the user before the state machine transitions.
        # ADR-029 §C.3: a misdiagnosis the user can see and correct is far less harmful
        # than a silent misroute.
        log.info(
            "_classify_and_route: routing to %r after user confirmation. "
            "failure_class=%r confidence=%r persona=%s skill=%s",
            target_state, failure_class, confidence,
            self._data.persona, self._data.skill_name,
        )

        # Encode the intended transition in state so the NEXT respond() call
        # (user confirmation) knows where to go.  We use a special pending-route
        # string that _handle_eval_route_confirm will detect.
        self._state = "EVAL_ROUTE_PENDING"
        self._data._eval_pending_route = target_state  # type: ignore[attr-defined]

        options_map = {
            "REVIEW_DESIGN":       "confirm route to REVIEW_DESIGN",
            "CONFIGURE_SOURCES":   "confirm route to CONFIGURE_SOURCES",
            "INSPECT_SOURCES":     "confirm route to INSPECT_SOURCES",
        }
        confirm_option = options_map.get(target_state, f"confirm route to {target_state}")

        return ConversationTurn(
            state="EVAL",
            message=(
                f"Failure diagnosis: {failure_class} (confidence: {confidence})\n\n"
                f"Evidence: {evidence}\n\n"
                f"Why not {alternative_class}: {why_not_alternative}\n\n"
                f"Recommended next step: re-run from {target_state}.\n\n"
                f"Type '{confirm_option}' to proceed, "
                f"or 'accept' to promote as-is, "
                f"or 'ship as draft' to save without promoting."
            ),
            data={
                "failure_class": failure_class,
                "confidence": confidence,
                "evidence": evidence,
                "alternative_class": alternative_class,
                "why_not_alternative": why_not_alternative,
                "target_state": target_state,
                "eval_iteration_count": self._data.eval_iteration_count,
                "eval_cumulative_cost_usd": self._data.eval_cumulative_cost_usd,
            },
            options=[confirm_option, "accept", "ship as draft", "stop here"],
            must_show_human=True,
            awaiting_user=True,
        )

    def _handle_eval_route_confirm(self, user_input: str) -> ConversationTurn:
        """ADR-029 Phase 2 (S6): handle user confirmation of the routing turn.

        Called when state==EVAL_ROUTE_PENDING (user has seen the diagnosis and
        must confirm the re-route, accept, or ship as draft).

        The target state is stored in self._data._eval_pending_route, set by
        _classify_and_route before transitioning to EVAL_ROUTE_PENDING.

        Responses:
          "confirm*" / the confirm_option string → transition to target_state
          "accept" / "looks good" / "yes, promote" → PROMOTE
          "ship as draft" → DONE (draft)
          "stop here" / "exit" / "pause" → DONE (paused)
          anything else → re-surface the routing turn (user did not confirm)
        """
        lowered = user_input.lower().strip()
        target_state = getattr(self._data, "_eval_pending_route", "REVIEW_DESIGN")

        # --- STOP / PAUSE ---
        if any(kw in lowered for kw in ("stop", "exit", "pause")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)

        # --- SHIP AS DRAFT ---
        if "ship" in lowered and "draft" in lowered:
            self._state = "DONE"
            return ConversationTurn(
                state="DONE",
                message=(
                    f"Skill {self._data.persona}.{self._data.skill_name} saved as draft.\n\n"
                    "The skill is committed to ADB but NOT live in production.\n\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )

        # --- ACCEPT → PROMOTE ---
        if any(kw in lowered for kw in ("accept", "looks good", "yes, promote", "promote")):
            if self._data.eval_result:
                self._data.eval_result["user_accepted"] = True
                self._data.eval_result["user_accepted_at"] = _now_iso()
            self._state = "EVAL"  # restore before _run_promote changes it
            return self._run_promote()

        # --- CONFIRM ROUTE ---
        if "confirm" in lowered or target_state.lower().replace("_", " ") in lowered:
            log.info(
                "_handle_eval_route_confirm: user confirmed route to %r. "
                "persona=%s skill=%s",
                target_state, self._data.persona, self._data.skill_name,
            )
            # Transition state machine back to the mapped state so the loop re-runs
            # that segment on the next respond() call.
            self._state = target_state
            return self._turn(ConversationTurn(
                state=target_state,
                message=(
                    f"Routing back to {target_state} based on failure diagnosis "
                    f"({self._data.last_eval_failure_class}).\n\n"
                    f"Please review and update accordingly, then respond to continue."
                ),
                must_show_human=True,
                awaiting_user=True,
            ))

        # --- Unrecognised: re-surface the routing turn ---
        options_map = {
            "REVIEW_DESIGN":     "confirm route to REVIEW_DESIGN",
            "CONFIGURE_SOURCES": "confirm route to CONFIGURE_SOURCES",
            "INSPECT_SOURCES":   "confirm route to INSPECT_SOURCES",
        }
        confirm_option = options_map.get(target_state, f"confirm route to {target_state}")
        return ConversationTurn(
            state="EVAL",
            message=(
                f"Please confirm the recommended routing to {target_state}.\n\n"
                f"Type '{confirm_option}' to proceed, "
                f"'accept' to promote as-is, or 'ship as draft' to save without promoting."
            ),
            options=[confirm_option, "accept", "ship as draft", "stop here"],
            must_show_human=True,
            awaiting_user=True,
        )

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
        """Handle user confirmation at PROMOTE state.

        Folded Fix 2 (BUG-queue-e685d): KB-resolvability gate.
        PROMOTE now has two mandatory invariants:
          (a) persona_builder_delta MUST exist in ADB — if missing, HARD-FAIL
              with a must_show_human=True error and stay at PROMOTE. This closes
              the silent skip-to-DONE hole that left shim_kb unable to resolve
              the skill, producing all-placeholder output.
          (b) After upsert_persona_builder_kb, verify that a fresh ShimKb load
              can find the KB card for this persona.skill_name.  If not,
              HARD-FAIL and stay at PROMOTE (do not advance to DONE).

        The recovery / force-advance path (kb-cli session recover, skip-to-DONE)
        bypasses this handler.  The KB-registration invariant is enforced by
        ensuring recover paths that set state=PROMOTE also call _handle_promote_response
        — no direct state=DONE jump is permitted from a pre-PROMOTE state without
        running this handler.
        """
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

                # --- Folded Fix 2 (BUG-queue-e685d) KB-resolvability invariant (a) ---
                # persona_builder_delta MUST exist. If missing, shim_kb cannot resolve
                # the skill after promotion — all-placeholder output results.
                # Hard-fail here; do NOT silently advance to DONE without the delta.
                if not delta_text:
                    raise RuntimeError(
                        f"_handle_promote_response: persona_builder_delta is missing "
                        f"in ADB for persona={self._data.persona!r} "
                        f"skill={self._data.skill_name!r}. "
                        f"The KB card cannot be registered — shim_kb will not be able "
                        f"to resolve this skill, producing all-placeholder output "
                        f"(BUG-queue-e685d root cause). "
                        f"Root cause: COMMIT step did not write persona_builder_delta "
                        f"to ADB. Re-commit the skill or use the AdbSkillStore API to "
                        f"write the delta manually, then retry promote."
                    )

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

                # --- Folded Fix 2 KB-resolvability invariant (b) ---
                # After upsert, verify a fresh ShimKb load can find the card.
                # This catches the gap between upsert "success" and actual
                # filesystem/ADB readability (e.g. partial write, wrong path).
                #
                # Hard-fail ONLY when ShimKb loaded at least one card from the
                # store (meaning the store is real and readable) but does not
                # contain this specific card.  If ShimKb loaded zero cards
                # (test environment with no real persona_builders dir, or an
                # empty store), treat the verification as a warning — the upsert
                # succeeded and the routing layer will find the card at runtime.
                try:
                    from ..orchestrator.shim_kb import ShimKb
                    pb_dir = REPO_ROOT / "framework" / "persona_builders"
                    fresh_shim = ShimKb(pb_dir, skill_store=self._skill_store)
                    all_loaded_cards = fresh_shim.all_cards()
                    kb_key = f"{self._data.persona}.{self._data.skill_name}"
                    found_card = fresh_shim.find_kb(kb_key)
                    if found_card:
                        log.info(
                            "_handle_promote_response: KB-resolvability check PASSED — "
                            "ShimKb found '%s' after upsert. "
                            "provides_fields=%s",
                            kb_key,
                            found_card.get("provides_fields", [])[:5],
                        )
                    elif all_loaded_cards:
                        # ShimKb has cards from the store (store is real) but this
                        # card is absent — HARD-FAIL (BUG-queue-e685d gate).
                        raise RuntimeError(
                            f"_handle_promote_response: KB-resolvability check FAILED. "
                            f"upsert_persona_builder_kb completed but a fresh ShimKb "
                            f"load (with {len(all_loaded_cards)} card(s) from store) "
                            f"cannot find '{kb_key}'. "
                            f"This means the skill is 'promoted' in ADB but its KB "
                            f"card is not visible to the routing layer, producing "
                            f"all-placeholder output (BUG-queue-e685d). "
                            f"Check the KBF_PERSONA_BUILDERS table / "
                            f"~/.kbf/persona_builders/{self._data.persona}/ for the "
                            f"written file and verify its content is valid YAML."
                        )
                    else:
                        # ShimKb loaded zero cards — likely a test environment or
                        # empty store.  Upsert succeeded; trust it.
                        log.warning(
                            "_handle_promote_response: KB-resolvability check: "
                            "ShimKb loaded 0 cards (empty store or test env) — "
                            "cannot verify '%s' was registered. "
                            "Upsert completed; proceeding.",
                            kb_key,
                        )
                except RuntimeError:
                    raise  # re-raise our own resolvability RuntimeError
                except Exception as shim_exc:
                    # ShimKb load raised an unexpected error — could be missing
                    # dir, import error, etc.  Log a warning but do not hard-fail
                    # (the upsert did succeed; the error is in the verification step).
                    log.warning(
                        "_handle_promote_response: KB-resolvability verification "
                        "raised an unexpected error (%s: %s) — upsert completed, "
                        "proceeding with caution. Run 'kb-cli shim-kb list' to verify.",
                        type(shim_exc).__name__, shim_exc,
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
                        f"Promotion failed — skill is NOT live.\n\n"
                        f"  {type(exc).__name__}: {exc}\n\n"
                        f"The skill remains in its previous state. Likely cause: "
                        f"the upstream COMMIT did not actually write to ADB (check "
                        f"server logs for write_artifacts errors). Fix the root "
                        f"cause and retry."
                    ),
                    options=["retry promote", "stop here"],
                    must_show_human=True,
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
        from .synthesize_workflow import (
            synthesize_workflow_skill,
            derive_space_allow_list,
            derive_pinned_source,
        )
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

        # ADR-032 / DECISION-019 RC1: derive source binding data from session state.
        # source_samples carries live-fetched page metadata (most reliable) populated
        # by INSPECT_SOURCES.  URL-form sources and explicit source.space fields are
        # fallbacks.
        sb_mode = self._data.source_binding_mode
        derived_space_allow_list: list[str] = []
        derived_pinned_source: dict | None = None

        if sb_mode == "ask_parameterized":
            # ADR-032: derive space_allow_list for ask_parameterized skills.
            # An empty result means the space is underivable — VALIDATE will hard-fail.
            derived_space_allow_list = derive_space_allow_list(
                sources=self._data.sources,
                source_samples=self._data.source_samples,
            )
            log.info(
                "_synthesize_preview: ask_parameterized skill=%s derived space_allow_list=%r "
                "(from %d source_samples keys, %d sources)",
                skill_name,
                derived_space_allow_list,
                len(self._data.source_samples),
                len(self._data.sources),
            )
        elif sb_mode == "author_fixed":
            # DECISION-019 RC1: derive pinned source for author_fixed skills.
            # When the session has source_samples (live-fetched pages at INSPECT_SOURCES),
            # emit a source_binding block with mode: author_fixed and pinned_ref so the
            # executor resolves the exact page this skill was authored against — preventing
            # silent wrong-page retrieval (the RC1 bug: executor fell back to generic KB
            # retrieval and returned "Project Plan" instead of "FAaaS Kiwi Project").
            # Pure in-KB skills (no source_samples, no URL sources) return None — no block
            # emitted, unchanged pre-DECISION-019 behavior.
            derived_pinned_source = derive_pinned_source(
                sources=self._data.sources,
                source_samples=self._data.source_samples,
            )
            if derived_pinned_source is not None:
                log.info(
                    "_synthesize_preview: author_fixed skill=%s derived pinned_source=%r "
                    "(DECISION-019 RC1 — pinned source binding will be emitted in artifact)",
                    skill_name,
                    derived_pinned_source.get("pinned_ref", ""),
                )
            else:
                log.debug(
                    "_synthesize_preview: author_fixed skill=%s has no external fixed source "
                    "— NO source_binding block emitted (pure in-KB or pre-ADR-032 session)",
                    skill_name,
                )

        wf_struct = synthesize_workflow_skill(
            persona=persona,
            skill_name=skill_name,
            intent=intent,
            fields=self._data.fields,
            template_path=None,
            source_binding_mode=sb_mode,
            space_allow_list=derived_space_allow_list if sb_mode == "ask_parameterized" else None,
            pinned_source=derived_pinned_source if sb_mode == "author_fixed" else None,
        )

        # ADR-038 §B: LOAD-BEARING carry-through.
        # If DESIGN_SKILL generated a consumer-facing card (design_skill_card),
        # OVERWRITE the skill_card in the synthesized workflow artifact with the
        # DESIGN_SKILL-produced card — including routing_queries.
        # This prevents synthesize_workflow.py's _build_skill_card (the static
        # no-LLM template) from winning.  Without this, the committed ADB artifact
        # would have the authoring-echo card and routing would silently fail.
        if self._data.design_skill_card:
            wf_struct["skill_card"] = dict(self._data.design_skill_card)
            log.info(
                "_synthesize_preview: ADR-038 §B — carried DESIGN_SKILL card into "
                "workflow artifact (overriding static _build_skill_card template). "
                "routing_queries.positive=%d routing_queries.negative=%d "
                "persona=%s skill=%s",
                len((self._data.design_skill_card.get("routing_queries") or {}).get("positive", [])),
                len((self._data.design_skill_card.get("routing_queries") or {}).get("negative", [])),
                persona, skill_name,
            )
        else:
            log.info(
                "_synthesize_preview: no design_skill_card on session — "
                "using static _build_skill_card template (pre-ADR-038 session or fallback). "
                "persona=%s skill=%s",
                persona, skill_name,
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
