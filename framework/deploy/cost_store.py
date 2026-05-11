"""Append-only token usage log for LLM cost telemetry (PDD V3 Track F).

Each call to record() appends a single JSON line to {store_root}/cost_log.jsonl.
query() reads the full log, applies optional filters, and aggregates.

Cross-cutting requirement (CLAUDE.md §10):
  - Cost telemetry: log tokens-per-ingest and tokens-per-retrieve.
  - Every content creation flows through the parser → every LLM call records cost here.

Usage:
    store = CostStore(store_root="~/.kbf/store")
    store.record(persona="ops_eng", operation="ingestion",
                 prompt_tokens=840, completion_tokens=210, skill_name="incident_summary")
    result = store.query(persona="ops_eng", start_date="2026-05-01")
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Known operation types for by_operation aggregation.
_KNOWN_OPERATIONS = ("ingestion", "retrieval", "synthesis")


class CostStore:
    """Filestore-backed append-only token usage log.

    Layout: {store_root}/cost_log.jsonl

    Each line is a JSON object with fields:
      timestamp   — ISO-8601 UTC string (YYYY-MM-DDThh:mm:ss.ffffffZ)
      persona     — persona slug (e.g. "ops_eng", "tpm")
      operation   — one of "ingestion", "retrieval", "synthesis" (open-ended)
      skill_name  — skill name (may be empty string)
      prompt      — number of prompt tokens (int)
      completion  — number of completion tokens (int)
      total       — prompt + completion (int, stored for convenience)
    """

    def __init__(self, store_root: str | Path) -> None:
        self._root = Path(store_root)
        self._log_path = self._root / "cost_log.jsonl"
        # Ensure the directory exists; do NOT create the file yet (lazy on first write).
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        persona: str,
        operation: str,
        prompt_tokens: int,
        completion_tokens: int,
        skill_name: str = "",
    ) -> None:
        """Append a token-usage entry to the log.

        Args:
            persona:           Persona slug (e.g. "ops_eng").
            operation:         Operation type ("ingestion", "retrieval", "synthesis").
            prompt_tokens:     Number of prompt/input tokens consumed.
            completion_tokens: Number of completion/output tokens consumed.
            skill_name:        Optional skill name for finer-grained tracking.
        """
        now = datetime.now(tz=timezone.utc)
        entry = {
            "timestamp": now.isoformat(),
            "persona": persona,
            "operation": operation,
            "skill_name": skill_name,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        }
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.error("cost_store: failed to write entry: %s", exc)

    def query(
        self,
        persona: str | None = None,
        skill_name: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """Aggregate token usage for the requested filters.

        Args:
            persona:    If set, restrict to entries matching this persona.
            skill_name: If set, restrict to entries matching this skill name.
            start_date: ISO-8601 date string (YYYY-MM-DD). Inclusive lower bound on timestamp.
            end_date:   ISO-8601 date string (YYYY-MM-DD). Inclusive upper bound on timestamp.

        Returns:
            dict with keys:
              period       — {"start": str, "end": str}
              total_tokens — int
              by_persona   — {persona: {"prompt": N, "completion": N, "total": N}, ...}
              by_operation — {"ingestion": N, "retrieval": N, "synthesis": N, ...}
        """
        by_persona: dict = {}
        total_tokens = 0
        by_operation: dict = {}

        if not self._log_path.exists():
            return self._empty_response(start_date, end_date)

        # Parse date boundaries once (compare against date portion of ISO timestamp).
        start_dt = _parse_date_bound(start_date)
        end_dt = _parse_date_bound(end_date, end_of_day=True)

        try:
            with open(self._log_path, "r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("cost_store: bad JSON on line %d, skipping", lineno)
                        continue

                    # --- Filter: date range ---
                    ts_str = entry.get("timestamp", "")
                    if ts_str:
                        try:
                            entry_dt = datetime.fromisoformat(ts_str)
                        except ValueError:
                            entry_dt = None
                        if entry_dt is not None:
                            if start_dt and entry_dt < start_dt:
                                continue
                            if end_dt and entry_dt > end_dt:
                                continue

                    # --- Filter: persona ---
                    entry_persona = entry.get("persona", "")
                    if persona and entry_persona != persona:
                        continue

                    # --- Filter: skill_name ---
                    entry_skill = entry.get("skill_name", "")
                    if skill_name and entry_skill != skill_name:
                        continue

                    # --- Aggregate ---
                    p = int(entry.get("prompt", 0))
                    c = int(entry.get("completion", 0))
                    t = p + c
                    total_tokens += t

                    # by_persona
                    if entry_persona not in by_persona:
                        by_persona[entry_persona] = {"prompt": 0, "completion": 0, "total": 0}
                    by_persona[entry_persona]["prompt"] += p
                    by_persona[entry_persona]["completion"] += c
                    by_persona[entry_persona]["total"] += t

                    # by_operation
                    op = entry.get("operation", "")
                    if op not in by_operation:
                        by_operation[op] = 0
                    by_operation[op] += t

        except OSError as exc:
            log.error("cost_store: failed to read log: %s", exc)
            return self._empty_response(start_date, end_date)

        return {
            "period": {
                "start": start_date or "",
                "end": end_date or "",
            },
            "total_tokens": total_tokens,
            "by_persona": by_persona,
            "by_operation": by_operation,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_response(start_date: str | None, end_date: str | None) -> dict:
        return {
            "period": {
                "start": start_date or "",
                "end": end_date or "",
            },
            "total_tokens": 0,
            "by_persona": {},
            "by_operation": {},
        }


def _parse_date_bound(date_str: str | None, end_of_day: bool = False) -> datetime | None:
    """Parse a YYYY-MM-DD date string into a timezone-aware datetime for comparison.

    Args:
        date_str:   Date string in YYYY-MM-DD format or None.
        end_of_day: If True, set time to 23:59:59.999999 (inclusive upper bound).

    Returns:
        datetime with UTC timezone, or None if date_str is falsy.
    """
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("cost_store: cannot parse date bound '%s'", date_str)
        return None
