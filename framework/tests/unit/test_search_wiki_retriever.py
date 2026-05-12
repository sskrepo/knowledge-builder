"""Unit tests for SearchWikiRetriever (GAP-R1)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_retriever(records=None, wiki_root=None):
    from framework.retrievers.search_wiki import SearchWikiRetriever

    store = MagicMock()
    store.search_pages.return_value = records or []
    return SearchWikiRetriever(wiki_store=store, wiki_root=wiki_root), store


def _make_record(page_id="P-001", title="Incident Runbook", path="", persona=None, tags=None):
    return {
        "page_id": page_id,
        "title": title,
        "path": path,
        "persona": persona,
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# Basic happy path
# ---------------------------------------------------------------------------

def test_returns_results_when_store_has_data(tmp_path):
    wiki_file = tmp_path / "P-001.md"
    wiki_file.write_text("# Runbook\nThis is a runbook body.", encoding="utf-8")

    rec = _make_record(page_id="P-001", path=str(wiki_file))
    retriever, store = _make_retriever(records=[rec])

    results = retriever(query="incident runbook")

    store.search_pages.assert_called_once_with("incident runbook")
    assert len(results) == 1
    r = results[0]
    assert r.content_id == "P-001"
    assert "Runbook" in r.text
    assert r.citation_url == "wiki://P-001"
    assert r.score == 1.0


def test_returns_empty_when_store_returns_no_matches():
    retriever, store = _make_retriever(records=[])
    results = retriever(query="something not found")
    assert results == []


def test_persona_filter_removes_non_matching_records():
    records = [
        _make_record(page_id="P-ops", persona="ops_eng"),
        _make_record(page_id="P-pm", persona="pm"),
    ]
    retriever, _ = _make_retriever(records=records)

    results = retriever(query="any query", persona="ops_eng")

    assert len(results) == 1
    assert results[0].content_id == "P-ops"


def test_persona_filter_none_returns_all():
    records = [
        _make_record(page_id="P-ops", persona="ops_eng"),
        _make_record(page_id="P-pm", persona="pm"),
    ]
    retriever, _ = _make_retriever(records=records)

    results = retriever(query="any query", persona=None)

    assert len(results) == 2


def test_max_results_caps_output():
    records = [_make_record(page_id=f"P-{i}") for i in range(20)]
    retriever, _ = _make_retriever(records=records)

    results = retriever(query="query", max_results=5)
    assert len(results) == 5


def test_missing_file_yields_empty_body(tmp_path):
    rec = _make_record(page_id="P-missing", path="/nonexistent/path/page.md")
    retriever, _ = _make_retriever(records=[rec])

    results = retriever(query="something")
    assert len(results) == 1
    assert results[0].text == ""


def test_result_metadata_has_required_fields():
    rec = _make_record(
        page_id="P-001",
        title="My Wiki Page",
        persona="tpm",
        tags=["ops", "runbook"],
    )
    retriever, _ = _make_retriever(records=[rec])

    results = retriever(query="wiki page")
    assert len(results) == 1

    meta = results[0].metadata
    assert meta["page_id"] == "P-001"
    assert meta["title"] == "My Wiki Page"
    assert meta["persona"] == "tpm"
    assert meta["tags"] == ["ops", "runbook"]


def test_result_is_result_type():
    from framework.core.interfaces import Result

    rec = _make_record()
    retriever, _ = _make_retriever(records=[rec])
    results = retriever(query="test")
    assert all(isinstance(r, Result) for r in results)
