"""Append-only JSONL store for two error streams, plus ADB-backed variant.

  errors.jsonl     — server-side auto-captured errors (written by mcp_transport)
  user_bugs.jsonl  — user-reported bugs submitted via the reportBug MCP tool

Both files live under {store_root}/ and are created on first write.

``ErrorStore`` — pure filesystem (laptop/dev mode)
``AdbErrorStore`` — dual-write: Oracle ADB primary + local JSONL for hot reads

Usage:
    store = ErrorStore("~/.kbf/store")
    store.record_error({
        "request_id": "req-a3f2c1",
        "timestamp": "2026-05-12T10:00:00Z",
        "tool": "authorSkill",
        ...
    })
    store.record_user_bug({...})
    errors = store.read_errors()
    bugs   = store.read_user_bugs()
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    import oracledb
    _ORACLEDB_AVAILABLE = True
except ImportError:
    _ORACLEDB_AVAILABLE = False

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL templates for AdbErrorStore (Oracle syntax)
# ---------------------------------------------------------------------------

_SQL_INSERT_ERROR = """
    INSERT INTO KB_SHIM.KBF_ERROR_LOG
        (request_id, timestamp_utc, tool, error_type, message, stack_trace, extra_json)
    VALUES
        (:request_id, :timestamp_utc, :tool, :error_type, :message, :stack_trace, :extra_json)
"""

_SQL_INSERT_BUG = """
    INSERT INTO KB_SHIM.KBF_BUG_REPORTS
        (request_id, queue_id, timestamp_utc, tool, description, extra_json)
    VALUES
        (:request_id, :queue_id, :timestamp_utc, :tool, :description, :extra_json)
