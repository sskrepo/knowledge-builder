"""Unit tests for BUG-queue-98ca0 fix: emcp_direct.canonical_identity resolves
display-by-title URLs via EmcpRuntime instead of returning Unresolvable(TRANSIENT).

Root cause (structural gap, NOT a keychain flake):
  canonical_identity() called resolve_to_numeric_id(session=None), which always
  returned Unresolvable(TRANSIENT) for /display/SPACE/Title URLs because
  session=None prevents the REST title-lookup. Meanwhile self.runtime (EmcpRuntime)
  already works for page fetches — the fix uses that same channel.

Tests (all with MOCKED EmcpRuntime — no live network):
  (a) display-by-title URL + mock returns page payload  → CanonicalRef (numeric id)
  (b) numeric ?pageId= ref                              → CanonicalRef, NO eMCP round-trip
  (c) bare numeric id ref                               → CanonicalRef, NO eMCP round-trip
  (d) eMCP fetch raises EmcpError (not-found signal)    → Unresolvable(NOT_FOUND, retryable=False)
  (e) eMCP fetch raises EmcpAuthError                   → Unresolvable(NO_ACCESS, retryable=False)
  (f) eMCP fetch raises transient EmcpError             → Unresolvable(TRANSIENT, retryable=True)
  (g) canonicalize_pinned_source / upper layers unchanged — still single-call and
      hard-fails on a genuine Unresolvable (behavior unchanged per scope)

The real laptop live-eMCP behavior is NOT deterministically unit-testable
(requires a live Keychain + OAuth session + reachable MCP server). The user
validates that via a live re-authoring run. These tests cover the logic path only.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest

from framework.adapters.confluence.emcp_direct import ConfluenceEmcpDirectAdapter
from framework.adapters._base import (
    CanonicalRef,
    Unresolvable,
    UNRESOLVABLE_NOT_FOUND,
    UNRESOLVABLE_NO_ACCESS,
    UNRESOLVABLE_TRANSIENT,
    UNRESOLVABLE_INVALID_REF,
)
from framework.core.emcp_runtime import EmcpAuthError, EmcpError


# ---------------------------------------------------------------------------
# Fixture: adapter with mocked runtime (no Keychain / network)
# ---------------------------------------------------------------------------

def _make_adapter() -> ConfluenceEmcpDirectAdapter:
    """Build an adapter with a fully-mocked EmcpRuntime."""
    adapter = object.__new__(ConfluenceEmcpDirectAdapter)
    adapter.runtime = MagicMock()
    adapter.server_name = "central_confluence"
    adapter.timeout_s = 60.0
    adapter.max_pages = 25
    return adapter


def _page_payload(numeric_id: str, title: str, space_key: str = "OCIFACP") -> str:
    """Build the JSON string the eMCP `fetch` tool returns for a page."""
    return json.dumps({
        "results": {
            "metadata": {
                "id": numeric_id,
                "title": title,
                "space": {"key": space_key},
                "version": 1,
                "updated": "2026-05-18 00:00:00",
                "labels": [],
                "content": {"value": "# page content"},
            }
        }
    })


# ---------------------------------------------------------------------------
# (a) display-by-title URL + mock returns page payload → CanonicalRef
# ---------------------------------------------------------------------------

class TestDisplayUrlResolvesViaEmcp:
    """(a) /display/SPACE/Title URL — resolved via eMCP fetch tool."""

    DISPLAY_URL = (
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
    )

    def test_returns_canonical_ref_not_unresolvable(self):
        """The fix: display-by-title URL must return CanonicalRef, NOT Unresolvable."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = _page_payload(
            "18625350641", "FAaaS Kiwi Project"
        )

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, CanonicalRef), (
            f"Expected CanonicalRef for display URL, got {type(result).__name__}: {result!r}. "
            "BUG-queue-98ca0: canonical_identity must resolve display URLs via eMCP, "
            "not return Unresolvable(TRANSIENT) with session=None."
        )

    def test_canonical_id_is_numeric_string(self):
        """canonical_id must be the numeric page id from the eMCP payload."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = _page_payload(
            "18625350641", "FAaaS Kiwi Project"
        )

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, CanonicalRef)
        assert result.canonical_id == "18625350641", (
            f"canonical_id must be the numeric id from payload, got {result.canonical_id!r}"
        )

    def test_connector_and_resource_type_correct(self):
        """CanonicalRef must carry the correct connector_id and resource_type."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = _page_payload(
            "18625350641", "FAaaS Kiwi Project"
        )

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, CanonicalRef)
        assert result.connector_id == "confluence"
        assert result.resource_type == "page"

    def test_display_hint_is_page_title(self):
        """display_hint must be the page title from the eMCP payload."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = _page_payload(
            "18625350641", "FAaaS Kiwi Project"
        )

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, CanonicalRef)
        assert result.display_hint == "FAaaS Kiwi Project"

    def test_emcp_fetch_tool_called_with_the_reference(self):
        """The eMCP 'fetch' tool must be called with the display URL as the 'id' arg."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = _page_payload(
            "18625350641", "FAaaS Kiwi Project"
        )

        adapter.canonical_identity(self.DISPLAY_URL, "page")

        adapter.runtime.call_tool_for_text.assert_called_once_with(
            "fetch", {"id": self.DISPLAY_URL}
        )


# ---------------------------------------------------------------------------
# (b) numeric ?pageId= ref → fast-path CanonicalRef, NO eMCP round-trip
# ---------------------------------------------------------------------------

class TestNumericPageIdFastPath:
    """(b) References containing a numeric page id bypass the eMCP call entirely."""

    def test_pageid_query_param(self):
        """?pageId=NNN form → fast-path CanonicalRef, no eMCP round-trip."""
        adapter = _make_adapter()

        ref = "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=18625350641"
        result = adapter.canonical_identity(ref, "page")

        assert isinstance(result, CanonicalRef)
        assert result.canonical_id == "18625350641"
        # Assert the mock was NOT called — no MCP round-trip for numeric refs.
        adapter.runtime.call_tool_for_text.assert_not_called()

    def test_pages_path_numeric(self):
        """/pages/NNN/ path form → fast-path CanonicalRef, no eMCP round-trip."""
        adapter = _make_adapter()

        ref = "https://confluence.oraclecorp.com/confluence/pages/18625350641"
        result = adapter.canonical_identity(ref, "page")

        assert isinstance(result, CanonicalRef)
        assert result.canonical_id == "18625350641"
        adapter.runtime.call_tool_for_text.assert_not_called()

    def test_bare_numeric_id(self):
        """Bare all-digit string → fast-path CanonicalRef, no eMCP round-trip."""
        adapter = _make_adapter()

        result = adapter.canonical_identity("18625350641", "page")

        assert isinstance(result, CanonicalRef)
        assert result.canonical_id == "18625350641"
        adapter.runtime.call_tool_for_text.assert_not_called()


# ---------------------------------------------------------------------------
# (c) eMCP fetch raises not-found signal → Unresolvable(NOT_FOUND, retryable=False)
# ---------------------------------------------------------------------------

class TestEmcpNotFoundMapping:
    """(c) eMCP reports page not found → typed NOT_FOUND Unresolvable."""

    DISPLAY_URL = (
        "https://confluence.oraclecorp.com/confluence/display/DEAD/Missing+Page"
    )

    def test_emcp_not_found_error_in_payload(self):
        """Server returns error=not_found in payload → Unresolvable(NOT_FOUND)."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = json.dumps({
            "error": "not_found",
        })

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, Unresolvable), (
            f"Expected Unresolvable for not-found, got {type(result).__name__}: {result!r}"
        )
        assert result.reason == UNRESOLVABLE_NOT_FOUND
        assert result.retryable is False

    def test_emcp_returns_empty_id_not_found(self):
        """Payload with no numeric id → Unresolvable(NOT_FOUND)."""
        adapter = _make_adapter()
        # meta has no 'id' field — server resolved something but gave no id
        adapter.runtime.call_tool_for_text.return_value = json.dumps({
            "results": {"metadata": {"title": "something", "space": {"key": "X"}}}
        })

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, Unresolvable)
        assert result.reason == UNRESOLVABLE_NOT_FOUND
        assert result.retryable is False


# ---------------------------------------------------------------------------
# (d) eMCP fetch raises EmcpAuthError → Unresolvable(NO_ACCESS, retryable=False)
# ---------------------------------------------------------------------------

class TestEmcpAuthErrorMapping:
    """(d) EmcpAuthError → typed NO_ACCESS Unresolvable, non-retryable."""

    DISPLAY_URL = (
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/Restricted+Page"
    )

    def test_emcp_auth_error_maps_to_no_access(self):
        """EmcpAuthError (keychain/OAuth failure) → Unresolvable(NO_ACCESS, retryable=False)."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.side_effect = EmcpAuthError(
            "keychain read failed — run `codex mcp login`"
        )

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, Unresolvable)
        assert result.reason == UNRESOLVABLE_NO_ACCESS
        assert result.retryable is False


