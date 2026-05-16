"""ADR-032 Phase-2a tests — P1-C (source_binding_mode capture + CLARIFY) and
P1-D (VALIDATE source_binding contract check).

Coverage (P1-C):
  _SessionData new fields:
    - source_binding_mode and source_binding_signal fields exist with defaults
    - to_dict persists them; from_dict restores them
    - pre-ADR-032 dict (missing keys) defaults safely (backward-compat regression)
    - _advance_to_capture_intent persists source_binding_mode from LLM output
    - ask_parameterized -> blocking CLARIFY fires (no auto-advance)
    - author_fixed -> no blocking CLARIFY added, advances
    - ambiguous -> blocking CLARIFY fires, resolves on answer to author_fixed or ask_parameterized

No live LLM calls — all tests use mocks.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from framework.skill_builder.conversation import (
    SkillBuilderConversation,
    _SessionData,
    _check_confluence_adapter_available,
    _validate_source_binding_contract,
)


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
    c._data.synth_id = "synth-adr032-test"
    return c


def _mock_llm_for_capture_intent(source_binding_mode: str, extra_blocking=None):
    """Return a mock LLM that emits the given source_binding_mode from capture_intent."""
    blocking = list(extra_blocking or [])
    if source_binding_mode in ("ask_parameterized", "ambiguous"):
        blocking.append(
            "Is the source page fixed at authoring time or supplied by "
            "the consumer at query time?"
        )
    mock_llm = MagicMock()
    mock_llm.chat.return_value = {
        "text": json.dumps({
            "output_kind": "email",
            "audience": "team",
            "cadence": "on_request",
            "scope_domains": ["project_tracking"],
            "success_criteria": ["accurate email"],
            "blocking_ambiguities": blocking,
            "nice_to_know_ambiguities": [],
            "source_binding_mode": source_binding_mode,
            "source_binding_signal": "accept a Confluence page" if source_binding_mode != "author_fixed" else "",
        })
    }
    return mock_llm


# ===========================================================================
# P1-C — _SessionData new fields
# ===========================================================================


class TestSessionDataNewFields:
    """ADR-032 P1-C: _SessionData must have source_binding_mode + source_binding_signal."""

    def test_source_binding_mode_field_exists(self):
        data = _SessionData()
        assert hasattr(data, "source_binding_mode"), (
            "_SessionData missing source_binding_mode field (ADR-032 P1-C)"
        )

    def test_source_binding_signal_field_exists(self):
        data = _SessionData()
        assert hasattr(data, "source_binding_signal"), (
            "_SessionData missing source_binding_signal field (ADR-032 P1-C)"
        )

    def test_source_binding_mode_default_is_author_fixed(self):
        """Default must be 'author_fixed' per ADR-032 §H migration rule."""
        data = _SessionData()
        assert data.source_binding_mode == "author_fixed", (
            f"Default source_binding_mode should be 'author_fixed', got {data.source_binding_mode!r}"
        )

    def test_source_binding_signal_default_is_empty(self):
        data = _SessionData()
        assert data.source_binding_signal == "", (
            f"Default source_binding_signal should be '', got {data.source_binding_signal!r}"
        )


# ===========================================================================
# P1-C — to_dict / from_dict round-trip
# ===========================================================================


class TestSourceBindingPersistence:
    """ADR-032 P1-C: source_binding_mode + signal must survive to_dict/from_dict."""

    def test_to_dict_includes_source_binding_mode(self):
        c = _make_conv()
        c._data.source_binding_mode = "ask_parameterized"
        d = c.to_dict()
        assert "source_binding_mode" in d, "source_binding_mode not in to_dict() output"
        assert d["source_binding_mode"] == "ask_parameterized"

    def test_to_dict_includes_source_binding_signal(self):
        c = _make_conv()
        c._data.source_binding_signal = "accept a Confluence page"
        d = c.to_dict()
        assert "source_binding_signal" in d, "source_binding_signal not in to_dict() output"
        assert d["source_binding_signal"] == "accept a Confluence page"

    def test_from_dict_restores_source_binding_mode(self):
        c = _make_conv()
        c._data.source_binding_mode = "ask_parameterized"
        c._data.source_binding_signal = "for a given page"
        d = c.to_dict()

        c2 = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
        assert c2._data.source_binding_mode == "ask_parameterized", (
            "source_binding_mode did not survive from_dict() round-trip"
        )
        assert c2._data.source_binding_signal == "for a given page", (
            "source_binding_signal did not survive from_dict() round-trip"
        )

    def test_from_dict_pre_adr032_dict_defaults_safely(self):
        """A pre-ADR-032 persisted dict (missing source_binding_mode/signal keys)
        must load without error and default to author_fixed + empty string.

        This is the backward-compat regression test — mirrors the f0591 discipline.
        """
        c = _make_conv()
        d = c.to_dict()
        # Simulate a pre-ADR-032 persisted dict by removing the new keys
        d.pop("source_binding_mode", None)
        d.pop("source_binding_signal", None)

        c2 = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
        assert c2._data.source_binding_mode == "author_fixed", (
            "Pre-ADR-032 dict should default source_binding_mode to 'author_fixed', "
            f"got {c2._data.source_binding_mode!r}"
        )
        assert c2._data.source_binding_signal == "", (
            "Pre-ADR-032 dict should default source_binding_signal to '', "
            f"got {c2._data.source_binding_signal!r}"
        )

    def test_from_dict_round_trip_all_three_modes(self):
        """All three mode values must survive a round-trip."""
        for mode in ("author_fixed", "ask_parameterized", "ambiguous"):
            c = _make_conv()
            c._data.source_binding_mode = mode
            d = c.to_dict()
            c2 = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
            assert c2._data.source_binding_mode == mode, (
                f"source_binding_mode={mode!r} did not survive round-trip"
            )


# ===========================================================================
# P1-C — capture_intent persists source_binding_mode from LLM output
# ===========================================================================


class TestCaptureIntentPersistsSourceBindingMode:
    """ADR-032 P1-C: _advance_to_capture_intent must read and persist source_binding_mode."""

    def test_capture_intent_author_fixed_no_clarify(self):
        """author_fixed mode -> no blocking CLARIFY for source-binding; advances."""
        c = _make_conv()
        # Two LLM calls: capture_intent + configure_sources (auto-advance path)
        capture_response = {
            "text": json.dumps({
                "output_kind": "email",
                "audience": "team",
                "cadence": "on_request",
                "scope_domains": ["project"],
                "success_criteria": ["accurate"],
                "blocking_ambiguities": [],
                "nice_to_know_ambiguities": [],
                "source_binding_mode": "author_fixed",
                "source_binding_signal": "",
            })
        }
        configure_response = {"text": "[]"}
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = [capture_response, configure_response]
        c._llm = mock_llm
        c._data.intent_description = "read project page 20030556732 and draft weekly email"
        c._data.sources = [{"kind": "confluence", "pages": ["20030556732"]}]

        turn = c._advance_to_capture_intent()

        # Must NOT go to CLARIFY for the source-binding question
        assert c._state != "CLARIFY" or (
            c._state == "CLARIFY" and all(
                q.get("context") != "source_binding_mode"
                for q in c._data._clarify_questions
            )
        ), (
            "author_fixed mode should not trigger source-binding CLARIFY"
        )
        # source_binding_mode must be persisted
        assert c._data.source_binding_mode == "author_fixed", (
            f"source_binding_mode should be 'author_fixed', got {c._data.source_binding_mode!r}"
        )

    def test_capture_intent_ask_parameterized_sets_clarify(self):
        """ask_parameterized mode -> blocking CLARIFY fires before CONFIGURE_SOURCES."""
        c = _make_conv()
        mock_llm = _mock_llm_for_capture_intent("ask_parameterized")
        c._llm = mock_llm
        c._data.intent_description = "accept a Confluence page and draft an email from it"

        turn = c._advance_to_capture_intent()

        # State must be CLARIFY
        assert c._state == "CLARIFY", (
            f"ask_parameterized should route to CLARIFY, got state={c._state!r}"
        )
        # Turn must be must_show_human (human must answer)
        assert turn.must_show_human is True, (
            "CLARIFY turn for source-binding must have must_show_human=True"
        )
        # source_binding_mode must be persisted (the transient "ask_parameterized" from LLM)
        assert c._data.source_binding_mode == "ask_parameterized", (
            f"source_binding_mode should be 'ask_parameterized', got {c._data.source_binding_mode!r}"
        )
        # There must be a pending source-binding clarify question
        sb_questions = [
            q for q in c._data._clarify_questions
            if q.get("context") == "source_binding_mode"
        ]
        assert sb_questions, (
            "No source-binding question (context='source_binding_mode') in _clarify_questions"
        )

    def test_capture_intent_ambiguous_sets_clarify(self):
        """ambiguous mode -> blocking CLARIFY fires before CONFIGURE_SOURCES."""
        c = _make_conv()
        mock_llm = _mock_llm_for_capture_intent("ambiguous")
        c._llm = mock_llm
        c._data.intent_description = "draft an email about project status somehow"

        turn = c._advance_to_capture_intent()

        assert c._state == "CLARIFY", (
            f"ambiguous should route to CLARIFY, got state={c._state!r}"
        )
        assert turn.must_show_human is True
        assert c._data.source_binding_mode in ("ambiguous", "ask_parameterized"), (
            "LLM 'ambiguous' mode should be stored as-is initially; resolved in CLARIFY"
        )

    def test_capture_intent_does_not_double_add_sb_question(self):
        """If the prompt already added the source-binding question to blocking_ambiguities,
        it must NOT appear twice in _clarify_questions.
        """
        c = _make_conv()
        # Prompt emits two blocking questions — one is the source-binding one
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "text": json.dumps({
                "output_kind": "email",
                "audience": "team",
                "cadence": "on_request",
                "scope_domains": ["proj"],
                "success_criteria": [],
                "blocking_ambiguities": [
                    "Which Confluence space — FA or PROJ?",
                    "Is the source page fixed at authoring time or supplied by "
                    "the consumer at query time?",
                ],
                "nice_to_know_ambiguities": [],
                "source_binding_mode": "ask_parameterized",
                "source_binding_signal": "accept a Confluence page",
            })
        }
        c._llm = mock_llm
        c._data.intent_description = "accept a page and draft"

        c._advance_to_capture_intent()

        sb_questions = [
            q for q in c._data._clarify_questions
            if "source page fixed" in q.get("question", "")
            or q.get("context") == "source_binding_mode"
        ]
        assert len(sb_questions) == 1, (
            f"source-binding clarify question appears {len(sb_questions)} times "
            f"(should be exactly 1 — no double-add)"
        )

    def test_capture_intent_signal_is_persisted(self):
        """source_binding_signal must be persisted on _SessionData."""
        c = _make_conv()
        mock_llm = _mock_llm_for_capture_intent("ask_parameterized")
        c._llm = mock_llm
        c._data.intent_description = "accept a Confluence page and draft an email from it"

        c._advance_to_capture_intent()

        assert c._data.source_binding_signal, (
            "source_binding_signal should be non-empty for ask_parameterized"
        )


# ===========================================================================
# P1-C — CLARIFY handler resolves source_binding_mode
# ===========================================================================


class TestClarifyResolvesSourceBindingMode:
    """ADR-032 P1-C: _handle_clarify_response must resolve source_binding_mode
    from the user's answer to the source-binding question.
    """

    def _setup_conv_at_clarify_for_sb(self, initial_mode="ask_parameterized"):
        """Build a conversation parked at CLARIFY with the source-binding question pending."""
        c = _make_conv()
        c._state = "CLARIFY"
        c._data.source_binding_mode = initial_mode
        c._data.clarification_log = []
        c._data._clarify_questions = [
            {
                "question": (
                    "Is the source page fixed at authoring time or supplied by "
                    "the consumer at query time?"
                ),
                "context": "source_binding_mode",
                "options": {"A": "author_fixed", "B": "ask_parameterized"},
                "resolved": False,
            }
        ]
        c._data._clarify_next_state = "CONFIGURE_SOURCES"
        return c

    def test_clarify_answer_a_resolves_to_author_fixed(self):
        """Answering 'A' resolves mode to author_fixed."""
        c = self._setup_conv_at_clarify_for_sb()
        with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
            c.respond("A")
        assert c._data.source_binding_mode == "author_fixed", (
            f"Answer 'A' should resolve to author_fixed, got {c._data.source_binding_mode!r}"
        )

    def test_clarify_answer_b_resolves_to_ask_parameterized(self):
        """Answering 'B' resolves mode to ask_parameterized."""
        c = self._setup_conv_at_clarify_for_sb()
        with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
            c.respond("B")
        assert c._data.source_binding_mode == "ask_parameterized", (
            f"Answer 'B' should resolve to ask_parameterized, got {c._data.source_binding_mode!r}"
        )

    def test_clarify_answer_fixed_resolves_to_author_fixed(self):
        """Answering 'fixed' or 'same page every time' resolves to author_fixed."""
        for answer in ("fixed", "same page every time", "always the same", "specific page"):
            c = self._setup_conv_at_clarify_for_sb("ambiguous")
            with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
                c.respond(answer)
            assert c._data.source_binding_mode == "author_fixed", (
                f"Answer {answer!r} should resolve to author_fixed, "
                f"got {c._data.source_binding_mode!r}"
            )

    def test_clarify_answer_dynamic_resolves_to_ask_parameterized(self):
        """Answering with dynamic/query-time language resolves to ask_parameterized."""
        for answer in ("different page each time", "user passes at query time",
                       "dynamic", "consumer supplies it", "B - parameterized"):
            c = self._setup_conv_at_clarify_for_sb("ambiguous")
            with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
                c.respond(answer)
            assert c._data.source_binding_mode == "ask_parameterized", (
                f"Answer {answer!r} should resolve to ask_parameterized, "
                f"got {c._data.source_binding_mode!r}"
            )

    def test_clarify_skip_defaults_to_author_fixed(self):
        """Skipping the source-binding question defaults to author_fixed (safer)."""
        c = self._setup_conv_at_clarify_for_sb("ask_parameterized")
        with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
            c.respond("skip")
        assert c._data.source_binding_mode == "author_fixed", (
            "Skipping source-binding clarification should default to author_fixed"
        )

    def test_clarify_non_sb_question_does_not_change_mode(self):
        """Answering a non-source-binding clarify question must NOT change source_binding_mode."""
        c = _make_conv()
        c._state = "CLARIFY"
        c._data.source_binding_mode = "ask_parameterized"
        c._data.clarification_log = []
        c._data._clarify_questions = [
            {
                "question": "Which Confluence space — FA or PROJ?",
                # No context="source_binding_mode" key
                "resolved": False,
            }
        ]
        c._data._clarify_next_state = "CONFIGURE_SOURCES"
        with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
            c.respond("FA space please")
        # Mode must be unchanged
        assert c._data.source_binding_mode == "ask_parameterized", (
            "Non-source-binding clarify question must not change source_binding_mode"
        )

    def test_clarify_blocking_does_not_auto_advance_before_answer(self):
        """CLARIFY must NOT auto-advance while source-binding question is unresolved."""
        c = self._setup_conv_at_clarify_for_sb("ask_parameterized")
        # Non-substantive reply — must NOT advance
        turn = c.respond("ok")
        # State must still be CLARIFY
        assert c._state == "CLARIFY", (
            f"CLARIFY must not advance on non-substantive answer 'ok', "
            f"got state={c._state!r}"
        )
        assert turn.state == "CLARIFY"
        assert turn.must_show_human is True

    def test_clarify_sb_question_resolves_mode_and_advances(self):
        """After answering the source-binding question, mode is resolved and session advances."""
        c = self._setup_conv_at_clarify_for_sb("ambiguous")
        with patch.object(c, "_advance_to_configure_sources_v2", return_value=MagicMock(state="CONFIGURE_SOURCES")):
            turn = c.respond("B — the user will supply the page each time")
        assert c._data.source_binding_mode == "ask_parameterized"
        # All questions must be resolved
        assert all(q.get("resolved") for q in c._data._clarify_questions), (
            "All clarify questions must be resolved after answering"
        )


# ===========================================================================
# P1-D — _validate_source_binding_contract (unit tests of the predicate)
# ===========================================================================


class TestValidateSourceBindingContract:
    """ADR-032 P1-D: _validate_source_binding_contract must return correct error lists."""

    def _well_formed_ask_parameterized_yaml(self, **overrides) -> dict:
        """Return a well-formed ask_parameterized skill YAML dict."""
        base = {
            "source_binding": {
                "mode": "ask_parameterized",
                "input_param": "page_id",
                "ingest_on_demand": True,
                "source_type": "confluence_page",
                "space_allow_list": ["FA", "PROJ"],
                "ephemeral_ttl_seconds": 300,
            },
            "trigger": {
                "on_request": {
                    "enabled": True,
                    "inputs": [
                        {"name": "page_id", "type": "confluence_page_ref", "required": True}
                    ],
                    "output_format": "email",
                }
            },
        }
        sb = dict(base["source_binding"])
        sb.update(overrides.get("source_binding", {}))
        base["source_binding"] = sb
        return base

    def test_well_formed_ask_parameterized_passes(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert errors == [], f"Well-formed ask_parameterized should pass: {errors}"

    def test_author_fixed_no_source_binding_passes(self):
        yaml_dict = {}  # no source_binding block = author_fixed default
        errors = _validate_source_binding_contract(yaml_dict, "author_fixed")
        assert errors == [], f"author_fixed with no source_binding should pass: {errors}"

    def test_ask_parameterized_missing_source_binding_block_fails(self):
        """ask_parameterized session + no source_binding block in YAML -> FAIL."""
        yaml_dict = {}  # no source_binding block
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert errors, "Missing source_binding block should fail validation"
        # Error should mention mode
        assert any("mode" in e for e in errors), (
            f"Error should mention 'mode': {errors}"
        )

    def test_ask_parameterized_missing_input_param_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        del yaml_dict["source_binding"]["input_param"]
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("input_param" in e for e in errors), (
            f"Missing input_param should produce an error: {errors}"
        )

    def test_ask_parameterized_input_param_not_in_trigger_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        yaml_dict["source_binding"]["input_param"] = "nonexistent_param"
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("input_param" in e or "nonexistent_param" in e for e in errors), (
            f"Mismatched input_param should produce an error: {errors}"
        )

    def test_ask_parameterized_missing_ingest_on_demand_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        del yaml_dict["source_binding"]["ingest_on_demand"]
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("ingest_on_demand" in e for e in errors), (
            f"Missing ingest_on_demand should produce an error: {errors}"
        )

    def test_ask_parameterized_missing_source_type_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        del yaml_dict["source_binding"]["source_type"]
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("source_type" in e for e in errors), (
            f"Missing source_type should produce an error: {errors}"
        )

    def test_ask_parameterized_empty_space_allow_list_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        yaml_dict["source_binding"]["space_allow_list"] = []
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("space_allow_list" in e for e in errors), (
            f"Empty space_allow_list should produce an error: {errors}"
        )

    def test_ask_parameterized_missing_space_allow_list_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        del yaml_dict["source_binding"]["space_allow_list"]
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("space_allow_list" in e for e in errors), (
            f"Missing space_allow_list should produce an error: {errors}"
        )

    def test_ask_parameterized_missing_ephemeral_ttl_fails(self):
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        del yaml_dict["source_binding"]["ephemeral_ttl_seconds"]
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert any("ephemeral_ttl_seconds" in e for e in errors), (
            f"Missing ephemeral_ttl_seconds should produce an error: {errors}"
        )

    def test_author_fixed_with_ask_parameterized_yaml_fails(self):
        """author_fixed session + YAML declares ask_parameterized -> FAIL."""
        yaml_dict = self._well_formed_ask_parameterized_yaml()
        errors = _validate_source_binding_contract(yaml_dict, "author_fixed")
        assert errors, (
            "author_fixed session + ask_parameterized YAML should fail validation"
        )
        assert any("author_fixed" in e or "ask_parameterized" in e for e in errors), (
            f"Error should mention mode conflict: {errors}"
        )


# ===========================================================================
# P1-D — _run_validate integration (source_binding check wired into VALIDATE)
# ===========================================================================


class TestRunValidateSourceBindingCheck:
    """ADR-032 P1-D: _run_validate must hard-fail when source_binding contract is violated."""

    def _make_conv_at_validate(
        self,
        skill_name="test_email",
        session_sb_mode="ask_parameterized",
        synthesized_yaml: dict | None = None,
    ) -> SkillBuilderConversation:
        """Build a conversation at the VALIDATE state with synthesized_artifacts set."""
        c = _make_conv()
        c._state = "COMMITTED"
        c._data.skill_name = skill_name
        c._data.source_binding_mode = session_sb_mode
        c._data.normalised_intent = {}

        # Inject the synthesized workflow skill YAML into synthesized_artifacts
        wf_key = f"framework/workflow_skills/tpm/{skill_name}.yaml"
        if synthesized_yaml is not None:
            c._data.synthesized_artifacts = {wf_key: synthesized_yaml}
        return c

    def _well_formed_yaml(self) -> dict:
        return {
            "source_binding": {
                "mode": "ask_parameterized",
                "input_param": "page_id",
                "ingest_on_demand": True,
                "source_type": "confluence_page",
                "space_allow_list": ["FA", "PROJ"],
                "ephemeral_ttl_seconds": 300,
            },
            "trigger": {
                "on_request": {
                    "enabled": True,
                    "inputs": [
                        {"name": "page_id", "type": "confluence_page_ref", "required": True}
                    ],
                    "output_format": "email",
                }
            },
            "requires_extractions": [{"kb": "tpm.test_email"}],
        }

    def _run_validate_with_mock_link_check(self, c, link_errors=None):
        """Run _run_validate with a mocked validate_workflow_links call.

        validate_workflow_links is imported locally inside _run_validate as:
          from .validate_links import validate_workflow_links
        so we patch it at the validate_links module level.
        """
        link_errors = link_errors or []
        with patch("framework.skill_builder.validate_links.validate_workflow_links") as mock_val:
            mock_val.return_value = link_errors
            return c._run_validate()

    def test_ask_parameterized_well_formed_passes(self):
        """ask_parameterized + well-formed source_binding YAML -> VALIDATE PASSES."""
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml=self._well_formed_yaml(),
        )
        # Patch adapter check to return True (adapter present)
        with patch(
            "framework.skill_builder.conversation._check_confluence_adapter_available",
            return_value=True,
        ):
            turn = self._run_validate_with_mock_link_check(c, link_errors=[])

        assert "PASSED" in turn.message or turn.data.get("validation", {}).get("passed"), (
            f"Well-formed ask_parameterized should pass VALIDATE: {turn.message!r}"
        )

    def test_ask_parameterized_missing_source_binding_block_fails(self):
        """ask_parameterized session + YAML missing source_binding -> VALIDATE FAILS."""
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml={},  # no source_binding block
        )
        turn = self._run_validate_with_mock_link_check(c, link_errors=[])

        assert "FAILED" in turn.message, (
            f"Missing source_binding block should fail VALIDATE: {turn.message!r}"
        )
        assert turn.data.get("validation", {}).get("passed") is False

    def test_ask_parameterized_empty_space_allow_list_fails(self):
        """ask_parameterized + empty space_allow_list -> VALIDATE FAILS."""
        bad_yaml = self._well_formed_yaml()
        bad_yaml["source_binding"]["space_allow_list"] = []
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml=bad_yaml,
        )
        turn = self._run_validate_with_mock_link_check(c, link_errors=[])

        assert "FAILED" in turn.message, (
            f"Empty space_allow_list should fail VALIDATE: {turn.message!r}"
        )

    def test_ask_parameterized_missing_input_param_fails(self):
        """ask_parameterized + missing input_param -> VALIDATE FAILS."""
        bad_yaml = self._well_formed_yaml()
        del bad_yaml["source_binding"]["input_param"]
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml=bad_yaml,
        )
        turn = self._run_validate_with_mock_link_check(c, link_errors=[])
        assert "FAILED" in turn.message

    def test_author_fixed_no_source_binding_passes(self):
        """author_fixed + no source_binding block -> VALIDATE PASSES (no regression)."""
        author_fixed_yaml = {
            "trigger": {
                "on_request": {
                    "enabled": True,
                    "inputs": [{"name": "input", "type": "string"}],
                    "output_format": "pptx",
                }
            },
            "requires_extractions": [{"kb": "tpm.weekly_exec_review"}],
        }
        c = self._make_conv_at_validate(
            session_sb_mode="author_fixed",
            synthesized_yaml=author_fixed_yaml,
        )
        turn = self._run_validate_with_mock_link_check(c, link_errors=[])

        assert "PASSED" in turn.message or turn.data.get("validation", {}).get("passed"), (
            f"author_fixed with no source_binding should pass VALIDATE: {turn.message!r}"
        )

    def test_author_fixed_with_ask_parameterized_yaml_fails(self):
        """author_fixed session + YAML declares ask_parameterized -> VALIDATE FAILS."""
        c = self._make_conv_at_validate(
            session_sb_mode="author_fixed",
            synthesized_yaml=self._well_formed_yaml(),  # declares ask_parameterized
        )
        turn = self._run_validate_with_mock_link_check(c, link_errors=[])
        assert "FAILED" in turn.message, (
            "author_fixed session + ask_parameterized YAML should fail VALIDATE"
        )

    def test_ask_parameterized_ingest_on_demand_no_adapter_fails(self):
        """ask_parameterized + ingest_on_demand:true + no Confluence adapter -> VALIDATE FAILS."""
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml=self._well_formed_yaml(),
        )
        # Patch adapter check to return False (no adapter configured)
        with patch(
            "framework.skill_builder.conversation._check_confluence_adapter_available",
            return_value=False,
        ):
            turn = self._run_validate_with_mock_link_check(c, link_errors=[])

        assert "FAILED" in turn.message, (
            "ask_parameterized + ingest_on_demand:true + no adapter should fail VALIDATE"
        )
        # Error message must be actionable
        assert any(
            kw in turn.message.lower()
            for kw in ("confluence", "adapter", "ingest_on_demand")
        ), (
            f"Error message should mention Confluence/adapter/ingest_on_demand: {turn.message!r}"
        )

    def test_validate_hard_fail_cannot_be_skipped_silently(self):
        """Source_binding contract violations MUST produce passed=False in validation_result."""
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml={},  # missing source_binding
        )
        self._run_validate_with_mock_link_check(c, link_errors=[])
        assert c._data.validation_result is not None
        assert c._data.validation_result.get("passed") is False, (
            "Contract violation must set passed=False in validation_result"
        )

    def test_existing_link_check_errors_plus_sb_errors_both_reported(self):
        """If ADR-017 link check fails AND source_binding fails, both errors appear."""
        c = self._make_conv_at_validate(
            session_sb_mode="ask_parameterized",
            synthesized_yaml={},  # missing source_binding
        )
        turn = self._run_validate_with_mock_link_check(
            c, link_errors=["unknown KB: tpm.test_email"]
        )
        assert "FAILED" in turn.message
        # Both types of error must appear
        all_errors = c._data.validation_result.get("errors", [])
        link_errors_present = any("unknown KB" in e or "tpm.test_email" in e for e in all_errors)
        sb_errors_present = any("source_binding" in e or "mode" in e for e in all_errors)
        assert link_errors_present, f"Link check error missing from errors: {all_errors}"
        assert sb_errors_present, f"Source_binding error missing from errors: {all_errors}"


# ===========================================================================
# P1-D — _check_confluence_adapter_available (unit tests)
# ===========================================================================


class TestCheckConfluenceAdapterAvailable:
    """ADR-032 P1-D: _check_confluence_adapter_available config-only check."""

    def test_returns_false_when_no_config_files(self, tmp_path):
        """No config files -> adapter unavailable (returns False)."""
        result = _check_confluence_adapter_available("laptop", tmp_path)
        assert result is False

    def test_returns_true_when_mode_configured(self, tmp_path):
        """Adapter config with a non-empty mode -> returns True."""
        import yaml
        cfg_dir = tmp_path / "framework" / "config" / "adapters"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "confluence.yaml").write_text(
            yaml.dump({"mode": "emcp_direct"}), encoding="utf-8"
        )
        result = _check_confluence_adapter_available("laptop", tmp_path)
        assert result is True

    def test_returns_false_when_mode_empty(self, tmp_path):
        """Adapter config with mode='' -> returns False."""
        import yaml
        cfg_dir = tmp_path / "framework" / "config" / "adapters"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "confluence.yaml").write_text(
            yaml.dump({"mode": ""}), encoding="utf-8"
        )
        result = _check_confluence_adapter_available("laptop", tmp_path)
        assert result is False

    def test_env_override_wins_over_base(self, tmp_path):
        """Env-specific override takes precedence over base config."""
        import yaml
        # Base has no mode
        cfg_dir = tmp_path / "framework" / "config" / "adapters"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "confluence.yaml").write_text(
            yaml.dump({}), encoding="utf-8"
        )
        # Env override sets mode
        env_cfg_dir = tmp_path / "framework" / "config"
        (env_cfg_dir / "laptop.yaml").write_text(
            yaml.dump({"adapters_overrides": {"confluence": {"mode": "codex_proxy"}}}),
            encoding="utf-8",
        )
        result = _check_confluence_adapter_available("laptop", tmp_path)
        assert result is True

    def test_does_not_make_http_call(self, tmp_path):
        """_check_confluence_adapter_available must be a config-only check — no HTTP calls."""
        import yaml
        cfg_dir = tmp_path / "framework" / "config" / "adapters"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "confluence.yaml").write_text(
            yaml.dump({"mode": "native", "native": {"base_url": "https://confluence.example.com"}}),
            encoding="utf-8",
        )
        # If this raises a connection error, the function makes HTTP calls — fail test
        with patch("urllib.request.urlopen") as mock_urlopen:
            _check_confluence_adapter_available("laptop", tmp_path)
            mock_urlopen.assert_not_called()