"""


class ErrorStore:
    """Filestore-backed append-only log for errors and user bug reports.

    Layout:
      {store_root}/errors.jsonl     — server-side error records
      {store_root}/user_bugs.jsonl  — user-reported bug records

    Thread safety: each write opens the file in append mode and writes a
    single JSON line terminated with ``\\n``.  On POSIX systems, appending a
    line shorter than PIPE_BUF (4096 bytes) to an O_APPEND file is atomic.
    For longer payloads the caller should not rely on atomicity, but no
    corruption can occur because JSON lines are self-delimiting.
    """

    def __init__(self, store_root: str | Path) -> None:
        self._root = Path(store_root)
        self._errors_path = self._root / "errors.jsonl"
        self._user_bugs_path = self._root / "user_bugs.jsonl"
        # Ensure the directory exists; files are created lazily on first write.
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def record_error(self, entry: dict) -> None:
        """Append a server-side error record to errors.jsonl.

        Args:
            entry: Dict with at minimum ``request_id``, ``timestamp``,
                   ``tool``, ``error_type``, ``message``.  Extra fields
                   are stored verbatim.
        """
        self._append(self._errors_path, entry)

    def record_user_bug(self, entry: dict) -> None:
        """Append a user-reported bug record to user_bugs.jsonl.

        Args:
            entry: Dict with at minimum ``request_id``, ``timestamp``,
                   ``tool``, ``description``, ``queue_id``.
        """
        self._append(self._user_bugs_path, entry)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def read_errors(self) -> list[dict]:
        """Return all records from errors.jsonl.

        Returns an empty list if the file does not exist yet.
        Malformed lines are skipped with a warning log.
        """
        return self._read_jsonl(self._errors_path)

    def read_user_bugs(self) -> list[dict]:
        """Return all records from user_bugs.jsonl.

        Returns an empty list if the file does not exist yet.
        Malformed lines are skipped with a warning log.
        """
        return self._read_jsonl(self._user_bugs_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append(self, path: Path, entry: dict) -> None:
        """Atomically append *entry* as a JSON line to *path*."""
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.error("error_store: failed to write to %s: %s", path.name, exc)

    def _read_jsonl(self, path: Path) -> list[dict]:
        """Read all JSON lines from *path*.  Missing file → empty list."""
        if not path.exists():
            return []
        records: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.warning(
                            "error_store: bad JSON in %s on line %d, skipping",
                            path.name, lineno,
                        )
        except OSError as exc:
            log.error("error_store: failed to read %s: %s", path.name, exc)
        return records


# ---------------------------------------------------------------------------
# ADB-backed implementation (DECISION-006 Option A)
# ---------------------------------------------------------------------------


class AdbErrorStore(ErrorStore):
    """Dual-write error store: Oracle ADB primary + local JSONL for hot reads.

    Writes to KB_SHIM.KBF_ERROR_LOG and KB_SHIM.KBF_BUG_REPORTS.
    Also appends to local JSONL files so ``kb-cli watch-bugs`` can read
    errors without needing a live ADB connection.

    Args:
        pool:       oracledb connection pool.  When None, falls back to pure
                    filesystem writes (mirrors the parent class behaviour).
        store_root: Path (or string) to the JSONL directory.
    """

    def __init__(self, pool, store_root: str | Path) -> None:
        super().__init__(store_root)
        self._pool = pool

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(tz=timezone.utc)

    def record_error(self, entry: dict) -> None:
        """Write to ADB + JSONL (dual-write).

        Falls back to JSONL-only when pool is None.
        """
        # Always write to JSONL for hot-read tools (watch-bugs)
        super().record_error(entry)

        if self._pool is None:
            return

        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str) if ts_str else self._now_utc()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = self._now_utc()

        # Separate well-known fields from the rest (stored in extra_json)
        known = {"request_id", "timestamp", "tool", "error_type", "message", "stack_trace"}
        extra = {k: v for k, v in entry.items() if k not in known}

        params = {
            "request_id":  entry.get("request_id", ""),
            "timestamp_utc": ts,
            "tool":        entry.get("tool", ""),
            "error_type":  entry.get("error_type", ""),
            "message":     entry.get("message", ""),
            "stack_trace": entry.get("stack_trace", ""),
            "extra_json":  json.dumps(extra) if extra else None,
        }

        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    # message, stack_trace, extra_json are CLOB columns.
                    # Stack traces from complex sessions can exceed the 4000-byte
                    # Oracle SQL VARCHAR2 limit (ORA-03146 without setinputsizes).
                    # Same fix as write_artifacts/content (BUG-queue-440da).
                    if _ORACLEDB_AVAILABLE:
                        cur.setinputsizes(
                            message=oracledb.DB_TYPE_CLOB,
                            stack_trace=oracledb.DB_TYPE_CLOB,
                            extra_json=oracledb.DB_TYPE_CLOB,
                        )
                    cur.execute(_SQL_INSERT_ERROR, params)
                conn.commit()
        except Exception as exc:
            log.warning("AdbErrorStore.record_error: ADB write failed: %s", exc)

    def record_user_bug(self, entry: dict) -> None:
        """Write to ADB + JSONL (dual-write).

        Falls back to JSONL-only when pool is None.
        """
        # Always write to JSONL for hot-read tools (watch-bugs)
        super().record_user_bug(entry)

        if self._pool is None:
            return

        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str) if ts_str else self._now_utc()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = self._now_utc()

        known = {"request_id", "queue_id", "timestamp", "tool", "description"}
        extra = {k: v for k, v in entry.items() if k not in known}

        params = {
            "request_id":  entry.get("request_id", ""),
            "queue_id":    entry.get("queue_id", ""),
            "timestamp_utc": ts,
            "tool":        entry.get("tool", ""),
            "description": entry.get("description", ""),
            "extra_json":  json.dumps(extra) if extra else None,
        }

        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    # description and extra_json are CLOB columns.
                    # Same fix as write_artifacts/content (BUG-queue-440da).
                    if _ORACLEDB_AVAILABLE:
                        cur.setinputsizes(
                            description=oracledb.DB_TYPE_CLOB,
                            extra_json=oracledb.DB_TYPE_CLOB,
                        )
                    cur.execute(_SQL_INSERT_BUG, params)
                conn.commit()
        except Exception as exc:
            log.warning("AdbErrorStore.record_user_bug: ADB write failed: %s", exc)
