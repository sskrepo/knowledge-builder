"""WikiMetadataStore — wiki/page KB metadata store.

Two implementations:
  WikiMetadataStore       — filestore-backed (laptop/no-ADB fallback ONLY).
  AdbWikiMetadataStore    — Oracle ADB-backed (DECISION-022; required for all
                            paths that serve promoted/consumable skills).

Factory: build_wiki_store(pool, env) selects the correct implementation and logs
the selection. Selection is NEVER silent — filestore fallback is explicitly logged
at WARNING so operators know portability is compromised.

DECISION-022 / ADR-023 (ADB-always for promoted artefacts):
  Wiki page *body content* + metadata is stored in KB_SHIM.KBF_WIKI_PAGES in the
  ADB-backed path.  The filestore path (~/.kbf/store/wiki_metadata/) is retained
  ONLY as an explicit laptop/no-ADB fallback — NOT the path for any ingest that
  could feed a PROMOTED skill.

canonical_ref contract (DECISION-020 §3 / ADR-039):
  ingest_page() stamps canonical_ref into the store record.
  search_wiki / read_wiki_page retrievers return it in passage metadata.
  executor _passage_matches_canonical() compares canonical==canonical from store.
  This round-trip must hold end-to-end: filestore OR ADB, both implementations
  preserve canonical_ref in the record dict they return.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import oracledb
    _ORACLEDB_AVAILABLE = True
except ImportError:
    _ORACLEDB_AVAILABLE = False

log = logging.getLogger(__name__)

_DEFAULT_ROOT = Path.home() / ".kbf" / "store" / "wiki_metadata"

# ---------------------------------------------------------------------------
# SQL templates — AdbWikiMetadataStore
# ---------------------------------------------------------------------------

_DDL_CREATE_WIKI_PAGES = """
    CREATE TABLE KB_SHIM.KBF_WIKI_PAGES (
        page_id           VARCHAR2(512)    NOT NULL,
        canonical_ref     CLOB,
        title             VARCHAR2(1000),
        space             VARCHAR2(200),
        persona           VARCHAR2(200),
        kb_scope          VARCHAR2(200),
        content           CLOB,
        content_hash      VARCHAR2(64),
        citation_url      VARCHAR2(2000),
        source_url        VARCHAR2(2000),
        tags              CLOB,
        last_modified     VARCHAR2(200),
        ingested_at       TIMESTAMP WITH TIME ZONE,
        extraction_version VARCHAR2(200),
        schema_version    NUMBER DEFAULT 1,
        CONSTRAINT pk_kbf_wiki_pages PRIMARY KEY (page_id)
    )
"""

_SQL_UPSERT_PAGE = """
    MERGE INTO KB_SHIM.KBF_WIKI_PAGES tgt
    USING DUAL ON (tgt.page_id = :page_id)
    WHEN MATCHED THEN UPDATE SET
        canonical_ref      = :canonical_ref,
        title              = :title,
        space              = :space,
        persona            = :persona,
        kb_scope           = :kb_scope,
        content            = :content,
        content_hash       = :content_hash,
        citation_url       = :citation_url,
        source_url         = :source_url,
        tags               = :tags,
        last_modified      = :last_modified,
        ingested_at        = :ingested_at,
        extraction_version = :extraction_version,
        schema_version     = :schema_version
    WHEN NOT MATCHED THEN INSERT
        (page_id, canonical_ref, title, space, persona, kb_scope, content,
         content_hash, citation_url, source_url, tags, last_modified,
         ingested_at, extraction_version, schema_version)
    VALUES
        (:page_id, :canonical_ref, :title, :space, :persona, :kb_scope, :content,
         :content_hash, :citation_url, :source_url, :tags, :last_modified,
         :ingested_at, :extraction_version, :schema_version)
"""

_SQL_GET_PAGE = """
    SELECT page_id, canonical_ref, title, space, persona, kb_scope, content,
           content_hash, citation_url, source_url, tags, last_modified,
           ingested_at, extraction_version, schema_version
    FROM KB_SHIM.KBF_WIKI_PAGES
    WHERE page_id = :page_id
