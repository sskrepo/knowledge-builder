"""Append-only JSONL store for two error streams.

  errors.jsonl     — server-side auto-captured errors (written by mcp_transport)
  user_bugs.jsonl  — user-reported bugs submitted via the reportBug MCP tool

Both files live under {store_root}/ and are created on first write.

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
from pathlib import Path

log = logging.getLogger(__name__)


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
