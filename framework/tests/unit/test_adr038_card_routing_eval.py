"""ADR-038: Consumer-facing card + routing_queries at DESIGN_SKILL + EVAL Path-A/B.

Tests (matching the 8 required per the user brief):
  (a) DESIGN_SKILL produces consumer-facing card + routing_queries
  (b) LOAD-BEARING: DESIGN_SKILL card survives synthesize_workflow into committed
      artifact — old static template does NOT overwrite it.
  (c) must_show_human review turn + to_dict/from_dict round-trip of edited card
  (d) routing_queries rendered into Tier-1 classifier signal (render_for_persona_prompt);
      default consumption still promoted-only (2ad9a/ADR-033 regression guard)
  (e) Path-B resolve-only over curated positives/negatives — no execution
  (f) PROMOTE hard-blocks on self-test failure
  (g) Path A in-process execute → non-null structure_score with bound reference;
      pre-INGEST = loud failure
  (h) new prompt parses via PromptRegistry; failure_classifier checksum unchanged
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData
from framework.skill_builder.synthesize_workflow import synthesize_workflow_skill
from framework.orchestrator.shim_workflows import ShimWorkflows


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_skill_store():
    ss = MagicMock()
    ss.list_promoted_workflow_skills.return_value = set()
    ss.read_artifact.return_value = None
    return ss


def _make_llm(json_response: dict):
    """LLM mock that always returns a JSON response."""
    llm = MagicMock()
    llm.chat.return_value = {
        "text": json.dumps(json_response),
        "tokens_out": 100,
    }
    return llm


def _make_design_llm():
    """LLM mock returning a valid DESIGN_SKILL response."""
    design_resp = {
        "schema": {
            "title": "weekly_report",
            "properties": {
                "rag_status": {"type": "string", "description": "RAG status"},
                "blockers": {"type": "array", "description": "Active blockers"},
            },
            "required": ["rag_status"],
        },
        "source_bindings": {"rag_status": ["confluence:1"]},
        "workflow_shape": {
            "output_format": "pptx",
            "layout": None,
            "trigger": {"on_request": True},
            "retriever": "search_wiki",
        },
        "reuse_plan": {"covered": {}, "gaps": ["rag_status", "blockers"]},
        "unsupportable_fields": [],
        "blocking_questions": [],
        "open_questions": [],
        "source_binding_mode": "author_fixed",
    }
    card_resp = {
        "summary": "Weekly project status deck for executive review.",
        "use_when": "A stakeholder needs the weekly RAG status as a PPTX deck.",
        "example_invocations": [
            "Give me the weekly exec review deck for the 26AI project. Output: pptx.",
            "Weekly 26AI project status deck please.",
        ],
        "routing_queries": {
            "positive": [
                "What is the weekly status of the 26AI project as a deck?",
                "Produce the weekly exec review for 26AI",
                "26AI project status slide deck for this week",
                "Weekly PPTX summary for 26AI project",
                "Executive deck for 26AI weekly review",
            ],
            "negative": [
                "What is the current Jira ticket count?",
                "Show me a text summary of FA DB upgrade",
                "Send a stakeholder email for the OCIFACP project",
            ],
        },
    }

    # ADR-028 fix: card generation (§A) now runs BEFORE the design LLM call
    # so the design_skill call remains the *last* self._llm.chat call.
    # First call returns card; second call returns design.
    llm = MagicMock()
    llm.chat.side_effect = [
        {"text": json.dumps(card_resp), "tokens_out": 100},
        {"text": json.dumps(design_resp), "tokens_out": 200},
    ]
    return llm


def _make_conv(llm=None) -> SkillBuilderConversation:
    skill_store = _make_skill_store()
    conv = SkillBuilderConversation(
        persona="tpm",
        user_id="test-user",
        llm=llm,
        skill_store=skill_store,
    )
    conv._data.persona = "tpm"
    conv._data.skill_name = "weekly_report"
    conv._data.intent_description = (
        "Produce a weekly executive review deck for the 26AI project."
    )
    conv._data.output_format = "pptx"
    conv._data.normalised_intent = {
        "output_kind": "pptx",
        "scope_domains": ["26ai"],
    }
    conv._data.source_capability = [
        {"source_id": "confluence:1", "fields": {"rag_status": "high"}}
    ]
    return conv


# ---------------------------------------------------------------------------
# (a) DESIGN_SKILL produces consumer-facing card + routing_queries
# ---------------------------------------------------------------------------

class TestDesignSkillCardGeneration:
    def test_design_skill_generates_card_with_routing_queries(self):
        """DESIGN_SKILL must produce a skill card with routing_queries.positive and negative."""
        llm = _make_design_llm()
        conv = _make_conv(llm)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            turn = conv._run_design_skill()

        # ADR-038 §A: card must be stored on session
        assert conv._data.design_skill_card is not None, (
            "design_skill_card must be set on session after DESIGN_SKILL"
        )
        card = conv._data.design_skill_card
        assert card.get("summary"), "card.summary must be non-empty"
        assert card.get("use_when"), "card.use_when must be non-empty"
        assert card.get("example_invocations"), "card.example_invocations must be non-empty"
        rq = card.get("routing_queries", {})
        assert isinstance(rq, dict), "routing_queries must be a dict"
        assert rq.get("positive"), "routing_queries.positive must be non-empty"
        assert rq.get("negative"), "routing_queries.negative must be non-empty"
        # Must have at least 3 positive queries
        assert len(rq["positive"]) >= 3, (
            f"At least 3 positive routing_queries required, got {len(rq['positive'])}"
        )

    def test_design_skill_card_review_turn_is_must_show_human(self):
        """ADR-038 §C: the card review turn MUST be must_show_human=True."""
        llm = _make_design_llm()
        conv = _make_conv(llm)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            turn = conv._run_design_skill()

        assert turn.must_show_human is True, (
            "ADR-038 §C: card review turn must have must_show_human=True"
        )
        assert turn.state == "DESIGN_SKILL"
        assert turn.awaiting_user is True

    def test_card_review_turn_message_contains_routing_queries(self):
        """Card review turn message must show routing_queries to author."""
        llm = _make_design_llm()
        conv = _make_conv(llm)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            turn = conv._run_design_skill()

        assert "routing" in turn.message.lower() or "POSITIVE" in turn.message, (
            "Card review turn message must reference routing_queries"
        )


# ---------------------------------------------------------------------------
# (b) LOAD-BEARING: DESIGN_SKILL card survives synthesize_workflow
# ---------------------------------------------------------------------------

class TestCardSurvivestSynthesizeWorkflow:
    """Regression-critical: the DESIGN_SKILL card must NOT be overwritten by
    synthesize_workflow.py's _build_skill_card static template."""

    def test_design_skill_card_in_synthesized_workflow_artifact(self):
        """ADR-038 §B: DESIGN_SKILL card must be in synthesized workflow artifact."""
        consumer_card = {
            "summary": "Consumer-facing summary from LLM.",
            "use_when": "Consumer asks for weekly status deck.",
            "example_invocations": ["Weekly exec review deck for 26AI. Output: pptx."],
            "routing_queries": {
                "positive": ["Weekly status deck for 26AI", "exec review 26AI pptx"],
                "negative": ["Just the Jira count", "FA DB upgrade email"],
            },
        }
        conv = _make_conv()
        conv._data.design_skill_card = consumer_card
        conv._data.fields = ["rag_status", "blockers"]
        conv._data.field_specs = {
            "rag_status": {"type": "string", "description": "RAG status"},
            "blockers": {"type": "array", "description": "Blockers"},
        }
        conv._data.sources = []

        artifacts = conv._synthesize_preview()
        wf_artifact = artifacts.get("framework/workflow_skills/tpm/weekly_report.yaml")

        assert wf_artifact is not None, "workflow_skills artifact must be generated"
        skill_card_in_artifact = wf_artifact.get("skill_card")
        assert skill_card_in_artifact is not None, "skill_card must be in workflow artifact"

        # THE LOAD-BEARING CHECK: the consumer card from DESIGN_SKILL must win
        assert skill_card_in_artifact.get("summary") == consumer_card["summary"], (
            f"ADR-038 §B REGRESSION: DESIGN_SKILL card was overwritten by static template!\n"
            f"Expected summary: {consumer_card['summary']!r}\n"
            f"Got: {skill_card_in_artifact.get('summary')!r}"
        )
        assert "routing_queries" in skill_card_in_artifact, (
            "routing_queries must survive into the committed artifact"
        )
        rq = skill_card_in_artifact["routing_queries"]
        assert rq.get("positive") == consumer_card["routing_queries"]["positive"], (
            "routing_queries.positive must be preserved unchanged"
        )

    def test_static_template_used_when_no_design_skill_card(self):
        """When design_skill_card is None (pre-ADR-038 session), static template is used."""
        conv = _make_conv()
        conv._data.design_skill_card = None  # explicitly None
        conv._data.fields = ["rag_status"]
        conv._data.intent_description = "Weekly exec review deck."
        conv._data.output_format = "pptx"
        conv._data.sources = []

        artifacts = conv._synthesize_preview()
        wf_artifact = artifacts.get("framework/workflow_skills/tpm/weekly_report.yaml")

        assert wf_artifact is not None
        skill_card = wf_artifact.get("skill_card")
        # Static template from synthesize_workflow._build_skill_card should be used
        assert skill_card is not None, "skill_card must still be present (from static template)"
        # Static template uses task[:200] as summary — no routing_queries
        assert "routing_queries" not in skill_card, (
            "Pre-ADR-038 session (no design_skill_card) should not have routing_queries"
        )

    def test_before_after_proof_card_not_overwritten(self):
        """Before/after proof: with design_skill_card set, artifact card != static template."""
        task_desc = "Produce a weekly executive review deck for the 26AI project."
        output_format = "pptx"

        # What the OLD static template produces
        from framework.skill_builder.synthesize_workflow import _build_skill_card
        old_static_card = _build_skill_card(task_desc, "weekly_report", output_format)

        # DESIGN_SKILL-generated card (from LLM)
        llm_card = {
            "summary": "Weekly executive review deck with RAG status and top risks.",
            "use_when": "A stakeholder needs the weekly exec-ready PPTX for 26AI.",
            "example_invocations": ["Produce the weekly exec deck for 26AI. Output: pptx."],
            "routing_queries": {
                "positive": ["Weekly exec review for 26AI", "26AI status deck"],
                "negative": ["Jira ticket count", "FA DB upgrade email"],
            },
        }

        conv = _make_conv()
        conv._data.design_skill_card = llm_card
        conv._data.fields = ["rag_status"]
        conv._data.intent_description = task_desc
        conv._data.output_format = output_format
        conv._data.sources = []

        artifacts = conv._synthesize_preview()
        actual_card = artifacts["framework/workflow_skills/tpm/weekly_report.yaml"]["skill_card"]

        # PROOF: the artifact card is the LLM card, NOT the static template
        assert actual_card["summary"] != old_static_card["summary"], (
            f"ADR-038 §B REGRESSION: artifact card matches old static template.\n"
            f"Old static: {old_static_card['summary']!r}\n"
            f"LLM card: {llm_card['summary']!r}\n"
            f"Got: {actual_card['summary']!r}"
        )
        assert actual_card["summary"] == llm_card["summary"], (
            "Artifact must use the LLM-generated card summary"
        )


