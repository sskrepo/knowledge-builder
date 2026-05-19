"""Tests for two bug fixes:

Fix 1 — executor.py Strategy 1b: ADB-backed wiki records (path="", content=CLOB)
  When WikiMetadataStore returns a record with path="" but content is set (ADB-backed,
  DECISION-022), Strategy 1b must use the inline content rather than silently returning
  empty body (which causes the passage to be skipped → Strategy 3 hard-fail).

Fix 2 — conversation.py _run_eval Path B: inject draft card into candidate list
  all_cards_including_draft() returns only on-disk cards. The in-authoring skill
  (not yet a committed disk YAML) was never in the candidate list, causing the LLM
  classifier to route positive queries to other skills. Fix: inject the draft card
  built from design_skill_card session data before calling classify().

Patch targets follow the DEFINING module pattern (same as test_decision021).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from framework.orchestrator.intent_classifier import IntentClassification
from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData


# ---------------------------------------------------------------------------
# Patch targets — defining module names (local imports in _run_eval)
# ---------------------------------------------------------------------------
_IC_PATH = "framework.orchestrator.intent_classifier.IntentClassifier"
_SF_PATH = "framework.orchestrator.shim_faaas.ShimFaaas"
_SW_PATH = "framework.orchestrator.shim_workflows.ShimWorkflows"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_skill_store(artifact=None):
    ss = MagicMock()
    ss.list_promoted_workflow_skills.return_value = set()
    ss.read_artifact.side_effect = Exception("no artifact — Path-A will skip")
    ss.promote.return_value = None
    return ss


def _make_stub_llm():
    llm = MagicMock()
    llm.provider = "stub"
    llm.chat.return_value = {"text": json.dumps({}), "tokens_out": 10}
    return llm


def _make_conv_at_eval(
    skill_name: str = "test_kiwi_skill",
    persona: str = "tpm",
    routing_queries: dict | None = None,
    design_skill_card_extra: dict | None = None,
) -> SkillBuilderConversation:
    """Create a SkillBuilderConversation ready for _run_eval."""
    skill_store = _make_skill_store()
    conv = SkillBuilderConversation(
        persona=persona,
        user_id="test-user",
        llm=_make_stub_llm(),
        skill_store=skill_store,
    )
    conv._state = "INGEST"
    conv._data.persona = persona
    conv._data.skill_name = skill_name
    conv._data.source_binding_mode = "author_fixed"
    card = {
        "summary": f"Generates {skill_name} output from Confluence.",
        "use_when": f"Invoke when you need {skill_name} output.",
        "example_invocations": [f"Create {skill_name} for exec review."],
        "do_not_invoke_if_phrases": ["not pptx", "mango project"],
        "routing_queries": routing_queries or {
            "positive": [f"Generate {skill_name} pptx for exec review"],
            "negative": [f"Tell me about something unrelated to {skill_name}"],
        },
    }
    if design_skill_card_extra:
        card.update(design_skill_card_extra)
    conv._data.design_skill_card = card
    conv._data.source_samples = {
        "confluence:test": [
            {"content": "Test content.", "source_citation": "https://example.com/test"}
        ]
    }
    conv._data.fields = ["rag_status"]
    conv._data.field_specs = {"rag_status": {"type": "string", "description": "RAG"}}
    return conv


def _run_eval_and_capture_candidates(
    conv: SkillBuilderConversation,
    classify_fn,
    disk_cards: list | None = None,
) -> list[dict]:
    """Run _run_eval, return the list of candidate cards passed to classify()."""
    received_workflows = []

    def recording_classify(q, persona=None, available_workflows=None, available_kbs=None):
        received_workflows.extend(available_workflows or [])
        return classify_fn(q, persona=persona, available_workflows=available_workflows)

    fake_classifier = MagicMock()
    fake_classifier._stub_mode.return_value = True
    fake_classifier.classify = recording_classify

    fake_shim_inst = MagicMock()
    fake_shim_inst.all_cards_including_draft.return_value = disk_cards or []
    fake_shim_inst.all_cards.return_value = []

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
            pass  # Path-A may fail; Path-B classify calls are what we assert

    return received_workflows


# ---------------------------------------------------------------------------
# Fix 1: Strategy 1b ADB inline content
# ---------------------------------------------------------------------------

class TestStrategy1bAdbContent(unittest.TestCase):
    """executor.py _retrieve_author_fixed_pinned — Strategy 1b ADB content path."""

    def _make_executor(self, wiki_store):
        from framework.workflow_runtime.executor import WorkflowExecutor
        llm = MagicMock()
        return WorkflowExecutor(llm=llm, wiki_store=wiki_store)

    def _make_wiki_store(self, records):
        store = MagicMock()
        store.list_pages.return_value = records
        return store

    def _make_cfg(self, pinned_ref="20382503622"):
        return {
            "workflow_skill": "test_skill",
            "persona": "tpm",
            "source_binding": {
                "mode": "author_fixed",
                "source_type": "confluence_page",
                "pinned_ref": pinned_ref,
                "ingest_on_demand": False,
            },
            "requires_extractions": [{"kb": "tpm.test_skill"}],
        }

    def test_strategy1b_uses_inline_content_when_path_empty(self):
        """Strategy 1b finds a passage from ADB content when path="" and content is set."""
        record = {
            "page_id": "20382503622",
            "title": "FAaaS Kiwi Project",
            "path": "",
            "content": "# FAaaS Kiwi Project\n\nStatus: Green\n",
            "canonical_ref": {
                "connector_id": "confluence",
                "resource_type": "page",
                "canonical_id": "20382503622",
            },
            "source_url": "https://confluence.oraclecorp.com/display/OCIFACP/FAaaS+Kiwi+Project",
            "persona": "tpm",
            "tags": [],
        }
        store = self._make_wiki_store([record])
        executor = self._make_executor(store)
        cfg = self._make_cfg()
        inputs = {"query": "Generate Kiwi PPTX"}

        passages = executor._retrieve_author_fixed_pinned(cfg, inputs, cfg["source_binding"])

        self.assertEqual(len(passages), 1, "Should have returned 1 passage from ADB content")
        self.assertIn("FAaaS Kiwi Project", passages[0]["text"])
        self.assertEqual(passages[0]["metadata"]["page_id"], "20382503622")
        self.assertEqual(passages[0]["metadata"]["canonical_ref"]["canonical_id"], "20382503622")

    def test_strategy1b_falls_through_to_strategy3_when_both_path_and_content_empty(self):
        """Strategy 1b returns no passage when both path="" and content="" → Strategy 3 raises."""
        record = {
            "page_id": "20382503622",
            "title": "FAaaS Kiwi Project",
            "path": "",
            "content": "",
            "canonical_ref": {
                "connector_id": "confluence",
                "resource_type": "page",
                "canonical_id": "20382503622",
            },
            "source_url": "https://confluence.oraclecorp.com/display/OCIFACP/FAaaS+Kiwi+Project",
            "persona": "tpm",
            "tags": [],
        }
        store = self._make_wiki_store([record])
        executor = self._make_executor(store)
        cfg = self._make_cfg()
        inputs = {"query": "Generate Kiwi PPTX"}

        from framework.workflow_runtime.executor import ConfluencePageNotInKBError
        with self.assertRaises(ConfluencePageNotInKBError):
            executor._retrieve_author_fixed_pinned(cfg, inputs, cfg["source_binding"])

    def test_strategy1b_filesystem_path_still_works(self):
        """Strategy 1b still reads from path when path is set (filestore backward-compat)."""
        content = "# Wiki page body from filesystem\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            record = {
                "page_id": "20382503622",
                "title": "FAaaS Kiwi Project",
                "path": tmp_path,
                "content": "",
                "canonical_ref": {
                    "connector_id": "confluence",
                    "resource_type": "page",
                    "canonical_id": "20382503622",
                },
                "source_url": "https://example.com",
                "persona": "tpm",
                "tags": [],
            }
            store = self._make_wiki_store([record])
            executor = self._make_executor(store)
            cfg = self._make_cfg()
            inputs = {"query": "Generate Kiwi PPTX"}

            passages = executor._retrieve_author_fixed_pinned(cfg, inputs, cfg["source_binding"])
            self.assertEqual(len(passages), 1)
            self.assertIn("Wiki page body from filesystem", passages[0]["text"])
        finally:
            os.unlink(tmp_path)

    def test_strategy1b_inline_content_fallback_when_path_file_missing(self):
        """When path exists but file is missing, fall back to inline content."""
        record = {
            "page_id": "20382503622",
            "title": "FAaaS Kiwi Project",
            "path": "/nonexistent/does/not/exist.md",
            "content": "# Fallback inline content from ADB\n",
            "canonical_ref": {
                "connector_id": "confluence",
                "resource_type": "page",
                "canonical_id": "20382503622",
            },
            "source_url": "https://example.com",
            "persona": "tpm",
            "tags": [],
        }
        store = self._make_wiki_store([record])
        executor = self._make_executor(store)
        cfg = self._make_cfg()
        inputs = {"query": "Generate Kiwi PPTX"}

        passages = executor._retrieve_author_fixed_pinned(cfg, inputs, cfg["source_binding"])
        self.assertEqual(len(passages), 1)
        self.assertIn("Fallback inline content from ADB", passages[0]["text"])


# ---------------------------------------------------------------------------
# Fix 2: Path B draft card injection
# ---------------------------------------------------------------------------

class TestPathBDraftCardInjection(unittest.TestCase):
    """conversation.py _run_eval — draft card injected into candidate list for Path B."""

    def test_draft_card_is_injected_when_no_disk_cards(self):
        """Draft card appears in candidates when there are no on-disk cards at all."""
        conv = _make_conv_at_eval(skill_name="my_unique_adb_only_skill")

        def classify_fn(q, persona=None, available_workflows=None):
            return IntentClassification(
                tier=1, confidence=0.9, persona="tpm", personas=None,
                workflow_skill="my_unique_adb_only_skill", reasoning="test",
            )

        candidates = _run_eval_and_capture_candidates(
            conv, classify_fn, disk_cards=[]
        )
        names = [c.get("name") for c in candidates]
        self.assertIn(
            "my_unique_adb_only_skill", names,
            f"Draft card not in candidates. Got names: {names}"
        )

    def test_draft_card_replaces_duplicate_disk_card(self):
        """When disk has a card with same skill_name, it's removed; draft card is added.

        The deduplication ensures only one card per skill_name is in the candidate set
        passed to each classify() call (not across all calls).
        """
        old_disk_card = {
            "name": "kiwi_pptx_skill",
            "persona": "tpm",
            "summary": "Old on-disk version",
        }
        conv = _make_conv_at_eval(skill_name="kiwi_pptx_skill")
        conv._data.design_skill_card["summary"] = "New draft version"

        # Capture the candidate list from a SINGLE classify call (first positive query)
        first_call_candidates = []

        def classify_fn(q, persona=None, available_workflows=None):
            if not first_call_candidates:
                first_call_candidates.extend(available_workflows or [])
            return IntentClassification(
                tier=1, confidence=0.9, persona="tpm", personas=None,
                workflow_skill="kiwi_pptx_skill", reasoning="test",
            )

        _run_eval_and_capture_candidates(
            conv, classify_fn, disk_cards=[old_disk_card]
        )
        # Each classify() call should have received exactly ONE card for this skill_name
        skill_cards = [c for c in first_call_candidates if c.get("name") == "kiwi_pptx_skill"]
        self.assertEqual(len(skill_cards), 1,
                         f"Expected exactly 1 'kiwi_pptx_skill' card per call. Got: {len(skill_cards)}")
        # Must be the draft card (summary=New draft version), not the old disk card
        self.assertNotEqual(skill_cards[0].get("summary"), "Old on-disk version",
                            "Got old disk card instead of draft card")
        self.assertEqual(skill_cards[0].get("summary"), "New draft version",
                         "Draft card summary mismatch")

    def test_other_disk_cards_are_not_removed(self):
        """Other skills' disk cards remain in the candidate list alongside the draft card."""
        other_disk_card = {
            "name": "weekly_email_skill",
            "persona": "tpm",
            "summary": "Weekly email skill — different from draft",
        }
        conv = _make_conv_at_eval(skill_name="brand_new_skill")

        def classify_fn(q, persona=None, available_workflows=None):
            return IntentClassification(
                tier=1, confidence=0.9, persona="tpm", personas=None,
                workflow_skill="brand_new_skill", reasoning="test",
            )

        candidates = _run_eval_and_capture_candidates(
            conv, classify_fn, disk_cards=[other_disk_card]
        )
        names = [c.get("name") for c in candidates]
        self.assertIn("weekly_email_skill", names,
                      f"Other disk card 'weekly_email_skill' was incorrectly removed. Got: {names}")
        self.assertIn("brand_new_skill", names,
                      f"Draft card 'brand_new_skill' is missing. Got: {names}")

    def test_draft_card_carries_routing_queries(self):
        """Draft card injected into candidates has routing_queries from design_skill_card."""
        conv = _make_conv_at_eval(
            skill_name="routing_queries_carrier",
            routing_queries={
                "positive": ["Unique positive query only this skill matches"],
                "negative": ["Unrelated negative query"],
            }
        )

        def classify_fn(q, persona=None, available_workflows=None):
            return IntentClassification(
                tier=1, confidence=0.9, persona="tpm", personas=None,
                workflow_skill="routing_queries_carrier", reasoning="test",
            )

        candidates = _run_eval_and_capture_candidates(conv, classify_fn, disk_cards=[])
        draft_cards = [c for c in candidates if c.get("name") == "routing_queries_carrier"]
        self.assertTrue(draft_cards, "Draft card not in candidates")
        card = draft_cards[0]
        # Card should carry _cfg.skill_card with routing_queries
        skill_card = card.get("_cfg", {}).get("skill_card", {})
        rq = skill_card.get("routing_queries") or card.get("routing_queries")
        self.assertIsNotNone(rq, "Draft card missing routing_queries")
        positive = (rq or {}).get("positive") or []
        self.assertTrue(
            any("Unique positive query" in p for p in positive),
            f"routing_queries.positive not carried through. Got: {positive}"
        )


if __name__ == "__main__":
    unittest.main()
