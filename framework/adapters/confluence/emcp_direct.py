"""Confluence emcp-direct adapter — talks straight to the Confluence MCP
server over HTTPS+OAuth, no codex LLM session in the loop.

Per-call cost: ~10 s for a page fetch, compared to 180 s timeout with
the prior codex_proxy transport (BUG-queue-d3ec0 / session 23dcaa10).

This adapter is laptop-only — it reads codex's stored OAuth bundle from
the macOS Keychain. For staging/prod use the base `mcp` adapter with a
service token.
"""
from __future__ import annotations

import json
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
from ...core.emcp_runtime import EmcpAuthError, EmcpError, EmcpRuntime

log = logging.getLogger(__name__)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    # Server returns "2026-05-13 20:53:03" (space, not T). Handle both.
    s2 = s.replace(" ", "T").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


class ConfluenceEmcpDirectAdapter:
    """Confluence adapter using direct HTTPS+OAuth to the emcp MCP server."""

    name = "confluence:emcp_direct"
    kind = "confluence"
    mode = "emcp_direct"

    # Tool names exposed by the real EE-Central-Confluence-MCP server
    # (verified empirically via tools/list on 2026-05-13).
    _TOOL_GET_PAGE = "get_page"
    _TOOL_GET_LABELS = "get_labels"
    _TOOL_GET_PAGE_CHILDREN = "get_page_children"
    _TOOL_CONFLUENCE_SEARCH = "confluence_search"
    _TOOL_SEARCH = "search"
    _TOOL_FETCH = "fetch"

    def __init__(self, cfg: dict) -> None:
        # cfg fields:
        #   server_name              — codex-side MCP server label (e.g. "central_confluence")
        #   keychain_account_suffix  — OPTIONAL hex hash; auto-discovered if omitted
        #   max_pages                — cap on per-list page count (optional, default 25)
        #   timeout_seconds          — per-HTTP-call timeout (optional, default 60)
        self.server_name: str = cfg["server_name"]
        self.keychain_account_suffix: str | None = cfg.get("keychain_account_suffix")
        self.max_pages: int = int(cfg.get("max_pages", 25))
        self.timeout_s: float = float(cfg.get("timeout_seconds", 60))
        self.runtime = EmcpRuntime(
            server_name=self.server_name,
            keychain_account_suffix=self.keychain_account_suffix,
            timeout_s=self.timeout_s,
        )

    # ------------------------------------------------------------------
    # Adapter Protocol
    # ------------------------------------------------------------------

    def healthcheck(self) -> HealthReport:
        try:
            tools = self.runtime.list_tools()
            names = [t.get("name", "") for t in tools]
            return HealthReport(True, self.mode, "ok", capabilities=names)
        except EmcpAuthError as exc:
            return HealthReport(False, self.mode, f"auth: {exc}")
        except Exception as exc:  # noqa: BLE001
            return HealthReport(False, self.mode, str(exc))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        """Search a Confluence space (optionally filtered by labels).

        We use the `confluence_search` tool with CQL. The emcp server's
        `search` tool is a generic full-text variant; `confluence_search`
        takes a CQL string which is closer to the framework's labels filter.
        """
        if not q.space:
            raise ValueError("confluence emcp_direct list requires SourceQuery.space")

        cql_parts = [f'space = "{q.space}"']
        for lbl in q.labels_include or []:
            cql_parts.append(f'label = "{lbl}"')
        for lbl in q.labels_exclude or []:
            cql_parts.append(f'label != "{lbl}"')
        cql = " AND ".join(cql_parts)
        cql += " order by lastModified desc"

        try:
            text = self.runtime.call_tool_for_text(
                self._TOOL_CONFLUENCE_SEARCH,
                {"cql": cql, "limit": self.max_pages},
            )
        except EmcpError:
            # Some emcp deployments expose only `search`. Fall back to that.
            text = self.runtime.call_tool_for_text(
                self._TOOL_SEARCH,
                {"query": f"space:{q.space}", "limit": self.max_pages},
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.warning("emcp_direct list: non-JSON response: %s", text[:200])
            return

        # The emcp `confluence_search` wraps results in {"results":{"results":[...]}}
        # — the outer is the SSE/tool wrapper, the inner is Confluence's own format.
        # Be flexible about which shape we get.
        results = (
            payload.get("results", {}).get("results")
            or payload.get("results")
            or payload.get("content", [])
        )
        if not isinstance(results, list):
            log.warning("emcp_direct list: unexpected results shape: %s", str(payload)[:200])
            return

        for p in results:
            if not isinstance(p, dict):
                continue
            page_id = (
                str(p.get("id"))
                if p.get("id") is not None
                else str(p.get("contentId") or p.get("content", {}).get("id") or "")
            )
            if not page_id:
                continue
            yield RawItemRef(
                kind="confluence_page",
                source="confluence",
                source_id=page_id,
                last_modified=_parse_iso(
                    p.get("lastModified")
                    or p.get("updated")
                    or (p.get("version") or {}).get("when"),
                ),
            )

    def fetch(self, ref: RawItemRef) -> RawItem:
        """Fetch a specific Confluence page by id (or full URL passed as id)."""
        # The emcp server's `get_page` accepts a numeric page_id. If we were
        # given a full URL, the calling layer (skill_builder) has already
        # extracted the id where possible. For safety we still pass whatever
        # we got — the server will 404 on a truly unparseable input.
        text = self.runtime.call_tool_for_text(
            self._TOOL_GET_PAGE,
            {
                "page_id": ref.source_id,
                "convert_to_markdown": True,
                "include_metadata": True,
            },
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EmcpError(
                f"emcp_direct fetch: non-JSON response from get_page({ref.source_id}): "
                f"{text[:200]}"
            ) from exc

        # Server response shape (verified against EE-Central-Confluence-MCP 2.14.5):
        #   { "results": { "metadata": { ...fields...,
        #                                "content": {"value": "<markdown>"} } },
        #     "instructions": "..." }
        # NB: `content` is nested INSIDE `metadata`, not alongside it. We were
        # bitten by this on the first live test (body came back empty). We also
        # accept the older sibling-shape as a fallback for older server versions.
        inner = payload.get("results") or payload
        meta = inner.get("metadata") or {}
        content = meta.get("content") or inner.get("content") or {}

        if meta.get("error") == "not_found" or payload.get("error") == "not_found":
            raise FileNotFoundError(f"Confluence page {ref.source_id} not found")

        return self.normalize(payload=payload, meta=meta, content=content,
                              source_id=str(ref.source_id))

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        cql = f'lastmodified >= "{since.strftime("%Y-%m-%d")}" order by lastModified desc'
        try:
            text = self.runtime.call_tool_for_text(
                self._TOOL_CONFLUENCE_SEARCH,
                {"cql": cql, "limit": self.max_pages},
            )
        except EmcpError:
            return iter([])
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return iter([])
        results = payload.get("results", {}).get("results") or payload.get("results") or []
        for p in results:
            if not isinstance(p, dict):
                continue
            page_id = str(p.get("id") or p.get("contentId") or "")
            if not page_id:
                continue
            ts = _parse_iso(p.get("lastModified") or p.get("updated"))
            yield ChangeEvent(
                kind="updated",
                source="confluence",
                source_id=page_id,
                timestamp=ts or datetime.utcnow(),
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

    def normalize(
        self,
        payload: dict,
        meta: dict,
        content: dict,
        source_id: str,
    ) -> RawItem:
        """Translate emcp get_page response into the framework's flat RawItem shape.

        Downstream consumers (ConfluenceWikiIngestor.ingest_page) expect:
          payload.body                  -> markdown / html
          payload.title                 -> page title
          payload.space.key             -> space key
          payload.version.number, .when -> version metadata
          payload.metadata.labels.results[i].name -> labels

        emcp `get_page` returns these as:
          results.metadata.{id,title,space.key,version,updated,labels[]}
          results.content.value
        """
        body_text = content.get("value", "") if isinstance(content, dict) else ""

        labels_in = meta.get("labels") or []
        if isinstance(labels_in, list) and labels_in and isinstance(labels_in[0], str):
            labels_normalized = [{"name": lbl} for lbl in labels_in]
        elif isinstance(labels_in, list):
            labels_normalized = [
                lbl if isinstance(lbl, dict) else {"name": str(lbl)}
                for lbl in labels_in
            ]
        else:
            labels_normalized = []

        flat_payload: dict[str, Any] = {
            "id": meta.get("id", source_id),
            "title": meta.get("title"),
            "space": meta.get("space") or {},
            "version": {
                "number": meta.get("version"),
                "when": meta.get("updated") or meta.get("lastModified"),
            },
            "body": body_text,
            "metadata": {"labels": {"results": labels_normalized}},
            "url": meta.get("url"),
        }

        meta_dict = {
            "title": meta.get("title"),
            "space": (meta.get("space") or {}).get("key"),
            "version": meta.get("version"),
            "updated_at": meta.get("updated") or meta.get("lastModified"),
            "labels": [lbl.get("name") for lbl in labels_normalized if isinstance(lbl, dict)],
            "url": meta.get("url"),
        }
        return to_raw_item(payload=flat_payload, metadata=meta_dict, source_id=source_id)

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> "ConfluenceEmcpDirectAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