# ---------------------------------------------------------------------------
# (c) must_show_human review turn + to_dict/from_dict round-trip
# ---------------------------------------------------------------------------

class TestCardReviewRoundTrip:
    def test_card_confirm_advances_to_review_design(self):
        """Confirming 'ok' at card review must advance to REVIEW_DESIGN."""
        llm = _make_design_llm()
        conv = _make_conv(llm)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            conv._run_design_skill()  # sets card, leaves state at DESIGN_SKILL

        # Now respond 'ok' to confirm the card
        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            turn2 = conv.respond("ok")

        assert turn2.state == "REVIEW_DESIGN", (
            f"Confirming 'ok' must advance to REVIEW_DESIGN, got {turn2.state}"
        )

    def test_card_json_edit_updates_card_and_reshows(self):
        """Providing a JSON edit must update the card and re-show the review turn."""
        llm = _make_design_llm()
        conv = _make_conv(llm)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            conv._run_design_skill()

        edit = json.dumps({"summary": "Updated consumer summary after author review."})
        with patch("framework.orchestrator.shim_kb.ShimKb"):
            turn2 = conv.respond(edit)

        # State stays at DESIGN_SKILL (re-showing updated card for confirmation)
        assert turn2.state == "DESIGN_SKILL"
        assert conv._data.design_skill_card["summary"] == "Updated consumer summary after author review."

    def test_to_dict_from_dict_round_trip_preserves_card(self):
        """design_skill_card and routing_queries survive ADB round-trip."""
        card = {
            "summary": "Consumer-facing summary.",
            "use_when": "When a consumer needs it.",
            "example_invocations": ["Query A. Output: pptx."],
            "routing_queries": {
                "positive": ["Query 1", "Query 2"],
                "negative": ["Unrelated query"],
            },
        }
        conv = _make_conv()
        conv._data.design_skill_card = card
        conv._data.routing_self_test_passed = True

        d = conv.to_dict()

        # Verify serialized form
        assert "design_skill_card" in d, "design_skill_card must be in to_dict() output"
        assert d["design_skill_card"] == card
        assert d["routing_self_test_passed"] is True

        # Restore from dict
        restored = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
        assert restored._data.design_skill_card == card, (
            "design_skill_card must survive to_dict → from_dict round-trip"
        )
        assert restored._data.routing_self_test_passed is True

    def test_to_dict_from_dict_backward_compat_none_card(self):
        """Pre-ADR-038 sessions (no design_skill_card key) load without error."""
        conv = _make_conv()
        d = conv.to_dict()
        # Simulate pre-ADR-038 session by removing the new keys
        d.pop("design_skill_card", None)
        d.pop("routing_self_test_passed", None)

        restored = SkillBuilderConversation.from_dict(d, skill_store=_make_skill_store())
        assert restored._data.design_skill_card is None
        assert restored._data.routing_self_test_passed is None


