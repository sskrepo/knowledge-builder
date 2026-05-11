"""text_to_sql MCP tool — NL-to-SQL with allowlist guardrails.

In stub/filestore mode: uses pattern matching to map common questions to
canned SQL templates, then executes them against fixture data in memory.
In production: routes to an LLM (OCI GenAI or OpenAI) with system prompt
constraining it to the allowlisted views.

Every response includes a citation (the view name + fixture path).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..adapters.udap_adapter import _load_fixtures

log = logging.getLogger(__name__)

_FORBIDDEN_KEYWORDS = frozenset({
    "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "GRANT",
    "REVOKE", "TRUNCATE", "MERGE", "CALL", "EXECUTE",
})

_ALLOWLIST_VIEWS = {
    "pod_health", "restart_counts", "refresh_progress",
    "fleet_inventory", "patching_status",
}


class _StubSqlEngine:
    """In-memory SQL-ish execution against fleet fixture JSON files.

    Supports a minimal SELECT-WHERE-LIMIT model over the 5 allowlisted views,
    implemented as Python logic over fixture data.  This is intentionally
    simple — the value is in the interface contract, not SQL completeness.
    """

    def execute(self, view: str, filters: dict, limit: int = 100) -> list[dict]:
        rt_map = {
            "pod_health": "pod",
            "restart_counts": "pod",
            "refresh_progress": "pod",
            "fleet_inventory": "pod",
            "patching_status": "node",
        }
        column_map = {
            "pod_health": ["pod_id", "tenant_id", "region", "status", "last_check_at"],
            "restart_counts": ["pod_id", "tenant_id", "restart_count", "restart_count_24h"],
            "refresh_progress": ["pod_id", "poddb_state", "last_refresh"],
            "fleet_inventory": ["pod_id", "fa_release", "region", "tenant_id", "status"],
            "patching_status": ["node_id", "current_patch", "target_patch", "patching_status"],
        }
        resource_type = rt_map.get(view, "pod")
        cols = column_map.get(view, [])
        records = _load_fixtures(resource_type)
        rows = []
        for rec in records:
            if filters:
                if not all(rec.get(k) == v for k, v in filters.items()):
                    continue
            row = {c: rec.get(c) for c in cols} if cols else dict(rec)
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows


_STUB_ENGINE = _StubSqlEngine()


_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    (
        re.compile(r"pod.health|health.pod|pod.status", re.I),
        "pod_health",
        {},
    ),
    (
        re.compile(r"restart.count|how.many.restart|restart.last.24", re.I),
        "restart_counts",
        {},
    ),
    (
        re.compile(r"fleet.inventor|all.pod|list.pod|pod.list", re.I),
        "fleet_inventory",
        {},
    ),
    (
        re.compile(r"patch|patching.status|patch.state", re.I),
        "patching_status",
        {},
    ),
    (
        re.compile(r"refresh.progress|poddb.refresh|db.refresh", re.I),
        "refresh_progress",
        {},
    ),
]

_TENANT_RE = re.compile(r"\btenant[- _]?([\w-]+)\b", re.I)


def _extract_tenant_filter(nl_query: str) -> dict:
    m = _TENANT_RE.search(nl_query)
    if m:
        raw = m.group(1).lower()
        return {"tenant_id": f"tenant-{raw}"}
    return {}


def _match_pattern(nl_query: str) -> tuple[str, dict] | None:
    tenant_filter = _extract_tenant_filter(nl_query)
    for pattern, view, extra_filters in _PATTERNS:
        if pattern.search(nl_query):
            merged = {**extra_filters, **tenant_filter}
            return view, merged
    return None


def _build_sql_string(view: str, filters: dict, limit: int) -> str:
    where_parts = [f"{k} = '{v}'" for k, v in filters.items()]
    where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    return f"SELECT * FROM {view}{where_clause} FETCH FIRST {limit} ROWS ONLY"


class TextToSqlRetriever:
    """NL-to-SQL retriever with guardrail enforcement.

    In stub mode (KBF_LLM_PROVIDER=stub or no LLM configured): pattern matching.
    In production: LLM generates SQL restricted to allowlisted views.
    """

    name = "text_to_sql"

    def __init__(self, llm=None, udap_adapter=None, allowlist_views: list[str] | None = None):
        self.llm = llm
        self.adapter = udap_adapter
        self.allowlist = set(allowlist_views) if allowlist_views else _ALLOWLIST_VIEWS

    def __call__(
        self,
        nl_query: str,
        limit: int = 100,
    ) -> dict:
        """Translate natural language to SQL and execute it.

        Returns:
            {
              "sql": str,              # the generated/matched SQL
              "results": list[dict],   # query rows
              "citation": str,         # view name — the data source
              "view": str,
              "matched_pattern": bool  # True if stub pattern matched, False if LLM
            }
        """
        self._check_forbidden(nl_query)

        match = _match_pattern(nl_query)

        if match is None and self.llm is not None:
            return self._llm_path(nl_query, limit)

        if match is None:
            return {
                "sql": "",
                "results": [],
                "citation": "udap://fleet/no-match",
                "view": "",
                "matched_pattern": False,
                "error": "No pattern matched and no LLM configured. Supported topics: "
                         "pod health, restart counts, fleet inventory, patching status, refresh progress.",
            }

        view, filters = match
        sql = _build_sql_string(view, filters, limit)
        rows = _STUB_ENGINE.execute(view, filters, limit)
        citation = f"udap://fleet/view/{view}"

        log.debug("text_to_sql(stub): nl=%r view=%s filters=%s → %d rows", nl_query, view, filters, len(rows))
        return {
            "sql": sql,
            "results": rows,
            "citation": citation,
            "view": view,
            "matched_pattern": True,
        }

    def _check_forbidden(self, sql_or_nl: str) -> None:
        tokens = set(re.findall(r"[A-Z]+", sql_or_nl.upper()))
        hits = tokens & _FORBIDDEN_KEYWORDS
        if hits:
            raise ValueError(f"query contains forbidden keywords: {hits}")

    def _llm_path(self, nl_query: str, limit: int) -> dict:
        raise NotImplementedError(
            "LLM-based text_to_sql not implemented in Phase 2 scope. "
            "Set KBF_LLM_PROVIDER=stub to use pattern matching."
        )
