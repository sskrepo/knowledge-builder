"""Tests for DECISION-020 §3/§4 write-side fix.

author_fixed + ingest_on_demand:false skills must have their pinned page
ingested into the persona KB at author INGEST time so the executor's
_retrieve_author_fixed_pinned() finds it via KB retrieval.

Round-trip: INGEST stamps canonical_ref in the wiki metadata store;
retrievers (search_wiki, read_wiki_page) forward it in passage metadata;
_passage_matches_canonical() returns True for the matching canonical_id.

Tests cover the DECISION-013 bug (BUG-queue-<uuid>):
  - author_fixed + ingest_on_demand:false -> pinned page content written to KB
    store keyed by canonical_id
  - round-trip with mocked adapter + real/temp KB store: _passage_matches_canonical
    returns True for the ingested page
  - ask_parameterized and markdown INGEST: pinned-page logic does NOT trigger
  - fetch-failure -> loud typed error (DECISION-020 §4/§6 no silent skip)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_canonical_ref(canonical_id: str) -> dict:
    return {
        "connector_id": "confluence",
        "resource_type": "page",
        "canonical_id": canonical_id,
    }


def _make_wiki_store(tmp_path: Path):
    """Return a real WikiMetadataStore backed by tmp_path."""
    from framework.stores.wiki_metadata_store import WikiMetadataStore
    return WikiMetadataStore(root=tmp_path / "wiki_metadata")


def _make_ingestor(tmp_path: Path, wiki_store, adapter=None, persona="tpm"):
    from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
    return ConfluenceWikiIngestor(
        wiki_root=tmp_path / "wiki",
        adapter=adapter,
        wiki_store=wiki_store,
        persona=persona,
    )


def _make_adapter_returning(page_id: str, body: str = "<p>Kiwi project PPTX page content.</p>"):
    """Return a mock Confluence adapter whose fetch() returns a RawItem."""
    from framework.adapters._base import RawItem
    raw_item = RawItem(
        kind="confluence_page",
        source="confluence",
        source_id=page_id,
        payload={"body": body},
        metadata={
            "title": f"Page {page_id}",
            "space": "FA",
            "url": f"https://confluence.example.com/pages/{page_id}",
            "labels": [],
        },
    )
    adapter = MagicMock()
    adapter.fetch.return_value = raw_item
    return adapter


# ---------------------------------------------------------------------------
# 1. ingest_page stamps canonical_ref in wiki_metadata_store record
# ---------------------------------------------------------------------------

class TestIngestPageStampsCanonicalRef:

    def test_canonical_ref_written_to_store_for_numeric_page_id(self, tmp_path):
        """ingest_page("20382503622") -> wiki_store record has canonical_ref."""
        wiki_store = _make_wiki_store(tmp_path)
        adapter = _make_adapter_returning("20382503622")
        ingestor = _make_ingestor(tmp_path, wiki_store, adapter=adapter)

        result = ingestor.ingest_page("20382503622")
        assert result["status"] == "new"

        record = wiki_store.get_page("20382503622")
        assert record is not None, "wiki_store must have a record for page 20382503622"
        assert "canonical_ref" in record, (
            "canonical_ref must be stamped in the wiki_metadata record "
            "(ADR-039 write-side -- executor canonical==canonical match requires it)"
        )
        cref = record["canonical_ref"]
        assert cref["connector_id"] == "confluence"
        assert cref["resource_type"] == "page"
        assert cref["canonical_id"] == "20382503622"

    def test_canonical_ref_absent_on_resolve_error_no_crash(self, tmp_path):
        """If resolve_to_numeric_id raises, ingest_page logs a warning but still succeeds."""
        wiki_store = _make_wiki_store(tmp_path)
        adapter = _make_adapter_returning("99999")
        ingestor = _make_ingestor(tmp_path, wiki_store, adapter=adapter)

        with patch(
            "framework.adapters.confluence.shared.resolve_to_numeric_id",
            side_effect=RuntimeError("resolve failed"),
        ):
            result = ingestor.ingest_page("99999")
            # Must not raise; status can be new/updated/unchanged
            assert result["status"] in ("new", "updated", "unchanged")

        # The record is written without canonical_ref; that is acceptable (warning path)
        record = wiki_store.get_page("99999")
        assert record is not None

    def test_unchanged_page_does_not_call_upsert_page(self, tmp_path):
        """Idempotency: second ingest of unchanged page does not touch wiki_store."""
        wiki_store = _make_wiki_store(tmp_path)
        adapter = _make_adapter_returning("20382503622")
        ingestor = _make_ingestor(tmp_path, wiki_store, adapter=adapter)

        # First ingest
        r1 = ingestor.ingest_page("20382503622")
        assert r1["status"] == "new"

        # Second ingest same content -> unchanged; upsert_page NOT called again
        mock_store = MagicMock(wraps=wiki_store)
        ingestor._wiki_store = mock_store
        r2 = ingestor.ingest_page("20382503622")
        assert r2["status"] == "unchanged"
        mock_store.upsert_page.assert_not_called()


# ---------------------------------------------------------------------------
# 2. WikiMetadataStore preserves canonical_ref through upsert_page / get_page
# ---------------------------------------------------------------------------

class TestWikiMetadataStorePreservesCanonicalRef:

    def test_upsert_and_get_round_trip_canonical_ref(self, tmp_path):
        """upsert_page with canonical_ref -> get_page returns the same canonical_ref."""
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        store = WikiMetadataStore(root=tmp_path / "wms")
        cref = _make_canonical_ref("20382503622")

        store.upsert_page({
            "page_id": "20382503622",
            "title": "Test Page",
            "path": "/tmp/test.md",
            "canonical_ref": cref,
        })

        record = store.get_page("20382503622")
        assert record is not None
        assert record.get("canonical_ref") == cref

    def test_upsert_without_canonical_ref_does_not_inject_none(self, tmp_path):
        """Backward compat: records without canonical_ref have no canonical_ref key."""
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        store = WikiMetadataStore(root=tmp_path / "wms")

        store.upsert_page({
            "page_id": "OLD-999",
            "title": "Old Page",
            "path": "/tmp/old.md",
        })

        record = store.get_page("OLD-999")
        assert record is not None
        # canonical_ref must not be present (or must be None-ish)
        assert not record.get("canonical_ref"), (
            "Records ingested without canonical_ref must not have a non-falsy canonical_ref"
        )


# ---------------------------------------------------------------------------
# 3. search_wiki retriever forwards canonical_ref from wiki_store record
# ---------------------------------------------------------------------------

class TestSearchWikiRetrieverForwardsCanonicalRef:

    def test_canonical_ref_in_passage_metadata_when_present_in_store(self, tmp_path):
        """search_wiki result includes canonical_ref from the store record."""
        from framework.retrievers.search_wiki import SearchWikiRetriever

        wiki_file = tmp_path / "page.md"
        wiki_file.write_text("Kiwi project PPTX content.", encoding="utf-8")

        store = MagicMock()
        store.search_pages.return_value = [{
            "page_id": "20382503622",
            "title": "Kiwi Project PPTX",
            "path": str(wiki_file),
            "persona": "tpm",
            "tags": [],
            "canonical_ref": _make_canonical_ref("20382503622"),
        }]

        retriever = SearchWikiRetriever(wiki_store=store)
        results = retriever(query="kiwi project")

        assert len(results) == 1
        meta = results[0].metadata
        assert "canonical_ref" in meta, (
            "search_wiki must forward canonical_ref in passage metadata "
            "(ADR-039 read-side -- executor _passage_matches_canonical checks it)"
        )
        assert meta["canonical_ref"]["canonical_id"] == "20382503622"
        assert meta["canonical_ref"]["connector_id"] == "confluence"

    def test_no_canonical_ref_in_passage_metadata_when_absent_from_store(self, tmp_path):
        """Backward compat: records without canonical_ref do not inject None."""
        from framework.retrievers.search_wiki import SearchWikiRetriever

        store = MagicMock()
        store.search_pages.return_value = [{
            "page_id": "OLD-999",
            "title": "Old Page",
            "path": "",
            "persona": "tpm",
            "tags": [],
            # No canonical_ref key
        }]

        retriever = SearchWikiRetriever(wiki_store=store)
        results = retriever(query="old page")

        assert len(results) == 1
        meta = results[0].metadata
        assert not meta.get("canonical_ref"), (
            "No canonical_ref must be injected for old records that lack it"
        )


# ---------------------------------------------------------------------------
# 4. read_wiki_page retriever forwards canonical_ref from wiki_store record
# ---------------------------------------------------------------------------

class TestReadWikiPageRetrieverForwardsCanonicalRef:

    def test_canonical_ref_in_passage_metadata_when_present(self, tmp_path):
        """read_wiki_page result includes canonical_ref from the store record."""
        from framework.retrievers.read_wiki_page import ReadWikiPageRetriever

        wiki_file = tmp_path / "page.md"
        wiki_file.write_text("Pinned page body.", encoding="utf-8")

        store = MagicMock()
        store.get_page.return_value = {
            "page_id": "20382503622",
            "title": "Kiwi Project PPTX",
            "path": str(wiki_file),
            "persona": "tpm",
            "tags": [],
            "canonical_ref": _make_canonical_ref("20382503622"),
        }

        retriever = ReadWikiPageRetriever(wiki_store=store)
        result = retriever(path="20382503622")

        assert result is not None
        meta = result.metadata
        assert "canonical_ref" in meta, (
            "read_wiki_page must forward canonical_ref in passage metadata"
        )
        assert meta["canonical_ref"]["canonical_id"] == "20382503622"

    def test_no_canonical_ref_when_absent_from_record(self, tmp_path):
        """Backward compat: records without canonical_ref do not inject None."""
        from framework.retrievers.read_wiki_page import ReadWikiPageRetriever

        wiki_file = tmp_path / "page.md"
        wiki_file.write_text("Body.", encoding="utf-8")

        store = MagicMock()
        store.get_page.return_value = {
            "page_id": "OLD-888",
            "title": "Old Page",
            "path": str(wiki_file),
        }

        retriever = ReadWikiPageRetriever(wiki_store=store)
        result = retriever(path="OLD-888")

        assert result is not None
        assert not result.metadata.get("canonical_ref")


# ---------------------------------------------------------------------------
# 5. Round-trip: ingest -> wiki_store -> search_wiki -> _passage_matches_canonical
# ---------------------------------------------------------------------------

class TestRoundTripCanonicalRefMatchesExecutorPattern:
    """Full round-trip with real WikiMetadataStore and real retrievers.

    Mimics what _retrieve_author_fixed_pinned() does:
    1. resolve_to_numeric_id(pinned_ref) -> CanonicalRef
    2. retriever(query, persona) -> [Result]
    3. _passage_matches_canonical(passage, canonical) -> True
    """

    def test_round_trip_ingest_and_retriever_match(self, tmp_path):
        from framework.adapters._base import CanonicalRef
        from framework.adapters.confluence.shared import resolve_to_numeric_id
        from framework.workflow_runtime.executor import _passage_matches_canonical
        from framework.retrievers.search_wiki import SearchWikiRetriever
        from framework.stores.wiki_metadata_store import WikiMetadataStore
        from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor

        PINNED_ID = "20382503622"

        # --- INGEST phase ---
        wiki_root = tmp_path / "wiki"
        wiki_store = WikiMetadataStore(root=tmp_path / "wiki_metadata")
        adapter = _make_adapter_returning(PINNED_ID, body="<p>Kiwi project page content.</p>")
        ingestor = ConfluenceWikiIngestor(
            wiki_root=wiki_root,
            adapter=adapter,
            wiki_store=wiki_store,
            persona="tpm",
        )
        ingest_result = ingestor.ingest_page(PINNED_ID)
        assert ingest_result["status"] == "new"

        # Verify wiki_store record has canonical_ref
        record = wiki_store.get_page(PINNED_ID)
        assert record is not None
        assert record.get("canonical_ref", {}).get("canonical_id") == PINNED_ID

        # --- RETRIEVAL phase (Strategy 1 mimic) ---
        real_wiki_store = WikiMetadataStore(root=tmp_path / "wiki_metadata")
        retriever = SearchWikiRetriever(wiki_store=real_wiki_store, wiki_root=str(wiki_root))
        results = retriever(query="Kiwi project page content", persona="tpm")

        # --- MATCH phase (executor's _passage_matches_canonical) ---
        canonical = resolve_to_numeric_id(
            reference=PINNED_ID,
            resource_type="page",
            session=None,
            base_url="",
        )
        assert isinstance(canonical, CanonicalRef), (
            f"resolve_to_numeric_id({PINNED_ID!r}) must return CanonicalRef, got {canonical!r}"
        )
        assert canonical.canonical_id == PINNED_ID

        passages = [
            {
                "text": getattr(r, "text", "") or "",
                "citation": getattr(r, "citation_url", "") or "",
                "metadata": getattr(r, "metadata", {}) or {},
            }
            for r in results
        ]

        matching = [p for p in passages if _passage_matches_canonical(p, canonical)]
        assert len(matching) >= 1, (
            f"Expected at least 1 passage matching canonical_id={PINNED_ID!r} "
            f"but got 0. Passages: {passages!r}. "
            "This is the DECISION-020 write-side bug: canonical_ref was not "
            "stamped by the ingestor or not forwarded by the retriever."
        )


# ---------------------------------------------------------------------------
# 6. ask_parameterized / no-pinned-ref: pinned-page logic does NOT trigger
# ---------------------------------------------------------------------------

def _make_conversation_session(skill_yaml: dict, source_binding_mode: str):
    """Build a minimal SkillBuilderConversation with synthesized_artifacts."""
    from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData

    session = SkillBuilderConversation.__new__(SkillBuilderConversation)
    session._data = _SessionData(
        persona="tpm",
        skill_name="test_skill",
        source_binding_mode=source_binding_mode,
    )
    session._data.synthesized_artifacts = {
        "framework/workflow_skills/tpm/test_skill.yaml": skill_yaml,
    }
    session._data.sources = []  # no space-based confluence sources
    session._llm = MagicMock()
    session._skill_store = MagicMock()
    session._state = "VALIDATE"
    return session


class TestAskParameterizedUnaffected:
    """ask_parameterized and author_fixed-without-pinned-ref: no pinned-page ingest."""

    def test_ask_parameterized_does_not_ingest_pinned_page(self):
        """ask_parameterized skills: no pinned-page ingest attempt."""
        skill_yaml = {
            "workflow_skill": "tpm.test_skill",
            "source_binding": {
                "mode": "ask_parameterized",
                "ingest_on_demand": True,
                "input_param": "page_url",
            },
        }
        session = _make_conversation_session(skill_yaml, "ask_parameterized")

        with patch(
            "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor.ingest_page"
        ) as mock_ingest_page:
            result = session._run_ingest()

        mock_ingest_page.assert_not_called()
        assert result.state == "INGEST"

    def test_author_fixed_no_pinned_ref_does_not_ingest_pinned_page(self):
        """author_fixed with no pinned_ref/canonical_ref: no pinned-page ingest."""
        skill_yaml = {
            "workflow_skill": "tpm.test_skill",
            "source_binding": {
                "mode": "author_fixed",
                "ingest_on_demand": False,
                # No pinned_ref, no canonical_ref
            },
        }
        session = _make_conversation_session(skill_yaml, "author_fixed")

        with patch(
            "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor.ingest_page"
        ) as mock_ingest_page:
            result = session._run_ingest()

        mock_ingest_page.assert_not_called()
        assert result.state == "INGEST"

    def test_author_fixed_ingest_on_demand_true_does_not_ingest_at_author_time(self):
        """author_fixed + ingest_on_demand:true: runtime fetch, NOT author-time ingest."""
        skill_yaml = {
            "workflow_skill": "tpm.test_skill",
            "source_binding": {
                "mode": "author_fixed",
                "ingest_on_demand": True,
                "pinned_ref": "20382503622",
                "canonical_ref": {"canonical_id": "20382503622"},
            },
        }
        session = _make_conversation_session(skill_yaml, "author_fixed")

        with patch(
            "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor.ingest_page"
        ) as mock_ingest_page:
            result = session._run_ingest()

        mock_ingest_page.assert_not_called()
        assert result.state == "INGEST"


# ---------------------------------------------------------------------------
# 7. Fetch failure -> loud typed error (DECISION-020 §4/§6)
# ---------------------------------------------------------------------------

class TestPinnedPageFetchFailureLoud:
    """author_fixed + !ingest_on_demand + adapter raises -> INGEST hard-fail."""

    def _make_pinned_session(self, pinned_id="20382503622"):
        skill_yaml = {
            "workflow_skill": "tpm.kiwi_skill",
            "source_binding": {
                "mode": "author_fixed",
                "ingest_on_demand": False,
                "pinned_ref": pinned_id,
                "canonical_ref": {"canonical_id": pinned_id},
            },
        }
        session = _make_conversation_session(skill_yaml, "author_fixed")
        session._data.skill_name = "kiwi_skill"
        session._data.synthesized_artifacts = {
            "framework/workflow_skills/tpm/kiwi_skill.yaml": skill_yaml,
        }
        return session, pinned_id

    def test_adapter_fetch_failure_causes_ingest_to_fail_loudly(self):
        """Adapter fetch raises -> INGEST must report status=failed."""
        session, pinned_id = self._make_pinned_session()

        failing_adapter = MagicMock()
        failing_adapter.fetch.side_effect = ConnectionError("Confluence unreachable")

        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=failing_adapter,
        ):
            with patch(
                "framework.stores.wiki_metadata_store.WikiMetadataStore.__init__",
                return_value=None,
            ):
                with patch.object(
                    __import__(
                        "framework.stores.wiki_metadata_store",
                        fromlist=["WikiMetadataStore"]
                    ).WikiMetadataStore,
                    "upsert_page",
                    return_value=None,
                ):
                    result = session._run_ingest()

        assert result.state == "INGEST"
        ingest_data = result.data.get("ingest", {})
        assert ingest_data.get("status") == "failed", (
            "INGEST must report status=failed when pinned page fetch fails "
            "(DECISION-020 §4/§6 -- no silent skip that defers failure to EVAL)"
        )
        failures = ingest_data.get("failures", [])
        assert any(
            pinned_id in str(f.get("space", "") + f.get("error", ""))
            for f in failures
        ), f"failure entry must reference {pinned_id!r}. Failures: {failures!r}"

    def test_adapter_none_causes_ingest_to_fail_loudly(self):
        """Adapter is None (not configured) -> INGEST must fail loudly."""
        session, pinned_id = self._make_pinned_session()

        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=None,
        ):
            result = session._run_ingest()

        assert result.state == "INGEST"
        ingest_data = result.data.get("ingest", {})
        assert ingest_data.get("status") == "failed", (
            "INGEST must fail loudly when adapter is None for author_fixed+pinned page "
            "(DECISION-020 §4/§6)"
        )
