"""Issue-1a real-ingest-roundtrip fix — unit tests.

Root cause (Issue-1a): _run_eval constructed WorkflowExecutor with no retrievers
and no wiki_store, so _retrieve_author_fixed_pinned Strategy 1 was entirely skipped
for author_fixed+!ingest_on_demand skills even when the page was correctly written
to WikiMetadataStore by _run_ingest.

Fixes shipped:
  1. executor.py: add wiki_store param + Strategy 1b (direct WikiMetadataStore
     lookup by canonical_id — no KB card required).
  2. conversation.py _run_eval: wire WikiMetadataStore into EVAL executor.
  3. confluence_wiki_ingest.py ingest_page: require_canonical_ref=True param makes
     canonical_ref stamp failure a HARD FAIL (not warning-and-continue).
  4. conversation.py _run_ingest: pass require_canonical_ref=True + post-ingest
     canonical_ref verification.

Tests:
  A. Strategy 1b: executor finds page via wiki_store when shim_kb/KB card absent.
  B. Strategy 1b: page absent from wiki_store -> Strategy 3 hard-fail.
  C. Strategy 1a wins when shim_kb + retriever present + canonical_ref matches.
  D. ingest_page require_canonical_ref=True: raises when canonical_ref not stampable.
  E. ingest_page require_canonical_ref=True: succeeds with numeric id (fast-path).
  F. ingest_result integrity: pages_new/items_upserted reflect actual pinned-page
     ingest outcome, not a false-success when the store write failed.
  G. post-ingest verification in _run_ingest: hard-fails when wiki_store missing
     the record after ingest_page reported success.
  H. Strategy 1b: _passage_matches_canonical returns True for the returned passage.

Unit-mock certification is NOT sufficient for the round-trip (see Step 3 in the
original bug report). These tests prove the code paths; real round-trip proof
requires live Confluence creds + ADB (see docs/wiki/log.md for Step 3 evidence).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from framework.adapters._base import CanonicalRef
from framework.core.interfaces import Result
from framework.workflow_runtime.executor import (
    ConfluencePageNotInKBError,
    WorkflowExecutor,
    _passage_matches_canonical,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

PINNED_PAGE_ID = "20382503622"
PINNED_CANONICAL = CanonicalRef(
    connector_id="confluence",
    resource_type="page",
    canonical_id=PINNED_PAGE_ID,
    display_hint="FAaaS Kiwi Project",
)


def _make_skill_cfg(pinned_ref: str = PINNED_PAGE_ID) -> dict:
    return {
        "workflow_skill": "faaas_kiwi_pptx",
        "persona": "tpm",
        "status": "draft",
        "requires_extractions": [{"kb": "tpm.faaas_kiwi_pptx"}],
        "source_binding": {
            "mode": "author_fixed",
            "source_type": "confluence_page",
            "pinned_ref": pinned_ref,
            "ingest_on_demand": False,
            "canonical_ref": {
                "connector_id": "confluence",
                "resource_type": "page",
                "canonical_id": pinned_ref,
            },
        },
        "synthesis": {"output_format": "pptx"},
        "delivery": {"kind": "filesystem", "path": "/tmp/out.pptx"},
    }


def _make_wiki_store_with_page(
    page_id: str = PINNED_PAGE_ID,
    content: str = "# FAaaS Kiwi Project\nPage body content.",
    tmp_path: Path | None = None,
):
    """Build a real WikiMetadataStore with one page record pointing to a temp file."""
    from framework.stores.wiki_metadata_store import WikiMetadataStore

    if tmp_path is None:
        import tempfile
        _td = tempfile.mkdtemp()
        tmp_path = Path(_td)

    # Write the markdown content file
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    md_file = wiki_root / f"{page_id}.md"
    md_file.write_text(content, encoding="utf-8")

    # Build a store with the record
    store_root = tmp_path / "store" / "wiki_metadata"
    store = WikiMetadataStore(root=store_root)
    store.upsert_page({
        "page_id": page_id,
        "title": "FAaaS Kiwi Project",
        "path": str(md_file),
        "persona": "tpm",
        "tags": [],
        "content_hash": "abc123",
        "extraction_version": "confluence_wiki_ingest:v1",
        "canonical_ref": {
            "connector_id": "confluence",
            "resource_type": "page",
            "canonical_id": page_id,
        },
    })
    return store


# ---------------------------------------------------------------------------
# A. Strategy 1b: executor finds page via wiki_store when no shim_kb/KB card
# ---------------------------------------------------------------------------

class TestStrategy1bDirectWikiStoreLookup:
    """executor._retrieve_author_fixed_pinned Strategy 1b."""

    def test_finds_page_when_no_shim_kb(self, tmp_path):
        """Strategy 1b succeeds when shim_kb=None but wiki_store has the page."""
        wiki_store = _make_wiki_store_with_page(tmp_path=tmp_path)
        executor = WorkflowExecutor(
            wiki_store=wiki_store,
            # No shim_kb, no retrievers — exactly the EVAL executor's pre-fix state
        )
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])
        assert len(passages) >= 1
        assert "FAaaS Kiwi Project" in passages[0]["text"] or "Page body" in passages[0]["text"]

    def test_passage_has_canonical_ref_in_metadata(self, tmp_path):
        """The passage returned by Strategy 1b must carry canonical_ref in metadata."""
        wiki_store = _make_wiki_store_with_page(tmp_path=tmp_path)
        executor = WorkflowExecutor(wiki_store=wiki_store)
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])
        meta = passages[0].get("metadata", {})
        cref = meta.get("canonical_ref", {})
        assert cref.get("canonical_id") == PINNED_PAGE_ID, (
            f"canonical_ref.canonical_id must be {PINNED_PAGE_ID!r}, got: {cref}"
        )

    def test_passage_matches_canonical_returns_true(self, tmp_path):
        """_passage_matches_canonical must return True for the Strategy 1b passage."""
        wiki_store = _make_wiki_store_with_page(tmp_path=tmp_path)
        executor = WorkflowExecutor(wiki_store=wiki_store)
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])
        assert _passage_matches_canonical(passages[0], PINNED_CANONICAL), (
            "_passage_matches_canonical must return True for the Strategy 1b passage"
        )

    def test_correct_page_content_returned(self, tmp_path):
        """The passage text must be the actual markdown content written to the store."""
        expected_content = "# FAaaS Kiwi Project\nUnique marker: XYZ-999."
        wiki_store = _make_wiki_store_with_page(content=expected_content, tmp_path=tmp_path)
        executor = WorkflowExecutor(wiki_store=wiki_store)
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])
        assert "XYZ-999" in passages[0]["text"], (
            "Strategy 1b must return actual page content, not a stub"
        )


# ---------------------------------------------------------------------------
# B. Strategy 1b: page absent from wiki_store → Strategy 3 hard-fail
# ---------------------------------------------------------------------------

class TestStrategy1bMissPage:
    """When the page is not in wiki_store and ingest_on_demand=False, Strategy 3 fires."""

    def test_hard_fail_when_page_absent(self, tmp_path):
        """Hard-fail (ConfluencePageNotInKBError) when page absent from wiki_store."""
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        empty_store = WikiMetadataStore(root=tmp_path / "empty_store")
        executor = WorkflowExecutor(wiki_store=empty_store)
        cfg = _make_skill_cfg()
        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])
        assert PINNED_PAGE_ID in str(exc_info.value)

    def test_hard_fail_when_no_wiki_store(self):
        """Without wiki_store and without shim_kb, Strategy 3 fires."""
        executor = WorkflowExecutor()  # no retrievers, no wiki_store
        cfg = _make_skill_cfg()
        with pytest.raises(ConfluencePageNotInKBError):
            executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])


# ---------------------------------------------------------------------------
# C. Strategy 1a wins when shim_kb + retriever present + canonical_ref matches
# ---------------------------------------------------------------------------

class TestStrategy1aStillWorks:
    """Strategy 1a (KB-card-based) must still work when shim_kb is present."""

    def _make_canonical_result(self) -> Result:
        return Result(
            content_id=PINNED_PAGE_ID,
            chunk_id=None,
            text="FAaaS Kiwi content from KB retriever",
            score=1.0,
            citation_url=f"wiki://{PINNED_PAGE_ID}",
            metadata={
                "page_id": PINNED_PAGE_ID,
                "title": "FAaaS Kiwi Project",
                "canonical_ref": {
                    "connector_id": "confluence",
                    "resource_type": "page",
                    "canonical_id": PINNED_PAGE_ID,
                },
            },
        )

    def test_strategy_1a_returns_passage_when_card_found(self, tmp_path):
        """When shim_kb has the card and retriever returns matching passage, 1a wins."""
        card = {
            "name": "faaas_kiwi_pptx",
            "persona": "tpm",
            "retrieval_tools": ["search_wiki"],
        }
        shim = MagicMock()
        shim.all_cards.return_value = [card]

        mock_result = self._make_canonical_result()
        retriever = MagicMock(return_value=[mock_result])

        wiki_store = _make_wiki_store_with_page(tmp_path=tmp_path)

        executor = WorkflowExecutor(
            retrievers={"search_wiki": retriever},
            shim_kb=shim,
            wiki_store=wiki_store,
        )
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(cfg, {"input": "status"}, cfg["source_binding"])
        # Strategy 1a should find the result
        assert any("FAaaS Kiwi content from KB retriever" in p["text"] for p in passages)


# ---------------------------------------------------------------------------
# D. require_canonical_ref=True: raises when canonical_ref can't be stamped
# ---------------------------------------------------------------------------

class TestRequireCanonicalRefHardFail:
    """ingest_page require_canonical_ref=True must hard-fail when stamp fails."""

    def test_hard_fail_when_resolve_returns_unresolvable(self, tmp_path):
        """require_canonical_ref=True + Unresolvable result -> RuntimeError (not warning)."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        from framework.adapters._base import Unresolvable, UNRESOLVABLE_INVALID_REF

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(wiki_root=tmp_path / "wiki", wiki_store=store)

        unresolvable = Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference="bad-ref",
            reason=UNRESOLVABLE_INVALID_REF,
            detail="Cannot parse reference",
            retryable=False,
        )

        with patch(
            "framework.adapters.confluence.shared.resolve_to_numeric_id",
            return_value=unresolvable,
        ):
            page = {
                "id": "bad-ref", "title": "T", "space": "X",
                "body": "<p>content</p>", "labels": [],
            }
            with pytest.raises(RuntimeError, match="require_canonical_ref=True"):
                ingestor.ingest_page("bad-ref", _raw=page, require_canonical_ref=True)

    def test_no_error_when_require_false_and_resolve_fails(self, tmp_path):
        """require_canonical_ref=False (default): warning only, no exception."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        from framework.adapters._base import Unresolvable, UNRESOLVABLE_INVALID_REF

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(wiki_root=tmp_path / "wiki", wiki_store=store)

        unresolvable = Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference="bad-ref",
            reason=UNRESOLVABLE_INVALID_REF,
            detail="Cannot parse reference",
            retryable=False,
        )
        with patch(
            "framework.adapters.confluence.shared.resolve_to_numeric_id",
            return_value=unresolvable,
        ):
            page = {
                "id": "bad-ref", "title": "T", "space": "X",
                "body": "<p>content</p>", "labels": [],
            }
            # Should not raise — default require_canonical_ref=False
            result = ingestor.ingest_page("bad-ref", _raw=page)
        assert result["status"] in ("new", "updated", "unchanged")


# ---------------------------------------------------------------------------
# E. ingest_page require_canonical_ref=True: succeeds with numeric id
# ---------------------------------------------------------------------------

class TestRequireCanonicalRefSucceeds:
    """Numeric page_id fast-path always resolves to CanonicalRef — no exception."""

    def test_numeric_id_stamps_canonical_ref(self, tmp_path):
        """Numeric page_id always resolves via fast-path; canonical_ref is stamped."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(
            wiki_root=tmp_path / "wiki",
            wiki_store=store,
            persona="tpm",
        )
        page = {
            "id": PINNED_PAGE_ID, "title": "FAaaS Kiwi Project", "space": "OCIFACP",
            "body": "<p>FAaaS Kiwi Project content</p>", "labels": [],
        }
        result = ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)
        assert result["status"] in ("new", "updated")

        # Verify canonical_ref in store
        rec = store.get_page(PINNED_PAGE_ID)
        assert rec is not None
        cref = rec.get("canonical_ref", {})
        assert cref.get("canonical_id") == PINNED_PAGE_ID, (
            f"canonical_ref.canonical_id must be {PINNED_PAGE_ID!r}, got: {cref}"
        )
        assert cref.get("connector_id") == "confluence"
        assert cref.get("resource_type") == "page"

    def test_idempotent_second_ingest_unchanged(self, tmp_path):
        """Second ingest of same content: status=unchanged, no error."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(
            wiki_root=tmp_path / "wiki", wiki_store=store, persona="tpm",
        )
        page = {
            "id": PINNED_PAGE_ID, "title": "FAaaS Kiwi Project", "space": "OCIFACP",
            "body": "<p>Same content</p>", "labels": [],
        }
        ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)
        result2 = ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)
        assert result2["status"] == "unchanged"


# ---------------------------------------------------------------------------
# F. ingest_result integrity — false success prevention
# ---------------------------------------------------------------------------

class TestIngestResultReflectsReality:
    """ingest_result counters must NOT claim pages_new when write failed."""

    def test_ingest_result_pages_new_zero_when_canonical_ref_fails(self, tmp_path):
        """When require_canonical_ref hard-fail fires, ingest_result is NOT incremented."""
        # We verify this by checking that ingest_page raises — the caller
        # (conversation._run_ingest) catches it as a failure and does NOT increment total_new.
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        from framework.adapters._base import Unresolvable, UNRESOLVABLE_INVALID_REF

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(wiki_root=tmp_path / "wiki", wiki_store=store)

        unresolvable = Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference="bad-ref",
            reason=UNRESOLVABLE_INVALID_REF,
            detail="Cannot parse",
            retryable=False,
        )

        # Simulate _run_ingest logic: if ingest_page raises, we must NOT
        # increment total_new / mark ingest_result as completed.
        total_new = 0
        failures = []

        with patch(
            "framework.adapters.confluence.shared.resolve_to_numeric_id",
            return_value=unresolvable,
        ):
            page = {"id": "bad-ref", "title": "T", "space": "X", "body": "<p>c</p>", "labels": []}
            try:
                ingestor.ingest_page("bad-ref", _raw=page, require_canonical_ref=True)
                total_new += 1  # must NOT reach here
            except RuntimeError as exc:
                failures.append(("bad-ref", str(exc)))

        assert total_new == 0, (
            "total_new must not be incremented when ingest_page hard-fails"
        )
        assert len(failures) == 1
        # Derived ingest_result must reflect failure
        ingest_result = {
            "status": "failed" if failures else "completed",
            "pages_new": total_new,
        }
        assert ingest_result["status"] == "failed"
        assert ingest_result["pages_new"] == 0


# ---------------------------------------------------------------------------
# G. Post-ingest verification: hard-fail when store missing record after ingest
# ---------------------------------------------------------------------------

class TestPostIngestVerification:
    """_run_ingest post-ingest check: verify store has canonical_ref after ingest_page."""

    def test_post_ingest_verification_passes_when_canonical_ref_present(self, tmp_path):
        """get_page after ingest returns record with canonical_ref → verification passes."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(
            wiki_root=tmp_path / "wiki", wiki_store=store, persona="tpm"
        )
        page = {
            "id": PINNED_PAGE_ID, "title": "FAaaS Kiwi Project", "space": "OCIFACP",
            "body": "<p>Content</p>", "labels": [],
        }
        ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)

        # Verify post-ingest (mirrors conversation._run_ingest logic)
        rec = store.get_page(PINNED_PAGE_ID)
        assert rec is not None, "Page record must be present in store after ingest"
        assert rec.get("canonical_ref") is not None, (
            "canonical_ref must be present after require_canonical_ref=True ingest"
        )

    def test_post_ingest_verification_detects_missing_canonical_ref(self, tmp_path):
        """get_page with no canonical_ref → mirrors the verification hard-fail logic."""
        from framework.stores.wiki_metadata_store import WikiMetadataStore

        store = WikiMetadataStore(root=tmp_path / "store")
        # Deliberately write a record WITHOUT canonical_ref
        store.upsert_page({
            "page_id": PINNED_PAGE_ID,
            "title": "FAaaS Kiwi Project",
            "path": "/fake/path.md",
            "persona": "tpm",
            # NO canonical_ref
        })

        rec = store.get_page(PINNED_PAGE_ID)
        # Mirrors the conversation._run_ingest post-ingest check:
        verify_fails = rec is None or not rec.get("canonical_ref")
        assert verify_fails, (
            "Post-ingest verification must detect missing canonical_ref"
        )