# ---------------------------------------------------------------------------
# (e) eMCP fetch raises transient EmcpError → Unresolvable(TRANSIENT, retryable=True)
# ---------------------------------------------------------------------------

class TestEmcpTransientErrorMapping:
    """(e) Transient EmcpError → typed TRANSIENT Unresolvable, retryable."""

    DISPLAY_URL = (
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/Some+Page"
    )

    def test_emcp_error_maps_to_transient(self):
        """Any generic EmcpError (network/timeout/server) → Unresolvable(TRANSIENT, retryable=True)."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.side_effect = EmcpError(
            "HTTP 503 from central_confluence after 3 attempts"
        )

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, Unresolvable)
        assert result.reason == UNRESOLVABLE_TRANSIENT
        assert result.retryable is True

    def test_non_json_response_maps_to_transient(self):
        """Non-JSON response (e.g. server error HTML) → Unresolvable(TRANSIENT, retryable=True)."""
        adapter = _make_adapter()
        adapter.runtime.call_tool_for_text.return_value = "<html>Internal Server Error</html>"

        result = adapter.canonical_identity(self.DISPLAY_URL, "page")

        assert isinstance(result, Unresolvable)
        assert result.reason == UNRESOLVABLE_TRANSIENT
        assert result.retryable is True


# ---------------------------------------------------------------------------
# (f) Upper-layer unchanged: canonicalize_pinned_source still hard-fails on Unresolvable
# ---------------------------------------------------------------------------

class TestUpperLayerUnchanged:
    """(f) canonicalize_pinned_source behavior is unchanged — hard-fails on Unresolvable.

    Per scope: we do NOT change synthesize_workflow.py, conversation.py, or any
    upper layer. This test proves the upper layer still enforces §4 hard-fail on
    a genuine Unresolvable — unchanged behavior.
    """

    def test_canonicalize_pinned_source_still_raises_on_unresolvable(self):
        """Upper-layer hard-fail contract: Unresolvable → PinnedSourceCanonicalizationError.

        The adapter is mocked to return Unresolvable(NOT_FOUND) — simulating
        a page that genuinely does not exist. canonicalize_pinned_source must
        raise PinnedSourceCanonicalizationError (§4 ADR-039 behavior unchanged).
        """
        from framework.skill_builder.synthesize_workflow import (
            canonicalize_pinned_source,
            PinnedSourceCanonicalizationError,
        )

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/DEAD/Ghost+Page"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["DEAD"],
        }
        not_found = Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference=display_url,
            reason=UNRESOLVABLE_NOT_FOUND,
            detail="Page does not exist.",
            retryable=False,
        )
        mock_canonicalize = MagicMock(return_value=not_found)

        with pytest.raises(PinnedSourceCanonicalizationError) as exc_info:
            canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        err = exc_info.value
        assert err.retryable is False
        assert err.reason == UNRESOLVABLE_NOT_FOUND
        # Exactly one call — no retry loop (unchanged behavior)
        mock_canonicalize.assert_called_once()

    def test_canonicalize_pinned_source_succeeds_with_canonical_ref(self):
        """Upper layer: CanonicalRef from adapter → pinned_ref = numeric id (unchanged)."""
        from framework.skill_builder.synthesize_workflow import canonicalize_pinned_source

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
        }
        canonical_ref = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id="18625350641",
            display_hint="FAaaS Kiwi Project",
        )
        mock_canonicalize = MagicMock(return_value=canonical_ref)

        result = canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        assert result["pinned_ref"] == "18625350641"
        assert result["canonical_ref"]["canonical_id"] == "18625350641"
        # Single call — unchanged behavior
        mock_canonicalize.assert_called_once()
