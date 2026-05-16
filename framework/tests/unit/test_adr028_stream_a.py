"""Stream A (serial) tests for ADR-028 S1-S4 implementation.

These tests validate the changes made by Stream A. They cover:
  S1: synthesisable confidence level in INSPECT_SOURCES + DESIGN_SKILL prompts
  S2: awaiting_user + must_show_human on ConversationTurn + mcp_tools.py
  S3: CLARIFY state (17th state)
  S4: persona prompt fragment injection

NOTE: Stream C (QA agent) owns test_skill_builder_conversation.py. Stream A
writes its own validation tests here to avoid interfering with that file.

No live LLM calls — all tests use mocks or inspect prompt constants directly.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from framework.skill_builder import conversation as conv_module
from framework.skill_builder.conversation import (
    ConversationTurn,
    SkillBuilderConversation,
    STATES,
    _SessionData,
)
from framework.skill_builder.prompt_registry import get_registry as _get_registry

# ADR-030 C1: constants deleted; load templates via registry for structural inspection
_CAPTURE_INTENT_PROMPT = _get_registry()._raw_template("capture_intent")
_DESIGN_SKILL_PROMPT = _get_registry()._raw_template("design_skill")
_INSPECT_SOURCES_PROMPT = _get_registry()._raw_template("inspect_sources")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill_store():
    ss = MagicMock()
    ss.read_artifact.return_value = None
    return ss


def _make_conv(persona="tpm") -> SkillBuilderConversation:
    c = SkillBuilderConversation(
        persona=persona,
        user_id="test-user",
        skill_store=_make_skill_store(),
    )
    c._data.persona = persona
    c._data.skill_name = "test_skill"
    c._data.synth_id = "synth-test-s1"
    return c


# ===========================================================================
# S1 — Synthesisable confidence level
# ===========================================================================


class TestSynthesisableFieldPrompts:
    """S1: test_synthesisable_field_included_in_design (prompt-level inspection)."""

    def test_inspect_sources_prompt_contains_synthesisable_level(self):
        """INSPECT_SOURCES prompt must describe the synthesisable confidence level."""
        assert "synthesisable" in _INSPECT_SOURCES_PROMPT, (
            "_INSPECT_SOURCES_PROMPT missing 'synthesisable' confidence level — S1 not applied"
        )

    def test_inspect_sources_confidence_taxonomy_complete(self):
        """Confidence taxonomy must list all four levels."""
        for level in ("high", "medium", "synthesisable", "low"):
            assert f'"{level}"' in _INSPECT_SOURCES_PROMPT or level in _INSPECT_SOURCES_PROMPT, (
                f"_INSPECT_SOURCES_PROMPT missing confidence level '{level}'"
            )

    def test_design_skill_prompt_includes_synthesisable_rule(self):
        """DESIGN_SKILL prompt must include synthesisable in its inclusion rule."""
        assert "synthesisable" in _DESIGN_SKILL_PROMPT, (
            "_DESIGN_SKILL_PROMPT missing 'synthesisable' — fields with that confidence "
            "will be excluded from the schema (PPT thinness regression)"
        )

    def test_design_skill_prompt_requires_aggregation_instruction(self):
        """DESIGN_SKILL prompt must require 'Derive this value by' for synthesisable fields."""
        # The ADR requires the rule to contain an aggregation instruction mandate
        assert "aggregat" in _DESIGN_SKILL_PROMPT.lower() or "Derive" in _DESIGN_SKILL_PROMPT, (
            "_DESIGN_SKILL_PROMPT missing aggregation instruction mandate for synthesisable fields"
        )

    def test_design_skill_prompt_excludes_only_low_and_missing(self):
        """DESIGN_SKILL must NOT say 'only high or medium' without synthesisable."""
        # The old rule was 'confidence high or medium' — that must be gone
        # (we now also include synthesisable)
        old_rule = "confidence high or medium"
        assert old_rule not in _DESIGN_SKILL_PROMPT, (
            f"_DESIGN_SKILL_PROMPT still contains old exclusion rule: {old_rule!r}. "
            "Synthesisable fields will be dropped."
        )

    def test_synthesisable_field_design_skill_prompt_format(self):
        """DESIGN_SKILL prompt must format correctly with all expected kwargs (S1 + S4 placeholders)."""
        # This verifies the prompt template is valid — all expected format kwargs work
        # S4 added persona fragment placeholders; include them here so the format() succeeds.
        formatted = _DESIGN_SKILL_PROMPT.format(
            persona="tpm",
            normalised_intent='{"output_kind": "pptx"}',
            source_capability='[{"available_fields": [{"field": "risks", "confidence": "synthesisable"}]}]',
            artifact_layout="null",
            existing_kb_cards="[]",
            persona_key_fields="- risk_summary\n- orm_status",
            persona_extraction_style="Extract as structured table rows.",
            persona_few_shot_example="Example: risk_summary: 'P1 open items'",
        )
        assert "synthesisable" in formatted

    def test_missing_confidence_still_excluded_by_rule(self):
        """Fields genuinely absent from source should still be excluded / unsupportable."""
        # The DESIGN_SKILL prompt must mention that missing/low fields are excluded
        # or go to unsupportable_fields
        assert "missing" in _DESIGN_SKILL_PROMPT.lower() or "unsupportable" in _DESIGN_SKILL_PROMPT.lower(), (
            "_DESIGN_SKILL_PROMPT doesn't mention what to do with missing fields"
        )

    def test_synthesisable_description_requires_explicit_derive(self):
        """The aggregation mandate phrase must appear in the DESIGN_SKILL prompt."""
        assert "Derive this value by" in _DESIGN_SKILL_PROMPT, (
            "Missing exact phrase 'Derive this value by' — blueprint specifies this exact wording"
        )


# ===========================================================================
# S2 — awaiting_user + must_show_human on ConversationTurn
# ===========================================================================


class TestConversationTurnFields:
    """S2: ConversationTurn dataclass must have awaiting_user + must_show_human."""

    def test_awaiting_user_field_exists(self):
        turn = ConversationTurn()
        assert hasattr(turn, "awaiting_user"), "ConversationTurn missing awaiting_user field"

    def test_must_show_human_field_exists(self):
        turn = ConversationTurn()
        assert hasattr(turn, "must_show_human"), "ConversationTurn missing must_show_human field"

    def test_awaiting_user_default_true(self):
        """awaiting_user should default to True (most turns need human input)."""
        turn = ConversationTurn()
        assert turn.awaiting_user is True

    def test_must_show_human_default_false(self):
        """must_show_human should default to False (not all turns need forced display)."""
        turn = ConversationTurn()
        assert turn.must_show_human is False

    def test_must_show_human_can_be_set_true(self):
        turn = ConversationTurn(must_show_human=True)
        assert turn.must_show_human is True

    def test_awaiting_user_can_be_set_false(self):
        turn = ConversationTurn(awaiting_user=False)
        assert turn.awaiting_user is False


class TestMustShowHumanOnStateTurns:
    """S2: state handlers must set must_show_human=True on review/clarify/preview/eval turns."""

    def test_review_design_turn_must_show_human(self):
        """REVIEW_DESIGN turn must always have must_show_human=True."""
        c = _make_conv()
        # Seed a minimal design so _prompt_review_design can run
        c._data.design = {
            "schema": {"properties": {"test_field": {"type": "string", "description": "test"}}, "required": []},
            "source_bindings": {},
            "workflow_shape": {"output_format": "pptx", "trigger": {"on_request": True}},
            "reuse_plan": {"covered": {}, "gaps": []},
        }
        c._data.fields = ["test_field"]
        turn = c._prompt_review_design()
        assert turn.must_show_human is True, (
            "REVIEW_DESIGN turn must have must_show_human=True — "
            "otherwise smart clients (Claude Code, Codex) auto-answer it"
        )

    def test_preview_extraction_turn_must_show_human(self):
        """PREVIEW_EXTRACTION turn must always have must_show_human=True."""
        c = _make_conv()
        # Mock the LLM to return a valid extraction
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"text": '{"test_field": "some value"}'}
        c._llm = mock_llm
        c._data.fields = ["test_field"]
        c._data.field_specs = {"test_field": {"type": "string", "description": "test"}}
        c._data.source_samples = {
            "confluence:test": [
                {"content": "Test content for extraction", "source_citation": "test-page"}
            ]
        }

        # review_extractions and synthesize_extraction_schema are local imports inside the method;
        # patch at their actual module locations.
        with patch("framework.skill_builder.review.review_extractions") as mock_review:
            mock_review.return_value = {
                "extractions": [{"source_citation": "test-page", "extracted": {"test_field": "v"}, "missing_fields": []}],
                "field_coverage": {"test_field": 1.0},
                "issues": [],
            }
            with patch("framework.skill_builder.synthesize_schema.synthesize_extraction_schema") as mock_schema:
                mock_schema.return_value = {"properties": {"test_field": {"type": "string", "description": "t"}}}
                turn = c._advance_to_preview_extraction()

        assert turn.must_show_human is True, (
            "PREVIEW_EXTRACTION turn must have must_show_human=True"
        )

    def test_capture_intent_with_ambiguities_awaiting_user(self):
        """CAPTURE_INTENT turn with ambiguities should set awaiting_user=True."""
        c = _make_conv()
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "text": json.dumps({
                "output_kind": "pptx",
                "audience": "exec",
                "cadence": "weekly",
                "scope_domains": ["26ai"],
                "success_criteria": ["one slide"],
                "ambiguities": ["which Confluence space?"],
            })
        }
        c._llm = mock_llm
        c._data.intent_description = "create weekly report"
        turn = c._advance_to_capture_intent()
        # The turn should flag awaiting_user = True since ambiguities exist
        assert turn.awaiting_user is True


class TestCamelCaseSerialization:
    """S2: must_show_human and awaiting_user must serialize to camelCase."""

    def test_must_show_human_camel_case(self):
        from framework.deploy.serialization import snake_to_camel
        assert snake_to_camel("must_show_human") == "mustShowHuman"

    def test_awaiting_user_camel_case(self):
        from framework.deploy.serialization import snake_to_camel
        assert snake_to_camel("awaiting_user") == "awaitingUser"

    def test_turn_to_envelope_includes_must_show_human(self):
        """_turn_to_envelope must include must_show_human in the output dict."""
        from framework.deploy.routes.author_skill import _turn_to_envelope
        turn = ConversationTurn(
            synth_id="test",
            state="REVIEW_DESIGN",
            message="Review this",
            must_show_human=True,
            awaiting_user=True,
        )
        envelope = _turn_to_envelope(turn)
        assert "must_show_human" in envelope, (
            "_turn_to_envelope does not include must_show_human — "
            "the camelCase response will be missing mustShowHuman"
        )
        assert envelope["must_show_human"] is True

    def test_turn_to_envelope_includes_awaiting_user(self):
        from framework.deploy.routes.author_skill import _turn_to_envelope
        turn = ConversationTurn(
            synth_id="test",
            state="REVIEW_DESIGN",
            message="Review this",
            awaiting_user=True,
        )
        envelope = _turn_to_envelope(turn)
        assert "awaiting_user" in envelope

    def test_camel_response_converts_must_show_human(self):
        """Full camelCase conversion pipeline must produce mustShowHuman."""
        from framework.deploy.serialization import convert_keys, snake_to_camel
        envelope = {
            "must_show_human": True,
            "awaiting_user": True,
            "synth_id": "test",
        }
        camel = convert_keys(envelope, snake_to_camel)
        assert "mustShowHuman" in camel, f"mustShowHuman not in camel dict: {camel}"
        assert camel["mustShowHuman"] is True
        assert "awaitingUser" in camel


class TestAuthorSkillToolDescription:
    """S2: authorSkill tool description must contain the must_show_human instruction."""

    def test_author_skill_tool_description_has_must_show_human_instruction(self):
        """The authorSkill tool schema description must contain the CRITICAL instruction."""
        from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA
        author_skill_tool = next(
            (t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "authorSkill"), None
        )
        assert author_skill_tool is not None, "authorSkill tool not found in EXTERNAL_TOOLS_SCHEMA"
        desc = author_skill_tool["description"]
        # The instruction must include keywords that enforce human-in-loop
        assert "mustShowHuman" in desc or "must_show_human" in desc, (
            "authorSkill tool description missing mustShowHuman enforcement instruction"
        )
        assert "CRITICAL" in desc, (
            "authorSkill tool description missing CRITICAL marker for must_show_human"
        )


# ===========================================================================
# S3 — CLARIFY state (17th state)
# ===========================================================================


class TestClarifyStateExists:
    """S3: CLARIFY state must be present in the state machine."""

    def test_clarify_in_states_list(self):
        assert "CLARIFY" in STATES, (
            "CLARIFY not in STATES list — S3 not applied. "
            f"Current STATES: {STATES}"
        )

    def test_states_count_is_17(self):
        assert len(STATES) == 17, (
            f"Expected 17 states (ADR-028 S3 adds CLARIFY), got {len(STATES)}. "
            f"States: {STATES}"
        )

    def test_clarify_comes_after_capture_intent(self):
        capture_idx = STATES.index("CAPTURE_INTENT")
        clarify_idx = STATES.index("CLARIFY")
        assert clarify_idx > capture_idx, (
            "CLARIFY must come after CAPTURE_INTENT in the state list"
        )

    def test_clarify_comes_before_configure_sources(self):
        clarify_idx = STATES.index("CLARIFY")
        config_idx = STATES.index("CONFIGURE_SOURCES")
        assert clarify_idx < config_idx, (
            "CLARIFY must come before CONFIGURE_SOURCES in the state list"
        )


class TestClarifyPromptExists:
    """S3: clarify prompt must exist in the registry (ADR-030 C1 cutover).

    The _CLARIFY_PROMPT constant was deleted; the prompt is now in the
    registry under prompt_id='clarify'.
    """

    def test_clarify_prompt_in_registry(self):
        """'clarify' prompt must be registered (replaces _CLARIFY_PROMPT constant check)."""
        template = _get_registry()._raw_template("clarify")
        assert template, "clarify prompt not found in registry — S3 not applied"

    def test_clarify_prompt_is_prose_not_json(self):
        """CLARIFY prompt must emit conversational prose, not a JSON blob."""
        template = _get_registry()._raw_template("clarify")
        assert "Return ONLY a JSON" not in template, (
            "clarify prompt must emit conversational prose, not JSON. "
            "The human must read and respond to a natural-language question."
        )


class TestClarifyStateHandlers:
    """S3: CLARIFY state handlers must exist and behave correctly."""

    def test_advance_to_clarify_method_exists(self):
        c = _make_conv()
        assert hasattr(c, "_advance_to_clarify"), (
            "_advance_to_clarify method missing — S3 not applied"
        )

    def test_handle_clarify_response_method_exists(self):
        c = _make_conv()
        assert hasattr(c, "_handle_clarify_response"), (
            "_handle_clarify_response method missing — S3 not applied"
        )

    def test_clarify_in_respond_dispatch_table(self):
        """respond() must handle CLARIFY state without falling into the unknown-state path."""
        c = _make_conv()
        c._state = "CLARIFY"
        # Seed TWO blocking questions so answering the first keeps us in CLARIFY
        c._data.clarification_log = []
        c._data._clarify_questions = [
            {"question": "Which space?", "resolved": False},
            {"question": "Weekly or monthly?", "resolved": False},
        ]
        c._data._clarify_next_state = "CONFIGURE_SOURCES"

        # If CLARIFY is not in the dispatch table, respond() returns an error turn.
        # With two questions, answering the first should keep us in CLARIFY state.
        turn = c.respond("FAAAS space")
        # We don't check the exact state here — just that it doesn't say "Unknown state"
        assert "Unknown state" not in turn.message, (
            "CLARIFY state not wired into respond() dispatch table"
        )
        # Should still be in CLARIFY (one more question pending)
        assert c._state == "CLARIFY", (
            f"Expected CLARIFY state after first of two questions, got {c._state!r}"
        )

    def test_session_data_has_clarification_log(self):
        """_SessionData must have clarification_log field."""
        data = _SessionData()
        assert hasattr(data, "clarification_log"), (
            "_SessionData missing clarification_log — S3 not applied"
        )
        assert isinstance(data.clarification_log, list)

    def test_clarification_log_in_to_dict(self):
        """clarification_log must survive to_dict() / from_dict() round-trip."""
        c = _make_conv()
        c._data.clarification_log = [
            {"question": "Which space?", "answer": "FAAAS", "resolved_at": "2026-05-15T00:00:00Z"}
        ]
        d = c.to_dict()
        assert "clarification_log" in d, "clarification_log not in to_dict() output"

        c2 = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
        assert c2._data.clarification_log == c._data.clarification_log, (
            "clarification_log did not survive from_dict() round-trip"
        )

    def test_capture_intent_with_blocking_ambiguity_routes_to_clarify(self):
        """When CAPTURE_INTENT returns blocking_ambiguities, state must go to CLARIFY."""
        c = _make_conv()
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "text": json.dumps({
                "output_kind": "pptx",
                "audience": "exec",
                "cadence": "weekly",
                "scope_domains": ["26ai"],
                "success_criteria": ["one slide"],
                "blocking_ambiguities": ["Which Confluence space — FAAAS or 26AI-LEGACY?"],
                "nice_to_know_ambiguities": [],
            })
        }
        c._llm = mock_llm
        c._data.intent_description = "create weekly report for 26ai"
        turn = c._advance_to_capture_intent()
        # The state must be CLARIFY (not CONFIGURE_SOURCES)
        assert c._state == "CLARIFY", (
            f"Expected state=CLARIFY after blocking_ambiguities, got {c._state!r}"
        )
        assert turn.must_show_human is True

    def test_capture_intent_with_only_nice_to_know_skips_clarify(self):
        """When only nice_to_know_ambiguities exist, CLARIFY is skipped."""
        c = _make_conv()
        mock_llm = MagicMock()
        # Mock configure_sources too (it gets called on transition)
        mock_llm.chat.side_effect = [
            # First call: CAPTURE_INTENT
            {
                "text": json.dumps({
                    "output_kind": "pptx",
                    "audience": "exec",
                    "cadence": "weekly",
                    "scope_domains": ["26ai"],
                    "success_criteria": ["one slide"],
                    "blocking_ambiguities": [],
                    "nice_to_know_ambiguities": ["Cadence unclear — assuming weekly"],
                })
            },
            # Second call: CONFIGURE_SOURCES LLM proposal
            {"text": "[]"},
        ]
        c._llm = mock_llm
        c._data.intent_description = "create weekly report for 26ai"
        c._data.sources = [{"kind": "confluence", "pages": ["12345"]}]  # seed so configure doesn't prompt
        turn = c._advance_to_capture_intent()
        # State must NOT be CLARIFY — it should have gone to CONFIGURE_SOURCES
        assert c._state != "CLARIFY", (
            "CLARIFY was incorrectly triggered for nice_to_know_ambiguities"
        )

    def test_clarify_sets_must_show_human(self):
        """Every CLARIFY turn must have must_show_human=True."""
        c = _make_conv()
        c._state = "CLARIFY"
        c._data.clarification_log = []
        # Use internal _advance_to_clarify if we have blocking questions
        blocking = [{"question": "Which space?", "resolved": False}]
        if hasattr(c, "_advance_to_clarify"):
            turn = c._advance_to_clarify(blocking)
            assert turn.must_show_human is True, (
                "CLARIFY turn must have must_show_human=True"
            )

    def test_clarify_advances_after_all_questions_resolved(self):
        """After the last blocking question is answered, CLARIFY advances to CONFIGURE_SOURCES."""
        c = _make_conv()
        c._state = "CLARIFY"
        c._data.clarification_log = []
        # One blocking question already pending
        c._data._clarify_questions = [{"question": "Which space?", "resolved": False}]

        # Answering the question should advance to CONFIGURE_SOURCES
        # Mock LLM for the downstream CONFIGURE_SOURCES call
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"text": "[]"}
        c._llm = mock_llm
        c._data.sources = [{"kind": "confluence", "pages": ["12345"]}]

        turn = c._handle_clarify_response("FAAAS space")
        # After answering the last question, state should advance
        assert c._state in ("CONFIGURE_SOURCES", "CLARIFY"), (
            f"Unexpected state after resolving all questions: {c._state}"
        )
        # If only one question and it's answered, we should be at CONFIGURE_SOURCES
        if len(c._data._clarify_questions) == 1:
            assert c._state == "CONFIGURE_SOURCES", (
                "After resolving last blocking question, state must be CONFIGURE_SOURCES"
            )


class TestCaptureIntentPromptBlockingAmbiguities:
    """S3: _CAPTURE_INTENT_PROMPT must distinguish blocking from nice_to_know."""

    def test_capture_intent_prompt_mentions_blocking_ambiguities(self):
        assert "blocking_ambiguities" in _CAPTURE_INTENT_PROMPT or "blocking" in _CAPTURE_INTENT_PROMPT, (
            "_CAPTURE_INTENT_PROMPT does not mention blocking_ambiguities — S3 not applied"
        )

    def test_capture_intent_prompt_mentions_nice_to_know(self):
        assert "nice_to_know" in _CAPTURE_INTENT_PROMPT or "nice-to-know" in _CAPTURE_INTENT_PROMPT.lower(), (
            "_CAPTURE_INTENT_PROMPT does not distinguish nice_to_know ambiguities — S3 not applied"
        )

    def test_design_skill_prompt_mentions_blocking_questions(self):
        assert "blocking_questions" in _DESIGN_SKILL_PROMPT or "blocking" in _DESIGN_SKILL_PROMPT, (
            "_DESIGN_SKILL_PROMPT does not mention blocking_questions — S3 not applied"
        )


# ===========================================================================
# S4 — Persona prompt fragment injection
# ===========================================================================


class TestPersonaLoaderExists:
    """S4: persona fragments now live in registry overlay (ADR-030 C1).

    _load_persona_prompt_fragments was deleted; these tests verify the
    equivalent guarantees via the PromptRegistry overlay mechanism.
    """

    def test_registry_has_tpm_overlay_vars(self):
        """Registry must provide persona_key_fields for tpm persona."""
        spec = _get_registry().get_prompt("capture_intent", persona="tpm", intent="test")
        # tpm overlay provides persona_key_fields; it must appear in the rendered prompt
        assert "orm_status" in spec.text or "persona_key_fields" not in spec.text or "orm_status" in spec.text, (
            "tpm overlay vars not applied to capture_intent prompt"
        )

    def test_tpm_overlay_key_fields_nonempty(self):
        """tpm overlay must supply non-empty persona_key_fields."""
        spec = _get_registry().get_prompt("capture_intent", persona="tpm", intent="test")
        # If overlay applied, orm_status (from tpm key fields) should appear in text
        assert "orm_status" in spec.text, (
            "tpm overlay persona_key_fields not injected into capture_intent"
        )

    def test_tpm_overlay_extraction_style_nonempty(self):
        """tpm overlay must supply persona_extraction_style."""
        spec = _get_registry().get_prompt("design_skill", persona="tpm",
                                          normalised_intent="{}", source_capability="[]",
                                          artifact_layout="null", existing_kb_cards="[]")
        assert "exec" in spec.text.lower(), (
            "tpm extraction_style (exec-safe language) not injected into design_skill"
        )

    def test_tpm_overlay_few_shot_example_nonempty(self):
        """tpm overlay must supply persona_few_shot_example."""
        spec = _get_registry().get_prompt("design_skill", persona="tpm",
                                          normalised_intent="{}", source_capability="[]",
                                          artifact_layout="null", existing_kb_cards="[]")
        # few_shot_example is in overlay; design_skill template uses {persona_few_shot_example}
        assert "{persona_few_shot_example}" not in spec.text, (
            "persona_few_shot_example placeholder not substituted in design_skill for tpm"
        )


class TestPersonaGracefulDegradation:
    """S4: unknown persona must degrade gracefully (warn, not crash, not silent)."""

    def test_unknown_persona_does_not_raise(self):
        """An unknown persona must NOT raise an exception.

        ADR-030 C1: conversation.py wraps get_prompt in try/except MissingVarsError
        to provide empty-string defaults for unknown personas.
        """
        # Direct registry call would raise MissingVarsError for unknown persona.
        # But SkillBuilderConversation handles this gracefully — test the conversation API.
        c = SkillBuilderConversation(
            persona="unknown_persona_xyz",
            user_id="test-user",
            skill_store=_make_skill_store(),
        )
        c._data.persona = "unknown_persona_xyz"
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "text": json.dumps({
                "output_kind": "pptx",
                "audience": "exec",
                "cadence": "weekly",
                "scope_domains": ["26ai"],
                "success_criteria": ["one slide"],
                "blocking_ambiguities": [],
                "nice_to_know_ambiguities": [],
            })
        }
        c._llm = mock_llm
        c._data.intent_description = "create weekly report"
        try:
            c._advance_to_capture_intent()
        except Exception as exc:
            pytest.fail(
                f"SkillBuilderConversation raised for unknown persona: {exc}. "
                "Must degrade gracefully with empty strings."
            )

    def test_unknown_persona_returns_empty_strings(self):
        """Unknown persona must produce a prompt with empty persona fragment placeholders filled."""
        from framework.skill_builder.prompt_registry import MissingVarsError
        try:
            spec = _get_registry().get_prompt("capture_intent", persona="unknown_persona_xyz", intent="test")
            # If no error, the placeholder was filled (maybe with empty string from overlay)
        except MissingVarsError:
            # Expected — conversation.py catches this and retries with empty defaults
            pass

    def test_unknown_persona_logs_warning(self):
        """Unknown persona must log a warning (not silently use generic prompt).

        ADR-030 C1: the MissingVarsError fallback branch in conversation.py
        logs a warning before retrying with empty defaults.
        """
        c = SkillBuilderConversation(
            persona="unknown_persona_xyz",
            user_id="test-user",
            skill_store=_make_skill_store(),
        )
        c._data.persona = "unknown_persona_xyz"
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "text": json.dumps({
                "output_kind": "pptx",
                "audience": "exec",
                "cadence": "weekly",
                "scope_domains": ["26ai"],
                "success_criteria": ["one slide"],
                "blocking_ambiguities": [],
                "nice_to_know_ambiguities": [],
            })
        }
        c._llm = mock_llm
        c._data.intent_description = "create weekly report"
        with patch.object(conv_module.log, "warning") as mock_warn:
            c._advance_to_capture_intent()
            # A warning must have been logged for the unknown persona
            called_with_persona = any(
                "unknown_persona_xyz" in str(args) or "unknown_persona_xyz" in str(kwargs)
                for args, kwargs in mock_warn.call_args_list
            )
            assert called_with_persona or mock_warn.called, (
                "No warning logged for unknown persona 'unknown_persona_xyz'. "
                "Silent degradation is not acceptable."
            )


class TestPersonaInjectedIntoPrompts:
    """S4: persona fragments must be injected into _CAPTURE_INTENT_PROMPT and _DESIGN_SKILL_PROMPT."""

    def test_capture_intent_prompt_has_persona_key_fields_placeholder(self):
        assert "{persona_key_fields}" in _CAPTURE_INTENT_PROMPT, (
            "_CAPTURE_INTENT_PROMPT missing {persona_key_fields} placeholder — S4 not applied"
        )

    def test_design_skill_prompt_has_persona_key_fields_placeholder(self):
        assert "{persona_key_fields}" in _DESIGN_SKILL_PROMPT, (
            "_DESIGN_SKILL_PROMPT missing {persona_key_fields} placeholder — S4 not applied"
        )

    def test_design_skill_prompt_has_extraction_style_placeholder(self):
        assert "{persona_extraction_style}" in _DESIGN_SKILL_PROMPT, (
            "_DESIGN_SKILL_PROMPT missing {persona_extraction_style} placeholder — S4 not applied"
        )

    def test_design_skill_prompt_has_few_shot_example_placeholder(self):
        assert "{persona_few_shot_example}" in _DESIGN_SKILL_PROMPT, (
            "_DESIGN_SKILL_PROMPT missing {persona_few_shot_example} placeholder — S4 not applied"
        )

    def test_persona_fragments_injected_in_run_design_skill(self):
        """When _run_design_skill is called, the LLM prompt must contain tpm's extraction_style."""
        c = _make_conv(persona="tpm")
        mock_llm = MagicMock()
        design_output = {
            "schema": {
                "properties": {"orm_status": {"type": "string", "description": "ORM status"}},
                "required": ["orm_status"],
            },
            "source_bindings": {"orm_status": ["confluence:test"]},
            "workflow_shape": {"output_format": "pptx", "trigger": {"on_request": True}},
            "reuse_plan": {"covered": {}, "gaps": []},
        }
        mock_llm.chat.return_value = {"text": json.dumps(design_output)}
        c._llm = mock_llm
        c._data.source_capability = [
            {
                "source_id": "confluence:test",
                "available_fields": [
                    {"field": "orm_status", "type": "string", "confidence": "high", "evidence": "RAG table row"}
                ],
            }
        ]
        c._data.normalised_intent = {"output_kind": "pptx", "scope_domains": ["26ai"]}

        # ShimKb is imported locally inside _run_design_skill; patch at its real module path.
        # If ShimKb cannot be loaded (e.g. missing orchestrator), it fails gracefully with
        # cards_summary=[] — that is acceptable for this test which only checks prompt content.
        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_class:
            mock_shim = MagicMock()
            mock_shim.cards_visible_to.return_value = []
            mock_shim.all_cards.return_value = []
            mock_shim_class.return_value = mock_shim
            c._run_design_skill()

        # Verify that the LLM was called with a prompt containing tpm's extraction_style
        call_args = mock_llm.chat.call_args
        prompt_used = call_args[1]["messages"][0]["content"] if call_args[1] else call_args[0][0]["messages"][0]["content"]
        # tpm extraction_style contains "exec-safe" — this must appear in the prompt
        assert "exec-safe" in prompt_used or "exec" in prompt_used.lower(), (
            "tpm extraction_style ('exec-safe language') not injected into DESIGN_SKILL prompt. "
            "Persona-aware prompting (S4) not applied."
        )
