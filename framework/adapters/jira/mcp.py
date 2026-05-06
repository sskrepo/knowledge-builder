"""Jira MCP adapter — full implementation.

Calls upstream MCP server's tools per cfg.tool_map. Probes capabilities at startup.
"""
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


class JiraMcpAdapter:
    name = "jira:mcp"
    kind = "jira"
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
        self._capabilities: list[str] = []

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
        if not self.rpm:
            return
        min_interval = 60.0 / self.rpm
        elapsed = time.time() - self._last_request
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request = time.time()

    def _call_tool(self, tool: str, args: dict) -> dict:
        self._throttle()
        r = self._session.post(
            f"{self.endpoint}/tools/call",
            json={"name": tool, "arguments": args}, timeout=60,
        )
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
            r = self._session.post(f"{self.endpoint}/tools/list",
                                   json={}, timeout=10)
            if r.status_code != 200:
                return HealthReport(False, self.mode, f"{r.status_code} from tools/list")
            tools = [t["name"] for t in r.json().get("tools", [])]
            self._capabilities = tools
            missing = [
                req_op for req_op in self.required_caps
                if self.tool_map.get(req_op) not in tools
            ]
            if missing:
                return HealthReport(False, self.mode,
                                    f"required capabilities missing: {missing}",
                                    capabilities=tools)
            return HealthReport(True, self.mode, "ok", capabilities=tools)
        except Exception as e:
            return HealthReport(False, self.mode, str(e))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if self._session is None:
            return
        if not q.jql:
            raise ValueError("Jira MCP list requires SourceQuery.jql")
        tool = self.tool_map["search_issues"]
        cursor = None
        while True:
            args = {"jql": q.jql, "maxResults": 100}
            if cursor:
                args["startAt"] = cursor
            res = self._call_tool(tool, args)
            issues = res.get("issues", res.get("content", []))
            if not issues:
                break
            for issue in issues:
                yield RawItemRef(
                    kind="jira_issue",
                    source="jira",
                    source_id=issue.get("key", issue.get("id")),
                    last_modified=_parse_iso((issue.get("fields") or {}).get("updated")),
                )
            cursor = res.get("nextCursor")
            if not cursor:
                break

    def fetch(self, ref: RawItemRef) -> RawItem:
        tool = self.tool_map["get_issue"]
        payload = self._call_tool(tool, {"issueIdOrKey": ref.source_id, "expand": "changelog,comments"})
        return self.normalize(payload)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # MCP path: poll on interval (no webhook propagation through MCP layer)
        jql = f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
        for ref in self.list(SourceQuery(jql=jql)):
            yield ChangeEvent(
                kind="updated",
                source="jira",
                source_id=ref.source_id,
                timestamp=ref.last_modified or datetime.utcnow(),
            )

    def normalize(self, mcp_response: dict) -> RawItem:
        """Translate upstream MCP tool's output to our canonical RawItem shape.

        Different MCP impls may shape responses differently. Heuristic:
        - if it has 'fields' at top level, treat as already-Jira-shaped
        - else look for `content[0].text` and parse as JSON
        """
        if "fields" in mcp_response:
            payload = mcp_response
        else:
            content = mcp_response.get("content") or []
            if content and isinstance(content[0], dict) and "text" in content[0]:
                payload = json.loads(content[0]["text"])
            else:
                payload = mcp_response

        fields = payload.get("fields", {})
        metadata = {
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "author": (fields.get("creator") or {}).get("displayName"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "labels": fields.get("labels", []),
            "components": [c.get("name") for c in fields.get("components", [])],
            "issuetype": (fields.get("issuetype") or {}).get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "status": (fields.get("status") or {}).get("name"),
            "project": (fields.get("project") or {}).get("key"),
        }
        source_id = payload.get("key", payload.get("id", "unknown"))
        return to_raw_item(payload=payload, metadata=metadata, source_id=source_id)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
