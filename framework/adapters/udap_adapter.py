"""UDAP / Sentinel adapter — read-through (no ingest) per ADR-001. STUB."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from ._base import Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport

class UdapAdapter:
    name = "udap"
    kind = "udap"
    mode = "read_through"

    def __init__(self, cfg: dict):
        self.connection = cfg["connection"]
        self.allowlist_file = cfg.get("allowlisted_views_file")
        self.guardrails = cfg.get("text_to_sql", {}).get("guardrails", {})

    def healthcheck(self) -> HealthReport:
        return HealthReport(healthy=True, mode=self.mode, notes="stub")

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        # UDAP is read-through; list returns the allowlist of available views.
        raise NotImplementedError("Phase 2 implementation")

    def fetch(self, ref: RawItemRef) -> RawItem:
        # Returns rows from a view as a synthetic RawItem.
        raise NotImplementedError("Phase 2 implementation")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # No change streaming; UDAP is queried live every retrieval.
        return iter([])
