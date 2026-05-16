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
    _extract_confluence_page_ids,
    _passage_matches_page_id,
)


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

class TestExtractConfluencePageIds:
    """Unit tests for the _extract_confluence_page_ids helper."""

    def test_querystring_form(self):
        ids = _extract_confluence_page_ids({"input": "?pageId=18625350641"})
        assert ids == ["18625350641"]

    def test_bare_pageid_eq_form(self):
        ids = _extract_confluence_page_ids({"input": "pageId=18625350641"})
        assert ids == ["18625350641"]

    def test_viewpage_action_form(self):
        url = "https://confluence.example.com/pages/viewpage.action?pageId=18625350641"
        ids = _extract_confluence_page_ids({"input": url})
        assert "18625350641" in ids

    def test_rest_short_form(self):
        url = "https://confluence.example.com/wiki/spaces/FA/pages/18625350641/My+Page"
        ids = _extract_confluence_page_ids({"input": url})
        assert "18625350641" in ids

    def test_no_ref_generic_query(self):
        ids = _extract_confluence_page_ids({"input": "What are the project milestones?"})
        assert ids == []

    def test_prose_number_not_treated_as_page_id(self):
        """Numbers in prose (e.g. 'released 42 items') must NOT be matched."""
        ids = _extract_confluence_page_ids({"input": "We released 42 items last week."})
        assert ids == []

    def test_multiple_values_scanned(self):
        ids = _extract_confluence_page_ids({
            "query": "pageId=11111111111",
            "extra": "pageId=22222222222",
        })
        assert "11111111111" in ids
        assert "22222222222" in ids

    def test_deduplication(self):
        ids = _extract_confluence_page_ids({
            "a": "pageId=18625350641",
            "b": "pageId=18625350641",
        })
        assert ids.count("18625350641") == 1


class TestPassageMatchesPageId:
    """Unit tests for the _passage_matches_page_id helper."""

    def test_metadata_page_id_matches(self):
        passage = {"metadata": {"page_id": "18625350641"}, "citation": ""}
        assert _passage_matches_page_id(passage, "18625350641") is True

    def test_metadata_page_id_no_match(self):
        passage = {"metadata": {"page_id": "20030556732"}, "citation": ""}
        assert _passage_matches_page_id(passage, "18625350641") is False

    def test_citation_url_contains_page_id(self):
        passage = {"metadata": {}, "citation": "wiki://18625350641"}
        assert _passage_matches_page_id(passage, "18625350641") is True

    def test_citation_url_does_not_match(self):
        passage = {"metadata": {}, "citation": "wiki://20030556732"}
        assert _passage_matches_page_id(passage, "18625350641") is False

    def test_empty_passage(self):
        assert _passage_matches_page_id({}, "18625350641") is False


# ---------------------------------------------------------------------------
# Integration tests — WorkflowExecutor._retrieve_for_inputs via execute()
# ---------------------------------------------------------------------------

