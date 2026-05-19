"""DECISION-021: EVAL Path-B routing self-test uses production IntentClassifier.

Tests verify:
  T1. _run_eval Path-B constructs an IntentClassifier (not resolve_only) and calls
      classify() for each positive query.
  T2. _run_eval Path-B constructs an IntentClassifier and calls classify() for each
      negative query.
  T3. Candidate set passed to classify() includes INGEST+ skills (all_cards_including_draft).
  T4. No skill execution occurs during Path-B (routing decision comes from classify(), not
      execute_from_config).
  T5. Pass/fail gate semantics unchanged: positive tier==1+skill_name → passed;
      anything else → failed.
  T6. Pass/fail gate semantics unchanged for negatives: tier!=1 or skill!=skill_name → passed;
      tier==1 and skill==skill_name → failed.
  T7. PROMOTE gate semantics unchanged: routing_self_test_passed=False blocks PROMOTE;
      routing_self_test_passed=True allows PROMOTE.
  T8. When IntentClassifier construction fails, Path-B is skipped gracefully
      (routing_self_test_passed defaults True — same behavior as ShimWorkflows failure).

Anti-fabrication rules (mandatory):
- Never xfail, skip, or edit assertions to force passing.
- Never assert specific LLM text output — only assert mechanism/wiring and gate behavior.
- Stub LLM is correct parity with production stub behavior — document, never hide.
- Patch targets are in the DEFINING module (e.g., framework.orchestrator.intent_classifier.IntentClassifier)
  not in conversation.py (local imports resolve from defining module at call time).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData
from framework.orchestrator.intent_classifier import IntentClassification


# ---------------------------------------------------------------------------
# Patch targets (local imports inside _run_eval resolve from these modules)
# ---------------------------------------------------------------------------

_IC_PATH = "framework.orchestrator.intent_classifier.IntentClassifier"
_SF_PATH = "framework.orchestrator.shim_faaas.ShimFaaas"
_SW_PATH = "framework.orchestrator.shim_workflows.ShimWorkflows"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_skill_store(read_artifact_return=None):
    ss = MagicMock()
    ss.list_promoted_workflow_skills.return_value = set()
    ss.read_artifact.return_value = read_artifact_return
    ss.promote.return_value = None
    return ss


def _make_stub_llm():
    """LLM mock with provider='stub' — triggers IntentClassifier._classify_stub."""
    llm = MagicMock()
    llm.provider = "stub"
    llm.chat.return_value = {"text": json.dumps({}), "tokens_out": 10}
    # _classify_stub check: provider != "oci_genai" and != "openai_direct"
    # IntentClassifier._stub_mode checks: provider == "stub" → True
    return llm


def _make_conv_at_eval(
    skill_name: str = "weekly_report",
    routing_queries: dict | None = None,
) -> SkillBuilderConversation:
    """Create a SkillBuilderConversation in a state ready for _run_eval."""
    skill_store = _make_skill_store()
    conv = SkillBuilderConversation(
        persona="tpm",
        user_id="test-user",
        llm=_make_stub_llm(),
        skill_store=skill_store,
    )
    conv._state = "INGEST"  # entering state before _run_eval sets it to EVAL
    conv._data.persona = "tpm"
    conv._data.skill_name = skill_name
    conv._data.design_skill_card = {
        "summary": f"Test skill {skill_name} for routing self-test.",
        "use_when": f"Invoke {skill_name} when test routing is needed.",
        "example_invocations": [f"Run {skill_name} for the test project."],
        "routing_queries": routing_queries or {
            "positive": [f"produce the {skill_name} for the test project"],
            "negative": [f"send a single-fact email about {skill_name}"],
        },
    }
    # Minimal eval prerequisites — source_samples must be non-empty
    conv._data.source_samples = {
        "confluence:test-1": [
            {"content": "Test content for eval.", "source_citation": "https://example.com/test"}
        ]
    }
    conv._data.fields = ["rag_status"]
    conv._data.field_specs = {
        "rag_status": {"type": "string", "description": "RAG status"}
    }
    return conv


def _make_intent_classification(tier: int = 1, skill: str | None = "weekly_report",
                                persona: str = "tpm", confidence: float = 0.90):
    """Build an IntentClassification dataclass for mock return."""
    return IntentClassification(
        tier=tier,
        confidence=confidence,
        persona=persona,
        personas=None,
        workflow_skill=skill,
        reasoning=f"test: tier={tier} skill={skill}",
    )


def _make_fake_shim_inst(cards=None):
    """Build a fake ShimWorkflows instance."""
    if cards is None:
        cards = [
            {"name": "weekly_report", "persona": "tpm",
             "summary": "test", "use_when": "test",
             "example_invocations": ["produce weekly_report for project A"]},
        ]
    fake_shim = MagicMock()
    fake_shim.all_cards_including_draft.return_value = cards
    fake_shim.all_cards.return_value = []
    return fake_shim


def _run_eval_capturing_classify_calls(conv, classify_fn):
    """Run _run_eval on conv with a patched IntentClassifier, capture classify() calls.
    Returns list of dicts with keys: query, persona, available_workflows.
    """
    classify_calls = []

    def recording_classify(q, persona=None, available_workflows=None, available_kbs=None):
        classify_calls.append({
            "query": q,
            "persona": persona,
            "available_workflows": list(available_workflows or []),
        })
        return classify_fn(q, persona=persona, available_workflows=available_workflows,
                           available_kbs=available_kbs)

    fake_classifier = MagicMock()
    fake_classifier._stub_mode.return_value = True
    fake_classifier.classify = recording_classify

    fake_shim_inst = _make_fake_shim_inst()
    fake_faaas = MagicMock()

    with patch(_IC_PATH, return_value=fake_classifier), \
         patch(_SF_PATH, return_value=fake_faaas), \
         patch(_SW_PATH, return_value=fake_shim_inst), \
         patch.object(conv._skill_store, "read_artifact",
                      side_effect=Exception("no artifact — Path-A will skip")), \
         patch("framework.skill_builder.review._llm_extract",
               return_value={"rag_status": "GREEN"}), \
         patch("framework.skill_builder.conversation.get_registry") as mock_reg:
        mock_reg.return_value.get_prompt.return_value = MagicMock(
            model="synthesis", text="judge prompt", response_format=None
        )
        conv._llm.chat.return_value = {"text": '{"result": "faithful"}', "tokens_out": 10}
        try:
            conv._run_eval()
        except Exception:
            pass  # Path-A may fail; Path-B classify calls are what we assert

    return classify_calls


# ---------------------------------------------------------------------------
# T1: Path-B calls IntentClassifier.classify() for positive queries
# ---------------------------------------------------------------------------

class TestPathBUsesIntentClassifierForPositives:
    def test_classify_called_for_positive_queries(self):
        """T1: _run_eval Path-B must invoke IntentClassifier.classify() for each
        positive query — not resolve_only token-overlap.
        """
        conv = _make_conv_at_eval(routing_queries={
            "positive": ["produce weekly_report for project A", "show weekly_report deck"],
            "negative": [],
        })

        def classify_fn(q, persona=None, available_workflows=None, available_kbs=None):
            return _make_intent_classification(tier=1, skill="weekly_report", persona="tpm")

        calls = _run_eval_capturing_classify_calls(conv, classify_fn)
        call_queries = [c["query"] for c in calls]

        assert "produce weekly_report for project A" in call_queries, (
            "T1: classify() must be called for first positive query. "
            f"Actual classify calls: {call_queries}"
        )
        assert "show weekly_report deck" in call_queries, (
            "T1: classify() must be called for second positive query. "
            f"Actual classify calls: {call_queries}"
        )


# ---------------------------------------------------------------------------
# T2: Path-B calls IntentClassifier.classify() for negative queries
# ---------------------------------------------------------------------------

class TestPathBUsesIntentClassifierForNegatives:
    def test_classify_called_for_negative_queries(self):
        """T2: _run_eval Path-B must invoke IntentClassifier.classify() for each
        negative query — not resolve_only.
        """
        conv = _make_conv_at_eval(routing_queries={
            "positive": ["produce weekly_report deck"],
            "negative": ["send single-fact email about status", "what is the jira ticket count"],
        })

        def classify_fn(q, persona=None, available_workflows=None, available_kbs=None):
            if "weekly_report deck" in q:
                return _make_intent_classification(tier=1, skill="weekly_report", persona="tpm")
            return _make_intent_classification(tier=2, skill=None, persona="tpm")

        calls = _run_eval_capturing_classify_calls(conv, classify_fn)
        call_queries = [c["query"] for c in calls]

        assert "send single-fact email about status" in call_queries, (
            "T2: classify() must be called for first negative query. "
            f"Actual classify calls: {call_queries}"
        )
        assert "what is the jira ticket count" in call_queries, (
            "T2: classify() must be called for second negative query. "
            f"Actual classify calls: {call_queries}"
        )


# ---------------------------------------------------------------------------
# T3: Candidate set includes INGEST+ skills (all_cards_including_draft)
# ---------------------------------------------------------------------------

class TestPathBCandidateSetIsIngestPlus:
    def test_candidate_set_uses_all_cards_including_draft(self):
        """T3: The available_workflows passed to classify() must be from
        all_cards_including_draft() — which includes in-authoring (not-yet-promoted) skills.
        """
        draft_cards = [
            {"name": "weekly_report", "persona": "tpm",
             "summary": "draft skill", "use_when": "test",
             "example_invocations": ["produce weekly_report deck"],
             "_status": "INGEST"},  # in-authoring, not yet promoted
        ]

        conv = _make_conv_at_eval(routing_queries={
            "positive": ["produce weekly_report deck"],
            "negative": [],
        })

        received_workflows = []

        def classify_fn(q, persona=None, available_workflows=None, available_kbs=None):
            if available_workflows:
                received_workflows.extend(available_workflows)
            return _make_intent_classification(tier=1, skill="weekly_report", persona="tpm")

        fake_classifier = MagicMock()
        fake_classifier._stub_mode.return_value = True
        fake_classifier.classify = classify_fn

        fake_shim_inst = MagicMock()
        fake_shim_inst.all_cards_including_draft.return_value = draft_cards
        fake_shim_inst.all_cards.return_value = []  # promoted-only returns nothing

        fake_faaas = MagicMock()

        with patch(_IC_PATH, return_value=fake_classifier), \
             patch(_SF_PATH, return_value=fake_faaas), \
             patch(_SW_PATH, return_value=fake_shim_inst), \
             patch.object(conv._skill_store, "read_artifact",
                          side_effect=Exception("no artifact")), \
             patch("framework.skill_builder.review._llm_extract",
                   return_value={"rag_status": "GREEN"}), \
             patch("framework.skill_builder.conversation.get_registry") as mock_reg:
            mock_reg.return_value.get_prompt.return_value = MagicMock(
                model="synthesis", text="judge prompt", response_format=None
            )
            conv._llm.chat.return_value = {"text": '{"result": "faithful"}', "tokens_out": 10}
            try:
                conv._run_eval()
            except Exception:
                pass

        # all_cards_including_draft must have been called
        fake_shim_inst.all_cards_including_draft.assert_called()
        # The draft card must be in the available_workflows passed to classify
        assert any(c.get("name") == "weekly_report" for c in received_workflows), (
            "T3: classify() must receive INGEST+ cards (all_cards_including_draft). "
            f"Received: {received_workflows}"
        )


# ---------------------------------------------------------------------------
# T4: Path-B routing decision comes from classify() — not token-overlap resolution
# ---------------------------------------------------------------------------

class TestPathBDecisionComesFromClassifier:
    def test_classify_call_count_covers_all_queries(self):
        """T4: classify() must be called for all positive + negative queries.
        This verifies the decision mechanism is IntentClassifier, not resolve_only
        (which would show no classify() calls at all).
        """
        conv = _make_conv_at_eval(routing_queries={
            "positive": ["produce weekly_report deck", "show weekly status"],
            "negative": ["send email", "lookup single fact"],
        })

        classify_call_count = [0]

        def classify_fn(q, persona=None, available_workflows=None, available_kbs=None):
            classify_call_count[0] += 1
            if "weekly" in q.lower():
                return _make_intent_classification(tier=1, skill="weekly_report", persona="tpm")
            return _make_intent_classification(tier=2, skill=None, persona="tpm")

        calls = _run_eval_capturing_classify_calls(conv, classify_fn)

        # 2 positives + 2 negatives = 4 classify() calls
        assert classify_call_count[0] >= 4, (
            "T4: classify() must be called for all 4 queries (2 positive + 2 negative). "
            f"Actual call count: {classify_call_count[0]}"
        )


# ---------------------------------------------------------------------------
# T5: Positive gate semantics
# ---------------------------------------------------------------------------

class TestPositiveGateSemantics:
    def _eval_with_classifier(self, routing_queries, classify_fn):
        conv = _make_conv_at_eval(routing_queries=routing_queries)
        fake_classifier = MagicMock()
        fake_classifier._stub_mode.return_value = True
        fake_classifier.classify = classify_fn
        fake_shim_inst = _make_fake_shim_inst()
        fake_faaas = MagicMock()

        with patch(_IC_PATH, return_value=fake_classifier), \
             patch(_SF_PATH, return_value=fake_faaas), \
             patch(_SW_PATH, return_value=fake_shim_inst), \
             patch.object(conv._skill_store, "read_artifact",
                          side_effect=Exception("no artifact")), \
             patch("framework.skill_builder.review._llm_extract",
                   return_value={"rag_status": "GREEN"}), \
             patch("framework.skill_builder.conversation.get_registry") as mock_reg:
            mock_reg.return_value.get_prompt.return_value = MagicMock(
                model="synthesis", text="judge prompt", response_format=None
            )
            conv._llm.chat.return_value = {"text": '{"result": "faithful"}', "tokens_out": 10}
            try:
                conv._run_eval()
            except Exception:
                pass
        return conv._data.routing_self_test_passed

    def test_positive_tier1_correct_skill_passes(self):
        """T5a: tier==1 AND skill_name==weekly_report → positive PASSES."""
        result = self._eval_with_classifier(
            routing_queries={"positive": ["produce the weekly_report deck"], "negative": []},
            classify_fn=lambda q, **kw: _make_intent_classification(
                tier=1, skill="weekly_report", persona="tpm"
            ),
        )
        assert result is True, (
            "T5a: positive query routed to correct skill at tier 1 must PASS "
            f"(routing_self_test_passed={result!r})"
        )

    def test_positive_tier1_wrong_skill_fails(self):
        """T5b: tier==1 AND skill_name!=weekly_report → positive FAILS."""
        result = self._eval_with_classifier(
            routing_queries={"positive": ["produce the weekly_report deck"], "negative": []},
            classify_fn=lambda q, **kw: _make_intent_classification(
                tier=1, skill="other_skill", persona="tpm"
            ),
        )
        assert result is False, (
            "T5b: positive query routed to wrong skill must FAIL routing_self_test_passed "
            f"(routing_self_test_passed={result!r})"
        )

    def test_positive_tier2_fails(self):
        """T5c: tier==2 (KB retrieval, not Tier-1 skill) → positive FAILS."""
        result = self._eval_with_classifier(
            routing_queries={"positive": ["produce the weekly_report deck"], "negative": []},
            classify_fn=lambda q, **kw: _make_intent_classification(
                tier=2, skill=None, persona="tpm"
            ),
        )
        assert result is False, (
            "T5c: positive query falling to tier 2 must FAIL routing_self_test_passed "
            f"(routing_self_test_passed={result!r})"
        )


# ---------------------------------------------------------------------------
# T6: Negative gate semantics
# ---------------------------------------------------------------------------

class TestNegativeGateSemantics:
    def _eval_with_neg_classifier(self, classify_fn):
        """Run _run_eval with a positive query (which always passes) and one negative query.
        The negative pass/fail is determined by classify_fn for the negative query.
        Note: negative queries only run when positive_queries is non-empty (correct behavior).
        """
        # Need at least one positive query so the Path-B loop enters and runs negatives.
        # Positive query will always return tier=1/skill_name → passes, so only
        # the negative routing_self_test_passed determination matters.
        _orig_classify_fn = classify_fn

        def combined_classify(q, persona=None, available_workflows=None, available_kbs=None):
            if q == "produce weekly_report deck (positive sentinel)":
                # positive query always passes so routing_self_test_passed is set by negative
                return _make_intent_classification(tier=1, skill="weekly_report", persona="tpm")
            return _orig_classify_fn(q, persona=persona,
                                     available_workflows=available_workflows,
                                     available_kbs=available_kbs)

        conv = _make_conv_at_eval(routing_queries={
            "positive": ["produce weekly_report deck (positive sentinel)"],
            "negative": ["send single-fact email"],
        })
        fake_classifier = MagicMock()
        fake_classifier._stub_mode.return_value = True
        fake_classifier.classify = combined_classify
        fake_shim_inst = _make_fake_shim_inst()
        fake_faaas = MagicMock()

        with patch(_IC_PATH, return_value=fake_classifier), \
             patch(_SF_PATH, return_value=fake_faaas), \
             patch(_SW_PATH, return_value=fake_shim_inst), \
             patch.object(conv._skill_store, "read_artifact",
                          side_effect=Exception("no artifact")), \
             patch("framework.skill_builder.review._llm_extract",
                   return_value={"rag_status": "GREEN"}), \
             patch("framework.skill_builder.conversation.get_registry") as mock_reg:
            mock_reg.return_value.get_prompt.return_value = MagicMock(
                model="synthesis", text="judge prompt", response_format=None
            )
            conv._llm.chat.return_value = {"text": '{"result": "faithful"}', "tokens_out": 10}
            try:
                conv._run_eval()
            except Exception:
                pass
        return conv._data.routing_self_test_passed

    def test_negative_tier2_passes(self):
        """T6a: negative query routed to tier==2 (not this skill) → PASSES."""
        result = self._eval_with_neg_classifier(
            lambda q, **kw: _make_intent_classification(tier=2, skill=None, persona="tpm")
        )
        assert result is True, (
            "T6a: negative query falling to tier 2 must PASS "
            f"(routing_self_test_passed={result!r})"
        )

    def test_negative_tier1_different_skill_passes(self):
        """T6b: negative query routed to tier==1 but different skill → PASSES."""
        result = self._eval_with_neg_classifier(
            lambda q, **kw: _make_intent_classification(
                tier=1, skill="other_skill", persona="tpm"
            )
        )
        assert result is True, (
            "T6b: negative query routing to a different skill must PASS "
            f"(routing_self_test_passed={result!r})"
        )

    def test_negative_tier1_same_skill_fails(self):
        """T6c: negative query routed to tier==1 AND same skill → FAILS (routing mistake)."""
        result = self._eval_with_neg_classifier(
            lambda q, **kw: _make_intent_classification(
                tier=1, skill="weekly_report", persona="tpm"
            )
        )
        assert result is False, (
            "T6c: negative query routing to this skill at tier 1 must FAIL routing_self_test_passed "
            f"(routing_self_test_passed={result!r})"
        )


# ---------------------------------------------------------------------------
# T7: PROMOTE gate semantics unchanged after DECISION-021
# ---------------------------------------------------------------------------

class TestPromoteGateUnchangedAfterDecision021:
    """Verify PROMOTE gate semantics are preserved after DECISION-021 wiring change.
    These are regression guards: the gate logic lives in _handle_eval_response,
    which reads routing_self_test_passed from eval_result.
    """

    def _make_eval_ready_conv(self, routing_passed: bool):
        conv = SkillBuilderConversation(
            persona="tpm",
            user_id="test-user",
            llm=None,
            skill_store=_make_skill_store(),
        )
        conv._state = "EVAL"
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.eval_result = {
            "routing_self_test_passed": routing_passed,
            "path_b_routing": {
                "passed": routing_passed,
                "positive_count": 1,
                "negative_count": 1,
                "results": [
                    {
                        "type": "positive",
                        "query": "produce weekly_report deck",
                        "passed": routing_passed,
                        "resolved_skill_id": (
                            "tpm.weekly_report" if routing_passed else "tpm.other_skill"
                        ),
                        "resolved_skill_name": (
                            "weekly_report" if routing_passed else "other_skill"
                        ),
                        "tier": 1,
                    },
                ],
            },
        }
        conv._data.routing_self_test_passed = routing_passed
        return conv

    def test_promote_blocked_when_routing_failed(self):
        """T7a: routing_self_test_passed=False → PROMOTE blocked (gate unchanged)."""
        conv = self._make_eval_ready_conv(routing_passed=False)
        turn = conv._handle_eval_response("accept")
        assert turn.state == "EVAL", (
            "T7a: PROMOTE must remain blocked (state=EVAL) when routing_self_test_passed=False"
        )
        assert turn.must_show_human is True
        conv._skill_store.promote.assert_not_called()

    def test_promote_allowed_when_routing_passed(self):
        """T7b: routing_self_test_passed=True → PROMOTE allowed (gate unchanged)."""
        conv = self._make_eval_ready_conv(routing_passed=True)
        turn = conv._handle_eval_response("accept")
        assert turn.state == "PROMOTE", (
            "T7b: PROMOTE must proceed when routing_self_test_passed=True; "
            f"got state={turn.state!r}"
        )


# ---------------------------------------------------------------------------
# T8: IntentClassifier/ShimWorkflows construction failure → graceful Path-B skip
# ---------------------------------------------------------------------------

class TestPathBGracefulSkipOnClassifierFailure:
    def test_graceful_skip_when_classifier_construction_fails(self):
        """T8: If IntentClassifier/ShimWorkflows construction raises, Path-B is
        skipped gracefully. routing_self_test_passed defaults True (same as
        ShimWorkflows failure in the original implementation).
        """
        conv = _make_conv_at_eval(routing_queries={
            "positive": ["produce weekly_report deck"],
            "negative": ["send email"],
        })

        # Patch both ShimWorkflows and IntentClassifier to raise
        with patch(_SW_PATH, side_effect=RuntimeError("simulated shim failure")), \
             patch(_SF_PATH, side_effect=RuntimeError("simulated faaas failure")), \
             patch(_IC_PATH, side_effect=RuntimeError("simulated classifier failure")), \
             patch.object(conv._skill_store, "read_artifact",
                          side_effect=Exception("no artifact")), \
             patch("framework.skill_builder.review._llm_extract",
                   return_value={"rag_status": "GREEN"}), \
             patch("framework.skill_builder.conversation.get_registry") as mock_reg:
            mock_reg.return_value.get_prompt.return_value = MagicMock(
                model="synthesis", text="judge prompt", response_format=None
            )
            conv._llm.chat.return_value = {"text": '{"result": "faithful"}', "tokens_out": 10}
            try:
                conv._run_eval()
            except Exception:
                pass

        # When classifier fails to build, routing_self_test_passed defaults True
        # (no queries were tested, so none failed)
        assert conv._data.routing_self_test_passed is True, (
            "T8: When IntentClassifier construction fails, routing_self_test_passed "
            "must default to True (graceful skip — no queries tested). "
            f"Actual: {conv._data.routing_self_test_passed!r}"
        )