"""

_SQL_LIST_PAGES = """
    SELECT page_id, canonical_ref, title, space, persona, kb_scope, content,
           content_hash, citation_url, source_url, tags, last_modified,
           ingested_at, extraction_version, schema_version
    FROM KB_SHIM.KBF_WIKI_PAGES
    ORDER BY ingested_at DESC
"""

_SQL_LIST_PAGES_PERSONA = """
    SELECT page_id, canonical_ref, title, space, persona, kb_scope, content,
           content_hash, citation_url, source_url, tags, last_modified,
           ingested_at, extraction_version, schema_version
    FROM KB_SHIM.KBF_WIKI_PAGES
    WHERE persona = :persona
    ORDER BY ingested_at DESC
"""

_SQL_DELETE_PAGE = """
    DELETE FROM KB_SHIM.KBF_WIKI_PAGES WHERE page_id = :page_id
"""

_SQL_CHECK_CONTENT_HASH = """
    SELECT content_hash FROM KB_SHIM.KBF_WIKI_PAGES WHERE page_id = :page_id
"""


def _run_sql_ddl(pool, ddl: str) -> bool:
    """Execute a DDL statement idempotently.

    Returns True if the DDL ran, False if the table already existed (ORA-00955).
    Raises on other errors.
    """
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(ddl)
                conn.commit()
                return True
            except Exception as exc:
                # ORA-00955 = table/view/sequence already exists
                if "ORA-00955" in str(exc):
                    return False
                raise


# ---------------------------------------------------------------------------
# Filestore implementation (laptop/no-ADB fallback ONLY — DECISION-022)
# ---------------------------------------------------------------------------

class WikiMetadataStore:
    """Filestore-backed wiki metadata store.

    DECISION-022: THIS IS THE FALLBACK PATH ONLY.
    Use AdbWikiMetadataStore for all paths that serve promoted/consumable skills.
    Filestore is laptop-local and NOT portable across hosts.

    Retains its existing interface for backward compatibility with existing
    unit tests and the explicit no-ADB laptop-init path.
    """

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else _DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert_page(self, page_meta: dict) -> str:
        """Insert or update a wiki page metadata record.

        Required keys: title, path.
        Optional: page_id, persona, tags, last_modified, content_hash,
                  extraction_version, canonical_ref.

        Returns the page_id (generated from path if not provided).
        """
        page_id = page_meta.get("page_id") or _derive_page_id(page_meta.get("path", ""))
        record: dict[str, Any] = {
            "page_id": page_id,
            "title": page_meta.get("title", ""),
            "path": page_meta.get("path", ""),
            "persona": page_meta.get("persona"),
            "tags": page_meta.get("tags") or [],
            "last_modified": page_meta.get("last_modified") or datetime.utcnow().isoformat() + "Z",
            "content_hash": page_meta.get("content_hash"),
            "extraction_version": page_meta.get("extraction_version"),
        }
        # ADR-039 (DECISION-020) write-side: preserve canonical_ref when stamped
        # by the ingestor so the search_wiki / read_wiki_page retrievers can return
        # it in passage metadata for executor canonical==canonical matching.
        if page_meta.get("canonical_ref") is not None:
            record["canonical_ref"] = page_meta["canonical_ref"]
        # page_id may contain characters illegal in filenames (slashes from a
        # URL, colons, '+' etc.). Sanitise to a filesystem-safe stem for the
        # JSON file while preserving the original page_id inside the record.
        # Without this, URL-form page_ids crashed at write_text(...) with
        # FileNotFoundError because '/' was treated as a directory separator.
        safe_stem = re.sub(r"[^\w.-]", "_", page_id) or "_unnamed"
        dest = self.root / f"{safe_stem}.json"
        dest.write_text(json.dumps(record, indent=2, default=str))
        log.debug("wiki_metadata upsert: page_id=%s stem=%s", page_id, safe_stem)
        return page_id

    def delete_page(self, page_id: str) -> bool:
        """Delete a page metadata record. Returns True if it existed."""
        safe_stem = re.sub(r"[^\w.-]", "_", page_id) or "_unnamed"
        path = self.root / f"{safe_stem}.json"
        if path.exists():
            path.unlink()
            log.debug("wiki_metadata delete: %s", page_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_page(self, page_id: str) -> dict | None:
        """Return metadata record for page_id, or None if not found."""
        safe_stem = re.sub(r"[^\w.-]", "_", page_id) or "_unnamed"
        path = self.root / f"{safe_stem}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def list_pages(
        self,
        persona: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        """Return all page records, optionally filtered by persona and/or tags.

        Tag filtering: all provided tags must be present in the record's tags list.
        """
        results: list[dict] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(p.read_text())
            except Exception as e:
                log.warning("wiki_metadata: could not read %s: %s", p, e)
                continue
            if persona is not None and record.get("persona") != persona:
                continue
            if tags:
                record_tags = set(record.get("tags") or [])
                if not all(t in record_tags for t in tags):
                    continue
            results.append(record)
        return results

    def search_pages(self, query: str) -> list[dict]:
        """Lexical search over title and tags. Returns ranked results."""
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        scored: list[tuple[float, dict]] = []
        for record in self.list_pages():
            candidate_tokens = set(
                _tokenize(record.get("title", ""))
                + _tokenize(" ".join(record.get("tags") or []))
            )
            if not candidate_tokens:
                continue
            overlap = len(query_tokens & candidate_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(query_tokens | candidate_tokens), 1)
            scored.append((score, record))

        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored]


# ---------------------------------------------------------------------------
# ADB-backed implementation (DECISION-022 — required for promoted skills)
# ---------------------------------------------------------------------------

class AdbWikiMetadataStore:
    """Oracle ADB-backed wiki/page KB store (DECISION-022).

    Stores wiki page metadata + full markdown content in KB_SHIM.KBF_WIKI_PAGES.
    Exposes the same interface as WikiMetadataStore so all callers are transparent
    to the backing store.

    KEY DIFFERENCES from WikiMetadataStore:
    - Content (full markdown) is stored in the ADB `content` CLOB column.
      search_wiki / read_wiki_page retrievers return it directly — no filesystem
      path lookup needed on the consuming host.
    - canonical_ref is stored as a JSON CLOB (DECISION-020 §3).
    - Idempotent by content_hash: if page_id+content_hash both match, upsert is
      a no-op (returns page_id without a DB write).
    - Pool REQUIRED. No silent fallback. Passing pool=None raises ValueError.

    CLOB columns: content, canonical_ref, tags — all bound via setinputsizes to
    avoid ORA-03146 (same pattern as AdbErrorStore / AdbSkillStore).
    """

    def __init__(self, pool) -> None:
        if pool is None:
            raise ValueError(
                "AdbWikiMetadataStore: pool is required. ADB is the source of truth "
                "for promoted-skill wiki pages (DECISION-022 / ADR-023). "
                "There is no stub-mode / no-op fallback. "
                "Use build_wiki_store(pool=None) for the explicit filestore fallback "
                "when ADB is genuinely unavailable (logs a WARNING)."
            )
        self._pool = pool
        self._ensure_table()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=timezone.utc)

    @staticmethod
    def _install_dict_rowfactory(cur) -> None:
        cols = [d[0].lower() for d in cur.description]
        cur.rowfactory = lambda *vals: dict(zip(cols, vals))

    @staticmethod
    def _read_lob(val) -> str | None:
        """Materialise an oracledb LOB object to str, or return val unchanged."""
        if val is None:
            return None
        if hasattr(val, "read"):
            return val.read()
        return val

    def _ensure_table(self) -> None:
        """Create KB_SHIM.KBF_WIKI_PAGES if it does not exist (idempotent DDL)."""
        created = _run_sql_ddl(self._pool, _DDL_CREATE_WIKI_PAGES)
        if created:
            log.info(
                "AdbWikiMetadataStore: created KB_SHIM.KBF_WIKI_PAGES "
                "(DECISION-022 ADB-backed wiki store)"
            )
        else:
            log.debug("AdbWikiMetadataStore: KB_SHIM.KBF_WIKI_PAGES already exists")

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert_page(self, page_meta: dict) -> str:
        """Insert or update a wiki page record in KB_SHIM.KBF_WIKI_PAGES.

        Idempotent: if page_id + content_hash both match the existing row,
        returns page_id without a DB write.

        Required keys: title (or page_id used as title).
        Optional: page_id, persona, tags, last_modified, content_hash,
                  extraction_version, canonical_ref, content (markdown body),
                  citation_url, source_url, space, kb_scope.

        Returns the page_id.
        """
        page_id = page_meta.get("page_id") or _derive_page_id(page_meta.get("path", ""))
        content_hash = page_meta.get("content_hash") or ""

        # Idempotency fast-path: check existing hash before writing.
        if content_hash:
            try:
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        cur.execute(_SQL_CHECK_CONTENT_HASH, {"page_id": page_id})
                        row = cur.fetchone()
                if row is not None:
                    existing_hash = row[0]
                    if hasattr(existing_hash, "read"):
                        existing_hash = existing_hash.read()
                    if existing_hash == content_hash:
                        log.debug(
                            "AdbWikiMetadataStore.upsert_page: no-op "
                            "(page_id=%s content_hash unchanged)", page_id,
                        )
                        return page_id
            except Exception as exc:
                log.warning(
                    "AdbWikiMetadataStore.upsert_page: hash-check failed (%s) "
                    "— proceeding with full upsert", exc,
                )

        # Prepare CLOB-capable params.
        canonical_ref = page_meta.get("canonical_ref")
        canonical_ref_json = (
            json.dumps(canonical_ref) if isinstance(canonical_ref, dict) else canonical_ref
        )

        tags = page_meta.get("tags") or []
        tags_json = json.dumps(tags) if isinstance(tags, list) else (tags or "[]")

        # content: full markdown body (stored so consuming hosts don't need filesystem).
        # page_meta["content"] is set by the ADB ingest path.
        # For records upserted without content (metadata-only), we store empty string.
        content = page_meta.get("content", "") or ""

        params = {
            "page_id":            page_id,
            "canonical_ref":      canonical_ref_json,
            "title":              page_meta.get("title", "") or page_id,
            "space":              page_meta.get("space", ""),
            "persona":            page_meta.get("persona", ""),
            "kb_scope":           page_meta.get("kb_scope", "") or page_meta.get("persona", ""),
            "content":            content,
            "content_hash":       content_hash,
            "citation_url":       page_meta.get("citation_url", "") or page_meta.get("source_url", ""),
            "source_url":         page_meta.get("source_url", ""),
            "tags":               tags_json,
            "last_modified":      page_meta.get("last_modified", "") or "",
            "ingested_at":        self._now(),
            "extraction_version": page_meta.get("extraction_version", "") or "",
            "schema_version":     int(page_meta.get("schema_version", 1) or 1),
        }

        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    if _ORACLEDB_AVAILABLE:
                        cur.setinputsizes(
                            canonical_ref=oracledb.DB_TYPE_CLOB,
                            content=oracledb.DB_TYPE_CLOB,
                            tags=oracledb.DB_TYPE_CLOB,
                        )
                    cur.execute(_SQL_UPSERT_PAGE, params)
                conn.commit()
        except Exception as exc:
            log.error(
                "AdbWikiMetadataStore.upsert_page: failed for page_id=%s: %s",
                page_id, exc,
            )
            raise

        log.info(
            "AdbWikiMetadataStore.upsert_page: page_id=%s persona=%s space=%s",
            page_id, params["persona"], params["space"],
        )
        return page_id

    def delete_page(self, page_id: str) -> bool:
        """Delete a page record. Returns True if a row was deleted."""
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_DELETE_PAGE, {"page_id": page_id})
                deleted = cur.rowcount > 0
            conn.commit()
        log.debug("AdbWikiMetadataStore.delete_page: page_id=%s deleted=%s", page_id, deleted)
        return deleted

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _row_to_record(self, row: dict) -> dict:
        """Convert an ADB row dict to the standard page record shape."""
        # Materialise LOB objects.
        canonical_ref_raw = self._read_lob(row.get("canonical_ref"))
        content_raw = self._read_lob(row.get("content"))
        tags_raw = self._read_lob(row.get("tags"))

        # Parse JSON fields.
        canonical_ref: dict | None = None
        if canonical_ref_raw:
            try:
                canonical_ref = json.loads(canonical_ref_raw)
            except Exception:
                canonical_ref = None

        tags: list = []
        if tags_raw:
            try:
                tags = json.loads(tags_raw)
            except Exception:
                tags = []

        record: dict = {
            "page_id":            row.get("page_id", ""),
            "title":              row.get("title", "") or "",
            "space":              row.get("space", "") or "",
            "persona":            row.get("persona", "") or "",
            "kb_scope":           row.get("kb_scope", "") or "",
            "content":            content_raw or "",
            "content_hash":       row.get("content_hash", "") or "",
            "citation_url":       row.get("citation_url", "") or "",
            "source_url":         row.get("source_url", "") or "",
            "tags":               tags,
            "last_modified":      str(row.get("last_modified", "") or ""),
            "ingested_at":        str(row.get("ingested_at", "") or ""),
            "extraction_version": row.get("extraction_version", "") or "",
            "schema_version":     row.get("schema_version") or 1,
            # path is not applicable in ADB-backed mode (no filesystem path on consuming host).
            # Callers should use the `content` field directly.  Set to "" to avoid
            # KeyError in callers that access record["path"] without checking.
            "path":               "",
        }
        if canonical_ref is not None:
            record["canonical_ref"] = canonical_ref
        return record

    def get_page(self, page_id: str) -> dict | None:
        """Return page record for page_id, or None if not found."""
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_GET_PAGE, {"page_id": page_id})
                self._install_dict_rowfactory(cur)
                row = cur.fetchone()

        if row is None:
            return None
        return self._row_to_record(row)

    def list_pages(
        self,
        persona: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        """Return all page records, optionally filtered by persona.

        Note: tag filtering is applied in Python (not SQL) for simplicity;
        ADB stores tags as a JSON CLOB.
        """
        sql = _SQL_LIST_PAGES_PERSONA if persona else _SQL_LIST_PAGES
        params: dict = {"persona": persona} if persona else {}

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                self._install_dict_rowfactory(cur)
                rows = cur.fetchall()

        results: list[dict] = []
        for row in rows:
            record = self._row_to_record(row)
            if tags:
                record_tags = set(record.get("tags") or [])
                if not all(t in record_tags for t in tags):
                    continue
            results.append(record)
        return results

    def search_pages(self, query: str) -> list[dict]:
        """Lexical search over title and tags. Returns ranked results.

        Uses the same Jaccard-overlap algorithm as WikiMetadataStore for
        consistency — both implementations behave identically on search.
        """
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        scored: list[tuple[float, dict]] = []
        for record in self.list_pages():
            candidate_tokens = set(
                _tokenize(record.get("title", ""))
                + _tokenize(" ".join(record.get("tags") or []))
            )
            if not candidate_tokens:
                continue
            overlap = len(query_tokens & candidate_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(query_tokens | candidate_tokens), 1)
            scored.append((score, record))

        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored]


# ---------------------------------------------------------------------------
# Factory (DECISION-022)
# ---------------------------------------------------------------------------

def build_wiki_store(pool=None, env: str = "") -> WikiMetadataStore | AdbWikiMetadataStore:
    """Return the appropriate wiki store implementation.

    DECISION-022 factory:
    - pool is not None  → AdbWikiMetadataStore(pool)  [required for promoted skills]
    - pool is None      → WikiMetadataStore() filestore [EXPLICIT FALLBACK ONLY]
      Logs at WARNING — this path is NEVER silent. Portability is compromised.

    Args:
        pool: oracledb connection pool. Pass None only when ADB is genuinely
              unavailable (unit test isolation, pure offline laptop-init).
        env:  KBF_ENV string for log context.
    """
    if pool is not None:
        store = AdbWikiMetadataStore(pool)
        log.info(
            "wiki_store: ADB-backed (KB_SHIM.KBF_WIKI_PAGES) env=%s — "
            "promoted skills will be portable across hosts (DECISION-022)",
            env or "unknown",
        )
        return store
    else:
        log.warning(
            "wiki_store: FILESTORE FALLBACK (~/.kbf/store/wiki_metadata) env=%s — "
            "NOT suitable for promoted/consumable skills (DECISION-022). "
            "ADB is required for portability. "
            "Provide an ADB pool to enable the ADB-backed wiki store.",
            env or "unknown",
        )
        return WikiMetadataStore()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _derive_page_id(path: str) -> str:
    """Generate a stable page_id from the wiki file path."""
    if not path:
        return "unknown-" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower().replace("/", "-").replace("\\", "-"))
    slug = slug.strip("-")[:80]
    suffix = hashlib.sha1(path.encode()).hexdigest()[:8]
    return f"{slug}-{suffix}"
