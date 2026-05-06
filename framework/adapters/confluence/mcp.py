"""Confluence MCP adapter — STUB. Phase 1 implementation."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from .._base import Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport
from .shared import to_raw_item, resolve_token

class ConfluenceMcpAdapter:
    name = "confluence:mcp"
    kind = "confluence"
    mode = "mcp"

    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"]
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.tool_map = cfg["tool_map"]
        self.required_caps = cfg.get("required_capabilities", [])
        self.poll_interval = cfg.get("poll_interval_seconds", 300)

    def healthcheck(self) -> HealthReport:
        # TODO Phase 1: call tools/list on endpoint, verify required_caps present
        return HealthReport(healthy=True, mode=self.mode, notes="stub",
                            capabilities=list(self.tool_map.values()))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        # TODO Phase 1: call self.tool_map["list_pages_in_space"] via MCP client
        raise NotImplementedError("Phase 1 implementation")

    def fetch(self, ref: RawItemRef) -> RawItem:
        # TODO Phase 1: call self.tool_map["get_page_by_id"]; normalize() into RawItem
        raise NotImplementedError("Phase 1 implementation")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # TODO Phase 1: poll via MCP every poll_interval; emit ChangeEvents
        raise NotImplementedError("Phase 1 implementation")

    def normalize(self, mcp_response: dict, kind: str) -> RawItem:
        """Translate upstream MCP response to canonical RawItem shape."""
        # TODO Phase 1: map MCP-flavored fields onto the same payload/metadata
        # keys the native adapter produces.
        raise NotImplementedError("Phase 1 implementation")
