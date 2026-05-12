"""Unit tests for ConfluenceWikiIngestor — GAP-I1 (WikiMetadataStore wiring)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_wiki_root(tmp_path):
    return tmp_path / "wiki"


@pytest.fixture
def mock_wiki_store():
    return MagicMock()


def _make_ingestor(tmp_wiki_root, mock_wiki_store, adapter=None):
    from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
    return ConfluenceWikiIngestor(
        wiki_root=tmp_wiki_root,
        adapter=adapter,
        wiki_store=mock_wiki_store,
    )


def _make_page_dict(page_id="TEST-001", title="Test Page", space="TS",
                    body="<p>Hello world</p>", labels=None):
    return {
        "id": page_id,
        "title": title,
        "space": space,
        "body": body,
        "labels": labels or [],
        "version": 1,
        "updated_at": "2026-01-01T00:00:00Z",
        "url": f"https://confluence.example.com/{space}/{page_id}",
    }


# ---------------------------------------------------------------------------
# GAP-I1: upsert_page is called on new pages
# ---------------------------------------------------------------------------

def test_ingest_page_calls_upsert_page_on_new(tmp_wiki_root, mock_wiki_store):
    ingestor = _make_ingestor(tmp_wiki_root, mock_wiki_store)
    page = _make_page_dict()
    result = ingestor.ingest_page(page["id"], _raw=page)

    assert result["status"] == "new"
    mock_wiki_store.upsert_page.assert_called_once()

    call_kwargs = mock_wiki_store.upsert_page.call_args[0][0]
    assert call_kwargs["page_id"] == "TEST-001"
    assert call_kwargs["title"] == "Test Page"
    assert "path" in call_kwargs
    assert call_kwargs["extraction_version"] == "confluence_wiki_ingest:v1"


def test_ingest_page_calls_upsert_page_on_updated(tmp_wiki_root, mock_wiki_store):
    ingestor = _make_ingestor(tmp_wiki_root, mock_wiki_store)
    page = _make_page_dict()

    # First ingest
    ingestor.ingest_page(page["id"], _raw=page)
    mock_wiki_store.reset_mock()

    # Change content, re-ingest — should call upsert again
    page["body"] = "<p>Updated content</p>"
    result = ingestor.ingest_page(page["id"], _raw=page)

    assert result["status"] == "updated"
    mock_wiki_store.upsert_page.assert_called_once()


def test_ingest_page_does_not_call_upsert_on_unchanged(tmp_wiki_root, mock_wiki_store):
    ingestor = _make_ingestor(tmp_wiki_root, mock_wiki_store)
    page = _make_page_dict()

    # First ingest
    ingestor.ingest_page(page["id"], _raw=page)
    mock_wiki_store.reset_mock()

    # Same content again — should NOT call upsert (no change)
    result = ingestor.ingest_page(page["id"], _raw=page)

    assert result["status"] == "unchanged"
    mock_wiki_store.upsert_page.assert_not_called()


def test_ingest_page_upsert_page_dict_has_correct_fields(tmp_wiki_root, mock_wiki_store):
    ingestor = _make_ingestor(tmp_wiki_root, mock_wiki_store)
    page = _make_page_dict(
        page_id="P-999",
        title="My Page",
        labels=["runbook", "incident"],
    )
    ingestor.ingest_page(page["id"], _raw=page)

    call_dict = mock_wiki_store.upsert_page.call_args[0][0]
    assert call_dict["page_id"] == "P-999"
    assert call_dict["title"] == "My Page"
    assert call_dict["tags"] == ["runbook", "incident"]
    assert "content_hash" in call_dict
    assert call_dict["extraction_version"] == "confluence_wiki_ingest:v1"


def test_default_wiki_store_is_created_when_not_provided(tmp_wiki_root):
    """When wiki_store is not given, a default WikiMetadataStore is instantiated."""
    from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
    from framework.stores.wiki_metadata_store import WikiMetadataStore

    ingestor = ConfluenceWikiIngestor(wiki_root=tmp_wiki_root)
    assert isinstance(ingestor._wiki_store, WikiMetadataStore)


def test_ingest_page_creates_md_and_meta_files(tmp_wiki_root, mock_wiki_store):
    ingestor = _make_ingestor(tmp_wiki_root, mock_wiki_store)
    page = _make_page_dict(space="MYSPACE")
    result = ingestor.ingest_page(page["id"], _raw=page)

    md_path = Path(result["path"])
    assert md_path.exists()
    assert md_path.suffix == ".md"

    meta_path = md_path.with_suffix(".meta.json")
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["page_id"] == "TEST-001"
    assert meta["title"] == "Test Page"