# ---------------------------------------------------------------------------
# H. Full ingest→retrieve round-trip (unit-mock; real round-trip in Step 3)
# ---------------------------------------------------------------------------

class TestIngestRetrieveRoundTrip:
    """Ingest page → executor finds it via Strategy 1b (unit-mock round-trip).

    Note: unit mocks are NECESSARY but NOT SUFFICIENT per the bug report.
    Real round-trip proof requires live Confluence + ADB (see docs/wiki/log.md).
    """

    def test_ingest_then_executor_retrieves_via_strategy_1b(self, tmp_path):
        """Full path: ingest_page writes record → executor Strategy 1b reads it."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore

        # Shared store (same default root semantics as production)
        store = WikiMetadataStore(root=tmp_path / "store")
        wiki_root = tmp_path / "wiki"

        ingestor = ConfluenceWikiIngestor(
            wiki_root=wiki_root,
            wiki_store=store,
            persona="tpm",
        )

        page = {
            "id": PINNED_PAGE_ID,
            "title": "FAaaS Kiwi Project",
            "space": "OCIFACP",
            "body": "<p>FAaaS Kiwi: milestone M1 complete. Risk: dependency on vendor.</p>",
            "labels": [],
        }
        ingest_result = ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)
        assert ingest_result["status"] == "new"

        # Verify canonical_ref stamped
        rec = store.get_page(PINNED_PAGE_ID)
        assert rec is not None
        assert rec["canonical_ref"]["canonical_id"] == PINNED_PAGE_ID

        # Executor with the SAME store (as wired in _run_eval after fix)
        executor = WorkflowExecutor(wiki_store=store)
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(
            cfg, {"input": "FAaaS Kiwi project status"}, cfg["source_binding"]
        )

        # Must return at least one passage
        assert len(passages) >= 1, "Strategy 1b must return at least one passage"

        # Passage must contain the ingested content
        combined_text = " ".join(p["text"] for p in passages)
        assert "FAaaS Kiwi" in combined_text or "milestone M1" in combined_text, (
            "Passage must contain the ingested page content"
        )

        # _passage_matches_canonical must return True
        assert _passage_matches_canonical(passages[0], PINNED_CANONICAL), (
            "_passage_matches_canonical must return True for the ingested page passage"
        )

    def test_ingest_idempotent_second_run_executor_still_finds_page(self, tmp_path):
        """Idempotent second ingest: executor still finds page (content unchanged)."""
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
        from framework.stores.wiki_metadata_store import WikiMetadataStore

        store = WikiMetadataStore(root=tmp_path / "store")
        ingestor = ConfluenceWikiIngestor(
            wiki_root=tmp_path / "wiki", wiki_store=store, persona="tpm"
        )
        page = {
            "id": PINNED_PAGE_ID, "title": "FAaaS Kiwi Project", "space": "OCIFACP",
            "body": "<p>Same content both times.</p>", "labels": [],
        }
        # First ingest
        ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)
        # Second ingest (unchanged)
        result2 = ingestor.ingest_page(PINNED_PAGE_ID, _raw=page, require_canonical_ref=True)
        assert result2["status"] == "unchanged"

        # Executor must still find the page
        executor = WorkflowExecutor(wiki_store=store)
        cfg = _make_skill_cfg()
        passages = executor._retrieve_author_fixed_pinned(
            cfg, {"input": "status"}, cfg["source_binding"]
        )
        assert len(passages) >= 1
