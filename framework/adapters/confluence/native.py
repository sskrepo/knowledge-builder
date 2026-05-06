"""Confluence native (REST) adapter — STUB. Phase 1 implementation."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from .._base import Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport
from .shared import to_raw_item, resolve_token

class ConfluenceNativeAdapter:
    name = "confluence:native"
    kind = "confluence"
    mode = "native"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"]
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.page_size = cfg.get("pagination", {}).get("page_size", 50)
        self.rpm = cfg.get("rate_limit", {}).get("requests_per_minute", 120)

    def healthcheck(self) -> HealthReport:
        # TODO Phase 1: GET /rest/api/space — return ok if 200
        return HealthReport(healthy=True, mode=self.mode, notes="stub")

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        # TODO Phase 1: GET /rest/api/content?spaceKey={q.space}, paginate
        raise NotImplementedError("Phase 1 implementation")

    def fetch(self, ref: RawItemRef) -> RawItem:
        # TODO Phase 1: GET /rest/api/content/{ref.source_id}?expand=body.storage,metadata.labels
        raise NotImplementedError("Phase 1 implementation")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # TODO Phase 1: webhook receiver normalizes to ChangeEvent
        raise NotImplementedError("Phase 1 implementation")
