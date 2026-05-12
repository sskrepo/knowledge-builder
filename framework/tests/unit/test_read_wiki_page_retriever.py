"""Unit tests for ReadWikiPageRetriever (GAP-R2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_retriever(store_record=None, wiki_root=None):
    from framework.retrievers.read_wiki_page import ReadWikiPageRetriever

    store = MagicMock()
    store.get_page.return_value = store_record
    return ReadWikiPageRetriever(wiki_store=store, wiki_root=wiki_root), store


# ---------------------------------------------------------------------------
# File-path lookup
# ---------------------------------------------------------------------------

def test_reads_body_from_absolute_path(tmp_path):
    wiki_file = tmp_path / "page.md"
    wiki_file.write_text("# My Page\nBody text here.", encoding="utf-8")

    retriever, store = _make_retriever(store_record=None)
    result = retriever(path=str(wiki_file))

    assert result is not None
    assert "Body text here." in result.text


def test_reads_body_from_wiki_root_relative_path(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    wiki_file = wiki_root / "subdir" / "page.md"
    wiki_file.parent.mkdir(parents=True)
    wiki_file.write_text("Relative path body.", encoding="utf-8")

    retriever, store = _make_retriever(store_record=None, wiki_root=str(wiki_root))
    result = retriever(path="subdir/page.md")

    assert result is not None
    assert "Relative path body." in result.text


# ---------------------------------------------------------------------------
# Metadata from store
# ---------------------------------------------------------------------------

def test_returns_metadata_from_store(tmp_path):
    wiki_file = tmp_path / "page.md"
    wiki_file.write_text("Page body.", encoding="utf-8")

    store_record = {
        "page_id": "P-777",
        "title": "My Page",
        "path": str(wiki_file),
        "persona": "pm",
        "tags": ["feature"],
    }
    retriever, store = _make_retriever(store_record=store_record)
    result = retriever(path=str(wiki_file))

    assert result is not None
    assert result.content_id == "P-777"
    assert result.metadata["title"] == "My Page"
    assert result.metadata["persona"] == "pm"
    assert result.metadata["tags"] == ["feature"]
    store.get_page.assert_called_once_with(str(wiki_file))


def test_citation_url_is_wiki_scheme_when_no_source_url(tmp_path):
    wiki_file = tmp_path / "page.md"
    wiki_file.write_text("Body.", encoding="utf-8")

    retriever, _ = _make_retriever(store_record=None)
    result = retriever(path=str(wiki_file))

    assert result is not None
    assert result.citation_url.startswith("wiki://")


def test_returns_none_when_no_file_and_no_store_record():
    retriever, _ = _make_retriever(store_record=None)
    result = retriever(path="/nonexistent/path/no-page.md")
    assert result is None


# ---------------------------------------------------------------------------
# Store-only lookup (page_id without file)
# ---------------------------------------------------------------------------

def test_returns_result_from_store_only_when_no_file_found():
    """Metadata-only result when store has a record but file is missing."""
    store_record = {
        "page_id": "P-999",
        "title": "Orphaned Page",
        "path": "/nonexistent/path/page.md",
        "persona": "tpm",
        "tags": [],
    }
    retriever, _ = _make_retriever(store_record=store_record)
    result = retriever(path="P-999")

    assert result is not None
    assert result.content_id == "P-999"
    assert result.text == ""  # file not found → empty body


def test_result_is_result_type(tmp_path):
    from framework.core.interfaces import Result

    wiki_file = tmp_path / "page.md"
    wiki_file.write_text("Some content.", encoding="utf-8")

    retriever, _ = _make_retriever(store_record=None)
    result = retriever(path=str(wiki_file))

    assert isinstance(result, Result)
    assert result.score == 1.0