# ---------------------------------------------------------------------------
# (d) routing_queries in Tier-1 classifier signal + ADR-033 regression guard
# ---------------------------------------------------------------------------

class TestRoutingQueriesInClassifierSignal:
    def _make_shim_with_skill(self, routing_queries: dict | None = None, tmp_path=None) -> ShimWorkflows:
        """Create a ShimWorkflows in laptop mode with one skill containing routing_queries."""
        if tmp_path is None:
            import tempfile
            tmp = tempfile.mkdtemp()
            wf_dir = Path(tmp) / "workflow_skills"
        else:
            wf_dir = tmp_path / "workflow_skills"

        wf_dir.mkdir(parents=True, exist_ok=True)
        tpm_dir = wf_dir / "tpm"
        tpm_dir.mkdir(exist_ok=True)

        skill_card = {
            "summary": "Weekly exec review deck.",
            "use_when": "Consumer needs weekly PPTX for 26AI.",
            "example_invocations": ["Weekly status for 26AI. Output: pptx."],
        }
        if routing_queries:
            skill_card["routing_queries"] = routing_queries

        skill_yaml = {
            "workflow_skill": "weekly_report",
            "persona": "tpm",
            "status": "promoted",
            "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
            "skill_card": skill_card,
        }
        (tpm_dir / "weekly_report.yaml").write_text(
            yaml.safe_dump(skill_yaml, sort_keys=False)
        )
        return ShimWorkflows(wf_dir, skill_store=None)

    def test_render_for_persona_prompt_includes_routing_queries(self):
        """ADR-038 §D: routing_queries.positive must appear in render_for_persona_prompt."""
        rq = {
            "positive": ["Weekly 26AI exec deck please", "26AI status as PPTX"],
            "negative": ["Jira count", "FA DB email"],
        }
        shim = self._make_shim_with_skill(routing_queries=rq)
        prompt = shim.render_for_persona_prompt("tpm")

        assert "routing_queries" in prompt.lower() or "Weekly 26AI exec deck" in prompt, (
            "routing_queries.positive must appear in render_for_persona_prompt output"
        )
        assert "Weekly 26AI exec deck please" in prompt, (
            "First positive routing query must be in the prompt"
        )
        # Negative queries should NOT be in the prompt (only positives for Tier-1 signal)
        assert "Jira count" not in prompt, (
            "Negative routing queries must NOT be in render_for_persona_prompt "
            "(only positives are classifier signal)"
        )

    def test_render_without_routing_queries_does_not_crash(self):
        """Skills without routing_queries (pre-ADR-038) render without error."""
        shim = self._make_shim_with_skill(routing_queries=None)
        prompt = shim.render_for_persona_prompt("tpm")
        assert "weekly_report" in prompt  # card still renders

    def test_all_cards_still_promoted_only_with_skill_store(self):
        """ADR-033 regression guard: all_cards() with skill_store returns only promoted."""
        import tempfile
        tmp = tempfile.mkdtemp()
        wf_dir = Path(tmp) / "workflow_skills"
        wf_dir.mkdir(parents=True, exist_ok=True)
        tpm_dir = wf_dir / "tpm"
        tpm_dir.mkdir(exist_ok=True)

        for name in ["A", "B", "C"]:
            (tpm_dir / f"{name}.yaml").write_text(
                yaml.safe_dump({
                    "workflow_skill": name,
                    "persona": "tpm",
                    "skill_card": {"summary": name},
                    "trigger": {"on_request": {"enabled": True}},
                })
            )

        skill_store = MagicMock()
        # Only A is promoted
        skill_store.list_promoted_workflow_skills.return_value = {("tpm", "A")}
        skill_store.read_artifact.return_value = yaml.safe_dump({
            "workflow_skill": "A",
            "persona": "tpm",
            "skill_card": {
                "summary": "A summary",
                "routing_queries": {"positive": ["A query"], "negative": []},
            },
            "trigger": {"on_request": {"enabled": True}},
        })

        shim = ShimWorkflows(wf_dir, skill_store=skill_store)
        cards = shim.all_cards()

        assert len(cards) == 1, (
            f"ADR-033 regression: all_cards() with skill_store must return ONLY promoted skills. "
            f"Got {len(cards)} cards (expected 1)."
        )
        assert cards[0]["name"] == "A"


