"""Unit tests for VectorSearchRetriever — fixture fallback path.

Coverage:
  - Known corpus delegates to registered Store (no fallback)
  - Unknown corpus triggers fixture fallback; returns Results
  - Fixture fallback ranks by keyword overlap
  - Fixture fallback for 26ai exec-review KB returns the W20 fixture
  - No fixture dir for corpus returns empty list (no crash)
  - ValueError no longer raised for unknown corpus (regression guard)
  - Fixture fallback handles malformed JSON files gracefully
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.retrievers.vector_search import VectorSearchRetriever
from framework.core.interfaces import Query, Result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**kwargs) -> Result:
    defaults = {
        "content_id": "x",
        "chunk_id": None,
        "text": "text",
        "score": 0.9,
        "citation_url": "http://example.com",
        "metadata": {},
    }
    defaults.update(kwargs)
    return Result(**defaults)


def _make_store(results: list[Result] | None = None) -> MagicMock:
    store = MagicMock()
    store.query.return_value = results or [_make_result()]
    return store


# ---------------------------------------------------------------------------
# Tests — registered store path
# ---------------------------------------------------------------------------


class TestVectorSearchRegisteredStore:
    """When a store is registered for the corpus, it should be used directly."""

    def test_known_corpus_uses_store(self):
        store = _make_store()
        ret = VectorSearchRetriever({"ops_incidents": store})
        results = ret(corpus="ops_incidents", query="auth service crash", k=5)
        assert store.query.called
        assert results == store.query.return_value

    def test_known_corpus_passes_correct_query(self):
        store = _make_store()
        ret = VectorSearchRetriever({"ops_incidents": store})
        ret(corpus="ops_incidents", query="pod restart loop", filters=[{"field": "sev", "values": ["sev1"]}], k=3)
        call_args = store.query.call_args[0][0]
        assert isinstance(call_args, Query)
        assert call_args.kind == "vector_knn"
        assert call_args.payload["query"] == "pod restart loop"
        assert call_args.limit == 3


# ---------------------------------------------------------------------------
# Tests — fixture fallback path
# ---------------------------------------------------------------------------


class TestVectorSearchFixtureFallback:
    """When corpus is not registered, fixture files should be used."""

    def test_unknown_corpus_does_not_raise(self):
        """Regression guard: must not raise ValueError for unknown corpus."""
        ret = VectorSearchRetriever({})
        # Should return a list (possibly empty), not raise
        try:
            results = ret(corpus="tpm.nonexistent_kb", query="anything", k=5)
            assert isinstance(results, list)
        except ValueError:
            pytest.fail("VectorSearchRetriever raised ValueError for unknown corpus — should return empty list")

    def test_26ai_fixture_found(self):
        """tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr must find the W20 fixture."""
        ret = VectorSearchRetriever({})
        results = ret(
            corpus="tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr",
            query="status of 26ai project",
            k=5,
        )
        assert len(results) > 0, (
            "fixture fallback must return at least one result for the 26ai exec-review corpus"
        )
        # The W20 fixture should be there
        texts = [r.text for r in results]
        combined = " ".join(texts)
        assert "26ai" in combined.lower(), "fixture results must contain 26ai project data"

    def test_26ai_fixture_result_has_citation(self):
        """Each fixture result must carry a non-empty citation URL."""
        ret = VectorSearchRetriever({})
        results = ret(
            corpus="tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr",
            query="overall rag status",
            k=3,
        )
        assert len(results) > 0
        for r in results:
            assert r.citation_url, f"result has empty citation_url: {r}"

    def test_26ai_fixture_score_nonzero(self):
        """All fixture results must have a score > 0 so they survive passage dedup."""
        ret = VectorSearchRetriever({})
        results = ret(
            corpus="tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr",
            query="schedule health blockers exec asks",
            k=5,
        )
        assert all(r.score > 0 for r in results), "fixture results must have score > 0"

    def test_weekly_ops_fixture_found_via_partial_match(self):
        """tpm.tpm_weekly_ops should find the weekly_ops fixture dir via partial match."""
        ret = VectorSearchRetriever({})
        results = ret(
            corpus="tpm.tpm_weekly_ops",
            query="blockers and milestones for this week",
            k=5,
        )
        assert len(results) > 0, (
            "fixture fallback must return results for tpm_weekly_ops via partial dir match"
        )

    def test_unknown_corpus_no_fixture_returns_empty(self):
        """A corpus with no matching fixture dir must return empty list, not crash."""
        ret = VectorSearchRetriever({})
        results = ret(corpus="tpm.never_ever_exists_kb_xyz", query="anything", k=5)
        assert results == []

    def test_malformed_json_in_fixture_dir(self, tmp_path):
        """Malformed JSON fixture files must be skipped, not crash the retriever."""
        import framework.retrievers.vector_search as vs_mod

        # Temporarily point _FIXTURES_DIR to a tmp dir with one bad file and one good file
        fixture_dir = tmp_path / "bad-kb"
        fixture_dir.mkdir()
        (fixture_dir / "bad.json").write_text("{not valid json{{")
        (fixture_dir / "good.json").write_text(json.dumps({
            "id": "good-1", "title": "Good fixture", "text": "project status is green",
            "url": "https://example.com/good",
        }))

        original = vs_mod._FIXTURES_DIR
        try:
            vs_mod._FIXTURES_DIR = tmp_path
            ret = VectorSearchRetriever({})
            results = ret(corpus="bad.kb", query="project status", k=5)
            assert len(results) == 1, "only the valid fixture should be returned"
            assert results[0].content_id == "good-1"
        finally:
            vs_mod._FIXTURES_DIR = original

    def test_keyword_overlap_ranking(self, tmp_path):
        """Fixture with more query term overlap should rank higher."""
        import framework.retrievers.vector_search as vs_mod

        fixture_dir = tmp_path / "rank-kb"
        fixture_dir.mkdir()
        # File A: many query terms
        (fixture_dir / "a.json").write_text(json.dumps({
            "id": "a", "status": "amber", "project": "26ai", "blockers": "gpu capacity",
            "url": "https://example.com/a",
        }))
        # File B: barely any query terms
        (fixture_dir / "b.json").write_text(json.dumps({
            "id": "b", "title": "Unrelated topic",
            "url": "https://example.com/b",
        }))

        original = vs_mod._FIXTURES_DIR
        try:
            vs_mod._FIXTURES_DIR = tmp_path
            ret = VectorSearchRetriever({})
            results = ret(corpus="rank.kb", query="26ai amber blockers gpu", k=5)
            assert len(results) == 2
            assert results[0].content_id == "a", (
                "fixture with higher keyword overlap must rank first"
            )
        finally:
            vs_mod._FIXTURES_DIR = original
