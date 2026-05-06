"""Jira native (REST) adapter — STUB. Phase 1 implementation."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from .._base import Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport
from .shared import to_raw_item, resolve_token

class JiraNativeAdapter:
    name = "jira:native"
    kind = "jira"
    mode = "native"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"]
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.page_size = cfg.get("pagination", {}).get("page_size", 100)
        self.issuetypes = cfg.get("issuetypes_supported", [])

    def healthcheck(self) -> HealthReport:
        return HealthReport(healthy=True, mode=self.mode, notes="stub")

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        # TODO Phase 1: POST /rest/api/2/search with q.jql, paginate
        raise NotImplementedError("Phase 1 implementation")

    def fetch(self, ref: RawItemRef) -> RawItem:
        # TODO Phase 1: GET /rest/api/2/issue/{ref.source_id}?expand=changelog
        raise NotImplementedError("Phase 1 implementation")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # TODO Phase 1: webhook receiver normalizes to ChangeEvent
        raise NotImplementedError("Phase 1 implementation")