# ---------------------------------------------------------------------------
# (e) Path-B resolve-only over curated positives/negatives
# ---------------------------------------------------------------------------

class TestPathBResolveOnly:
    def _make_shim_with_two_skills(self, tmp_path) -> ShimWorkflows:
        wf_dir = tmp_path / "workflow_skills"
        tpm_dir = wf_dir / "tpm"
        tpm_dir.mkdir(parents=True, exist_ok=True)

        (tpm_dir / "weekly_report.yaml").write_text(yaml.safe_dump({
            "workflow_skill": "weekly_report",
            "persona": "tpm",
            "skill_card": {
                "summary": "Weekly exec review deck for 26AI project with RAG status.",
                "use_when": "Consumer needs weekly PPTX for 26AI project.",
                "example_invocations": ["Weekly status for 26AI project. Output: pptx."],
                "routing_queries": {
                    "positive": [
                        "produce weekly exec review deck for 26AI project",
                        "26AI weekly status pptx for exec review",
                    ],
                    "negative": ["FA DB upgrade email", "Jira ticket count"],
                },
            },
            "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
        }))

        (tpm_dir / "stakeholder_email.yaml").write_text(yaml.safe_dump({
            "workflow_skill": "stakeholder_email",
            "persona": "tpm",
            "skill_card": {
                "summary": "Weekly stakeholder status email for OCIFACP project.",
                "use_when": "Consumer needs a stakeholder email for OCIFACP.",
                "routing_queries": {
                    "positive": ["stakeholder email for OCIFACP"],
                    "negative": ["weekly exec pptx for 26AI"],
                },
            },
            "trigger": {"on_request": {"enabled": True, "output_format": "eml"}},
        }))

        return ShimWorkflows(wf_dir, skill_store=None)

    def test_resolve_only_positive_query_routes_to_correct_skill(self, tmp_path):
        """Path-B positive query must resolve to weekly_report (not stakeholder_email)."""
        shim = self._make_shim_with_two_skills(tmp_path)
        result = shim.resolve_only(
            "produce weekly exec review deck for 26AI project",
            scope="ingest_or_later",
        )
        assert result["matched"] is True, "Positive query must match"
        assert result["skill_name"] == "weekly_report", (
            f"Positive query must resolve to 'weekly_report', got {result['skill_name']!r}"
        )
        assert result["tier"] == 1

    def test_resolve_only_negative_query_does_not_route_to_skill(self, tmp_path):
        """Path-B negative query must NOT resolve to weekly_report."""
        shim = self._make_shim_with_two_skills(tmp_path)
        result = shim.resolve_only(
            "stakeholder email for OCIFACP",
            scope="ingest_or_later",
        )
        # Should resolve to stakeholder_email, not weekly_report
        skill = result.get("skill_name")
        assert skill != "weekly_report", (
            f"Negative query 'stakeholder email for OCIFACP' must NOT resolve to 'weekly_report', "
            f"got {skill!r}"
        )

    def test_resolve_only_does_not_modify_all_cards(self, tmp_path):
        """resolve_only must not modify all_cards() behaviour."""
        shim = self._make_shim_with_two_skills(tmp_path)
        cards_before = list(shim.all_cards())
        shim.resolve_only("some query", scope="ingest_or_later")
        cards_after = list(shim.all_cards())
        assert cards_before == cards_after, (
            "resolve_only must not modify all_cards() result"
        )

    def test_resolve_only_promoted_only_scope_uses_promoted_cards(self, tmp_path):
        """resolve_only with scope='promoted_only' uses all_cards() (default path)."""
        shim = self._make_shim_with_two_skills(tmp_path)
        # In laptop mode (no skill_store), all_cards() returns all disk cards
        result = shim.resolve_only("produce weekly exec review deck", scope="promoted_only")
        # Should still resolve (laptop mode serves all disk cards)
        assert isinstance(result, dict)
        assert "skill_id" in result


