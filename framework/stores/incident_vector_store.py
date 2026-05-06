"""IncidentVectorStore — Oracle 23ai vector store for the incident KB.

Handles:
  - Schema migration (kb_incidents.sql)
  - Idempotent upsert of ContentItem + Chunk + Edge
  - Embedding generation via LLMClient (text-embedding-3-large, 3072 dims)
  - Chunk text splitting via Chunker
  - Vector kNN queries with metadata filtering

Bodies for incident KB live in this DB (not git, since these aren't curated wikis).

Per: ADR-002 (storage shape), ADR-003 (Store Protocol), ADR-008 (multi-axis),
     framework/stores/sql/kb_incidents.sql (schema)

Status: STUB — Phase 1 STORY-005 fills the bodies.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..core.content import ContentItem, Chunk, Edge
from ..core.llm import LLMClient
from ..parsers.chunker import Chunker
from ._base import BaseStore, Query, Result

log = logging.getLogger(__name__)

EMBEDDING_DIM = 3072  # text-embedding-3-large; pinned per ADR-001 / ADR-003.
DDL_PATH = Path(__file__).parent / "sql" / "kb_incidents.sql"


class IncidentVectorStore(BaseStore):
    """Vector + edges store for the operational incident KB."""

    kind = "vector"
    schema_name = "kb_incidents"

    def __init__(
        self,
        adb_pool,            # oracledb connection pool
        llm: LLMClient,
        chunker: Chunker | None = None,
    ):
        self.pool = adb_pool
        self.llm = llm
        self.chunker = chunker or Chunker(target_tokens=512, overlap_tokens=64)

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------
    def migrate(self) -> None:
        """Run kb_incidents.sql against the configured schema. Idempotent."""
        # TODO Phase 1 STORY-005:
        # 1. Read DDL_PATH; split on ';'; execute each non-empty statement.
        # 2. Use IF-NOT-EXISTS / DDL trigger to make it idempotent
        #    (Oracle has no IF NOT EXISTS for CREATE TABLE; wrap with EXCEPTION
        #    handlers or pre-check USER_TABLES).
        # 3. Run as KB_INCIDENTS_RW from env config adb.schemas.kb_incidents.user.
        raise NotImplementedError("STORY-005 wk2")

    # ------------------------------------------------------------------
    # Write path (used by ingestion pipeline)
    # ------------------------------------------------------------------
    def upsert(self, items: list[ContentItem]) -> None:
        """Idempotent upsert. Skips items whose source_sha matches stored row."""
        for item in items:
            if self._unchanged(item):
                log.debug("skip unchanged item id=%s", item.id)
                continue
            self._validate(item)
            chunks = self._build_chunks(item)
            self._embed_chunks(chunks)
            self._upsert_content_item(item)
            self._upsert_chunks(chunks)
            self._upsert_edges(item.edges)

    def _unchanged(self, item: ContentItem) -> bool:
        # TODO Phase 1: SELECT source_sha FROM content_items WHERE id=:id;
        # return existing == item.metadata['source_sha']
        return False

    def _validate(self, item: ContentItem) -> None:
        """Enforce ADR-008 + spec §10 invariants."""
        required = {"persona_visibility", "owner", "classification",
                    "source_sha", "parser_version", "schema_version"}
        missing = required - set(item.metadata.keys())
        if missing:
            raise ValueError(f"ContentItem {item.id} missing metadata: {missing}")

    def _build_chunks(self, item: ContentItem) -> list[Chunk]:
        """Split body into ~512-token chunks. Inherits ContentItem metadata."""
        # TODO Phase 1: use Chunker; preserve heading_path; assign sha-based ids.
        return self.chunker.chunk(item.id, item.body, item.metadata)

    def _embed_chunks(self, chunks: list[Chunk]) -> None:
        """Generate embeddings via OpenAI; mutates chunks in place. Batched."""
        # TODO Phase 1:
        # for batch in batched(chunks, n=32):
        #     vectors = self.llm.embed(model="text-embedding-3-large",
        #                              input=[c.text for c in batch])
        #     for c, v in zip(batch, vectors):
        #         c.embedding = v
        raise NotImplementedError("STORY-005 wk2")

    def _upsert_content_item(self, item: ContentItem) -> None:
        """MERGE into content_items."""
        # TODO Phase 1: build MERGE statement with USING (SELECT ... FROM dual);
        # uses bind variables for everything; JSON columns serialized via json.dumps.
        # See kb_incidents.sql for column list.
        raise NotImplementedError("STORY-005 wk2")

    def _upsert_chunks(self, chunks: list[Chunk]) -> None:
        """Bulk MERGE into chunks. Embeddings as VECTOR via TO_VECTOR()."""
        # TODO Phase 1: use executemany; VECTOR bind via array binding;
        # ensure dim == EMBEDDING_DIM before insert (fail loud on mismatch).
        raise NotImplementedError("STORY-005 wk2")

    def _upsert_edges(self, edges: list[Edge]) -> None:
        """MERGE into edges. PK is (src, dst, rel)."""
        raise NotImplementedError("STORY-005 wk2")

    # ------------------------------------------------------------------
    # Read path (used by vector_search retriever)
    # ------------------------------------------------------------------
    def query(self, q: Query) -> list[Result]:
        """Vector kNN with metadata filters."""
        if q.kind == "vector_knn":
            return self._vector_knn(q)
        if q.kind == "incident_summary":
            return self._incident_summary(q)
        raise ValueError(f"unsupported query kind: {q.kind}")

    def _vector_knn(self, q: Query) -> list[Result]:
        """SELECT chunks ORDER BY VECTOR_DISTANCE; apply metadata filters."""
        # TODO Phase 1:
        # query_vec = self.llm.embed(model="text-embedding-3-large",
        #                            input=[q.payload["query"]])[0]
        # filters = self._build_where_clause(q.payload.get("filters", {}))
        # SQL = f"""
        #   SELECT c.id, c.content_id, c.text,
        #          VECTOR_DISTANCE(c.embedding, :qv, COSINE) AS dist,
        #          ci.title, ci.metadata_extra, ci.source, ci.source_id
        #   FROM   chunks c JOIN content_items ci ON c.content_id = ci.id
        #   {filters}
        #   ORDER BY dist
        #   FETCH APPROX FIRST :k ROWS ONLY
        # """
        # rows = exec(SQL, qv=query_vec, k=q.limit, **filter_binds)
        # return [Result(...citation_url=jira_link(ci.source_id)) for r in rows]
        raise NotImplementedError("STORY-008 wk3")

    def _incident_summary(self, q: Query) -> list[Result]:
        """Direct lookup by incident_id."""
        # TODO Phase 1:
        # SELECT id, title, body, metadata_extra
        #   FROM content_items WHERE source='jira' AND source_id = :iid
        raise NotImplementedError("STORY-008 wk3")

    def _build_where_clause(self, filters: dict) -> str:
        """Build WHERE clause from ADR-008 filters: functional_area, resources, services, kind, time_window."""
        # TODO Phase 1: emit JSON_EXISTS / JSON_VALUE predicates per dim.
        raise NotImplementedError("STORY-008 wk3")

    # ------------------------------------------------------------------
    # Delete (lifecycle: superseded → deleted, never silent drop)
    # ------------------------------------------------------------------
    def delete(self, ids: list[str]) -> None:
        # TODO Phase 1: Tag rows superseded=1 (or hard delete after retention window).
        raise NotImplementedError("STORY-005 wk2")
