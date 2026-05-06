"""Jira MCP adapter — STUB. Phase 1 implementation."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from .._base import Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport
from .shared import resolve_token

class JiraMcpAdapter:
    name = "jira:mcp"
    kind = "jira"
    mode = "mcp"

    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"]
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.tool_map = cfg["tool_map"]
        self.required_caps = cfg.get("required_capabilities", [])
        self.poll_interval = cfg.get("poll_interval_seconds", 300)

    def healthcheck(self) -> HealthReport:
        return HealthReport(healthy=True, mode=self.mode, notes="stub",
                            capabilities=list(self.tool_map.values()))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        raise NotImplementedError("Phase 1 implementation")

    def fetch(self, ref: RawItemRef) -> RawItem:
        raise NotImplementedError("Phase 1 implementation")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        raise NotImplementedError("Phase 1 implementation")

    def normalize(self, mcp_response: dict) -> RawItem:
        raise NotImplementedError("Phase 1 implementation")
