"""Confluence MCP adapter — full implementation."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Iterable

from .._base import (
    Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport,
)
from .shared import resolve_token, to_raw_item

log = logging.getLogger(__name__)


class ConfluenceMcpAdapter:
    name = "confluence:mcp"
    kind = "confluence"
    mode = "mcp"

    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"].rstrip("/")
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.tool_map = cfg["tool_map"]
        self.required_caps = cfg.get("required_capabilities", [])
        self.poll_interval = cfg.get("poll_interval_seconds", 300)
        self.rpm = cfg.get("rate_limit", {}).get("requests_per_minute", 60)
        self._session = self._build_session()
        self._last_request = 0.0

    def _build_session(self):
        try:
            import requests
        except ImportError:
            return None
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        return s

    def _throttle(self) -> None:
        if not self.rpm: return
        min_interval = 60.0 / self.rpm
        elapsed = time.time() - self._last_request
        if elapsed < min_interval: time.sleep(min_interval - elapsed)
        self._last_request = time.time()

    def _call_tool(self, tool: str, args: dict) -> dict:
        self._throttle()
        r = self._session.post(f"{self.endpoint}/tools/call",
                               json={"name": tool, "arguments": args}, timeout=60)
        r.raise_for_status()
        result = r.json()
        if "error" in result:
            raise RuntimeError(f"MCP tool error from {tool}: {result['error']}")
        return result.get("content", result)

    def healthcheck(self) -> HealthReport:
        if self._session is None:
            return HealthReport(False, self.mode, "requests not installed")
        try:
            self._throttle()
            r = self._session.post(f"{self.endpoint}/tools/list", json={}, timeout=10)
            if r.status_code != 200:
                return HealthReport(False, self.mode, f"{r.status_code} from tools/list")
            tools = [t["name"] for t in r.json().get("tools", [])]
            missing = [op for op in self.required_caps if self.tool_map.get(op) not in tools]
            if missing:
                return HealthReport(False, self.mode, f"missing: {missing}", capabilities=tools)
            return HealthReport(True, self.mode, "ok", capabilities=tools)
        except Exception as e:
            return HealthReport(False, self.mode, str(e))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if self._session is None: return
        if not q.space:
            raise ValueError("Confluence MCP list requires SourceQuery.space")
        tool = self.tool_map["list_pages_in_space"]
        cursor = None
        while True:
            args = {"spaceKey": q.space, "limit": 50}
            if cursor: args["start"] = cursor
            if q.labels_include: args["labels"] = q.labels_include
            res = self._call_tool(tool, args)
            pages = res.get("results", res.get("content", []))
            if not pages: break
            for p in pages:
                yield RawItemRef(
                    kind="confluence_page",
                    source="confluence",
                    source_id=str(p.get("id", p.get("contentId"))),
                    last_modified=_parse_iso((p.get("version") or {}).get("when")),
                )
            cursor = res.get("nextStart")
            if not cursor: break

    def fetch(self, ref: RawItemRef) -> RawItem:
        tool = self.tool_map["get_page_by_id"]
        payload = self._call_tool(tool, {"pageId": ref.source_id, "expand": "body.storage,metadata.labels"})
        return self.normalize(payload, ref.source_id)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # Polling-only via MCP; webhook fan-out is upstream's responsibility
        if "search" not in self.tool_map: return iter([])
        cql = f'lastmodified >= "{since.strftime("%Y-%m-%d")}"'
        res = self._call_tool(self.tool_map["search"], {"cql": cql, "limit": 50})
        for p in res.get("results", []):
            yield ChangeEvent(
                kind="updated", source="confluence",
                source_id=str(p.get("id")),
                timestamp=_parse_iso((p.get("version") or {}).get("when")) or datetime.utcnow(),
            )

    def normalize(self, mcp_response: dict, source_id: str) -> RawItem:
        if "body" in mcp_response or "id" in mcp_response:
            payload = mcp_response
        else:
            content = mcp_response.get("content") or []
            if content and isinstance(content[0], dict) and "text" in content[0]:
                payload = json.loads(content[0]["text"])
            else:
                payload = mcp_response
        metadata = {
            "title": payload.get("title"),
            "space": (payload.get("space") or {}).get("key"),
            "version": (payload.get("version") or {}).get("number"),
            "updated_at": (payload.get("version") or {}).get("when"),
            "labels": [lbl.get("name") for lbl in
                       (payload.get("metadata", {}).get("labels", {}).get("results", []))],
        }
        return to_raw_item(payload=payload, metadata=metadata, source_id=str(source_id))


def _parse_iso(s: str | None) -> datetime | None:
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError: return None