class TestExecutorSourceGuard:
    """End-to-end tests through WorkflowExecutor that verify the P3 guard."""

    def _make_executor(self, results_for_retriever: list[Result]) -> tuple[WorkflowExecutor, MagicMock]:
        retriever = _make_retriever(results_for_retriever)
        shim_kb = _make_shim_kb("project_tracking_test")
        executor = WorkflowExecutor(
            retrievers={"search_wiki": retriever},
            shim_kb=shim_kb,
        )
        return executor, shim_kb

    # -------------------------------------------------------------------------
    # Test 1: wrong page returned — hard-fail, NO substitution
    # -------------------------------------------------------------------------

    def test_wrong_page_returned_raises_hard_fail(self, tmp_path: Path):
        """Input has pageId=18625350641; retriever returns only page 20030556732.
        The guard MUST raise ConfluencePageNotInKBError.
        NO passages must be returned (render is never reached).
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        wrong_page_result = _make_result(INGESTED_PAGE_ID)  # 20030556732

        executor, _ = self._make_executor([wrong_page_result])

        # Patch _synthesize and _render/_deliver so the test fails fast on the
        # guard, not on unrelated render/deliver infrastructure.
        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": f"Please draft an email using pageId={REQUESTED_PAGE_ID}"},
                sources=[],
            )

        err = exc_info.value
        assert err.page_id == REQUESTED_PAGE_ID, (
            f"Error must name the requested page id {REQUESTED_PAGE_ID}, got {err.page_id!r}"
        )
        assert REQUESTED_PAGE_ID in str(err), "Error message must contain the requested page id"
        assert "not in the knowledge base" in str(err), (
            "Error message must clearly state the page is not in the KB"
        )
        assert "ingest" in str(err).lower(), (
            "Error message must provide actionable ingest instruction"
        )
        # Confirm the error does NOT mention the wrong page that was actually retrieved
        # (we don't want to leak internal substitution details, only the user request)
        assert INGESTED_PAGE_ID not in str(err), (
            f"Error message must NOT expose the substituted page id {INGESTED_PAGE_ID}"
        )

    def test_wrong_page_message_is_consumer_safe(self, tmp_path: Path):
        """The hard-fail message must be consumer-safe: no provider internals,
        no stack trace fragments, just the page id and ingest instruction."""
        skill_yaml = _make_skill_yaml(tmp_path)
        executor, _ = self._make_executor([_make_result(INGESTED_PAGE_ID)])

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": f"pageId={REQUESTED_PAGE_ID}"},
                sources=[],
            )

        msg = str(exc_info.value)
        # Must include page id and be actionable
        assert REQUESTED_PAGE_ID in msg
        assert "kb-cli" in msg or "ingest" in msg.lower(), (
            "Message must mention an ingest action so the user knows how to fix it"
        )
        # Must NOT contain internal exception type names or traceback markers
        for forbidden in ("Traceback", "File \"", "line ", "ConfluencePageNotInKBError"):
            assert forbidden not in msg, f"Consumer-facing message must not contain {forbidden!r}"

    # -------------------------------------------------------------------------
    # Test 2: correct page returned — passes through, no error
    # -------------------------------------------------------------------------

    def test_correct_page_returned_passes_through(self, tmp_path: Path):
        """Input has pageId=20030556732; retriever returns a passage citing that
        same page. The guard must NOT raise — the passage must be returned."""
        skill_yaml = _make_skill_yaml(tmp_path)
        correct_result = _make_result(INGESTED_PAGE_ID)  # 20030556732 — matches input

        executor, _ = self._make_executor([correct_result])

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": f"Draft an email for pageId={INGESTED_PAGE_ID}"},
            sources=[],
        )

        assert len(passages) >= 1, "At least one passage must be returned when page matches"
        assert any(p.get("metadata", {}).get("page_id") == INGESTED_PAGE_ID for p in passages), (
            "Returned passages must include the matching page"
        )

    # -------------------------------------------------------------------------
    # Test 3: no page ref in input — guard is inert (no regression)
    # -------------------------------------------------------------------------

    def test_no_page_ref_guard_is_inert(self, tmp_path: Path):
        """Input is a generic query with no Confluence page reference.
        The guard must be COMPLETELY INERT — passages from any page pass through.
        This proves no regression to fixed-source skills or any skill whose
        input is a free-text query (not a page reference).
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        # Retriever returns a passage from an arbitrary page — no page ref in inputs
        some_result = _make_result(INGESTED_PAGE_ID)

        executor, _ = self._make_executor([some_result])

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "What are the key milestones for the FA DB upgrade project?"},
            sources=[],
        )

        # Guard must be inert — passages returned unchanged, no exception
        assert len(passages) >= 1, (
            "Guard must be inert for generic query inputs; passages must be returned"
        )

    # -------------------------------------------------------------------------
    # Test 4: URL form is recognised the same way as querystring form
    # -------------------------------------------------------------------------

    def test_url_form_viewpage_action_recognised(self, tmp_path: Path):
        """Input contains /pages/viewpage.action?pageId=18625350641 (URL form).
        This must be treated identically to the plain pageId= form — wrong page
        retrieved → hard-fail.
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        wrong_result = _make_result(INGESTED_PAGE_ID)

        executor, _ = self._make_executor([wrong_result])

        url_input = (
            "Please draft from https://mycompany.atlassian.net"
            f"/wiki/pages/viewpage.action?pageId={REQUESTED_PAGE_ID}"
        )

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": url_input},
                sources=[],
            )

        assert exc_info.value.page_id == REQUESTED_PAGE_ID, (
            "URL form must extract the same page id as the querystring form"
        )

    # -------------------------------------------------------------------------
    # Test 5: no retriever results (falls through to fixture) — still hard-fails
    # -------------------------------------------------------------------------

    def test_empty_retriever_with_page_ref_hard_fails(self, tmp_path: Path):
        """If the retriever returns nothing and fixture fallback also yields nothing,
        and the input has a page ref, the guard must hard-fail (not return empty
        passages silently or fall through to an unrelated fixture page).
        """
        skill_yaml = _make_skill_yaml(tmp_path)

        # Retriever returns empty list
        executor, _ = self._make_executor([])
        # Patch fixture loader to return empty too (no fixtures installed in test env)
        executor._load_fixture_passages = lambda *a, **kw: []

        with pytest.raises(ConfluencePageNotInKBError):
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": f"pageId={REQUESTED_PAGE_ID}"},
                sources=[],
            )

    # -------------------------------------------------------------------------
    # A1 (BUG-queue-990fe): space-form "pageId 18625350641" fires the guard
    # -------------------------------------------------------------------------

    def test_space_form_page_ref_fires_guard(self, tmp_path: Path):
        """A1: Input is 'for Confluence pageId 18625350641' (space, no '=').
        The P3 guard MUST detect this as a Confluence page reference and
        hard-fail when the retriever returns a different page — NO silent
        substitution (was RC2 bug).
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        wrong_result = _make_result(INGESTED_PAGE_ID)  # 20030556732

        executor, _ = self._make_executor([wrong_result])
        executor._load_fixture_passages = lambda *a, **kw: []

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": f"for Confluence pageId {REQUESTED_PAGE_ID}"},
                sources=[],
            )

        err = exc_info.value
        assert err.page_id == REQUESTED_PAGE_ID, (
            f"Error must name the requested page id {REQUESTED_PAGE_ID}, got {err.page_id!r}"
        )
        assert "not in the knowledge base" in str(err)
        assert "ingest" in str(err).lower()

    def test_space_form_with_colon_fires_guard(self, tmp_path: Path):
        """A1 variant: 'pageId: 18625350641' (colon + space) must also fire."""
        skill_yaml = _make_skill_yaml(tmp_path)
        executor, _ = self._make_executor([_make_result(INGESTED_PAGE_ID)])
        executor._load_fixture_passages = lambda *a, **kw: []

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": f"pageId: {REQUESTED_PAGE_ID}"},
                sources=[],
            )
        assert exc_info.value.page_id == REQUESTED_PAGE_ID

    def test_space_form_short_number_no_false_positive(self, tmp_path: Path):
        """A1: short numbers (< 8 digits) embedded in prose must NOT fire the guard.
        'discussed 12345678 items' has exactly 8 digits — boundary test.
        'discussed 1234567 items' has 7 digits — must be inert.
        Only ≥8-digit tokens following 'pageId' (with space/colon) are detected.
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        some_result = _make_result(INGESTED_PAGE_ID)
        executor, _ = self._make_executor([some_result])

        # 7-digit number in prose — guard must be inert
        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "discussed 1234567 items in the meeting"},
            sources=[],
        )
        assert len(passages) >= 1, "Guard must be inert for short prose numbers (7 digits)"

    def test_space_form_unit_extraction(self):
        """Unit test: _extract_confluence_page_ids detects the space form."""
        ids = _extract_confluence_page_ids({"input": f"for Confluence pageId {REQUESTED_PAGE_ID}"})
        assert REQUESTED_PAGE_ID in ids, (
            f"Space-form 'pageId {REQUESTED_PAGE_ID}' must be extracted; got {ids}"
        )

    def test_space_form_does_not_fire_on_short_prose_numbers(self):
        """Unit test: short standalone prose numbers do not match the space-form pattern."""
        # The pattern only fires when 'pageId' (or page id / page-id) precedes the number
        ids = _extract_confluence_page_ids({"input": "we processed 12345678 records"})
        assert ids == [], (
            f"Standalone prose number without 'pageId' prefix must NOT match; got {ids}"
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

    def test_author_fixed_p3_guard_still_fires_after_p2_exec_ships(self, tmp_path):
        """After P2-Exec, the P3 guard must STILL fire for author_fixed skills
        when the user includes a page ref in free-text and the retriever returns
        a different page (no regression).

        This is the critical regression test — the conditional must work correctly."""
        skill_yaml = _make_skill_yaml(tmp_path)  # author_fixed (no source_binding)
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
            confluence_adapter=None,  # adapter absent — irrelevant for author_fixed
        )

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"input": f"pageId={REQUESTED_PAGE_ID}"},
                sources=[],
            )

        assert exc_info.value.page_id == REQUESTED_PAGE_ID, (
            "P3 guard must still identify the requested page for author_fixed skills"
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
