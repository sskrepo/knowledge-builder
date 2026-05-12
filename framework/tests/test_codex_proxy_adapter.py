"""Tests for the codex_proxy adapters and shared runtime.

The runtime spawns `codex mcp-server`; we mock the CodexProxyRuntime entirely
at the adapter level. The runtime itself is exercised separately by injecting
a fake subprocess.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from framework.core.codex_proxy_runtime import (
    CodexProxyError,
    CodexProxyRuntime,
    parse_json_from_codex_response,
)


# ---------------------------------------------------------------------------
# parse_json_from_codex_response
# ---------------------------------------------------------------------------

class TestJsonExtraction:
    def test_bare_json_object(self) -> None:
        assert parse_json_from_codex_response('{"a": 1}') == {"a": 1}

    def test_bare_json_array(self) -> None:
        assert parse_json_from_codex_response('[1, 2, 3]') == [1, 2, 3]

    def test_markdown_fenced_json(self) -> None:
        text = '```json\n{"results": [{"id": "1"}]}\n```'
        assert parse_json_from_codex_response(text) == {"results": [{"id": "1"}]}

    def test_unlabeled_fenced_json(self) -> None:
        text = '```\n{"x": true}\n```'
        assert parse_json_from_codex_response(text) == {"x": True}

    def test_fenced_with_surrounding_prose(self) -> None:
        text = "Here you go:\n\n```json\n{\"k\": \"v\"}\n```\n\nLet me know."
        assert parse_json_from_codex_response(text) == {"k": "v"}

    def test_object_embedded_in_prose(self) -> None:
        text = "The answer is {\"answer\": 42} as you can see."
        assert parse_json_from_codex_response(text) == {"answer": 42}

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(CodexProxyError):
            parse_json_from_codex_response("not json at all, friend")

    def test_invalid_fenced_block_raises(self) -> None:
        with pytest.raises(CodexProxyError):
            parse_json_from_codex_response("```json\n{not valid}\n```")


# ---------------------------------------------------------------------------
# CodexProxyRuntime — with mocked subprocess
# ---------------------------------------------------------------------------

class _FakeProc:
    """A mock Popen that lets a test queue stdout JSON lines."""

    def __init__(self) -> None:
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.flush = MagicMock()
        self._stdout_lines: list[bytes] = []
        self._stdout_idx = 0
        self._poll_value: int | None = None

    @property
    def stdout(self):
        proc = self

        class _Iter:
            def __iter__(self_inner):
                return self_inner

            def __next__(self_inner):
                if proc._stdout_idx >= len(proc._stdout_lines):
                    raise StopIteration
                line = proc._stdout_lines[proc._stdout_idx]
                proc._stdout_idx += 1
                return line

        return _Iter()

    def queue(self, msg: dict) -> None:
        self._stdout_lines.append((json.dumps(msg) + "\n").encode("utf-8"))

    def poll(self):  # noqa: D401
        return self._poll_value

    def terminate(self) -> None:
        self._poll_value = 0

    def wait(self, timeout: float | None = None) -> int:
        return self._poll_value if self._poll_value is not None else 0

    def kill(self) -> None:
        self._poll_value = -9


class TestCodexProxyRuntime:
    def _runtime(self, fake_proc: _FakeProc) -> CodexProxyRuntime:
        runtime = CodexProxyRuntime(request_timeout_s=2.0, init_timeout_s=2.0)
        runtime._proc = fake_proc  # injected — _start() short-circuits
        return runtime

    def test_initialize_then_tool_call_returns_text(self) -> None:
        proc = _FakeProc()
        proc.queue({
            "jsonrpc": "2.0", "id": 0,
            "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "codex-mcp-server"}},
        })
        proc.queue({
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "content": [{"type": "text", "text": '```json\n{"ok": true}\n```'}],
                "structuredContent": {"threadId": "thr-1", "content": '```json\n{"ok": true}\n```'},
            },
        })
        runtime = self._runtime(proc)
        # Manually trigger the lazy start path: run initialize via _start()
        with patch("subprocess.Popen", return_value=proc):
            text = runtime.call_codex_tool("ping")

        assert "ok" in text
        assert runtime.last_thread_id == "thr-1"

    def test_call_for_json_returns_parsed(self) -> None:
        proc = _FakeProc()
        proc.queue({"jsonrpc": "2.0", "id": 0, "result": {}})
        proc.queue({
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"results": [{"id": "p1"}]}'}],
                "structuredContent": {"threadId": "thr-x", "content": '{"results": [{"id": "p1"}]}'},
            },
        })
        runtime = self._runtime(proc)
        with patch("subprocess.Popen", return_value=proc):
            data = runtime.call_for_json("anything")
        assert data == {"results": [{"id": "p1"}]}

    def test_subsequent_call_uses_codex_reply(self) -> None:
        proc = _FakeProc()
        proc.queue({"jsonrpc": "2.0", "id": 0, "result": {}})
        proc.queue({
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{}'}],
                "structuredContent": {"threadId": "thr-a", "content": '{}'},
            },
        })
        proc.queue({
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "content": [{"type": "text", "text": '{}'}],
                "structuredContent": {"threadId": "thr-a", "content": '{}'},
            },
        })
        runtime = self._runtime(proc)
        with patch("subprocess.Popen", return_value=proc):
            runtime.call_codex_tool("first")
            runtime.call_codex_tool("second")

        # second call's stdin write should reference codex-reply
        all_writes = [c.args[0].decode() for c in proc.stdin.write.call_args_list]
        second_call = [w for w in all_writes if '"tools/call"' in w][1]
        assert "codex-reply" in second_call
        assert "thr-a" in second_call

    def test_init_error_raises(self) -> None:
        proc = _FakeProc()
        proc.queue({"jsonrpc": "2.0", "id": 0, "error": {"code": -1, "message": "boom"}})
        runtime = self._runtime(proc)
        with patch("subprocess.Popen", return_value=proc):
            with pytest.raises(CodexProxyError, match="initialize failed"):
                runtime.call_codex_tool("anything")

    def test_tool_call_error_raises(self) -> None:
        proc = _FakeProc()
        proc.queue({"jsonrpc": "2.0", "id": 0, "result": {}})
        proc.queue({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "fail"}})
        runtime = self._runtime(proc)
        with patch("subprocess.Popen", return_value=proc):
            with pytest.raises(CodexProxyError, match="codex tool error"):
                runtime.call_codex_tool("x")

    def test_subprocess_exit_raises(self) -> None:
        proc = _FakeProc()
        proc.queue({"jsonrpc": "2.0", "id": 0, "result": {}})
        proc._poll_value = 1  # exited before responding
        runtime = self._runtime(proc)
        with patch("subprocess.Popen", return_value=proc):
            with pytest.raises(CodexProxyError, match="exited"):
                runtime.call_codex_tool("x")

    def test_close_idempotent(self) -> None:
        runtime = CodexProxyRuntime()
        runtime.close()  # no proc to close
        runtime.close()


# ---------------------------------------------------------------------------
# ConfluenceCodexProxyAdapter
# ---------------------------------------------------------------------------

CONFLUENCE_CFG = {"server_name": "central_confluence", "timeout_seconds": 5}


class TestConfluenceAdapter:
    def _adapter(self):
        from framework.adapters.confluence.codex_proxy import ConfluenceCodexProxyAdapter
        adapter = ConfluenceCodexProxyAdapter(CONFLUENCE_CFG)
        adapter.runtime = MagicMock()
        return adapter

    def test_healthcheck_ok(self) -> None:
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {"tools": ["confluence_search", "confluence_get"]}
        report = adapter.healthcheck()
        assert report.healthy is True
        assert "confluence_search" in report.capabilities

    def test_healthcheck_bad_shape(self) -> None:
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {"oops": "no tools field"}
        report = adapter.healthcheck()
        assert report.healthy is False

    def test_healthcheck_runtime_error(self) -> None:
        adapter = self._adapter()
        adapter.runtime.call_for_json.side_effect = CodexProxyError("auth required")
        report = adapter.healthcheck()
        assert report.healthy is False
        assert "auth required" in report.notes

    def test_list_returns_refs(self) -> None:
        from framework.adapters._base import SourceQuery
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {
            "results": [
                {"id": "11", "title": "A", "updatedAt": "2024-01-01T00:00:00Z"},
                {"id": "22", "title": "B", "updatedAt": "2024-02-01T00:00:00Z"},
            ]
        }
        refs = list(adapter.list(SourceQuery(space="ENG")))
        assert [r.source_id for r in refs] == ["11", "22"]
        assert refs[0].last_modified == datetime.fromisoformat("2024-01-01T00:00:00+00:00")

    def test_list_requires_space(self) -> None:
        from framework.adapters._base import SourceQuery
        adapter = self._adapter()
        with pytest.raises(ValueError, match="space"):
            list(adapter.list(SourceQuery()))

    def test_fetch_returns_raw_item(self) -> None:
        from framework.adapters._base import RawItemRef
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {
            "id": "999",
            "title": "My Page",
            "space": {"key": "ENG"},
            "version": {"number": 3, "when": "2024-03-01T00:00:00Z"},
            "body": {"storage": {"value": "<p>hi</p>"}},
            "metadata": {"labels": {"results": [{"name": "runbook"}]}},
        }
        item = adapter.fetch(RawItemRef("confluence_page", "confluence", "999"))
        assert item.source_id == "999"
        assert item.metadata["title"] == "My Page"
        assert item.metadata["space"] == "ENG"
        assert item.metadata["labels"] == ["runbook"]

    def test_fetch_not_found_raises(self) -> None:
        from framework.adapters._base import RawItemRef
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {"error": "not_found"}
        with pytest.raises(FileNotFoundError):
            adapter.fetch(RawItemRef("confluence_page", "confluence", "missing"))

    def test_stream_changes(self) -> None:
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {
            "results": [{"id": "5", "updatedAt": "2024-04-01T12:00:00Z"}]
        }
        events = list(adapter.stream_changes(datetime(2024, 4, 1)))
        assert len(events) == 1
        assert events[0].source_id == "5"
        assert events[0].kind == "updated"

    def test_close_delegates(self) -> None:
        adapter = self._adapter()
        adapter.close()
        adapter.runtime.close.assert_called_once()


# ---------------------------------------------------------------------------
# JiraCodexProxyAdapter
# ---------------------------------------------------------------------------

JIRA_CFG = {"server_name": "central_jira", "timeout_seconds": 5}


class TestJiraAdapter:
    def _adapter(self):
        from framework.adapters.jira.codex_proxy import JiraCodexProxyAdapter
        adapter = JiraCodexProxyAdapter(JIRA_CFG)
        adapter.runtime = MagicMock()
        return adapter

    def test_healthcheck_ok(self) -> None:
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {"tools": ["jira_search", "jira_get_issue"]}
        report = adapter.healthcheck()
        assert report.healthy is True

    def test_list_returns_refs(self) -> None:
        from framework.adapters._base import SourceQuery
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {
            "issues": [
                {"key": "ENG-1", "updatedAt": "2024-01-01T00:00:00Z"},
                {"key": "ENG-2", "updatedAt": "2024-01-02T00:00:00Z"},
            ]
        }
        refs = list(adapter.list(SourceQuery(jql="project=ENG")))
        assert [r.source_id for r in refs] == ["ENG-1", "ENG-2"]

    def test_list_requires_jql(self) -> None:
        from framework.adapters._base import SourceQuery
        adapter = self._adapter()
        with pytest.raises(ValueError, match="jql"):
            list(adapter.list(SourceQuery()))

    def test_fetch_returns_raw_item(self) -> None:
        from framework.adapters._base import RawItemRef
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {
            "key": "ENG-42",
            "fields": {
                "summary": "Fix",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "High"},
                "creator": {"displayName": "Alice"},
                "assignee": {"displayName": "Bob"},
                "labels": ["backend"],
                "components": [{"name": "API"}],
                "project": {"key": "ENG"},
                "created": "2024-01-01T00:00:00Z",
                "updated": "2024-01-10T00:00:00Z",
            },
        }
        item = adapter.fetch(RawItemRef("jira_issue", "jira", "ENG-42"))
        assert item.source_id == "ENG-42"
        assert item.metadata["status"] == "In Progress"
        assert item.metadata["assignee"] == "Bob"
        assert item.metadata["components"] == ["API"]

    def test_fetch_not_found_raises(self) -> None:
        from framework.adapters._base import RawItemRef
        adapter = self._adapter()
        adapter.runtime.call_for_json.return_value = {"error": "not_found"}
        with pytest.raises(FileNotFoundError):
            adapter.fetch(RawItemRef("jira_issue", "jira", "GHOST-1"))


# ---------------------------------------------------------------------------
# Factory env guard
# ---------------------------------------------------------------------------

class TestFactoryGuard:
    def test_confluence_factory_allows_dev(self, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "dev")
        adapter = make_confluence_adapter({
            "mode": "codex_proxy",
            "codex_proxy": {"server_name": "central_confluence"},
        })
        assert adapter.mode == "codex_proxy"

    def test_confluence_factory_blocks_prod(self, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "prod")
        with pytest.raises(RuntimeError, match="laptop-only"):
            make_confluence_adapter({
                "mode": "codex_proxy",
                "codex_proxy": {"server_name": "central_confluence"},
            })

    def test_jira_factory_allows_laptop(self, monkeypatch) -> None:
        from framework.adapters.jira import make_jira_adapter
        monkeypatch.setenv("KBF_ENV", "laptop")
        adapter = make_jira_adapter({
            "mode": "codex_proxy",
            "codex_proxy": {"server_name": "central_jira"},
        })
        assert adapter.mode == "codex_proxy"

    def test_jira_factory_blocks_staging(self, monkeypatch) -> None:
        from framework.adapters.jira import make_jira_adapter
        monkeypatch.setenv("KBF_ENV", "staging")
        with pytest.raises(RuntimeError, match="laptop-only"):
            make_jira_adapter({
                "mode": "codex_proxy",
                "codex_proxy": {"server_name": "central_jira"},
            })