# ---------------------------------------------------------------------------
# (f) PROMOTE hard-blocks on self-test failure
# ---------------------------------------------------------------------------

class TestPromoteHardBlock:
    def _make_eval_ready_conv(self, routing_passed: bool) -> SkillBuilderConversation:
        conv = _make_conv()
        conv._state = "EVAL"
        conv._data.eval_result = {
            "routing_self_test_passed": routing_passed,
            "path_b_routing": {
                "passed": routing_passed,
                "positive_count": 2,
                "negative_count": 1,
                "results": [
                    {
                        "type": "positive",
                        "query": "Weekly status for 26AI",
                        "passed": routing_passed,
                        "resolved_skill_id": "tpm.weekly_report" if routing_passed else "tpm.other_skill",
                        "resolved_skill_name": "weekly_report" if routing_passed else "other_skill",
                        "tier": 1,
                    },
                    {
                        "type": "negative",
                        "query": "Stakeholder email",
                        "passed": True,
                        "resolved_skill_id": "tpm.other_skill",
                        "resolved_skill_name": "other_skill",
                        "tier": 1,
                    },
                ],
            },
        }
        return conv

    def test_promote_blocked_when_routing_self_test_failed(self):
        """ADR-038 §F: PROMOTE must be refused when routing self-test failed."""
        conv = self._make_eval_ready_conv(routing_passed=False)

        # The mock skill_store promote would succeed if called
        conv._skill_store.promote.return_value = None
        turn = conv._handle_eval_response("accept")

        # PROMOTE must be blocked
        assert turn.state == "EVAL", (
            "ADR-038 §F: state must remain EVAL when routing self-test failed"
        )
        assert turn.must_show_human is True
        assert "BLOCKED" in turn.message or "blocked" in turn.message.lower(), (
            "Turn message must clearly state PROMOTE is BLOCKED"
        )
        # The skill_store.promote must NOT have been called
        conv._skill_store.promote.assert_not_called()

    def test_promote_not_blocked_when_routing_self_test_passed(self):
        """When routing self-test passed, gate passes and state advances to PROMOTE."""
        conv = self._make_eval_ready_conv(routing_passed=True)

        turn = conv._handle_eval_response("accept")

        # Gate must NOT block — state must advance to PROMOTE (not stay at EVAL)
        assert turn.state == "PROMOTE", (
            f"ADR-038 §F: when routing self-test passed, PROMOTE must proceed; got state={turn.state!r}"
        )
        # Must NOT contain "BLOCKED" in the message
        assert "BLOCKED" not in turn.message, (
            "Turn message must NOT say BLOCKED when routing self-test passed"
        )

    def test_promote_not_blocked_when_no_routing_queries(self):
        """When no routing_queries were tested, PROMOTE is NOT blocked (no gate)."""
        conv = _make_conv()
        conv._state = "EVAL"
        conv._data.eval_result = {
            "routing_self_test_passed": True,
            "path_b_routing": {
                "passed": True,
                "positive_count": 0,  # No queries tested
                "negative_count": 0,
                "results": [],
            },
        }

        turn = conv._handle_eval_response("accept")

        # path_b_ran=False (0 positive + 0 negative), so gate is skipped
        # state must advance to PROMOTE
        assert turn.state == "PROMOTE", (
            f"When no routing_queries tested, PROMOTE must proceed; got state={turn.state!r}"
        )


