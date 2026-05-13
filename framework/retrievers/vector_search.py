"""vector_search MCP tool — semantic recall over a named vector corpus.

In production (ADB/pgvector available): delegates to the registered Store for
the named corpus (set up at server startup in mcp_server.py).

On laptop / dev (no ADB pool): falls back to keyword-overlap search over
framework/_dev_fixtures/<kb-name>/*.json fixture files.  The fallback is
transparent — callers see the same Result interface either way.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..core.interfaces import Query, Result

log = logging.getLogger(__name__)

# Resolve once at import time — safe because this module is always loaded from
# inside the framework/ tree.
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "framework" / "_dev_fixtures"


class VectorSearchRetriever:
    name = "vector_search"

    def __init__(self, stores_by_corpus: dict):
        # corpus name → Store instance (e.g. "ops_incidents" -> IncidentVectorStore)
        self.stores = stores_by_corpus

    def __call__(
        self,
        corpus: str,
        query: str,
        filters: list[dict] | None = None,
        k: int = 10,
        persona: str | None = None,
    ) -> list[Result]:
        store = self.stores.get(corpus)
        if store:
            q = Query(
                kind="vector_knn",
                payload={"query": query, "filters": filters or []},
                persona=persona,
                limit=k,
            )
            return store.query(q)

        # --- Laptop / dev fixture fallback ---------------------------------
        # When no ADB store is registered for this corpus, search dev fixture
        # JSON files by keyword overlap.  This keeps Tier 2 retrieval
        # functional on localhost without a running pgvector instance.
        results = self._fixture_fallback(corpus, query, k)
        if results:
            log.debug(
                "vector_search fixture fallback: corpus=%s query=%r hits=%d",
                corpus, query[:60], len(results),
            )
        else:
            log.debug(
                "vector_search: no store and no fixtures for corpus=%s; "
                "available stores: %s", corpus, list(self.stores),
            )
        return results

    # ------------------------------------------------------------------
    # Fixture fallback — keyword-overlap search over _dev_fixtures/
    # ------------------------------------------------------------------

    def _fixture_fallback(self, corpus: str, query: str, k: int) -> list[Result]:
        """Return up to k Results from dev fixture JSON files for the given corpus.

        Corpus-to-dir matching mirrors the logic in
        WorkflowExecutor._load_fixture_passages():
          "tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr"
          → kb_name = "generate-a-weekly-exec-review-pptx-for-the-26ai-pr"
          → matches _dev_fixtures/generate-a-weekly-exec-review-pptx-for-the-26ai-pr/
        """
        if not _FIXTURES_DIR.exists():
            return []

        # Derive a filesystem-friendly name from the corpus key
        kb_name = corpus.split(".")[-1].replace("_", "-").lower()

        # Find the best matching fixture directory
        fixture_dir: Path | None = None
        best_overlap = 0
        for d in _FIXTURES_DIR.iterdir():
            if not d.is_dir():
                continue
            dir_name = d.name.replace("_", "-").lower()
            # Exact match wins immediately
            if dir_name == kb_name:
                fixture_dir = d
                break
            # Partial match — prefer longer shared substring
            if dir_name in kb_name or kb_name in dir_name:
                shared = min(len(dir_name), len(kb_name))
                if shared > best_overlap:
                    best_overlap = shared
                    fixture_dir = d

        if fixture_dir is None:
            return []

        q_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored: list[tuple[float, str, dict, str]] = []

        for fpath in sorted(fixture_dir.glob("*.json")):
            try:
                data = json.loads(fpath.read_text())
            except Exception:
                continue
            text = json.dumps(data, indent=2)
            text_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
            overlap = (
                len(q_tokens & text_tokens) / max(len(q_tokens | text_tokens), 1)
            )
            scored.append((overlap, text, data, str(fpath)))

        scored.sort(key=lambda x: -x[0])

        results: list[Result] = []
        for score, text, data, fpath_str in scored[:k]:
            results.append(
                Result(
                    content_id=data.get("id") or data.get("source_id") or Path(fpath_str).stem,
                    chunk_id=None,
                    text=text,
                    score=max(score, 0.05),   # keep a floor so passage isn't pruned
                    citation_url=(
                        data.get("url")
                        or f"fixture://{Path(fpath_str).name}"
                    ),
                    metadata=data,
                )
            )
        return results
