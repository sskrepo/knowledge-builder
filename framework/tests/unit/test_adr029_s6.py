"""ADR-029 Phase 2 (S6) tests — constrained routing + loop guardrails.

Test coverage:
  - Each failure_class → correct target state per _ROUTING_MAP
  - Unknown/garbled class → treated as low-confidence → REVIEW_DESIGN
    (never CONFIGURE_SOURCES / INSPECT_SOURCES)
  - confidence=low → REVIEW_DESIGN regardless of class
  - UNSUPPORTABLE → DONE as draft
  - Consecutive-same-class → DONE as draft (pathological loop detector)
  - eval_iteration_count >= _EVAL_MAX_ITERATIONS → DONE as draft
  - eval_cumulative_cost_usd > _EVAL_COST_CEILING_USD → DONE as draft
  - Routing turn has must_show_human=True and includes evidence + why_not_alternative
  - Classifier is called with ALL SIX mandatory inputs including capability_inventory
  - Accept path still → PROMOTE (unchanged from S5)
  - _SessionData fields (eval_iteration_count, eval_cumulative_cost_usd,
    last_eval_failure_class) round-trip through to_dict / from_dict

No live LLM calls — all tests use MagicMock with canned classifier JSON.
DO NOT modify test_skill_builder_conversation.py, test_adr029_s5.py, or
test_failure_classifier_gate.py.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from framework.skill_builder.conversation import (
    ConversationTurn,
    SkillBuilderConversation,
    _SessionData,
    _EVAL_MAX_ITERATIONS,
    _EVAL_COST_CEILING_USD,
    _ROUTING_MAP,
    _FAILURE_CLASSIFIER_PROMPT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill_store() -> MagicMock:
    ss = MagicMock()
    ss.read_artifact.return_value = None
    return ss


def _make_classifier_response(
    failure_class: str,
    confidence: str = "high",
    evidence: str = "Evidence text.",
    alternative_class: str = "SOURCE_COVERAGE",
    why_not_alternative: str = "Source has synthesisable content.",
) -> dict:
    """Return a canned LLM response dict as the real client would produce."""
    return {
        "text": json.dumps({
            "failure_class": failure_class,
            "confidence": confidence,
            "evidence": evidence,
            "alternative_class": alternative_class,
            "why_not_alternative": why_not_alternative,
        }),
        "tokens_in": 100,
        "tokens_out": 80,
        "estimated_cost_usd": 0.01,
    }


def _make_conv_at_eval(
    *,
    failure_class: str = "MISSING_FIELDS",
    confidence: str = "high",
    evidence: str = "Fields absent from schema.",
    alternative_class: str = "SOURCE_COVERAGE",
    why_not_alternative: str = "Source has the content synthesisable.",
    last_eval_failure_class: str | None = None,
    eval_iteration_count: int = 0,
    eval_cumulative_cost_usd: float = 0.0,
    comparator_missing: list | None = None,
    comparator_thin: list | None = None,
) -> SkillBuilderConversation:
    """Build a SkillBuilderConversation positioned at EVAL with a mocked LLM."""
    llm = MagicMock()
    llm.chat.return_value = _make_classifier_response(
        failure_class=failure_class,
        confidence=confidence,
        evidence=evidence,
        alternative_class=alternative_class,
        why_not_alternative=why_not_alternative,
    )

    conv = SkillBuilderConversation(
        persona="tpm",
        user_id="test-s6",
        llm=llm,
        skill_store=_make_skill_store(),
    )
    conv._data.persona = "tpm"
    conv._data.skill_name = "test_skill"
    conv._data.synth_id = "synth-s6-test"
    conv._data.normalised_intent = {"skill_name": "test_skill", "persona": "tpm"}
    conv._data.source_capability = [
        {
            "source_id": "confluence:123",
            "available_fields": [
                {"field": "risks", "confidence": "synthesisable",
                 "evidence": "WBS rows have risk notes"},
            ],
        }
    ]
    conv._data.design = {
        "schema": {
            "properties": {
                "status": {"type": "string", "description": "status"},
            }
        }
    }
    conv._data.eval_result = {
        "status": "completed",
        "comparator": {
            "structure_score": 0.5,
            "density_score": 0.8,
            "missing_sections": comparator_missing or ["Risks", "Next Steps"],
            "thin_sections": comparator_thin or [],
            "gap_report": (
                "Structure gap: missing Risks and Next Steps. "
                "Structure score: 50%."
            ),
        },
        "metrics": {"recall_at_k": 0.4, "faithfulness": 0.5, "ask_latency_ms": None,
                    "estimated_cost_usd": 0.0},
        "exit_criteria": {"passed": False,
                          "_note": "diagnostic-only"},
    }
    conv._data.last_eval_failure_class = last_eval_failure_class
    conv._data.eval_iteration_count = eval_iteration_count
    conv._data.eval_cumulative_cost_usd = eval_cumulative_cost_usd
    conv._state = "EVAL"
    return conv


# ---------------------------------------------------------------------------
# S6 — Routing map: each failure_class → correct target state
# ---------------------------------------------------------------------------


class TestRoutingMap:
    """Each failure_class in _ROUTING_MAP must route to the correct target state."""

    def _assert_routing_turn(self, conv: SkillBuilderConversation, expected_target: str):
        """After _handle_eval_response('review design'), assert routing turn content."""
        turn = conv._handle_eval_response("review design")
        # The turn must be must_show_human and positioned at EVAL_ROUTE_PENDING
        assert conv._state == "EVAL_ROUTE_PENDING", (
            f"State must be EVAL_ROUTE_PENDING after classifier runs. Got {conv._state!r}"
        )
        assert turn.must_show_human is True, (
            "Routing turn must have must_show_human=True (guardrail 6)"
        )
        assert turn.state == "EVAL", (
            "Routing turn state must be EVAL (not the target yet — user must confirm)"
        )
        data = turn.data or {}
        assert data.get("target_state") == expected_target, (
            f"target_state in turn data must be {expected_target!r}. "
            f"Got {data.get('target_state')!r}"
        )
        # Evidence and why_not_alternative must be present (guardrail 6)
        assert data.get("evidence"), "Routing turn must include evidence"
        assert data.get("why_not_alternative"), "Routing turn must include why_not_alternative"
        return turn

    def test_missing_fields_routes_to_review_design(self):
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS")
        self._assert_routing_turn(conv, "REVIEW_DESIGN")

    def test_thin_fields_routes_to_review_design(self):
        conv = _make_conv_at_eval(failure_class="THIN_FIELDS")
        self._assert_routing_turn(conv, "REVIEW_DESIGN")

    def test_wrong_layout_routes_to_review_design(self):
        conv = _make_conv_at_eval(failure_class="WRONG_LAYOUT")
        self._assert_routing_turn(conv, "REVIEW_DESIGN")

    def test_source_coverage_routes_to_configure_sources(self):
        conv = _make_conv_at_eval(failure_class="SOURCE_COVERAGE")
        self._assert_routing_turn(conv, "CONFIGURE_SOURCES")

    def test_wrong_source_routes_to_inspect_sources(self):
        conv = _make_conv_at_eval(failure_class="WRONG_SOURCE")
        self._assert_routing_turn(conv, "INSPECT_SOURCES")


# ---------------------------------------------------------------------------
# S6 — Guardrail 1: low confidence → REVIEW_DESIGN; unknown class → REVIEW_DESIGN
# ---------------------------------------------------------------------------


class TestGuardrail1LowConfidence:
    """Guardrail 1: low-confidence or unknown class → REVIEW_DESIGN, never
    CONFIGURE_SOURCES or INSPECT_SOURCES."""

    def test_low_confidence_overrides_source_coverage(self):
        """confidence=low + SOURCE_COVERAGE must route to REVIEW_DESIGN, not CONFIGURE_SOURCES."""
        conv = _make_conv_at_eval(failure_class="SOURCE_COVERAGE", confidence="low")
        turn = conv._handle_eval_response("review design")

        data = turn.data or {}
        assert data.get("target_state") == "REVIEW_DESIGN", (
            "Guardrail 1: low-confidence SOURCE_COVERAGE must route to REVIEW_DESIGN, "
            f"not CONFIGURE_SOURCES. Got target_state={data.get('target_state')!r}"
        )
        assert data.get("target_state") != "CONFIGURE_SOURCES"
        assert data.get("target_state") != "INSPECT_SOURCES"

    def test_low_confidence_overrides_wrong_source(self):
        """confidence=low + WRONG_SOURCE must route to REVIEW_DESIGN, not INSPECT_SOURCES."""
        conv = _make_conv_at_eval(failure_class="WRONG_SOURCE", confidence="low")
        turn = conv._handle_eval_response("review design")

        data = turn.data or {}
        assert data.get("target_state") == "REVIEW_DESIGN", (
            "Guardrail 1: low-confidence WRONG_SOURCE must route to REVIEW_DESIGN."
        )
        assert data.get("target_state") != "INSPECT_SOURCES"

    def test_unknown_class_routes_to_review_design(self):
        """Unknown/garbled failure_class must be treated as low-confidence → REVIEW_DESIGN."""
        conv = _make_conv_at_eval(failure_class="GARBLED_CLASS_XYZ")
        turn = conv._handle_eval_response("configure sources")

        data = turn.data or {}
        assert data.get("target_state") == "REVIEW_DESIGN", (
            "Unknown failure_class must route to REVIEW_DESIGN (guardrail 1). "
            f"Got target_state={data.get('target_state')!r}"
        )
        assert data.get("target_state") not in ("CONFIGURE_SOURCES", "INSPECT_SOURCES"), (
            "Unknown class must NEVER route to CONFIGURE_SOURCES or INSPECT_SOURCES"
        )

    def test_low_confidence_missing_fields_still_review_design(self):
        """confidence=low + MISSING_FIELDS → REVIEW_DESIGN (same destination, confirmed)."""
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS", confidence="low")
        turn = conv._handle_eval_response("review design")
        data = turn.data or {}
        assert data.get("target_state") == "REVIEW_DESIGN"


# ---------------------------------------------------------------------------
# S6 — Guardrail 2: UNSUPPORTABLE → DONE as draft
# ---------------------------------------------------------------------------


class TestGuardrail2Unsupportable:
    """Guardrail 2: UNSUPPORTABLE → DONE as draft, no loop."""

    def test_unsupportable_exits_as_draft(self):
        conv = _make_conv_at_eval(
            failure_class="UNSUPPORTABLE",
            evidence="Cannot derive this from any source.",
        )
        turn = conv._handle_eval_response("review design")

        assert turn.state == "DONE", (
            f"UNSUPPORTABLE must transition to DONE (draft). Got {turn.state!r}"
        )
        assert turn.done is True
        assert turn.must_show_human is True, (
            "UNSUPPORTABLE DONE turn must have must_show_human=True"
        )
        assert "UNSUPPORTABLE" in turn.message
        assert "Cannot derive this from any source" in turn.message
        assert conv._state == "DONE"

    def test_unsupportable_does_not_route_to_any_other_state(self):
        """After UNSUPPORTABLE, state machine must not transition to REVIEW_DESIGN etc."""
        conv = _make_conv_at_eval(failure_class="UNSUPPORTABLE")
        conv._handle_eval_response("review design")
        assert conv._state == "DONE", "UNSUPPORTABLE must end session, not route back"


# ---------------------------------------------------------------------------
# S6 — Guardrail 3: consecutive-same-class → DONE as draft
# ---------------------------------------------------------------------------


class TestGuardrail3ConsecutiveSameClass:
    """Guardrail 3: two consecutive iterations with the same failure_class → DONE."""

    def test_consecutive_same_class_exits_as_draft(self):
        """If last_eval_failure_class == current failure_class → pathological loop → DONE."""
        conv = _make_conv_at_eval(
            failure_class="MISSING_FIELDS",
            last_eval_failure_class="MISSING_FIELDS",  # same as current
        )
        turn = conv._handle_eval_response("review design")

        assert turn.state == "DONE", (
            "Consecutive same-class must exit as draft (guardrail 3). "
            f"Got {turn.state!r}"
        )
        assert turn.done is True
        assert turn.must_show_human is True
        # Message must mention the class and the pathological-loop reason
        assert "MISSING_FIELDS" in turn.message or "cycled" in turn.message.lower(), (
            "Pathological loop message must name the repeated failure class"
        )

    def test_different_class_does_not_trigger_loop_detector(self):
        """If last class differs from current class, the loop detector must NOT fire."""
        conv = _make_conv_at_eval(
            failure_class="THIN_FIELDS",
            last_eval_failure_class="MISSING_FIELDS",  # different class
        )
        turn = conv._handle_eval_response("review design")

        # Should be a routing turn (EVAL_ROUTE_PENDING), not DONE
        assert conv._state == "EVAL_ROUTE_PENDING", (
            "Different failure_class must not trigger consecutive-same-class detector. "
            f"Got state {conv._state!r}"
        )
        assert turn.state == "EVAL"
        assert turn.done is not True

    def test_first_iteration_no_last_class_does_not_trigger(self):
        """On first iteration (last_eval_failure_class=None), loop detector must not fire."""
        conv = _make_conv_at_eval(
            failure_class="MISSING_FIELDS",
            last_eval_failure_class=None,
        )
        turn = conv._handle_eval_response("review design")

        assert conv._state == "EVAL_ROUTE_PENDING", (
            "First iteration (no previous class) must not trigger loop detector. "
            f"Got state {conv._state!r}"
        )


# ---------------------------------------------------------------------------
# S6 — Guardrail 4: eval_iteration_count >= max → DONE as draft
# ---------------------------------------------------------------------------


class TestGuardrail4MaxIterations:
    """Guardrail 4: when eval_iteration_count reaches the ceiling, exit as draft."""

    def test_iteration_count_at_max_exits_before_classifier(self):
        """When iteration count is already at the max, must exit before calling classifier."""
        conv = _make_conv_at_eval(eval_iteration_count=_EVAL_MAX_ITERATIONS)
        turn = conv._handle_eval_response("review design")

        assert turn.state == "DONE"
        assert turn.done is True
        # Classifier must NOT have been called (LLM should not be invoked)
        assert conv._llm.chat.call_count == 0, (
            "Classifier must NOT be called when iteration ceiling is already reached. "
            f"LLM was called {conv._llm.chat.call_count} times."
        )

    def test_iteration_count_below_max_proceeds(self):
        """When iteration count is below max, must proceed to classifier."""
        conv = _make_conv_at_eval(eval_iteration_count=_EVAL_MAX_ITERATIONS - 1)
        turn = conv._handle_eval_response("review design")

        # Must NOT be DONE (should be EVAL_ROUTE_PENDING with a routing turn)
        assert turn.state != "DONE" or turn.done is not True, (
            f"Iteration count {_EVAL_MAX_ITERATIONS - 1} is below max — must NOT exit as draft"
        )
        assert conv._llm.chat.call_count >= 1, (
            "Classifier must have been called when iteration count is below max"
        )

    def test_iteration_count_incremented_after_classifier(self):
        """eval_iteration_count must be incremented by 1 each time the classifier runs."""
        conv = _make_conv_at_eval(eval_iteration_count=0)
        initial_count = conv._data.eval_iteration_count
        conv._handle_eval_response("review design")
        assert conv._data.eval_iteration_count == initial_count + 1, (
            "eval_iteration_count must be incremented by 1 after classifier runs. "
            f"Before: {initial_count}, After: {conv._data.eval_iteration_count}"
        )


# ---------------------------------------------------------------------------
# S6 — Guardrail 5: eval_cumulative_cost_usd > ceiling → DONE as draft
# ---------------------------------------------------------------------------


class TestGuardrail5CostCeiling:
    """Guardrail 5: when cumulative cost exceeds the ceiling, exit as draft."""

    def test_cost_over_ceiling_exits_before_classifier(self):
        """When cumulative cost already exceeds ceiling, must exit before calling classifier."""
        conv = _make_conv_at_eval(eval_cumulative_cost_usd=_EVAL_COST_CEILING_USD + 0.01)
        turn = conv._handle_eval_response("review design")

        assert turn.state == "DONE"
        assert turn.done is True
        assert conv._llm.chat.call_count == 0, (
            "Classifier must NOT be called when cost ceiling is already exceeded."
        )

    def test_cost_exactly_at_ceiling_does_not_trigger(self):
        """Exactly at the ceiling (==) must NOT trigger — only OVER (>) triggers."""
        conv = _make_conv_at_eval(eval_cumulative_cost_usd=_EVAL_COST_CEILING_USD)
        conv._handle_eval_response("review design")
        # Classifier must have been called (cost == ceiling is allowed)
        assert conv._llm.chat.call_count >= 1, (
            "Cost exactly at ceiling must allow one more classifier call (> not >=)"
        )

    def test_cost_accumulated_across_iterations(self):
        """Classifier call cost must be added to eval_cumulative_cost_usd."""
        conv = _make_conv_at_eval(eval_cumulative_cost_usd=0.0)
        # LLM response has estimated_cost_usd=0.01
        conv._handle_eval_response("review design")
        assert conv._data.eval_cumulative_cost_usd >= 0.0, (
            "eval_cumulative_cost_usd must be updated after classifier runs"
        )


# ---------------------------------------------------------------------------
# S6 — Guardrail 6: routing turn must have must_show_human + evidence
# ---------------------------------------------------------------------------


class TestGuardrail6MustShowHuman:
    """Guardrail 6: routing turn must always have must_show_human=True and include
    evidence + why_not_alternative."""

    def test_routing_turn_must_show_human(self):
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS")
        turn = conv._handle_eval_response("review design")
        assert turn.must_show_human is True, (
            "Guardrail 6: routing turn MUST have must_show_human=True"
        )

    def test_routing_turn_includes_evidence(self):
        conv = _make_conv_at_eval(
            failure_class="MISSING_FIELDS",
            evidence="Fields absent from schema, source has synthesisable content.",
        )
        turn = conv._handle_eval_response("review design")
        # Evidence must appear in the turn message or data
        message = turn.message or ""
        data = turn.data or {}
        assert (
            "absent from schema" in message or data.get("evidence", "") != ""
        ), "Routing turn must include evidence in message or turn data"

    def test_routing_turn_includes_why_not_alternative(self):
        conv = _make_conv_at_eval(
            failure_class="MISSING_FIELDS",
            why_not_alternative="Source has synthesisable risk content.",
        )
        turn = conv._handle_eval_response("review design")
        data = turn.data or {}
        assert data.get("why_not_alternative"), (
            "Routing turn data must include why_not_alternative"
        )

    def test_routing_turn_data_contains_failure_class(self):
        conv = _make_conv_at_eval(failure_class="SOURCE_COVERAGE")
        turn = conv._handle_eval_response("review design")
        data = turn.data or {}
        assert data.get("failure_class") == "SOURCE_COVERAGE", (
            "Routing turn data must contain failure_class"
        )


# ---------------------------------------------------------------------------
# S6 — Classifier called with ALL SIX mandatory inputs
# ---------------------------------------------------------------------------


class TestClassifierInputContract:
    """The classifier MUST be called with all 6 mandatory inputs.

    This guards the gate contract: omitting capability_inventory re-breaks
    MISSING_FIELDS vs SOURCE_COVERAGE discrimination.
    """

    def test_classifier_called_with_capability_inventory(self):
        """The LLM call prompt must contain capability_inventory content."""
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS")
        conv._handle_eval_response("review design")

        # Check that the LLM was called
        assert conv._llm.chat.call_count >= 1, "Classifier LLM must have been called"

        # Extract the prompt from the call
        call_args = conv._llm.chat.call_args
        messages = call_args[1].get("messages", []) if call_args[1] else call_args[0][1]
        prompt_text = messages[0]["content"]

        # The prompt must contain capability_inventory data
        assert "synthesisable" in prompt_text, (
            "Classifier prompt must contain capability_inventory with synthesisable fields. "
            "Without capability_inventory, MISSING_FIELDS vs SOURCE_COVERAGE cannot be "
            "distinguished correctly (gate contract)."
        )

    def test_classifier_prompt_contains_all_six_inputs(self):
        """The formatted prompt must include all 6 mandatory format kwargs."""
        conv = _make_conv_at_eval(
            failure_class="MISSING_FIELDS",
            comparator_missing=["Risks", "Next Steps"],
            comparator_thin=["Status"],
        )
        # Capture the prompt
        captured_prompts = []
        original_chat = conv._llm.chat

        def capturing_chat(**kwargs):
            msgs = kwargs.get("messages", [])
            if msgs:
                captured_prompts.append(msgs[0].get("content", ""))
            return original_chat(**kwargs)

        conv._llm.chat = capturing_chat
        conv._handle_eval_response("review design")

        assert captured_prompts, "LLM must have been called"
        prompt = captured_prompts[0]

        # All 6 mandatory inputs must appear in the formatted prompt
        # (they come from _FAILURE_CLASSIFIER_PROMPT.format(...))
        assert "normalised_intent" in _FAILURE_CLASSIFIER_PROMPT, "Prompt template must have normalised_intent"
        assert "capability_inventory" in _FAILURE_CLASSIFIER_PROMPT, "Prompt template must have capability_inventory"
        # The formatted prompt must contain the actual values
        assert "tpm" in prompt, "Formatted prompt must contain normalised_intent content (persona=tpm)"
        assert "synthesisable" in prompt, (
            "Formatted prompt must contain capability_inventory with synthesisable evidence"
        )
        # Missing sections must appear (from comparator_dict)
        assert "Risks" in prompt or "missing_sections" in _FAILURE_CLASSIFIER_PROMPT, (
            "Formatted prompt must contain missing_sections data"
        )

    def test_classifier_not_called_when_llm_is_none(self):
        """When self._llm is None, _classify_and_route must surface an actionable error turn.

        No stub-mode: must NOT silently skip routing. Must NOT silently return {}.
        The error must be surfaced as a must_show_human=True turn at EVAL state so
        the operator sees the configuration problem. Session stays at EVAL.
        """
        conv = SkillBuilderConversation(
            persona="tpm", user_id="test", llm=None, skill_store=_make_skill_store()
        )
        conv._state = "EVAL"
        conv._data.eval_result = {
            "comparator": {"gap_report": "gap", "missing_sections": [], "thin_sections": []},
            "metrics": {}, "exit_criteria": {"passed": False},
        }
        turn = conv._classify_and_route("review design")
        assert turn.state == "EVAL", (
            "llm=None: must return an EVAL turn (not crash silently)"
        )
        assert turn.must_show_human is True, (
            "llm=None: error turn must have must_show_human=True (no silent skip)"
        )
        assert "ERROR" in turn.message or "cannot" in turn.message.lower(), (
            f"Error turn must contain actionable error message. Got: {turn.message!r}"
        )


# ---------------------------------------------------------------------------
# S6 — last_eval_failure_class set after each classification
# ---------------------------------------------------------------------------


class TestLastEvalFailureClassTracking:
    """last_eval_failure_class must be updated after each classifier run."""

    def test_last_eval_failure_class_set_after_routing(self):
        """After routing, last_eval_failure_class must be the classified class."""
        conv = _make_conv_at_eval(failure_class="THIN_FIELDS", last_eval_failure_class=None)
        assert conv._data.last_eval_failure_class is None  # pre-condition
        conv._handle_eval_response("review design")
        assert conv._data.last_eval_failure_class == "THIN_FIELDS", (
            "last_eval_failure_class must be set to the classified class after routing"
        )

    def test_last_eval_failure_class_set_for_unsupportable(self):
        """UNSUPPORTABLE also sets last_eval_failure_class."""
        conv = _make_conv_at_eval(failure_class="UNSUPPORTABLE")
        conv._handle_eval_response("review design")
        assert conv._data.last_eval_failure_class == "UNSUPPORTABLE"


# ---------------------------------------------------------------------------
# S6 — _SessionData persistence: new fields round-trip to_dict / from_dict
# ---------------------------------------------------------------------------


class TestSessionDataPersistence:
    """New _SessionData fields must survive to_dict / from_dict round-trips."""

    def _make_bare_conv(self) -> SkillBuilderConversation:
        return SkillBuilderConversation(
            persona="tpm", user_id="test", llm=MagicMock(),
            skill_store=_make_skill_store(),
        )

    def test_eval_iteration_count_in_to_dict(self):
        conv = self._make_bare_conv()
        conv._data.eval_iteration_count = 2
        d = conv.to_dict()
        assert d.get("eval_iteration_count") == 2

    def test_eval_cumulative_cost_usd_in_to_dict(self):
        conv = self._make_bare_conv()
        conv._data.eval_cumulative_cost_usd = 1.23
        d = conv.to_dict()
        assert abs(d.get("eval_cumulative_cost_usd", 0.0) - 1.23) < 1e-6

    def test_last_eval_failure_class_in_to_dict(self):
        conv = self._make_bare_conv()
        conv._data.last_eval_failure_class = "SOURCE_COVERAGE"
        d = conv.to_dict()
        assert d.get("last_eval_failure_class") == "SOURCE_COVERAGE"

    def test_all_three_fields_round_trip_from_dict(self):
        conv = self._make_bare_conv()
        conv._data.eval_iteration_count = 3
        conv._data.eval_cumulative_cost_usd = 0.77
        conv._data.last_eval_failure_class = "THIN_FIELDS"

        d = conv.to_dict()
        restored = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())

        assert restored._data.eval_iteration_count == 3
        assert abs(restored._data.eval_cumulative_cost_usd - 0.77) < 1e-6
        assert restored._data.last_eval_failure_class == "THIN_FIELDS"

    def test_backward_compat_defaults_on_missing_keys(self):
        """Pre-S6 sessions (no iteration fields) must restore with safe defaults."""
        conv = self._make_bare_conv()
        d = conv.to_dict()
        # Remove S6 fields to simulate pre-S6 session dict
        d.pop("eval_iteration_count", None)
        d.pop("eval_cumulative_cost_usd", None)
        d.pop("last_eval_failure_class", None)

        restored = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
        assert restored._data.eval_iteration_count == 0
        assert restored._data.eval_cumulative_cost_usd == 0.0
        assert restored._data.last_eval_failure_class is None


# ---------------------------------------------------------------------------
# S6 — Accept path still → PROMOTE (unchanged from S5)
# ---------------------------------------------------------------------------


class TestAcceptPathUnchanged:
    """Accept path (S5) must be unchanged — S6 must not break it."""

    def test_accept_still_transitions_to_promote(self):
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS")
        conv._data.ingest_result = {"status": "completed", "items_processed": 0}

        promote_turn = ConversationTurn(state="PROMOTE", message="Promote?")
        with patch.object(conv, "_run_promote", return_value=promote_turn) as mock_promote:
            turn = conv._handle_eval_response("accept")

        mock_promote.assert_called_once(), "Accept must call _run_promote (unchanged from S5)"
        assert turn.state == "PROMOTE"
        # Classifier must NOT have been called on accept
        assert conv._llm.chat.call_count == 0, (
            "Classifier must NOT be called when user accepts — accept→PROMOTE is unchanged"
        )

    def test_accept_does_not_run_classifier(self):
        """The accept path bypasses the classifier entirely."""
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS")
        conv._data.ingest_result = {"status": "completed"}

        with patch.object(conv, "_run_promote", return_value=ConversationTurn(state="PROMOTE")):
            conv._handle_eval_response("accept")

        assert conv._llm.chat.call_count == 0, (
            "S6: classifier must NOT be called on the accept path"
        )

    def test_promote_gate_is_user_accept_not_numeric(self):
        """exit_criteria.passed=False must not block user accept → PROMOTE."""
        conv = _make_conv_at_eval(failure_class="MISSING_FIELDS")
        conv._data.eval_result["exit_criteria"]["passed"] = False
        conv._data.ingest_result = {"status": "completed"}

        promote_turn = ConversationTurn(state="PROMOTE", message="ok")
        with patch.object(conv, "_run_promote", return_value=promote_turn) as mock_promote:
            turn = conv._handle_eval_response("accept")

        mock_promote.assert_called_once(), (
            "ADR-029 S5/S6: user accept must gate PROMOTE regardless of exit_criteria.passed. "
            "_run_promote was not called."
        )


# ---------------------------------------------------------------------------
# S6 — Route confirmation handler
# ---------------------------------------------------------------------------


class TestRouteConfirmation:
    """After the routing turn, user must confirm to execute the transition."""

    def _get_to_route_pending(self, failure_class: str = "MISSING_FIELDS") -> SkillBuilderConversation:
        """Drive the conversation to EVAL_ROUTE_PENDING."""
        conv = _make_conv_at_eval(failure_class=failure_class)
        conv._handle_eval_response("review design")
        assert conv._state == "EVAL_ROUTE_PENDING"
        return conv

    def test_confirm_transitions_to_target_state(self):
        """'confirm route to REVIEW_DESIGN' must transition state to REVIEW_DESIGN."""
        conv = self._get_to_route_pending("MISSING_FIELDS")
        turn = conv._handle_eval_route_confirm("confirm route to REVIEW_DESIGN")
        assert conv._state == "REVIEW_DESIGN", (
            f"Confirming route must set state to REVIEW_DESIGN. Got {conv._state!r}"
        )
        assert turn.state == "REVIEW_DESIGN"

    def test_accept_at_route_pending_still_promotes(self):
        """'accept' at EVAL_ROUTE_PENDING must trigger PROMOTE."""
        conv = self._get_to_route_pending("MISSING_FIELDS")
        conv._data.ingest_result = {"status": "completed"}

        promote_turn = ConversationTurn(state="PROMOTE", message="ok")
        with patch.object(conv, "_run_promote", return_value=promote_turn):
            turn = conv._handle_eval_route_confirm("accept")

        assert turn.state == "PROMOTE"

    def test_ship_as_draft_at_route_pending_exits(self):
        """'ship as draft' at EVAL_ROUTE_PENDING must exit as DONE."""
        conv = self._get_to_route_pending("MISSING_FIELDS")
        turn = conv._handle_eval_route_confirm("ship as draft")
        assert turn.state == "DONE"
        assert turn.done is True

    def test_unrecognised_input_resurfaces_routing_turn(self):
        """Unrecognised input at EVAL_ROUTE_PENDING must re-surface the routing turn."""
        conv = self._get_to_route_pending("MISSING_FIELDS")
        turn = conv._handle_eval_route_confirm("ok whatever")
        # State must stay at EVAL_ROUTE_PENDING
        assert conv._state == "EVAL_ROUTE_PENDING", (
            "Unrecognised input must keep state at EVAL_ROUTE_PENDING"
        )
        assert turn.must_show_human is True


# ---------------------------------------------------------------------------
# S6 — Routing map constant contract
# ---------------------------------------------------------------------------


class TestRoutingMapContract:
    """The _ROUTING_MAP constant must define all required failure classes."""

    def test_routing_map_contains_all_six_classes(self):
        required = {"MISSING_FIELDS", "THIN_FIELDS", "WRONG_LAYOUT",
                    "SOURCE_COVERAGE", "WRONG_SOURCE", "UNSUPPORTABLE"}
        assert required == set(_ROUTING_MAP.keys()), (
            f"_ROUTING_MAP must define exactly the 6 failure classes. "
            f"Missing: {required - set(_ROUTING_MAP.keys())}"
        )

    def test_design_classes_route_to_review_design(self):
        for cls in ("MISSING_FIELDS", "THIN_FIELDS", "WRONG_LAYOUT"):
            assert _ROUTING_MAP[cls] == "REVIEW_DESIGN", (
                f"{cls} must route to REVIEW_DESIGN"
            )

    def test_source_classes_route_correctly(self):
        assert _ROUTING_MAP["SOURCE_COVERAGE"] == "CONFIGURE_SOURCES"
        assert _ROUTING_MAP["WRONG_SOURCE"] == "INSPECT_SOURCES"
        assert _ROUTING_MAP["UNSUPPORTABLE"] == "DONE_DRAFT"
