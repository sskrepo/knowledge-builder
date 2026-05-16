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


# ---------------------------------------------------------------------------
# A2 (BUG-queue-990fe RC1): persona propagation — constructor param fallback
# ---------------------------------------------------------------------------

def _make_ingestor_with_persona(tmp_wiki_root, mock_wiki_store, persona, adapter=None):
    from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
    return ConfluenceWikiIngestor(
        wiki_root=tmp_wiki_root,
        adapter=adapter,
        wiki_store=mock_wiki_store,
        persona=persona,
    )


def test_ingestor_persona_used_when_raw_has_no_persona(tmp_wiki_root, mock_wiki_store):
    """A2: When raw item has no persona field, ingestor-level persona is the fallback.

    ConfluenceWikiIngestor(persona='tpm') → page with no raw persona →
    wiki_metadata record must have persona='tpm'.
    """
    ingestor = _make_ingestor_with_persona(tmp_wiki_root, mock_wiki_store, persona="tpm")
    page = _make_page_dict()
    # No 'persona' key in raw page dict
    assert "persona" not in page

    ingestor.ingest_page(page["id"], _raw=page)

    call_kwargs = mock_wiki_store.upsert_page.call_args[0][0]
    assert call_kwargs["persona"] == "tpm", (
        f"Expected persona='tpm' from ingestor fallback, got {call_kwargs['persona']!r}"
    )


def test_raw_persona_wins_over_ingestor_persona(tmp_wiki_root, mock_wiki_store):
    """A2: When raw item carries its own persona, it wins — ingestor param is NOT used.

    Raw wins rule: if the raw item has persona='pm', the stored persona must be
    'pm' even when ConfluenceWikiIngestor(persona='tpm') was constructed.
    """
    ingestor = _make_ingestor_with_persona(tmp_wiki_root, mock_wiki_store, persona="tpm")
    page = _make_page_dict()
    page["persona"] = "pm"  # raw item has its own persona

    ingestor.ingest_page(page["id"], _raw=page)

    call_kwargs = mock_wiki_store.upsert_page.call_args[0][0]
    assert call_kwargs["persona"] == "pm", (
        f"Raw persona 'pm' must win over ingestor persona 'tpm', got {call_kwargs['persona']!r}"
    )


def test_ingestor_no_persona_and_raw_no_persona_stores_null(tmp_wiki_root, mock_wiki_store):
    """A2: When neither ingestor nor raw has a persona, stored persona is None.

    This is the pre-fix baseline — null persona is still allowed when genuinely
    unknown (the P2-Exec path bypasses the filter anyway).
    """
    ingestor = _make_ingestor(tmp_wiki_root, mock_wiki_store)  # no persona param
    page = _make_page_dict()

    ingestor.ingest_page(page["id"], _raw=page)

    call_kwargs = mock_wiki_store.upsert_page.call_args[0][0]
    assert call_kwargs["persona"] is None, (
        f"With no persona from either source, stored persona must be None, got {call_kwargs['persona']!r}"
    )


# ---------------------------------------------------------------------------
# A4 (BUG-queue-990fe): wiki-meta backfill-persona command
# ---------------------------------------------------------------------------

