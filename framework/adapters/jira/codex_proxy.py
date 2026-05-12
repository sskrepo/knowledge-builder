"""Jira Codex Proxy adapter — LLM-mediated via `codex mcp-server`.

ADR-020 (amended 2026-05-11). The org's Jira MCP server is exposed as an
HTTPS+OAuth endpoint registered in Codex (`codex mcp add central_jira <url>`).
There is no spawn command for the framework to launch directly. The laptop
path that reuses Codex's OAuth is to ask Codex itself, via its `mcp-server`
stdio interface, to call the Jira MCP server on our behalf.

Cost: each adapter call runs a Codex LLM session (~30-60s, 1-3k tokens). This
adapter is laptop-only; production uses `mode: mcp` with a service token.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from .._base import (
    ChangeEvent,
    HealthReport,
    RawItem,
    RawItemRef,
    SourceQuery,
)
from .shared import to_raw_item
from ...core.codex_proxy_runtime import CodexProxyError, CodexProxyRuntime

log = logging.getLogger(__name__)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class JiraCodexProxyAdapter:
    """Jira adapter that brokers calls through Codex's `codex` MCP tool."""

    name = "jira:codex_proxy"
    kind = "jira"
    mode = "codex_proxy"

    def __init__(self, cfg: dict) -> None:
        self.server_name: str = cfg["server_name"]
        self.timeout_s: float = float(cfg.get("timeout_seconds", 180))
        self.max_issues: int = int(cfg.get("max_issues_per_list", 50))
        self.runtime: CodexProxyRuntime = CodexProxyRuntime(
            codex_bin=cfg.get("codex_bin", "codex"),
            request_timeout_s=self.timeout_s,
        )

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _prompt_health(self) -> str:
        return (
            f"Use the {self.server_name} MCP server. List the available tools "
            f"by name. Return ONLY a JSON object: "
            f"{{\"tools\": [\"tool_name\", ...]}}. No prose. JSON only."
        )

    def _prompt_list(self, q: SourceQuery) -> str:
        return (
            f"Use the {self.server_name} MCP server to search Jira issues "
            f"matching JQL: {q.jql!r}. Return at most {self.max_issues} "
            f"results ordered by updated DESC. "
            f"Return ONLY a JSON object: "
            f"{{\"issues\": [{{\"key\": \"...\", \"updatedAt\": \"<ISO-8601>\"}}]}}. "
            f"No prose. JSON only."
        )

    def _prompt_fetch(self, key: str) -> str:
        return (
            f"Use the {self.server_name} MCP server to fetch the Jira issue "
            f"'{key}', including its summary, description, status, type, "
            f"priority, creator, assignee, labels, components, project, "
            f"created/updated timestamps, and the latest 10 comments. "
            f"Return ONLY a JSON object exactly like: "
            f"{{\"key\": \"{key}\", \"fields\": {{\"summary\": \"...\", "
            f"\"description\": \"...\", \"status\": {{\"name\": \"...\"}}, "
            f"\"issuetype\": {{\"name\": \"...\"}}, \"priority\": {{\"name\": \"...\"}}, "
            f"\"creator\": {{\"displayName\": \"...\"}}, "
            f"\"assignee\": {{\"displayName\": \"...\"}}, "
            f"\"labels\": [\"...\"], "
            f"\"components\": [{{\"name\": \"...\"}}], "
            f"\"project\": {{\"key\": \"...\"}}, "
            f"\"created\": \"<ISO-8601>\", \"updated\": \"<ISO-8601>\", "
            f"\"comment\": {{\"comments\": [{{\"author\": {{\"displayName\": \"...\"}}, "
            f"\"created\": \"<ISO-8601>\", \"body\": \"...\"}}]}}}}}}. "
            f"If the issue does not exist, return {{\"error\": \"not_found\"}}. "
            f"No prose. JSON only."
        )

    def _prompt_search_since(self, since: datetime) -> str:
        jql = f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
        return (
            f"Use the {self.server_name} MCP server to search Jira with JQL: "
            f"{jql!r}. Return at most {self.max_issues} results. "
            f"Return ONLY a JSON object: "
            f"{{\"issues\": [{{\"key\": \"...\", \"updatedAt\": \"<ISO-8601>\"}}]}}. "
            f"No prose. JSON only."
        )

    # ------------------------------------------------------------------
    # Adapter Protocol
    # ------------------------------------------------------------------

    def healthcheck(self) -> HealthReport:
        try:
            data = self.runtime.call_for_json(
                self._prompt_health(), timeout_s=self.timeout_s
            )
            tools = data.get("tools") if isinstance(data, dict) else None
            if not isinstance(tools, list):
                return HealthReport(
                    False, self.mode,
                    f"unexpected health-probe shape: {data!r}",
                )
            return HealthReport(True, self.mode, "ok", capabilities=tools)
        except CodexProxyError as exc:
            return HealthReport(False, self.mode, str(exc))
        except Exception as exc:  # noqa: BLE001
            return HealthReport(False, self.mode, f"unexpected error: {exc}")

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if not q.jql:
            raise ValueError("jira codex_proxy list requires SourceQuery.jql")
        data = self.runtime.call_for_json(
            self._prompt_list(q), timeout_s=self.timeout_s
        )
        for issue in _extract_results(data, key="issues"):
            yield RawItemRef(
                kind="jira_issue",
                source="jira",
                source_id=str(issue.get("key", issue.get("id", ""))),
                last_modified=_parse_iso(issue.get("updatedAt") or issue.get("updated")),
            )

    def fetch(self, ref: RawItemRef) -> RawItem:
        data = self.runtime.call_for_json(
            self._prompt_fetch(ref.source_id), timeout_s=self.timeout_s
        )
        if isinstance(data, dict) and data.get("error") == "not_found":
            raise FileNotFoundError(f"Jira issue {ref.source_id} not found")
        return self.normalize(data)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        data = self.runtime.call_for_json(
            self._prompt_search_since(since), timeout_s=self.timeout_s
        )
        for issue in _extract_results(data, key="issues"):
            yield ChangeEvent(
                kind="updated",
                source="jira",
                source_id=str(issue.get("key", "")),
                timestamp=_parse_iso(issue.get("updatedAt")) or datetime.utcnow(),
            )

    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]:
        for step in recipe:
            q = SourceQuery(jql=step.get("jql"), extra=step.get("extra", {}))
            yield from self.list(q)

    def normalize(self, payload: dict) -> RawItem:
        fields = payload.get("fields", {}) if isinstance(payload, dict) else {}
        metadata = {
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "author": (fields.get("creator") or {}).get("displayName"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "labels": fields.get("labels", []),
            "components": [
                c.get("name") for c in fields.get("components", [])
                if isinstance(c, dict)
            ],
            "issuetype": (fields.get("issuetype") or {}).get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "status": (fields.get("status") or {}).get("name"),
            "project": (fields.get("project") or {}).get("key"),
        }
        source_id = payload.get("key", payload.get("id", "unknown"))
        return to_raw_item(payload=payload, metadata=metadata, source_id=str(source_id))

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> "JiraCodexProxyAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _extract_results(data: Any, *, key: str) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []
