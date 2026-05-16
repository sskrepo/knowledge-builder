"""BUG-queue-2ad9a FIX 2 — synthesize_workflow skill card output_format token.

Verifies that _build_skill_card (and by extension synthesize_workflow_skill)
produces example_invocations that:
  1. Are longer than 150 characters (so enough context is present for the
     Tier-1 LLM router to distinguish skills).
  2. Contain the output_format token (e.g. 'eml', 'pptx') so the router
     can distinguish a .eml workflow from a .pptx workflow even when task
     descriptions are similar.

Before the fix: example_invocations[0] = task[:100] which (a) was truncated
too early and (b) never mentioned the output format, causing silent wrong-output
routing in the Tier-1 classifier (BUG-queue-2ad9a root cause).
"""
from __future__ import annotations

import pytest

from framework.skill_builder.synthesize_workflow import (
    synthesize_workflow_skill,
    _build_skill_card,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LONG_TASK = (
    "Create a weekly executive review deck for the 26AI project that pulls "
    "status from Confluence pages including ORM, risk register, and key "
    "milestone tracker. Produce a slide deck with a per-project RAG summary, "
    "top 3 risks, and owner actions."
)  # 290+ chars


# ---------------------------------------------------------------------------
# _build_skill_card unit tests
# ---------------------------------------------------------------------------

class TestBuildSkillCard:
    def test_example_invocation_longer_than_150_chars(self):
        """example_invocations[0] must be > 150 chars (was capped at 100 before fix)."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        ex = card["example_invocations"][0]
        assert len(ex) > 150, (
            f"example_invocations[0] length={len(ex)} must be > 150 "
            f"so the Tier-1 router has enough context. Got: {ex!r}"
        )

    def test_example_invocation_contains_output_format_pptx(self):
        """example_invocations[0] must contain 'pptx' for pptx skills."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        ex = card["example_invocations"][0]
        assert "pptx" in ex.lower(), (
            f"'pptx' must appear in example_invocations[0] so the router "
            f"can distinguish it from .eml skills. Got: {ex!r}"
        )

    def test_example_invocation_contains_output_format_eml(self):
        """example_invocations[0] must contain 'eml' for email skills."""
        card = _build_skill_card(
            "Send a project tracking stakeholder status email every Monday morning",
            "stakeholder_status_email",
            "eml",
        )
        ex = card["example_invocations"][0]
        assert "eml" in ex.lower(), (
            f"'eml' must appear in example_invocations[0]. Got: {ex!r}"
        )

    def test_use_when_contains_output_format(self):
        """use_when should also mention the output format."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        assert "pptx" in card["use_when"].lower(), (
            f"use_when should reference 'pptx' output type. Got: {card['use_when']!r}"
        )

    def test_summary_unchanged(self):
        """summary should still be task[:200]."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        assert card["summary"] == _LONG_TASK[:200]

    def test_do_not_use_for_present(self):
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        assert "do_not_use_for" in card
        assert len(card["do_not_use_for"]) > 0

    def test_default_output_format_markdown(self):
        """Default output_format is 'markdown' if not provided."""
        card = _build_skill_card("some task", "some_skill")
        ex = card["example_invocations"][0]
        assert "markdown" in ex.lower(), (
            f"Default output_format 'markdown' must appear in example. Got: {ex!r}"
        )


# ---------------------------------------------------------------------------
# synthesize_workflow_skill integration (output_format flows through)
# ---------------------------------------------------------------------------

class TestSynthesizeWorkflowSkill:
    def _make_intent(self, output_format: str = "pptx") -> dict:
        return {
            "task_description": _LONG_TASK,
            "output_format": output_format,
            "trigger": {"on_request": True},
            "delivery": {"kind": "filesystem", "path": f"~/.kbf/outputs/test.{output_format}"},
        }

    def test_pptx_skill_card_contains_pptx_token(self):
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_exec_review",
            intent=self._make_intent("pptx"),
            fields=["rag_status", "top_milestones"],
        )
        ex = result["skill_card"]["example_invocations"][0]
        assert "pptx" in ex.lower(), (
            f"synthesize_workflow_skill: pptx skill card must include 'pptx' token. Got: {ex!r}"
        )
        assert len(ex) > 150, (
            f"example_invocations[0] length={len(ex)} must be > 150 chars."
        )

    def test_eml_skill_card_contains_eml_token(self):
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="stakeholder_status_email",
            intent=self._make_intent("eml"),
            fields=["project_name", "rag_status"],
        )
        ex = result["skill_card"]["example_invocations"][0]
        assert "eml" in ex.lower(), (
            f"synthesize_workflow_skill: eml skill card must include 'eml' token. Got: {ex!r}"
        )

    def test_pptx_and_eml_example_invocations_are_different(self):
        """pptx and eml skills with the same task must produce different
        example_invocations so the Tier-1 classifier can tell them apart."""
        pptx_result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_pptx",
            intent=self._make_intent("pptx"),
            fields=["rag_status"],
        )
        eml_result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_eml",
            intent=self._make_intent("eml"),
            fields=["rag_status"],
        )
        pptx_ex = pptx_result["skill_card"]["example_invocations"][0]
        eml_ex = eml_result["skill_card"]["example_invocations"][0]
        assert pptx_ex != eml_ex, (
            "pptx and eml skill example_invocations must differ "
            "(output_format token differentiates them for the Tier-1 router)."
        )
        assert "pptx" in pptx_ex.lower()
        assert "eml" in eml_ex.lower()
