"""Confluence Codex Proxy adapter — LLM-mediated via `codex mcp-server`.

ADR-020 (amended 2026-05-11). The org's Confluence MCP server is exposed as
an HTTPS+OAuth endpoint registered in Codex (`codex mcp add central_confluence
<url>`). There is no spawn command for the framework to launch directly. The
laptop path that reuses Codex's OAuth is to ask Codex itself, via its
`mcp-server` stdio interface, to call the Confluence MCP server on our behalf.

Cost: each adapter call runs a Codex LLM session (~30-60s, 1-3k tokens). This
adapter is laptop-only; production uses `mode: mcp` with a service token.

The `server_name` config field is the Codex-registered MCP server label
(e.g. `central_confluence`). The adapter constructs prompts that explicitly
direct Codex to use that server.
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


class ConfluenceCodexProxyAdapter:
    """Confluence adapter that brokers calls through Codex's `codex` MCP tool."""

    name = "confluence:codex_proxy"
    kind = "confluence"
    mode = "codex_proxy"

    def __init__(self, cfg: dict) -> None:
        self.server_name: str = cfg["server_name"]
        self.timeout_s: float = float(cfg.get("timeout_seconds", 180))
        self.max_pages: int = int(cfg.get("max_pages_per_list", 25))
        self.runtime: CodexProxyRuntime = CodexProxyRuntime(
            codex_bin=cfg.get("codex_bin", "codex"),
            request_timeout_s=self.timeout_s,
        )

    # ------------------------------------------------------------------
    # Prompt builders — every prompt enforces "JSON only, no prose".
    # ------------------------------------------------------------------

    def _prompt_health(self) -> str:
        return (
            f"Use the {self.server_name} MCP server. Call its capabilities "
            f"discovery — list available tools by name. "
            f"Return ONLY a JSON object: {{\"tools\": [\"tool_name\", ...]}}. "
            f"Do not include prose, do not summarize. JSON only."
        )

    def _prompt_list(self, q: SourceQuery) -> str:
        labels = q.labels_include or []
        labels_clause = (
            f" Restrict results to pages labelled with any of {labels}." if labels else ""
        )
        return (
            f"Use the {self.server_name} MCP server to search Confluence in "
            f"space '{q.space}'.{labels_clause} Return at most {self.max_pages} "
            f"recently-updated pages. "
            f"Return ONLY a JSON object exactly like: "
            f"{{\"results\": [{{\"id\": \"...\", \"title\": \"...\", "
            f"\"updatedAt\": \"<ISO-8601>\"}}]}}. "
            f"If no results, return {{\"results\": []}}. No prose. JSON only."
        )

    def _prompt_fetch(self, page_id: str) -> str:
        return (
            f"Use the {self.server_name} MCP server to fetch the Confluence "
            f"page with id '{page_id}'. Include the page body (storage format "
            f"if available), title, space key, version number, last-updated "
            f"timestamp, and labels. "
            f"Return ONLY a JSON object exactly like: "
            f"{{\"id\": \"...\", \"title\": \"...\", \"space\": {{\"key\": \"...\"}}, "
            f"\"version\": {{\"number\": <int>, \"when\": \"<ISO-8601>\"}}, "
            f"\"body\": {{\"storage\": {{\"value\": \"<html or markdown>\"}}}}, "
            f"\"metadata\": {{\"labels\": {{\"results\": [{{\"name\": \"...\"}}]}}}}}}. "
            f"If the page does not exist, return {{\"error\": \"not_found\"}}. "
            f"No prose. JSON only."
        )

    def _prompt_search_since(self, since: datetime) -> str:
        return (
            f"Use the {self.server_name} MCP server to find Confluence pages "
            f"modified on or after {since.strftime('%Y-%m-%d')}. "
            f"Return at most {self.max_pages} most-recent results. "
            f"Return ONLY a JSON object: "
            f"{{\"results\": [{{\"id\": \"...\", \"updatedAt\": \"<ISO-8601>\"}}]}}. "
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
        if not q.space:
            raise ValueError("confluence codex_proxy list requires SourceQuery.space")
        data = self.runtime.call_for_json(
            self._prompt_list(q), timeout_s=self.timeout_s
        )
        results = _extract_results(data, key="results")
        for p in results:
            yield RawItemRef(
                kind="confluence_page",
                source="confluence",
                source_id=str(p.get("id", "")),
                last_modified=_parse_iso(p.get("updatedAt") or p.get("lastModified")),
            )

    def fetch(self, ref: RawItemRef) -> RawItem:
        data = self.runtime.call_for_json(
            self._prompt_fetch(ref.source_id), timeout_s=self.timeout_s
        )
        if isinstance(data, dict) and data.get("error") == "not_found":
            raise FileNotFoundError(f"Confluence page {ref.source_id} not found")
        return self.normalize(data, ref.source_id)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        data = self.runtime.call_for_json(
            self._prompt_search_since(since), timeout_s=self.timeout_s
        )
        results = _extract_results(data, key="results")
        for p in results:
            yield ChangeEvent(
                kind="updated",
                source="confluence",
                source_id=str(p.get("id", "")),
                timestamp=_parse_iso(p.get("updatedAt")) or datetime.utcnow(),
            )

    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]:
        for step in recipe:
            q = SourceQuery(
                space=step.get("space"),
                labels_include=step.get("labels_include", []),
                labels_exclude=step.get("labels_exclude", []),
                extra=step.get("extra", {}),
            )
            yield from self.list(q)

    def normalize(self, payload: dict, source_id: str) -> RawItem:
        metadata = {
            "title": payload.get("title"),
            "space": (payload.get("space") or {}).get("key"),
            "version": (payload.get("version") or {}).get("number"),
            "updated_at": (payload.get("version") or {}).get("when"),
            "labels": [
                lbl.get("name")
                for lbl in (
                    payload.get("metadata", {}).get("labels", {}).get("results", [])
                )
                if isinstance(lbl, dict)
            ],
        }
        return to_raw_item(payload=payload, metadata=metadata, source_id=str(source_id))

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> "ConfluenceCodexProxyAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _extract_results(data: Any, *, key: str) -> list[dict]:
    """Codex sometimes returns a bare list; sometimes {key: [...]}. Tolerate both."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []
