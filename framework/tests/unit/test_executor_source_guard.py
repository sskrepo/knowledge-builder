"""ADR-032 P3 guard — ConfluencePageNotInKBError hard-fail tests.

Tests the no-silent-substitution invariant added to WorkflowExecutor._retrieve_for_inputs.

ADR-032 P2-Exec update: the P3 guard is now CONDITIONAL:
  - author_fixed skills: regex heuristic guard is RETAINED. Still hard-fails
    when the user supplies a page ref in free-text inputs and the retriever
    returns a different page.
  - ask_parameterized skills: regex guard is NOT applied. Instead, the schema-
    driven source_binding.input_param path is used (ephemeral fetch). The
    TestAskParameterizedRoutesToEphemeral class below proves this.

Coverage:
  1. pageId=18625350641 in input + retriever returns ONLY a passage citing
     page 20030556732 → hard-fail with actionable message; NO substitution
     (passages are not returned; render is never reached).
  2. pageId=20030556732 in input + retriever returns a passage citing
     20030556732 → passes through (matching page is accepted).
  3. Generic query (NO page ref) + retriever returns passages → UNCHANGED
     behaviour; guard is inert (proves no regression to fixed-source skills).
  4. URL form /pages/viewpage.action?pageId=18625350641 → same extraction as
     plain pageId= form.
  NEW (P2-Exec):
  5. ask_parameterized skill → ephemeral path taken (adapter called), regex guard
     NOT applied.
  6. ask_parameterized skill with page ref in free-text does NOT trigger P3 guard
     (the guard is conditional to author_fixed only).

All tests use the mock retriever / shim_kb patterns from existing executor tests.
No live LLM or DB calls.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from framework.core.interfaces import Result
from framework.workflow_runtime.executor import (
    ConfluencePageNotInKBError,
    WorkflowExecutor,
    _passage_matches_canonical,
)
from framework.adapters._base import CanonicalRef
from framework.adapters.confluence.shared import _extract_numeric_id_fast


# ---------------------------------------------------------------------------
# Helpers — build a minimal skill YAML and retriever / shim_kb mocks
# ---------------------------------------------------------------------------

REQUESTED_PAGE_ID = "18625350641"
INGESTED_PAGE_ID  = "20030556732"


def _make_skill_yaml(tmp_path: Path, skill_name: str = "test_skill") -> Path:
    """Write a minimal workflow skill YAML with one requires_extractions entry."""
    cfg = {
        "workflow_skill": skill_name,
        "persona": "tpm",
        "status": "promoted",
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [{"name": "input", "type": "string"}],
                "output_format": "email",
                "response_mode": "artifact_url",
            },
        },
        "requires_extractions": [
            {"kb": "tpm.project_tracking_test"},
        ],
        "synthesis": {
            "output_format": "email",
        },
        "delivery": {"kind": "filesystem", "path": "/tmp/test_output.eml"},
    }
    skill_dir = tmp_path / "workflow_skills" / "tpm"
    skill_dir.mkdir(parents=True, exist_ok=True)
    p = skill_dir / f"{skill_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _make_result(page_id: str) -> Result:
    """Build a Result whose metadata.page_id and citation_url identify page_id."""
    return Result(
        content_id=page_id,
        chunk_id=None,
        text=f"Content from page {page_id}",
        score=0.9,
        citation_url=f"wiki://{page_id}",
        metadata={"page_id": page_id, "title": f"Page {page_id}"},
    )


def _make_retriever(results: list[Result]):
    """Return a callable that ignores its arguments and returns `results`."""
    def retriever(query: str, persona: str | None = None) -> list[Result]:
        return results
    return retriever


def _make_shim_kb(kb_name: str = "project_tracking_test") -> MagicMock:
    """Minimal ShimKb mock returning a single card with search_wiki tool."""
    card = {
        "name": kb_name,
        "persona": "tpm",
        "retrieval_tools": ["search_wiki"],
    }
    shim = MagicMock()
    shim.all_cards.return_value = [card]
    return shim


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestExtractNumericIdFast:
    """ADR-039: unit tests for Confluence fast-path numeric ID extraction.

    Replaces deleted _extract_confluence_page_ids tests.
    Source identity is now resolved at author/bind time via canonical_identity(),
    not scanned from inputs at execution time.
    """

    def test_querystring_form(self):
        assert _extract_numeric_id_fast("?pageId=18625350641") == "18625350641"

    def test_bare_pageid_eq_form(self):
        assert _extract_numeric_id_fast("pageId=18625350641") == "18625350641"

    def test_viewpage_action_form(self):
        url = "https://confluence.example.com/pages/viewpage.action?pageId=18625350641"
        assert _extract_numeric_id_fast(url) == "18625350641"

    def test_pages_path_form(self):
        url = "https://confluence.example.com/wiki/spaces/FA/pages/18625350641/My+Page"
        assert _extract_numeric_id_fast(url) == "18625350641"

    def test_rest_api_form(self):
        url = "https://confluence.example.com/rest/api/content/18625350641"
        assert _extract_numeric_id_fast(url) == "18625350641"

    def test_bare_numeric_string(self):
        assert _extract_numeric_id_fast("18625350641") == "18625350641"

    def test_no_match_generic_query(self):
        assert _extract_numeric_id_fast("What are the project milestones?") is None

    def test_no_match_prose_number(self):
        """Short numbers in prose must NOT be matched."""
        assert _extract_numeric_id_fast("We released 42 items last week.") is None


class TestPassageMatchesCanonical:
    """ADR-039: unit tests for canonical==canonical passage matching.

    Replaces deleted _passage_matches_page_id tests.
    Two-sided canonical comparison: executor compares canonical_ref from
    passage metadata against the CanonicalRef from canonical_identity().
    """

    def _make_canonical(self, canonical_id: str) -> CanonicalRef:
        return CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id=canonical_id,
        )

    def test_canonical_ref_matches(self):
        passage = {
            "metadata": {
                "canonical_ref": {
                    "connector_id": "confluence",
                    "resource_type": "page",
                    "canonical_id": "18625350641",
                }
            },
            "citation": "",
        }
        assert _passage_matches_canonical(passage, self._make_canonical("18625350641")) is True

    def test_canonical_ref_no_match_different_id(self):
        passage = {
            "metadata": {
                "canonical_ref": {
                    "connector_id": "confluence",
                    "resource_type": "page",
                    "canonical_id": "20030556732",
                }
            },
            "citation": "",
        }
        assert _passage_matches_canonical(passage, self._make_canonical("18625350641")) is False

    def test_no_canonical_ref_returns_false(self):
        """Passage without canonical_ref metadata returns False (not an error)."""
        passage = {"metadata": {"page_id": "18625350641"}, "citation": "wiki://18625350641"}
        assert _passage_matches_canonical(passage, self._make_canonical("18625350641")) is False

    def test_empty_passage_returns_false(self):
        assert _passage_matches_canonical({}, self._make_canonical("18625350641")) is False

    def test_different_connector_id_no_match(self):
        passage = {
            "metadata": {
                "canonical_ref": {
                    "connector_id": "jira",
                    "resource_type": "page",
                    "canonical_id": "18625350641",
                }
            },
        }
        assert _passage_matches_canonical(passage, self._make_canonical("18625350641")) is False


# ---------------------------------------------------------------------------
# Integration tests — WorkflowExecutor._retrieve_for_inputs via execute()
# ---------------------------------------------------------------------------

class TestExecutorSourceGuard:
    """ADR-039: end-to-end tests for WorkflowExecutor source integrity.

    The P3 heuristic guard (input-scanning for pageId= patterns) was DELETED
    by ADR-039 (DECISION-020). Source identity is now enforced via
    source_binding.pinned_ref + canonical==canonical comparison.

    These tests verify the new canonical identity behavior:
    - author_fixed with no source_binding: generic KB retrieval, no guard
    - author_fixed with source_binding.pinned_ref: canonical==canonical guard
    - The ConfluencePageNotInKBError is still raised when pinned page absent
    """

    def _make_executor(self, results_for_retriever: list[Result]) -> tuple[WorkflowExecutor, MagicMock]:
        retriever = _make_retriever(results_for_retriever)
        shim_kb = _make_shim_kb("project_tracking_test")
        executor = WorkflowExecutor(
            retrievers={"search_wiki": retriever},
            shim_kb=shim_kb,
        )
        return executor, shim_kb

    # -------------------------------------------------------------------------
    # Test 1: no page ref in generic input — guard is inert (no regression)
    # ADR-039: the P3 input-scan guard is deleted; generic author_fixed passes through.
    # -------------------------------------------------------------------------

    def test_no_page_ref_guard_is_inert(self, tmp_path: Path):
        """Generic query with no source_binding: passages pass through, no exception.
        ADR-039: no P3 heuristic scanning of inputs — guard is structurally absent.
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        some_result = _make_result(INGESTED_PAGE_ID)

        executor, _ = self._make_executor([some_result])

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "What are the key milestones for the FA DB upgrade project?"},
            sources=[],
        )

        assert len(passages) >= 1, (
            "Generic author_fixed (no source_binding) must return passages without error"
        )

    def test_generic_pageid_input_no_exception(self, tmp_path: Path):
        """ADR-039: pageId= in inputs NO LONGER triggers a P3 guard.
        The old P3 heuristic is deleted. Generic author_fixed with no source_binding
        simply returns whatever the retriever provides.
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        some_result = _make_result(INGESTED_PAGE_ID)
        executor, _ = self._make_executor([some_result])

        # With ADR-039, pageId= in inputs does NOT trigger a guard for generic author_fixed
        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": f"Please draft an email using pageId={REQUESTED_PAGE_ID}"},
            sources=[],
        )
        # Returns passages without error — the guard is now canonical==canonical via pinned_ref
        assert len(passages) >= 1, (
            "ADR-039: generic author_fixed with pageId= in inputs must NOT raise "
            "(P3 heuristic deleted; identity guard requires source_binding.pinned_ref)"
        )

    # -------------------------------------------------------------------------
    # Test 2: author_fixed with pinned_ref — pinned page not in KB → hard-fail
    # This is the NEW canonical identity guard (replaces P3 heuristic).
    # -------------------------------------------------------------------------

    def _make_pinned_skill_yaml(self, tmp_path: Path, pinned_ref: str) -> Path:
        """Write a minimal author_fixed skill YAML with source_binding.pinned_ref."""
        cfg = {
            "workflow_skill": "pinned_skill",
            "persona": "tpm",
            "status": "promoted",
            "trigger": {
                "on_request": {
                    "enabled": True,
                    "inputs": [{"name": "input", "type": "string"}],
                    "output_format": "email",
                    "response_mode": "artifact_url",
                },
            },
            "source_binding": {
                "mode": "author_fixed",
                "source_type": "confluence_page",
                "pinned_ref": pinned_ref,
                "ingest_on_demand": False,
            },
            "requires_extractions": [
                {"field": "summary", "from": "body", "kb": "project_tracking_test"}
            ],
            "synthesis": {"template": "summarize", "mapping": [{"slide": 1, "field": "summary"}]},
            "delivery": {"channel": "email"},
        }
        p = tmp_path / "pinned_skill.yaml"
        p.write_text(yaml.dump(cfg))
        return p

    def test_pinned_ref_absent_from_kb_hard_fails(self, tmp_path: Path):
        """author_fixed with source_binding.pinned_ref: pinned page not in KB → hard-fail.
        ADR-039: canonical==canonical path replaces P3 heuristic.
        The executor resolves pinned_ref → CanonicalRef → searches KB → no match → error.
        """
        pinned_ref = REQUESTED_PAGE_ID  # numeric page ID
        skill_yaml = self._make_pinned_skill_yaml(tmp_path, pinned_ref)

        # Retriever returns a result for a DIFFERENT page (wrong canonical_id)
        wrong_result = _make_result(INGESTED_PAGE_ID)  # 20030556732 — different page
        executor, _ = self._make_executor([wrong_result])
        executor._load_fixture_passages = lambda *a, **kw: []

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": "Draft the weekly review"},
                sources=[],
            )

        err = exc_info.value
        assert REQUESTED_PAGE_ID in str(err.page_id) or REQUESTED_PAGE_ID in str(err), (
            f"Error must reference the pinned page {REQUESTED_PAGE_ID}"
        )

    def test_pinned_ref_correct_page_in_kb_passes(self, tmp_path: Path):
        """author_fixed with source_binding.pinned_ref: correct canonical page found → passes.
        The retriever returns a passage with canonical_ref.canonical_id matching pinned_ref.
        """
        pinned_ref = INGESTED_PAGE_ID  # numeric page ID
        skill_yaml = self._make_pinned_skill_yaml(tmp_path, pinned_ref)

        # Make a result whose metadata has canonical_ref stamped with the pinned ID
        correct_result = _make_result(INGESTED_PAGE_ID)
        # Add canonical_ref to the result's metadata so _passage_matches_canonical works
        correct_result.metadata["canonical_ref"] = {
            "connector_id": "confluence",
            "resource_type": "page",
            "canonical_id": INGESTED_PAGE_ID,
        }
        executor, _ = self._make_executor([correct_result])

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "Draft the weekly review"},
            sources=[],
        )

        assert len(passages) >= 1, "Correct pinned page must return passages"

    # -------------------------------------------------------------------------
    # Test 3: no page ref in generic input — unchanged behavior
    # -------------------------------------------------------------------------

    def test_space_form_short_number_no_false_positive(self, tmp_path: Path):
        """ADR-039: short numbers in prose never trigger any guard.
        The P3 heuristic is deleted; generic author_fixed returns passages freely.
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        some_result = _make_result(INGESTED_PAGE_ID)
        executor, _ = self._make_executor([some_result])

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "discussed 1234567 items in the meeting"},
            sources=[],
        )
        assert len(passages) >= 1, "Guard must be inert for prose numbers (ADR-039)"

    def test_space_form_unit_extraction(self):
        """ADR-039: pageId-prefixed form resolves via fast-path numeric extraction."""
        # Source identity is now resolved via canonical_identity() at author/bind time.
        # The fast-path numeric extraction handles all URL forms including bare numeric IDs.
        result = _extract_numeric_id_fast(f"pageId={REQUESTED_PAGE_ID}")
        assert result == REQUESTED_PAGE_ID, (
            f"Fast-path must extract numeric ID from pageId= form; got {result}"
        )

    def test_space_form_does_not_fire_on_short_prose_numbers(self):
        """ADR-039: short standalone prose numbers must NOT match as page IDs."""
        result = _extract_numeric_id_fast("we processed 12345678 records")
        assert result is None, (
            f"Standalone prose number without pageId prefix must NOT match; got {result}"
        )