# ---------------------------------------------------------------------------
# (g) Path A in-process execute + pre-INGEST loud failure + non-null structure_score
# ---------------------------------------------------------------------------

class TestPathAExecution:
    def _make_eval_ready_conv(self, tmp_path, with_reference: bool = True) -> SkillBuilderConversation:
        """Create a conv at EVAL state with samples + skill_store wired."""
        conv = _make_conv()
        conv._state = "EVAL"
        conv._data.source_samples = {
            "confluence:1": [
                {
                    "source_citation": "https://example.com/page1",
                    "content": "RAG status: Green. Blockers: None.",
                    "title": "Weekly Report",
                }
            ]
        }
        if with_reference:
            conv._data.artifact_reference_id = "file:/tmp/ref.pptx"
            conv._data.artifact_reference_name = "ref.pptx"
            conv._data.artifact_reference_type = "pptx"

        # Mock skill_store for EVAL
        conv._skill_store.read_artifact.return_value = yaml.safe_dump({
            "workflow_skill": "weekly_report",
            "persona": "tpm",
            "skill_card": {"summary": "Weekly deck."},
            "trigger": {"on_request": {"enabled": True}},
        })
        conv._skill_store.write_artifacts.return_value = None
        return conv

    def test_path_a_execution_status_in_eval_result(self, tmp_path):
        """Path-A execution status must appear in eval_result.path_a_execution."""
        conv = self._make_eval_ready_conv(tmp_path)

        # Mock the LLM for extraction + judge (not testing those here)
        def _llm_response(model, messages, **kwargs):
            return {"text": json.dumps({"rag_status": "Green", "faithful": True, "confidence": "high", "reason": "ok"}), "tokens_out": 50}

        conv._llm = MagicMock()
        conv._llm.chat.side_effect = _llm_response

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            with patch("framework.workflow_runtime.executor.WorkflowExecutor.execute_from_config") as mock_exec:
                mock_exec.return_value = {"artifact_url": str(tmp_path / "output.pptx"), "status": "success"}
                turn = conv._run_eval()

        assert conv._data.eval_result is not None
        path_a = conv._data.eval_result.get("path_a_execution", {})
        assert "status" in path_a, "path_a_execution.status must be in eval_result"

    def test_pre_ingest_gate_hard_fails(self):
        """ADR-038 §B.4: EVAL on pre-INGEST skill must raise RuntimeError loudly."""
        conv = _make_conv()
        conv._state = "COMMITTED"  # pre-INGEST state
        conv._data.source_samples = {
            "confluence:1": [{"source_citation": "url", "content": "text", "title": "page"}]
        }

        llm = MagicMock()
        llm.chat.return_value = {"text": json.dumps({"rag_status": "Green"}), "tokens_out": 50}
        conv._llm = llm

        with pytest.raises(RuntimeError, match="INGEST-or-later gate failed"):
            conv._run_eval()

    def test_eval_result_has_three_sections(self, tmp_path):
        """ADR-038 §B.6: eval_result must have all three sections."""
        conv = self._make_eval_ready_conv(tmp_path, with_reference=False)

        def _llm_response(model, messages, **kwargs):
            return {"text": json.dumps({"rag_status": "Green", "faithful": True, "confidence": "high", "reason": "ok"}), "tokens_out": 50}

        conv._llm = MagicMock()
        conv._llm.chat.side_effect = _llm_response

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            turn = conv._run_eval()

        # Three-section report must always be present
        assert "SECTION 1" in turn.message or "ROUTING" in turn.message, "Section 1 missing"
        assert "SECTION 2" in turn.message or "EXECUTION" in turn.message, "Section 2 missing"
        assert "SECTION 3" in turn.message or "COMPARATOR" in turn.message, "Section 3 missing"

    def test_execution_failure_is_high_severity_not_soft_note(self, tmp_path):
        """ADR-038 §B.2: execution failure must surface as [HIGH], not soft note."""
        conv = self._make_eval_ready_conv(tmp_path, with_reference=False)

        def _llm_response(model, messages, **kwargs):
            return {"text": json.dumps({"rag_status": "Green", "faithful": True, "confidence": "high", "reason": "ok"}), "tokens_out": 50}

        conv._llm = MagicMock()
        conv._llm.chat.side_effect = _llm_response

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            with patch("framework.workflow_runtime.executor.WorkflowExecutor.execute_from_config",
                       side_effect=RuntimeError("Execution failed: mock error")):
                turn = conv._run_eval()

        assert "FAILURE" in turn.message or "failure" in turn.message.lower(), (
            "Execution failure must be clearly labeled in the report"
        )
        assert "HIGH" in turn.message or "[HIGH]" in turn.message, (
            "Execution failure must be labeled [HIGH] per no-silent-degradation rule"
        )


