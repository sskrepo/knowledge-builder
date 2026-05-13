"""IncidentVectorStore — Oracle 23ai vector store for the incident KB.

Per ADR-002 §kb_incidents, ADR-003 (Store Protocol), ADR-008 (multi-axis),
ADR-012 (in-DB embedding via DBMS_VECTOR), ADR-013 (filter strictness).

External deps it cannot run without:
  - Oracle 23ai Autonomous Database (kb_incidents schema migrated)
  - OCIGenAI credential `OCI_VECTOR_CRED` set up in DB (see ADR-012)
  - oracledb Python driver

Bodies for incident KB live in this DB (not git — these aren't curated wikis).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.content import ContentItem, Chunk, Edge
from ..core.llm import LLMClient
from ..core.interfaces import Query, Result
from ..parsers.chunker import Chunker
from ._base import BaseStore

log = logging.getLogger(__name__)

EMBEDDING_DIM = 3072
DDL_PATH = Path(__file__).parent / "sql" / "kb_incidents.sql"


class IncidentVectorStore(BaseStore):
    """Vector + edges store for the operational incident KB."""

    kind = "vector"
    schema_name = "kb_incidents"

    def __init__(
        self,
        adb_pool,
        llm: LLMClient | None = None,
        chunker: Chunker | None = None,
        jira_base_url: str = "",
        confluence_base_url: str = "",
    ):
        self.pool = adb_pool
        self.llm = llm  # None valid for migration-only; embed/search will raise if None
        self.chunker = chunker or Chunker(target_tokens=512, overlap_tokens=64)
        self.jira_base_url = jira_base_url.rstrip("/")
        self.confluence_base_url = confluence_base_url.rstrip("/")

    # ---- Migration ---------------------------------------------------
    def migrate(self) -> None:
        """Run kb_incidents.sql against the configured schema. Idempotent."""
        if self.pool is None:
            log.warning("no adb_pool; migrate is a no-op")
            return
        sql = DDL_PATH.read_text()
        with self.pool.acquire() as conn:
            with conn.cursor() as cur:
                for stmt in self._split_sql(sql):
                    if not stmt.strip():
                        continue
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        # Idempotency: ORA-955 ("name already used by existing object")
                        # is OK for CREATE TABLE; real errors propagate.
                        msg = str(e)
                        if "ORA-00955" in msg or "ORA-01408" in msg:
                            log.debug("migrate: ignored existing-object %s", msg.split("\n")[0])
                        else:
                            log.error("migrate: failing stmt: %s\n%s",
                                      stmt[:200], msg)
                            raise
            conn.commit()

    @staticmethod
    def _split_sql(sql: str) -> list[str]:
        """Split DDL on `;` while respecting PL/SQL blocks."""
        out: list[str] = []
        buf: list[str] = []
        in_plsql = False
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            if stripped.upper().startswith("BEGIN") or stripped.upper().startswith("CREATE OR REPLACE PROCEDURE"):
                in_plsql = True
            buf.append(line)
            if in_plsql and stripped == "/":
                out.append("\n".join(buf[:-1]))
                buf = []
                in_plsql = False
            elif not in_plsql and stripped.endswith(";"):
                out.append("\n".join(buf).rstrip(";").strip())
                buf = []
        if buf:
            tail = "\n".join(buf).strip()
            if tail:
                out.append(tail)
        return [s for s in out if s.strip()]

    # ---- Write path --------------------------------------------------
    def upsert(self, items: list[ContentItem]) -> None:
        """Idempotent upsert. After bulk insert, fires the embedding proc (ADR-012)."""
        if self.pool is None:
            log.warning("no adb_pool; upsert is a no-op")
            return
        for item in items:
            if self._unchanged(item):
                log.debug("skip unchanged id=%s", item.id)
                continue
            item.validate()
            chunks = self._build_chunks(item)
            with self.pool.acquire() as conn:
                with conn.cursor() as cur:
                    self._upsert_content_item(cur, item)
                    self._upsert_chunks(cur, chunks)
                    self._upsert_edges(cur, item.edges)
                conn.commit()
        # ADR-012: in-DB embedding proc fills NULL embeddings
        self._call_embed_proc()

    def _unchanged(self, item: ContentItem) -> bool:
        with self.pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_sha FROM content_items WHERE id = :id",
                    id=item.id,
                )
                row = cur.fetchone()
                return bool(row and row[0] == item.metadata.source_sha)

    def _build_chunks(self, item: ContentItem) -> list[Chunk]:
        return self.chunker.chunk(item.id, item.body, item.metadata.to_dict())

    def _upsert_content_item(self, cur, item: ContentItem) -> None:
        m = item.metadata
        sql = """
        MERGE INTO content_items tgt
        USING (SELECT :id id FROM dual) src ON (tgt.id = src.id)
        WHEN MATCHED THEN UPDATE SET
            source = :source, source_id = :source_id, path = :path,
            title = :title, body = :body,
            persona = :persona,
            primary_axis_kind = :pak, primary_axis_value = :pav,
            functional_area_all = :fa_all, resources = :resources,
            services = :services, kind = :kind,
            persona_visibility = :pvis, owner = :owner,
            classification = :classification,
            source_sha = :source_sha, parser_version = :pver,
            schema_version = :sver, updated_at = SYSTIMESTAMP,
            extracted_by = :extracted_by, extraction_schema = :extraction_schema,
            metadata_drift = :drift, metadata_extra = :extra
        WHEN NOT MATCHED THEN INSERT (
            id, source, source_id, path, title, body,
            persona, primary_axis_kind, primary_axis_value,
            functional_area_all, resources, services, kind,
            persona_visibility, owner, classification,
            source_sha, parser_version, schema_version,
            created_at, updated_at,
            extracted_by, extraction_schema, metadata_drift, metadata_extra
        ) VALUES (
            :id, :source, :source_id, :path, :title, :body,
            :persona, :pak, :pav,
            :fa_all, :resources, :services, :kind,
            :pvis, :owner, :classification,
            :source_sha, :pver, :sver,
            SYSTIMESTAMP, SYSTIMESTAMP,
            :extracted_by, :extraction_schema, :drift, :extra
        )
        """
        cur.execute(sql, {
            "id": item.id,
            "source": item.source, "source_id": item.source_id,
            "path": item.path, "title": item.title, "body": item.body,
            "persona": item.persona,
            "pak": item.primary_axis_kind, "pav": item.primary_axis_value,
            "fa_all": json.dumps(item.functional_area_all),
            "resources": json.dumps(item.resources),
            "services": json.dumps(item.services),
            "kind": item.kind,
            "pvis": json.dumps(m.persona_visibility),
            "owner": m.owner, "classification": m.classification,
            "source_sha": m.source_sha, "pver": m.parser_version,
            "sver": m.schema_version,
            "extracted_by": m.extracted_by,
            "extraction_schema": m.extraction_schema,
            "drift": 1 if m.metadata_drift else 0,
            "extra": json.dumps(m.extra, default=str),
        })

    def _upsert_chunks(self, cur, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        sql = """
        MERGE INTO chunks tgt
        USING (SELECT :id id FROM dual) src ON (tgt.id = src.id)
        WHEN MATCHED THEN UPDATE SET
            text = :text, heading_path = :hp, metadata = :metadata,
            source_sha = :source_sha, parser_version = :pver,
            schema_version = :sver,
            embedding = NULL  -- force re-embed when text changes
        WHEN NOT MATCHED THEN INSERT (
            id, content_id, ord, text, heading_path, metadata,
            source_sha, parser_version, schema_version, created_at
        ) VALUES (
            :id, :content_id, :ord, :text, :hp, :metadata,
            :source_sha, :pver, :sver, SYSTIMESTAMP
        )
        """
        rows = [{
            "id": c.id, "content_id": c.content_id, "ord": c.ord,
            "text": c.text, "hp": json.dumps(c.heading_path),
            "metadata": json.dumps(c.metadata, default=str),
            "source_sha": c.metadata.get("source_sha", ""),
            "pver": c.metadata.get("parser_version", ""),
            "sver": c.metadata.get("schema_version", 1),
        } for c in chunks]
        cur.executemany(sql, rows)

    def _upsert_edges(self, cur, edges: list[Edge]) -> None:
        if not edges:
            return
        sql = """
        MERGE INTO edges tgt
        USING (SELECT :src src, :dst dst, :rel rel FROM dual) src
        ON (tgt.src = src.src AND tgt.dst = src.dst AND tgt.rel = src.rel)
        WHEN MATCHED THEN UPDATE SET metadata = :metadata
        WHEN NOT MATCHED THEN INSERT (src, dst, rel, metadata, created_at)
            VALUES (:src, :dst, :rel, :metadata, SYSTIMESTAMP)
        """
        rows = [{
            "src": e.src, "dst": e.dst, "rel": e.rel,
            "metadata": json.dumps(e.metadata, default=str),
        } for e in edges]
        cur.executemany(sql, rows)

    def _call_embed_proc(self) -> None:
        """ADR-012: trigger DBMS_VECTOR-based in-DB embedding for NULL rows."""
        with self.pool.acquire() as conn:
            with conn.cursor() as cur:
                try:
                    cur.callproc("batch_insert_datasets_vectors_kbi")
                except Exception as e:
                    # Procedure may not exist if migrate hasn't run yet
                    log.warning("embed proc call failed: %s", e)

    def delete(self, ids: list[str]) -> None:
        if not ids or self.pool is None:
            return
        with self.pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "DELETE FROM chunks WHERE content_id = :id",
                    [{"id": i} for i in ids],
                )
                cur.executemany(
                    "DELETE FROM content_items WHERE id = :id",
                    [{"id": i} for i in ids],
                )
            conn.commit()

    # ---- Read path ---------------------------------------------------
    def query(self, q: Query) -> list[Result]:
        if q.kind == "vector_knn":
            return self._vector_knn(q)
        if q.kind == "incident_summary":
            return self._incident_summary(q)
        raise ValueError(f"unsupported query kind: {q.kind}")

    def _vector_knn(self, q: Query) -> list[Result]:
        """Vector kNN with metadata filters (per ADR-013) + recency tiebreaker."""
        if self.pool is None:
            return []
        query_text = q.payload["query"]
        filters = q.payload.get("filters", [])  # list of RetrievalFilter dicts
        k = max(q.limit, 1)
        # AIRA-pattern: fetch 2x to allow app-side dedup/cap
        fetch_k = min(k * 2, 50)

        # 1. embed query (app-side per ADR-012 query-time)
        query_vec = self.llm.embed("text-embedding-3-large", [query_text])[0]

        # 2. build WHERE + score factor per ADR-013
        where_sql, score_factor_sql, binds = self._build_where_and_score(filters)

        # 3. recency expression — AIRA-pattern tiebreaker
        recency_expr = (
            "GREATEST(SYSDATE - "
            "COALESCE(CAST(JSON_VALUE(metadata_extra, '$.raw_metadata.created_at') AS DATE), "
            "         CAST(updated_at AS DATE)), 0)"
        )

        sql = f"""
        SELECT
            c.id              AS chunk_id,
            c.content_id      AS content_id,
            c.text            AS text,
            ci.title          AS title,
            ci.source         AS source,
            ci.source_id      AS source_id,
            ci.metadata_extra AS metadata,
            (1.0 / (1.0 + VECTOR_DISTANCE(c.embedding, :qv, COSINE))) * ({score_factor_sql}) AS score,
            {recency_expr}    AS age_days
        FROM chunks c
        JOIN content_items ci ON c.content_id = ci.id
        WHERE {where_sql}
          AND c.embedding IS NOT NULL
        ORDER BY score DESC, NVL(age_days, 3650) ASC
        FETCH FIRST :limit ROWS ONLY
        """
        binds["qv"] = query_vec
        binds["limit"] = fetch_k

        with self.pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, binds)
                rows = cur.fetchall()

        results: list[Result] = []
        for r in rows[:k]:
            chunk_id_, content_id_, text, title, source, source_id, meta_json, score, age = r
            results.append(Result(
                content_id=content_id_,
                chunk_id=chunk_id_,
                text=text,
                score=float(score),
                citation_url=self._citation_url(source, source_id),
                metadata={
                    "title": title,
                    "source": source,
                    "age_days": float(age) if age else None,
                    "raw": json.loads(meta_json) if meta_json else {},
                },
            ))
        return results

    def _incident_summary(self, q: Query) -> list[Result]:
        if self.pool is None:
            return []
        incident_id = q.payload["incident_id"]
        with self.pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_id, title, body, source, metadata_extra "
                    "FROM content_items WHERE source = 'jira' AND source_id = :iid",
                    iid=incident_id,
                )
                row = cur.fetchone()
                if not row:
                    return []
                cid, sid, title, body, source, meta = row
                return [Result(
                    content_id=cid, chunk_id=None,
                    text=body, score=1.0,
                    citation_url=self._citation_url(source, sid),
                    metadata={"title": title, "raw": json.loads(meta) if meta else {}},
                )]

    def _build_where_and_score(self, filters: list[dict]) -> tuple[str, str, dict]:
        """Per ADR-013."""
        where_clauses: list[str] = []
        score_factors: list[str] = ["1.00"]
        binds: dict[str, Any] = {}
        for i, f in enumerate(filters):
            field = f.get("field")
            values = f.get("values") or []
            strictness = f.get("strictness", "hard")
            if not field or not values or strictness == "off":
                continue
            placeholders = []
            for j, v in enumerate(values):
                key = f"{field}_v{i}_{j}"
                binds[key] = v
                placeholders.append(f":{key}")
            placeholders_sql = ", ".join(placeholders)

            # JSON-array fields use JSON_EXISTS; scalar fields use IN
            if field in {"functional_area_all", "resources", "services", "persona_visibility"}:
                json_path = ", ".join([f'$ ? (@ == "{v}")' for v in values])  # built differently
                # Rebuild safely:
                json_clauses = " OR ".join([
                    f"JSON_EXISTS(ci.{field}, '$[*] ? (@ == \"{v}\")')"
                    for v in values
                ])
                clause = f"({json_clauses})"
            else:
                col = f"ci.{field}"
                clause = f"{col} IN ({placeholders_sql})"

            if strictness == "hard":
                where_clauses.append(clause)
            elif strictness == "soft":
                mult = float(f.get("soft_multiplier", 0.90))
                score_factors.append(
                    f"CASE WHEN {clause} THEN 1.00 ELSE {mult:.2f} END"
                )

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        score_factor_sql = " * ".join(score_factors)
        return where_sql, score_factor_sql, binds

    def _citation_url(self, source: str, source_id: str) -> str:
        """Build a resolvable citation URL.

        When base URLs are configured (non-empty), produce real HTTPS links.
        Falls back to scheme://id in dev/filestore mode where no base URL is known.
        """
        if source == "jira" and self.jira_base_url:
            return f"{self.jira_base_url}/browse/{source_id}"
        if source == "confluence" and self.confluence_base_url:
            return f"{self.confluence_base_url}/pages/{source_id}"
        # Fallback for dev / filestore mode
        return f"{source}://{source_id}"