# ---------------------------------------------------------------------------
# ADR-032 P2-Exec: Conditional guard behavior
# Tests that ask_parameterized routes to ephemeral path (not P3 regex guard)
# and that author_fixed still uses the regex guard.
# ---------------------------------------------------------------------------

def _make_ask_parameterized_skill_yaml(
    tmp_path: Path,
    skill_name: str = "ask_param_skill",
    space_allow_list: list | None = None,
) -> Path:
    """Write a minimal ask_parameterized skill YAML for conditional-guard tests."""
    if space_allow_list is None:
        space_allow_list = ["FA", "PROJ"]
    cfg = {
        "workflow_skill": skill_name,
        "persona": "tpm",
        "status": "promoted",
        "source_binding": {
            "mode": "ask_parameterized",
            "input_param": "page_id",
            "ingest_on_demand": True,
            "source_type": "confluence_page",
            "space_allow_list": space_allow_list,
            "ephemeral_ttl_seconds": 300,
        },
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [
                    {"name": "page_id", "type": "confluence_page_ref", "required": True}
                ],
                "output_format": "email",
                "response_mode": "artifact_url",
            },
        },
        "requires_extractions": [{"kb": f"tpm.{skill_name}"}],
        "synthesis": {"output_format": "email"},
        "delivery": {"kind": "filesystem", "path": f"/tmp/{skill_name}.eml"},
    }
    d = tmp_path / "workflow_skills" / "tpm"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{skill_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


class TestAskParameterizedRoutesToEphemeral:
    """ADR-032 P2-Exec — verify the conditional guard behavior.

    author_fixed skills: P3 regex guard retained (existing tests above).
    ask_parameterized skills: schema-driven ephemeral path used; regex guard NOT applied.
    """

    def _make_adapter(self, page_id=REQUESTED_PAGE_ID, space="FA"):
        raw_item = MagicMock()
        raw_item.metadata = {
            "page_id": page_id,
            "space": space,
            "title": f"Page {page_id}",
            "url": f"https://conf.example.com/spaces/{space}/pages/{page_id}/",
        }
        raw_item.payload = {"body": "Project status: Green. Next steps: ..."}
        raw_item.text = "Project status: Green. Next steps: ..."
        adapter = MagicMock()
        adapter.fetch.return_value = raw_item
        return adapter

    def test_ask_parameterized_routes_to_ephemeral_path_not_regex_guard(self, tmp_path):
        """ask_parameterized skill → adapter.fetch called for the requested page;
        the P3 regex guard block is NOT entered (the skill returned earlier via
        _retrieve_ask_parameterized).

        Structural proof: even if the input contains 'pageId=X' in free-text,
        the ask_parameterized path reads from source_binding.input_param, not regex."""
        skill_yaml = _make_ask_parameterized_skill_yaml(tmp_path)
        adapter = self._make_adapter(REQUESTED_PAGE_ID, "FA")
        llm = MagicMock()
        llm.chat.return_value = {"text": '{"status": "Green"}', "tokens_out": 10}
        executor = WorkflowExecutor(
            confluence_adapter=adapter, llm=llm,
        )

        url = f"https://conf.example.com/spaces/FA/pages/{REQUESTED_PAGE_ID}/My+Page"
        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"page_id": url},
            sources=[],
        )

        # Adapter must have been called (ephemeral path taken)
        adapter.fetch.assert_called_once()
        assert len(passages) >= 1
        assert all(
            p.get("metadata", {}).get("ephemeral") is True for p in passages
        ), "All passages from ask_parameterized must be ephemeral"

    def test_ask_parameterized_page_ref_in_free_text_does_not_trigger_p3_guard(self, tmp_path):
        """Even if 'pageId=18625350641' appears in a free-text input field for an
        ask_parameterized skill, the P3 regex guard must NOT fire.

        The ask_parameterized branch returns before reaching the P3 guard block.
        This confirms the guard is conditional (author_fixed only)."""
        skill_yaml = _make_ask_parameterized_skill_yaml(tmp_path)
        adapter = self._make_adapter(REQUESTED_PAGE_ID, "FA")
        llm = MagicMock()
        llm.chat.return_value = {"text": '{"status": "Green"}', "tokens_out": 10}
        executor = WorkflowExecutor(confluence_adapter=adapter, llm=llm)

        # The skill's structured input field carries the URL form
        url = f"https://conf.example.com/spaces/FA/pages/{REQUESTED_PAGE_ID}/"
        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"page_id": url},  # structured input — schema-driven, not free-text
            sources=[],
        )

        # Must NOT raise ConfluencePageNotInKBError
        assert len(passages) >= 1

    def test_author_fixed_p3_guard_deleted_by_adr039(self, tmp_path):
        """ADR-039 (DECISION-020): The P3 regex input-scan guard has been DELETED.

        Previously this test verified that pageId= in free-text inputs caused a
        ConfluencePageNotInKBError for author_fixed skills. ADR-039 removes this
        heuristic entirely. Source identity is now enforced at author-time via
        source_binding.pinned_ref + canonical==canonical comparison.

        A generic author_fixed skill with no source_binding.pinned_ref now returns
        whatever the retriever provides — even if the input mentions pageId=X.
        """
        skill_yaml = _make_skill_yaml(tmp_path)  # generic author_fixed (no source_binding)
        wrong_result = Result(
            content_id=INGESTED_PAGE_ID,
            chunk_id=None,
            text=f"Content from {INGESTED_PAGE_ID}",
            score=0.9,
            citation_url=f"wiki://{INGESTED_PAGE_ID}",
            metadata={"page_id": INGESTED_PAGE_ID},
        )
        retriever = _make_retriever([wrong_result])
        shim_kb = _make_shim_kb()
        executor = WorkflowExecutor(
            retrievers={"search_wiki": retriever},
            shim_kb=shim_kb,
            confluence_adapter=None,
        )

        # ADR-039: P3 guard deleted — no exception; passages are returned.
        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": f"pageId={REQUESTED_PAGE_ID}"},
            sources=[],
        )
        assert len(passages) >= 1, (
            "ADR-039: P3 guard deleted; generic author_fixed returns passages "
            "regardless of pageId= in inputs"
        )

    def test_ask_parameterized_adapter_none_raises_before_regex_guard(self, tmp_path):
        """ask_parameterized + adapter None → hard-fail from ephemeral path (adapter
        unavailable), NOT from the P3 regex guard.

        The error reason must mention the adapter, not the KB ingest instruction."""
        skill_yaml = _make_ask_parameterized_skill_yaml(tmp_path)
        executor = WorkflowExecutor(confluence_adapter=None)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": REQUESTED_PAGE_ID},
                sources=[],
            )

        msg = str(exc_info.value)
        # Must mention Confluence adapter (ephemeral path hard-fail), not KB ingest
        assert "adapter" in msg.lower() or "Confluence adapter" in msg, (
            f"ask_parameterized adapter-None error must mention adapter; got: {msg!r}"
        )
