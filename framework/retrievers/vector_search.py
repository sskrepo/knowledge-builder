"""vector_search MCP tool — semantic recall over a named vector corpus."""
from __future__ import annotations
from typing import Any
from ..core.interfaces import Query, Result

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
        if not store:
            raise ValueError(f"unknown corpus: {corpus}; available: {list(self.stores)}")
        q = Query(
            kind="vector_knn",
            payload={"query": query, "filters": filters or []},
            persona=persona,
            limit=k,
        )
        return store.query(q)