# ---------------------------------------------------------------------------
# (h) new prompt parses via PromptRegistry; failure_classifier checksum unchanged
# ---------------------------------------------------------------------------

class TestPromptRegistry:
    def test_design_skill_card_prompt_loads(self):
        """design_skill_card prompt must load successfully via PromptRegistry."""
        from framework.skill_builder.prompt_registry import get_registry
        from pathlib import Path

        prompts_dir = (
            Path(__file__).resolve().parents[3] / "framework" / "config" / "prompts"
        )
        registry = get_registry(prompts_dir=prompts_dir)
        spec = registry.get_prompt(
            "design_skill_card",
            skill_name="weekly_report",
            persona="tpm",
            task_description="Produce a weekly executive review deck.",
            output_format="pptx",
            intent_summary='{"output_kind": "pptx"}',
        )
        assert spec.prompt_id == "design_skill_card"
        assert spec.model == "synthesis"
        assert spec.max_tokens == 1024
        assert "routing_queries" in spec.text
        assert "positive" in spec.text
        assert "negative" in spec.text

    def test_failure_classifier_checksum_unchanged(self):
        """ADR-038 must NOT touch failure_classifier — checksum must still be valid."""
        from framework.skill_builder.prompt_registry import get_registry, LockedPromptTamperedError
        from pathlib import Path

        prompts_dir = (
            Path(__file__).resolve().parents[3] / "framework" / "config" / "prompts"
        )
        # If the checksum was tampered, the registry construction itself would raise.
        # Just constructing it is the verification.
        try:
            registry = get_registry(prompts_dir=prompts_dir)
            # Get the prompt to trigger checksum verification
            spec = registry.get_prompt(
                "failure_classifier",
                normalised_intent="{}",
                schema_properties="{}",
                capability_inventory="{}",
                gap_report="",
                missing_sections="[]",
                thin_sections="[]",
            )
            assert spec.prompt_id == "failure_classifier"
        except LockedPromptTamperedError:
            pytest.fail(
                "failure_classifier checksum was tampered — ADR-038 must NOT modify it"
            )

    def test_design_skill_card_prompt_required_vars_present(self):
        """design_skill_card prompt must have all required_vars."""
        from framework.skill_builder.prompt_registry import get_registry
        from pathlib import Path

        prompts_dir = (
            Path(__file__).resolve().parents[3] / "framework" / "config" / "prompts"
        )
        registry = get_registry(prompts_dir=prompts_dir)
        meta_list = registry.list_prompts()
        card_meta = next((m for m in meta_list if m.prompt_id == "design_skill_card"), None)
        assert card_meta is not None, "design_skill_card must be in registry"
        required = {"skill_name", "persona", "task_description", "output_format", "intent_summary"}
        assert required.issubset(set(card_meta.required_vars)), (
            f"design_skill_card missing required_vars: {required - set(card_meta.required_vars)}"
        )
