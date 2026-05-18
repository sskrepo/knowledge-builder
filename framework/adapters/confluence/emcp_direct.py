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
    AdapterWithIdentity,
    CanonicalRef,
    CanonicalResult,
    ChangeEvent,
    HealthReport,
    RawItem,
    RawItemRef,
    SourceQuery,
    Unresolvable,
    UNRESOLVABLE_NOT_FOUND,
    UNRESOLVABLE_NO_ACCESS,
    UNRESOLVABLE_TRANSIENT,
)
from .shared import to_raw_item, resolve_to_numeric_id, _DISPLAY_URL_PATTERN, _extract_numeric_id_fast
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


class ConfluenceEmcpDirectAdapter(AdapterWithIdentity):
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
        """Fetch a specific Confluence page by numeric id or URL.

        The emcp Confluence MCP server exposes TWO tools that retrieve a page:
          - `get_page`: accepts a numeric page_id or (title + space_key).
                        Explicitly does NOT accept URLs ("Confluence page ID
                        or tiny-link shortcode only, not a URL").
          - `fetch`:    accepts a canonical page URL, a tiny link, OR a numeric
                        page ID. Universal — resolves URLs server-side.

        We dispatch based on input shape so the user can paste either form:
          - URL or http-prefixed → `fetch` (URL-resolving)
          - everything else (pure digits, tiny-link)      → `get_page`
        Both tools return the same response shape, so normalize() is unchanged.
        """
        source_id = str(ref.source_id)
        is_url = source_id.startswith(("http://", "https://"))

        if is_url:
            text = self.runtime.call_tool_for_text(
                self._TOOL_FETCH, {"id": source_id},
            )
        else:
            text = self.runtime.call_tool_for_text(
                self._TOOL_GET_PAGE,
                {
                    "page_id": source_id,
                    "convert_to_markdown": True,
                    "include_metadata": True,
                },
            )
        tool_used = self._TOOL_FETCH if is_url else self._TOOL_GET_PAGE
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EmcpError(
                f"emcp_direct fetch: non-JSON response from {tool_used}({source_id}): "
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

        # Sanity check: a successful fetch MUST yield at least a title and
        # a space key. If both are missing the server returned an error
        # envelope or could not resolve the input. Surface as
        # FileNotFoundError so the ingest pipeline records a clear failure
        # rather than crashing downstream at `space.lower()`.
        if not meta.get("title") and not (meta.get("space") or {}).get("key"):
            raise FileNotFoundError(
                f"Confluence page {source_id!r}: {tool_used} returned no usable "
                f"metadata (title/space missing). The page may not exist, may be "
                f"inaccessible to this OAuth identity, or the URL/tiny-link could "
                f"not be resolved."
            )

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

        Downstream consumer ConfluenceWikiIngestor._fetch_page does:
          body_html = (
            raw.payload.get("body", {}).get("storage", {}).get("value", "")
            or raw.payload.get("body", "")
          )

        That chain REQUIRES `body` to be a dict ({"storage": {"value": "..."}})
        — if `body` is a plain string, Python raises
          AttributeError: 'str' object has no attribute 'get'
        before the `or` fallback can fire (BUG-queue-cf562, session
        synth-tpm-8bb804ae). We therefore emit `body` in the nested Confluence-
        native shape so this adapter is interchangeable with mcp/native/
        codex_proxy adapters — all of which already use the nested form.

        emcp `get_page` returns:
          results.metadata.{id,title,space.key,version,updated,labels[],
                            content.value}
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
            # Nested Confluence-native shape — see comment above.
            "body": {"storage": {"value": body_text}},
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

    # ------------------------------------------------------------------
    # ADR-039 (DECISION-020): canonical_identity implementation
    # ------------------------------------------------------------------

    def _resolve_via_emcp(self, reference: str, resource_type: str) -> CanonicalResult:
        """Resolve a display-by-title URL to a CanonicalRef via the eMCP fetch tool.

        Uses the SAME EmcpRuntime channel that CONFIGURE_SOURCES/sampler uses
        successfully to fetch pages. The `fetch` tool resolves URLs (including
        /display/SPACE/Title forms) server-side and returns the numeric page id
        in the payload.

        Called ONLY for display-by-title URLs (and non-numeric refs) where
        resolve_to_numeric_id(session=None) would structurally fail (TRANSIENT).
        Numeric refs go through the fast-path and never reach this method.

        Error mapping:
          - FileNotFoundError / 'not_found' error in payload   → NOT_FOUND, retryable=False
          - EmcpAuthError (keychain/permission failure)         → NO_ACCESS, retryable=False
          - EmcpError / any other exception (network/transient) → TRANSIENT, retryable=True
        """
        try:
            text = self.runtime.call_tool_for_text(
                self._TOOL_FETCH, {"id": reference},
            )
        except EmcpAuthError as exc:
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NO_ACCESS,
                detail=(
                    f"eMCP auth failure resolving {reference!r}: {exc}. "
                    "Run `codex mcp login` to restore the OAuth binding."
                ),
                retryable=False,
            )
        except EmcpError as exc:
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_TRANSIENT,
                detail=f"eMCP network/transient error resolving {reference!r}: {exc}",
                retryable=True,
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_TRANSIENT,
                detail=(
                    f"eMCP fetch returned non-JSON for {reference!r}: {text[:200]}"
                ),
                retryable=True,
            )

        # Parse the response using the same path as fetch() / normalize()
        inner = payload.get("results") or payload
        meta = inner.get("metadata") or {}

        # Server signals not-found explicitly
        if meta.get("error") == "not_found" or payload.get("error") == "not_found":
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NOT_FOUND,
                detail=f"Confluence page not found for reference {reference!r}.",
                retryable=False,
            )

        # Extract numeric page id from payload — the `id` field in metadata
        numeric_id = str(meta.get("id") or "")
        if not numeric_id:
            # Fallback: some server versions expose id at the top level
            numeric_id = str(inner.get("id") or "")

        if not numeric_id:
            # No id returned — treat as not found
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NOT_FOUND,
                detail=(
                    f"eMCP fetch returned no numeric page id for {reference!r}. "
                    f"Server meta: {str(meta)[:200]}"
                ),
                retryable=False,
            )

        display_hint = meta.get("title") or ""
        log.info(
            "emcp_direct canonical_identity: resolved %r → numeric id %s (title=%r)",
            reference, numeric_id, display_hint,
        )
        return CanonicalRef(
            connector_id="confluence",
            resource_type=resource_type,
            canonical_id=numeric_id,
            display_hint=display_hint,
        )

    def canonical_identity(self, reference: str, resource_type: str) -> CanonicalResult:
        """Resolve any Confluence reference to a CanonicalRef with numeric page ID.

        emcp_direct mode — two-path resolution:

        Fast path (no MCP round-trip):
          If `reference` contains a numeric page ID in any of the known forms
          (?pageId=NNN, /pages/NNN/, bare digit string, etc.), extract it
          directly and return CanonicalRef.  This preserves the behavior of
          shared.resolve_to_numeric_id() Step 1 — no eMCP call needed.

        eMCP path (for display-by-title and other non-numeric refs):
          For /display/SPACE/Title URLs (and any other reference that doesn't
          contain a numeric id), use the same EmcpRuntime channel that
          CONFIGURE_SOURCES/sampler uses successfully to fetch pages.  The
          `fetch` tool resolves URLs server-side and returns the numeric `id`
          in the response payload.  We read that numeric id and build the
          CanonicalRef.

          This closes the structural gap reported in BUG-queue-98ca0: with
          session=None, shared.resolve_to_numeric_id always returned
          Unresolvable(TRANSIENT) for display URLs — even though the EmcpRuntime
          channel was already working (the SAME channel fetches pages fine).
          The prior keychain-retry RCA was a mis-diagnosis; the root cause was
          that canonical_identity was not using the available eMCP channel.

        Error returns (typed — never a raw string):
          - not found           → Unresolvable(NOT_FOUND, retryable=False)
          - auth / permission   → Unresolvable(NO_ACCESS, retryable=False)
          - network / transient → Unresolvable(TRANSIENT, retryable=True)
        """
        # Step 1: fast-path numeric extraction — zero MCP round-trip.
        # Delegates to the same shared algorithm used by native.py / mcp.py.
        # When it succeeds it returns CanonicalRef directly.
        # When session=None and a numeric id is present, shared returns it too.
        fast_result = resolve_to_numeric_id(
            reference=reference,
            resource_type=resource_type,
            session=None,   # fast-path only; session required for title lookup
            base_url="",    # not used when session=None
        )

        # If fast-path found a numeric id → return it (no eMCP round-trip).
        if isinstance(fast_result, CanonicalRef):
            return fast_result

        # fast_result is Unresolvable — check whether it's a display-by-title
        # URL or any other non-numeric form that the eMCP channel can resolve.
        # For INVALID_REF (not a recognized Confluence reference at all) we
        # preserve the Unresolvable from shared rather than making an eMCP call
        # that would also fail.
        from .._base import UNRESOLVABLE_INVALID_REF
        if fast_result.reason == UNRESOLVABLE_INVALID_REF:
            # Unknown reference form — not a display URL, not a numeric id.
            # Return shared's typed result unchanged.
            return fast_result

        # Display-by-title URL (or other non-numeric ref the eMCP fetch tool
        # can resolve): use the EmcpRuntime channel.
        return self._resolve_via_emcp(reference=reference, resource_type=resource_type)

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> "ConfluenceEmcpDirectAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
