"""Regression tests for BUG-queue-cf562: emcp_direct adapter produced
`payload.body` as a plain string, but the ingestor's _fetch_page expected
the nested Confluence-native shape `{"storage": {"value": "..."}}`. The
resulting AttributeError crashed every INGEST for page-URL sources.

Two layers of defense are tested:
  1. The adapter now emits the nested shape (preserves the contract).
  2. The ingestor is robust to either shape (belt-and-suspenders).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from framework.adapters.confluence.emcp_direct import ConfluenceEmcpDirectAdapter
from framework.adapters._base import RawItemRef
from framework.ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor


class TestEmcpDirectBodyShape:
    """The emcp_direct adapter must emit body in the canonical nested shape."""

    @pytest.fixture
    def adapter(self, monkeypatch):
        # Avoid Keychain / network — stub the runtime entirely.
        monkeypatch.setattr(
            ConfluenceEmcpDirectAdapter,
            "__init__",
            lambda self, cfg: setattr(self, "runtime", MagicMock()) or None,
        )
        a = ConfluenceEmcpDirectAdapter({})
        a.server_name = "test"
        a.timeout_s = 5.0
        a.max_pages = 25
        return a

    def test_fetch_returns_body_as_nested_dict_not_string(self, adapter):
        """The bug: previously body came back as a plain string. The ingestor
        then called .get("storage", {}) on that string and crashed."""
        # Mock the server response — exact shape from EE-Central-Confluence-MCP.
        adapter.runtime.call_tool_for_text.return_value = (
            '{"results":{"metadata":{"id":"123","title":"Test Page",'
            '"space":{"key":"OCIFACP","name":"Fusion Apps Control Plane"},'
            '"version":42,"updated":"2026-05-13 20:00:00","labels":[],'
            '"content":{"value":"# real markdown body here"}}}}'
        )

        item = adapter.fetch(RawItemRef(
            kind="confluence_page", source="confluence", source_id="123",
        ))

        body = item.payload.get("body")
        assert isinstance(body, dict), (
            f"body must be a dict (nested Confluence shape), got "
            f"{type(body).__name__}={body!r}. Plain-string body crashed the "
            f"ingestor at _fetch_page (BUG-queue-cf562)."
        )
        assert body == {"storage": {"value": "# real markdown body here"}}

    def test_fetch_body_dict_has_get_method(self, adapter):
        """The crash signature was AttributeError: 'str' object has no
        attribute 'get'. Confirm the returned body responds to .get()."""
        adapter.runtime.call_tool_for_text.return_value = (
            '{"results":{"metadata":{"id":"x","title":"T",'
            '"space":{"key":"S"},"version":1,"updated":"2026-01-01",'
            '"labels":[],"content":{"value":"hello"}}}}'
        )
        item = adapter.fetch(RawItemRef(
            kind="confluence_page", source="confluence", source_id="x",
        ))
        # This is the EXACT chain the ingestor does at _fetch_page:
        body_html = item.payload.get("body", {}).get("storage", {}).get("value", "")
        assert body_html == "hello", (
            "the ingestor's chained .get() calls must not raise"
        )

    def test_fetch_handles_empty_content(self, adapter):
        """If a page has no content, body should still be a dict (with empty value)."""
        adapter.runtime.call_tool_for_text.return_value = (
            '{"results":{"metadata":{"id":"x","title":"Empty",'
            '"space":{"key":"S"},"version":1,"updated":"2026-01-01",'
            '"labels":[]}}}'
        )
        item = adapter.fetch(RawItemRef(
            kind="confluence_page", source="confluence", source_id="x",
        ))
        body = item.payload.get("body")
        assert isinstance(body, dict)
        assert body.get("storage", {}).get("value") == ""


class TestIngestorRobustToBodyShape:
    """Belt-and-suspenders: even if a future adapter regresses to plain-string
    body, _fetch_page must not crash. It coerces to a string and continues."""

    def _make_ingestor(self, tmp_path, raw_payload: dict, raw_metadata: dict):
        """Build an ingestor with a stub adapter that returns the given payload."""
        from framework.adapters._base import RawItem
        adapter = MagicMock()
        adapter.fetch.return_value = RawItem(
            kind="confluence_page", source="confluence",
            source_id="123",
            payload=raw_payload,
            metadata=raw_metadata,
        )
        ingestor = ConfluenceWikiIngestor(
            wiki_root=tmp_path / "wiki",
            adapter=adapter,
        )
        return ingestor

    def test_handles_nested_body_dict(self, tmp_path):
        ing = self._make_ingestor(
            tmp_path,
            raw_payload={"body": {"storage": {"value": "<p>hi</p>"}}},
            raw_metadata={"title": "T", "space": "S", "labels": []},
        )
        raw = ing._fetch_page("123")
        assert raw["body"] == "<p>hi</p>"

    def test_handles_plain_string_body_without_crashing(self, tmp_path):
        """The exact bug from BUG-queue-cf562: an adapter returned body as
        a plain string. The old code raised AttributeError. The new code
        recognises the string and uses it directly."""
        ing = self._make_ingestor(
            tmp_path,
            raw_payload={"body": "# markdown body as plain string"},
            raw_metadata={"title": "T", "space": "S", "labels": []},
        )
        raw = ing._fetch_page("123")  # MUST NOT raise AttributeError
        assert raw["body"] == "# markdown body as plain string"

    def test_handles_missing_body(self, tmp_path):
        ing = self._make_ingestor(
            tmp_path,
            raw_payload={},  # no body key at all
            raw_metadata={"title": "T", "space": "S", "labels": []},
        )
        raw = ing._fetch_page("123")
        assert raw["body"] == ""

    def test_handles_unexpected_body_type(self, tmp_path):
        """Defensive: if body is somehow neither dict nor str (e.g. None or
        an int from a broken adapter), don't crash — return empty string."""
        ing = self._make_ingestor(
            tmp_path,
            raw_payload={"body": None},
            raw_metadata={"title": "T", "space": "S", "labels": []},
        )
        raw = ing._fetch_page("123")
        assert raw["body"] == ""