def test_backfill_persona_sets_null_persona_records(tmp_path):
    """A4: Null-persona records get the target persona; non-null records untouched."""
    import json as _json
    import re as _re
    from unittest.mock import patch

    store_root = tmp_path / "wiki_metadata"
    store_root.mkdir(parents=True)

    # Record with null persona (target)
    null_rec = {"page_id": "18625350641", "title": "Some Page", "persona": None, "path": "/fake"}
    safe_stem_null = _re.sub(r"[^\w.-]", "_", "18625350641")
    (store_root / f"{safe_stem_null}.json").write_text(_json.dumps(null_rec, indent=2))

    # Record with non-null persona (must be untouched)
    nonnull_rec = {"page_id": "20030556732", "title": "Other Page", "persona": "architect", "path": "/fake2"}
    safe_stem_nonnull = _re.sub(r"[^\w.-]", "_", "20030556732")
    (store_root / f"{safe_stem_nonnull}.json").write_text(_json.dumps(nonnull_rec, indent=2))

    # Run backfill via CLI function
    from framework.cli.kb_cli import cmd_wiki_meta_backfill_persona

    class FakeArgs:
        persona = "tpm"
        page_id = None
        dry_run = False

    with patch.dict("os.environ", {"KBF_STORE_ROOT": str(tmp_path)}):
        result = cmd_wiki_meta_backfill_persona(FakeArgs())

    assert result == 0

    # Null persona record must now have persona="tpm"
    updated = _json.loads((store_root / f"{safe_stem_null}.json").read_text())
    assert updated["persona"] == "tpm", f"Expected 'tpm' after backfill, got {updated['persona']!r}"

    # Non-null persona record must be untouched
    untouched = _json.loads((store_root / f"{safe_stem_nonnull}.json").read_text())
    assert untouched["persona"] == "architect", (
        f"Non-null persona must be preserved, got {untouched['persona']!r}"
    )


def test_backfill_persona_is_idempotent(tmp_path):
    """A4: Running backfill twice is a no-op — second run changes nothing."""
    import json as _json
    import re as _re
    from unittest.mock import patch

    store_root = tmp_path / "wiki_metadata"
    store_root.mkdir(parents=True)

    null_rec = {"page_id": "18625350641", "title": "Page", "persona": None, "path": "/fake"}
    safe_stem = _re.sub(r"[^\w.-]", "_", "18625350641")
    rec_path = store_root / f"{safe_stem}.json"
    rec_path.write_text(_json.dumps(null_rec, indent=2))

    from framework.cli.kb_cli import cmd_wiki_meta_backfill_persona

    class FakeArgs:
        persona = "tpm"
        page_id = None
        dry_run = False

    with patch.dict("os.environ", {"KBF_STORE_ROOT": str(tmp_path)}):
        cmd_wiki_meta_backfill_persona(FakeArgs())
        result = cmd_wiki_meta_backfill_persona(FakeArgs())

    assert result == 0
    record = _json.loads(rec_path.read_text())
    assert record["persona"] == "tpm", "Second run must leave persona='tpm' (idempotent)"


def test_backfill_persona_single_page_id(tmp_path):
    """A4: --page-id targets exactly one record, leaves others untouched."""
    import json as _json
    import re as _re
    from unittest.mock import patch

    store_root = tmp_path / "wiki_metadata"
    store_root.mkdir(parents=True)

    target = {"page_id": "18625350641", "title": "Target", "persona": None, "path": "/fake"}
    other  = {"page_id": "99999999999", "title": "Other",  "persona": None, "path": "/fake2"}

    for rec in (target, other):
        safe = _re.sub(r"[^\w.-]", "_", rec["page_id"])
        (store_root / f"{safe}.json").write_text(_json.dumps(rec, indent=2))

    from framework.cli.kb_cli import cmd_wiki_meta_backfill_persona

    class FakeArgs:
        persona = "tpm"
        page_id = "18625350641"
        dry_run = False

    with patch.dict("os.environ", {"KBF_STORE_ROOT": str(tmp_path)}):
        result = cmd_wiki_meta_backfill_persona(FakeArgs())

    assert result == 0

    # Target record updated
    safe_target = _re.sub(r"[^\w.-]", "_", "18625350641")
    updated = _json.loads((store_root / f"{safe_target}.json").read_text())
    assert updated["persona"] == "tpm"

    # Other null-persona record untouched (--page-id scopes to one record)
    safe_other = _re.sub(r"[^\w.-]", "_", "99999999999")
    other_after = _json.loads((store_root / f"{safe_other}.json").read_text())
    assert other_after["persona"] is None, "Other null-persona record must be untouched"
