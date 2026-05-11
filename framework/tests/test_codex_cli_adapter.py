"""Comprehensive tests for the Codex CLI stdio MCP transport adapters.

Coverage:
  - config.toml parsing: find server by name, missing server error, missing file error
  - _spawn(): subprocess.Popen called with correct args/env, initialize handshake
  - _rpc(): JSON-RPC framing — request written, response correlated by id, timeout
  - healthcheck(): tools/list probe, missing capabilities, server not found
  - Confluence list(): mock _call_tool, verify RawItemRef output, pagination
  - Confluence fetch(): mock _call_tool, verify RawItem via normalize()
  - Confluence stream_changes(): polling via search tool
  - Confluence discover(): delegates to list()
  - Jira list(): mock _call_tool, verify RawItemRef output, pagination
  - Jira fetch(): mock _call_tool, verify RawItem via normalize()
  - Jira stream_changes(): polling via list()
  - Jira discover(): delegates to list()
  - close(): terminates subprocess
  - context manager: close called on __exit__
  - Environment guard: KBF_ENV=prod raises RuntimeError in factory
  - codex not installed (config.toml missing) -> clear error
  - server not configured -> clear error
  - id mismatch in _rpc -> RuntimeError
  - JSON-RPC error in response -> RuntimeError
  - stdout closed unexpectedly -> RuntimeError
"""
from __future__ import annotations

import io
import json
import os
import textwrap
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to build fake subprocess stdout payloads
# ---------------------------------------------------------------------------

def _encode(*messages: dict) -> bytes:
    """Concatenate newline-delimited JSON messages as bytes."""
    return b"".join((json.dumps(m) + "\n").encode() for m in messages)


def _init_ok_resp() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "confluence-mcp", "version": "1.0.0"},
            "capabilities": {},
        },
    }


def _tool_list_resp(seq_id: int, tool_names: list[str]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": seq_id,
        "result": {"tools": [{"name": n} for n in tool_names]},
    }


def _tool_call_resp(seq_id: int, content: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": seq_id,
        "result": content,
    }


# ---------------------------------------------------------------------------
# Config.toml fixtures
# ---------------------------------------------------------------------------

SAMPLE_TOML = textwrap.dedent("""\
    [[mcpServers]]
    name = "confluence"
    command = "npx"
    args = ["-y", "@company/confluence-mcp-server"]

    [mcpServers.env]
    CONFLUENCE_URL = "https://confluence.example.internal"
    CONFLUENCE_TOKEN = "tok-abc"

    [[mcpServers]]
    name = "jira"
    command = "npx"
    args = ["-y", "@company/jira-mcp-server"]

    [mcpServers.env]
    JIRA_URL = "https://jira.example.internal"
    JIRA_TOKEN = "tok-xyz"
""")

CONFLUENCE_CFG = {
    "server_name": "confluence",
    "tool_map": {
        "list_pages_in_space": "confluence.list_pages",
        "get_page_by_id": "confluence.get_page",
        "search": "confluence.search",
    },
    "required_capabilities": ["list_pages_in_space", "get_page_by_id"],
    "timeout_seconds": 5,
    "max_retries": 1,
}

JIRA_CFG = {
    "server_name": "jira",
    "tool_map": {
        "search_issues": "jira.search",
        "get_issue": "jira.get_issue",
        "list_comments": "jira.list_comments",
    },
    "required_capabilities": ["search_issues", "get_issue"],
    "timeout_seconds": 5,
    "max_retries": 1,
}


@pytest.fixture
def toml_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(SAMPLE_TOML)
    return p


# ---------------------------------------------------------------------------
# Utility: build a mock Popen that replays a sequence of JSON-RPC messages
# ---------------------------------------------------------------------------

class _FakeStdout:
    """File-like object whose readline() returns one pre-loaded JSON message at a time."""

    def __init__(self, messages: list[dict]) -> None:
        self._lines = [json.dumps(m) + "\n" for m in messages]
        self._pos = 0

    def readline(self) -> bytes:
        if self._pos >= len(self._lines):
            return b""  # EOF
        line = self._lines[self._pos].encode()
        self._pos += 1
        return line


def _make_proc(responses: list[dict]) -> MagicMock:
    """Return a mock Popen whose stdout replays the given sequence of JSON-RPC responses."""
    proc = MagicMock()
    proc.poll.return_value = None  # still alive
    proc.pid = 12345
    proc.stdout = _FakeStdout(responses)
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.flush = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# ConfluenceCodexCliAdapter tests
# ---------------------------------------------------------------------------

class TestConfluenceConfigTomlParsing:
    def test_finds_server_by_name(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        entry = adapter._load_server_entry()
        assert entry["name"] == "confluence"
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "@company/confluence-mcp-server"]

    def test_missing_server_raises_key_error(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter(
            {**CONFLUENCE_CFG, "server_name": "nonexistent", "config_path": str(toml_path)}
        )
        with pytest.raises(KeyError, match="nonexistent"):
            adapter._load_server_entry()

    def test_missing_config_file_raises_file_not_found(self, tmp_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter(
            {**CONFLUENCE_CFG, "config_path": str(tmp_path / "does_not_exist.toml")}
        )
        with pytest.raises(FileNotFoundError, match="Codex config not found"):
            adapter._load_server_entry()

    def test_error_message_mentions_codex_install(self, tmp_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter(
            {**CONFLUENCE_CFG, "config_path": str(tmp_path / "no.toml")}
        )
        with pytest.raises(FileNotFoundError, match="npm install"):
            adapter._load_server_entry()


class TestConfluenceSpawn:
    def test_spawn_calls_popen_with_correct_args(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        responses = [_init_ok_resp()]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter._spawn()

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "npx"
        assert "-y" in cmd
        assert "@company/confluence-mcp-server" in cmd

    def test_spawn_merges_env_from_toml(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        responses = [_init_ok_resp()]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter._spawn()

        passed_env = mock_popen.call_args[1]["env"]
        assert passed_env["CONFLUENCE_URL"] == "https://confluence.example.internal"
        assert passed_env["CONFLUENCE_TOKEN"] == "tok-abc"

    def test_spawn_sends_initialize_request(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        responses = [_init_ok_resp()]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc):
            adapter._spawn()

        # First write should be the initialize request
        first_write = mock_proc.stdin.write.call_args_list[0][0][0]
        msg = json.loads(first_write.decode())
        assert msg["method"] == "initialize"
        assert msg["id"] == 0
        assert msg["params"]["protocolVersion"] == "2024-11-05"

    def test_spawn_sends_initialized_notification(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        responses = [_init_ok_resp()]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc):
            adapter._spawn()

        # Second write should be the initialized notification (no id)
        second_write = mock_proc.stdin.write.call_args_list[1][0][0]
        notif = json.loads(second_write.decode())
        assert notif["method"] == "notifications/initialized"
        assert "id" not in notif

    def test_spawn_sets_seq_to_one_after_init(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        responses = [_init_ok_resp()]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc):
            adapter._spawn()

        assert adapter._seq == 1

    def test_spawn_raises_on_initialize_error(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        error_resp = {"jsonrpc": "2.0", "id": 0, "error": {"code": -32600, "message": "Bad request"}}
        mock_proc = _make_proc([error_resp])

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="MCP initialize failed"):
                adapter._spawn()

    def test_spawn_no_op_if_process_already_running(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        adapter._proc = MagicMock()
        adapter._proc.poll.return_value = None  # still alive

        with patch("subprocess.Popen") as mock_popen:
            adapter._spawn()

        mock_popen.assert_not_called()


class TestRpc:
    def _adapter_with_proc(self, responses: list[dict], toml_path: Path):
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        adapter._proc = _make_proc(responses)
        adapter._seq = 1  # post-initialize state
        return adapter

    def test_rpc_sends_correct_json_rpc_request(self, toml_path: Path) -> None:
        resp = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        adapter = self._adapter_with_proc([resp], toml_path)
        adapter._rpc("tools/list", {})
        written = adapter._proc.stdin.write.call_args_list[0][0][0]
        msg = json.loads(written.decode())
        assert msg["jsonrpc"] == "2.0"
        assert msg["method"] == "tools/list"
        assert msg["id"] == 1
        assert msg["params"] == {}

    def test_rpc_increments_seq(self, toml_path: Path) -> None:
        resps = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
        ]
        adapter = self._adapter_with_proc(resps, toml_path)
        adapter._rpc("tools/list", {})
        adapter._rpc("tools/list", {})
        assert adapter._seq == 3

    def test_rpc_raises_on_id_mismatch(self, toml_path: Path) -> None:
        wrong_id_resp = {"jsonrpc": "2.0", "id": 999, "result": {}}
        adapter = self._adapter_with_proc([wrong_id_resp], toml_path)
        with pytest.raises(RuntimeError, match="RPC id mismatch"):
            adapter._rpc("tools/list", {})

    def test_rpc_raises_on_jsonrpc_error(self, toml_path: Path) -> None:
        error_resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
        adapter = self._adapter_with_proc([error_resp], toml_path)
        with pytest.raises(RuntimeError, match="JSON-RPC error"):
            adapter._rpc("tools/list", {})

    def test_rpc_raises_on_empty_stdout(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        proc = MagicMock()
        proc.poll.return_value = 1  # exited
        proc.stdout = _FakeStdout([])  # EOF immediately
        proc.stdin = MagicMock()
        adapter._proc = proc
        adapter._seq = 1

        with pytest.raises(RuntimeError, match="closed stdout unexpectedly"):
            adapter._rpc("tools/list", {})


class TestConfluenceHealthcheck:
    def test_healthcheck_ok(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        # Responses: initialize + tools/list
        responses = [
            _init_ok_resp(),
            _tool_list_resp(1, ["confluence.list_pages", "confluence.get_page", "confluence.search"]),
        ]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc):
            report = adapter.healthcheck()

        assert report.healthy is True
        assert "confluence.list_pages" in report.capabilities

    def test_healthcheck_missing_capability(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        # tools/list returns only search — missing list_pages and get_page
        responses = [
            _init_ok_resp(),
            _tool_list_resp(1, ["confluence.search"]),
        ]
        mock_proc = _make_proc(responses)

        with patch("subprocess.Popen", return_value=mock_proc):
            report = adapter.healthcheck()

        assert report.healthy is False
        assert "missing" in report.notes.lower()

    def test_healthcheck_config_missing(self, tmp_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter(
            {**CONFLUENCE_CFG, "config_path": str(tmp_path / "missing.toml")}
        )
        report = adapter.healthcheck()
        assert report.healthy is False
        assert "Codex config not found" in report.notes

    def test_healthcheck_server_not_configured(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter(
            {**CONFLUENCE_CFG, "server_name": "ghost", "config_path": str(toml_path)}
        )
        report = adapter.healthcheck()
        assert report.healthy is False
        assert "ghost" in report.notes


class TestConfluenceList:
    def test_list_returns_raw_item_refs(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import SourceQuery, RawItemRef
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})

        pages_result = {
            "results": [
                {"id": "111", "title": "Page A", "version": {"when": "2024-01-01T00:00:00Z"}},
                {"id": "222", "title": "Page B", "version": {"when": "2024-02-01T00:00:00Z"}},
            ]
        }
        with patch.object(adapter, "_call_tool", return_value=pages_result):
            refs = list(adapter.list(SourceQuery(space="ENG")))

        assert len(refs) == 2
        assert all(isinstance(r, RawItemRef) for r in refs)
        assert refs[0].source_id == "111"
        assert refs[1].source_id == "222"
        assert refs[0].kind == "confluence_page"
        assert refs[0].source == "confluence"

    def test_list_requires_space(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import SourceQuery
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        with pytest.raises(ValueError, match="space"):
            list(adapter.list(SourceQuery()))

    def test_list_paginates(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import SourceQuery
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})

        page1 = {"results": [{"id": "1", "version": {}}], "nextStart": 50}
        page2 = {"results": [{"id": "2", "version": {}}]}

        with patch.object(adapter, "_call_tool", side_effect=[page1, page2]) as mock_call:
            refs = list(adapter.list(SourceQuery(space="ENG")))

        assert len(refs) == 2
        assert mock_call.call_count == 2
        # Second call should include "start": 50
        second_args = mock_call.call_args_list[1][0][1]
        assert second_args.get("start") == 50

    def test_list_passes_labels(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import SourceQuery
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})

        empty_result: dict = {"results": []}
        with patch.object(adapter, "_call_tool", return_value=empty_result) as mock_call:
            list(adapter.list(SourceQuery(space="ENG", labels_include=["runbook"])))

        call_args = mock_call.call_args_list[0][0][1]
        assert call_args.get("labels") == ["runbook"]


class TestConfluenceFetch:
    def test_fetch_returns_raw_item(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import RawItemRef, RawItem
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        ref = RawItemRef(kind="confluence_page", source="confluence", source_id="999")

        page_payload = {
            "id": "999",
            "title": "My Page",
            "body": {"storage": {"value": "<p>hello</p>"}},
            "space": {"key": "ENG"},
            "version": {"number": 3, "when": "2024-03-01T00:00:00Z"},
            "metadata": {"labels": {"results": [{"name": "runbook"}]}},
        }
        with patch.object(adapter, "_call_tool", return_value=page_payload):
            item = adapter.fetch(ref)

        assert isinstance(item, RawItem)
        assert item.source_id == "999"
        assert item.kind == "confluence_page"
        assert item.metadata["title"] == "My Page"
        assert item.metadata["space"] == "ENG"
        assert item.metadata["labels"] == ["runbook"]

    def test_fetch_calls_correct_tool(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import RawItemRef
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        ref = RawItemRef(kind="confluence_page", source="confluence", source_id="42")

        page_payload = {"id": "42", "title": "T", "body": {}, "version": {}, "metadata": {}}
        with patch.object(adapter, "_call_tool", return_value=page_payload) as mock_call:
            adapter.fetch(ref)

        mock_call.assert_called_once_with(
            "confluence.get_page",
            {"pageId": "42", "expand": "body.storage,metadata.labels"},
        )


class TestConfluenceStreamChanges:
    def test_stream_changes_yields_change_events(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import ChangeEvent
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})

        search_result = {
            "results": [
                {"id": "10", "version": {"when": "2024-04-01T12:00:00Z"}},
                {"id": "20", "version": {"when": "2024-04-02T08:00:00Z"}},
            ]
        }
        with patch.object(adapter, "_call_tool", return_value=search_result):
            events = list(adapter.stream_changes(datetime(2024, 4, 1)))

        assert len(events) == 2
        assert all(isinstance(e, ChangeEvent) for e in events)
        assert events[0].source_id == "10"
        assert events[0].kind == "updated"

    def test_stream_changes_no_search_tool_returns_empty(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        cfg_no_search = {**CONFLUENCE_CFG, "tool_map": {
            "list_pages_in_space": "confluence.list_pages",
            "get_page_by_id": "confluence.get_page",
        }}
        adapter = ConfluenceCodexCliAdapter({**cfg_no_search, "config_path": str(toml_path)})
        events = list(adapter.stream_changes(datetime(2024, 1, 1)))
        assert events == []


class TestConfluenceDiscover:
    def test_discover_delegates_to_list(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        from framework.adapters._base import RawItemRef
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})

        ref = RawItemRef(kind="confluence_page", source="confluence", source_id="7")
        with patch.object(adapter, "list", return_value=iter([ref])) as mock_list:
            refs = list(adapter.discover([{"space": "OPS", "labels_include": ["oncall"]}]))

        mock_list.assert_called_once()
        assert refs == [ref]


class TestConfluenceClose:
    def test_close_terminates_subprocess(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        proc = MagicMock()
        adapter._proc = proc
        adapter.close()
        proc.terminate.assert_called_once()

    def test_close_sets_proc_to_none(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        adapter._proc = MagicMock()
        adapter.close()
        assert adapter._proc is None

    def test_close_idempotent_when_proc_is_none(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        adapter.close()  # proc is already None — should not raise

    def test_context_manager_closes_on_exit(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        proc = MagicMock()
        adapter._proc = proc
        with adapter:
            pass
        proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# JiraCodexCliAdapter tests
# ---------------------------------------------------------------------------

class TestJiraConfigTomlParsing:
    def test_finds_jira_server_by_name(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        entry = adapter._load_server_entry()
        assert entry["name"] == "jira"
        assert entry["command"] == "npx"

    def test_missing_jira_server_raises_key_error(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter(
            {**JIRA_CFG, "server_name": "missing-jira", "config_path": str(toml_path)}
        )
        with pytest.raises(KeyError, match="missing-jira"):
            adapter._load_server_entry()

    def test_missing_config_file_raises_file_not_found(self, tmp_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter(
            {**JIRA_CFG, "config_path": str(tmp_path / "no.toml")}
        )
        with pytest.raises(FileNotFoundError, match="Codex config not found"):
            adapter._load_server_entry()


class TestJiraHealthcheck:
    def test_healthcheck_ok(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        responses = [
            _init_ok_resp(),
            _tool_list_resp(1, ["jira.search", "jira.get_issue", "jira.list_comments"]),
        ]
        mock_proc = _make_proc(responses)
        with patch("subprocess.Popen", return_value=mock_proc):
            report = adapter.healthcheck()
        assert report.healthy is True
        assert report.mode == "codex_cli"

    def test_healthcheck_missing_capability(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        # Only search present, get_issue missing
        responses = [
            _init_ok_resp(),
            _tool_list_resp(1, ["jira.search"]),
        ]
        mock_proc = _make_proc(responses)
        with patch("subprocess.Popen", return_value=mock_proc):
            report = adapter.healthcheck()
        assert report.healthy is False
        assert "missing" in report.notes.lower()


class TestJiraList:
    def test_list_returns_raw_item_refs(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import SourceQuery, RawItemRef
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})

        issues_result = {
            "issues": [
                {"key": "ENG-1", "fields": {"updated": "2024-01-01T00:00:00Z"}},
                {"key": "ENG-2", "fields": {"updated": "2024-01-02T00:00:00Z"}},
            ]
        }
        with patch.object(adapter, "_call_tool", return_value=issues_result):
            refs = list(adapter.list(SourceQuery(jql="project=ENG")))

        assert len(refs) == 2
        assert all(isinstance(r, RawItemRef) for r in refs)
        assert refs[0].source_id == "ENG-1"
        assert refs[0].kind == "jira_issue"
        assert refs[0].source == "jira"

    def test_list_requires_jql(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import SourceQuery
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        with pytest.raises(ValueError, match="jql"):
            list(adapter.list(SourceQuery()))

    def test_list_paginates_with_start_at(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import SourceQuery
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})

        page1 = {"issues": [{"key": "ENG-1", "fields": {}}], "nextCursor": "100"}
        page2 = {"issues": [{"key": "ENG-2", "fields": {}}]}

        with patch.object(adapter, "_call_tool", side_effect=[page1, page2]) as mock_call:
            refs = list(adapter.list(SourceQuery(jql="project=ENG")))

        assert len(refs) == 2
        second_args = mock_call.call_args_list[1][0][1]
        assert second_args.get("startAt") == "100"


class TestJiraFetch:
    def test_fetch_returns_raw_item(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import RawItemRef, RawItem
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        ref = RawItemRef(kind="jira_issue", source="jira", source_id="ENG-42")

        issue_payload = {
            "key": "ENG-42",
            "fields": {
                "summary": "Fix the bug",
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
        with patch.object(adapter, "_call_tool", return_value=issue_payload):
            item = adapter.fetch(ref)

        assert isinstance(item, RawItem)
        assert item.source_id == "ENG-42"
        assert item.kind == "jira_issue"
        assert item.metadata["status"] == "In Progress"
        assert item.metadata["assignee"] == "Bob"
        assert item.metadata["labels"] == ["backend"]

    def test_fetch_calls_correct_tool(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import RawItemRef
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        ref = RawItemRef(kind="jira_issue", source="jira", source_id="OPS-5")

        payload = {"key": "OPS-5", "fields": {}}
        with patch.object(adapter, "_call_tool", return_value=payload) as mock_call:
            adapter.fetch(ref)

        mock_call.assert_called_once_with(
            "jira.get_issue",
            {"issueIdOrKey": "OPS-5", "expand": "changelog,comments"},
        )

    def test_fetch_normalizes_mcp_wrapped_response(self, toml_path: Path) -> None:
        """When MCP tool returns content[0].text (wrapped JSON), normalize should unwrap."""
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import RawItemRef
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        ref = RawItemRef(kind="jira_issue", source="jira", source_id="ENG-99")

        inner = {"key": "ENG-99", "fields": {"summary": "Wrapped", "status": {"name": "Open"}}}
        wrapped_payload = {"content": [{"text": json.dumps(inner)}]}
        with patch.object(adapter, "_call_tool", return_value=wrapped_payload):
            item = adapter.fetch(ref)

        assert item.source_id == "ENG-99"
        assert item.metadata["status"] == "Open"


class TestJiraStreamChanges:
    def test_stream_changes_yields_change_events(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import ChangeEvent, RawItemRef
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})

        refs = [
            RawItemRef("jira_issue", "jira", "ENG-1", datetime(2024, 5, 1, 10)),
            RawItemRef("jira_issue", "jira", "ENG-2", datetime(2024, 5, 2, 8)),
        ]
        with patch.object(adapter, "list", return_value=iter(refs)):
            events = list(adapter.stream_changes(datetime(2024, 5, 1)))

        assert len(events) == 2
        assert all(isinstance(e, ChangeEvent) for e in events)
        assert events[0].source_id == "ENG-1"


class TestJiraDiscover:
    def test_discover_delegates_to_list(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        from framework.adapters._base import RawItemRef
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})

        ref = RawItemRef(kind="jira_issue", source="jira", source_id="ENG-3")
        with patch.object(adapter, "list", return_value=iter([ref])) as mock_list:
            refs = list(adapter.discover([{"jql": "project=ENG AND priority=High"}]))

        mock_list.assert_called_once()
        assert refs == [ref]


class TestJiraClose:
    def test_close_terminates_subprocess(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        proc = MagicMock()
        adapter._proc = proc
        adapter.close()
        proc.terminate.assert_called_once()

    def test_context_manager_closes_on_exit(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        proc = MagicMock()
        adapter._proc = proc
        with adapter:
            pass
        proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Factory / environment guard tests
# ---------------------------------------------------------------------------

class TestFactoryEnvironmentGuard:
    def test_confluence_factory_allows_dev_env(self, toml_path: Path, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "dev")
        cfg = {"mode": "codex_cli", "codex_cli": {**CONFLUENCE_CFG, "config_path": str(toml_path)}}
        adapter = make_confluence_adapter(cfg)
        assert adapter.mode == "codex_cli"

    def test_confluence_factory_allows_laptop_env(self, toml_path: Path, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "laptop")
        cfg = {"mode": "codex_cli", "codex_cli": {**CONFLUENCE_CFG, "config_path": str(toml_path)}}
        adapter = make_confluence_adapter(cfg)
        assert adapter.mode == "codex_cli"

    def test_confluence_factory_blocks_staging(self, toml_path: Path, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "staging")
        cfg = {"mode": "codex_cli", "codex_cli": {**CONFLUENCE_CFG, "config_path": str(toml_path)}}
        with pytest.raises(RuntimeError, match="laptop-only"):
            make_confluence_adapter(cfg)

    def test_confluence_factory_blocks_prod(self, toml_path: Path, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "prod")
        cfg = {"mode": "codex_cli", "codex_cli": {**CONFLUENCE_CFG, "config_path": str(toml_path)}}
        with pytest.raises(RuntimeError, match="laptop-only"):
            make_confluence_adapter(cfg)

    def test_jira_factory_allows_dev_env(self, toml_path: Path, monkeypatch) -> None:
        from framework.adapters.jira import make_jira_adapter
        monkeypatch.setenv("KBF_ENV", "dev")
        cfg = {"mode": "codex_cli", "codex_cli": {**JIRA_CFG, "config_path": str(toml_path)}}
        adapter = make_jira_adapter(cfg)
        assert adapter.mode == "codex_cli"

    def test_jira_factory_blocks_prod(self, toml_path: Path, monkeypatch) -> None:
        from framework.adapters.jira import make_jira_adapter
        monkeypatch.setenv("KBF_ENV", "prod")
        cfg = {"mode": "codex_cli", "codex_cli": {**JIRA_CFG, "config_path": str(toml_path)}}
        with pytest.raises(RuntimeError, match="laptop-only"):
            make_jira_adapter(cfg)

    def test_confluence_factory_unknown_mode_raises(self, monkeypatch) -> None:
        from framework.adapters.confluence import make_confluence_adapter
        monkeypatch.setenv("KBF_ENV", "dev")
        with pytest.raises(ValueError, match="unknown confluence mode"):
            make_confluence_adapter({"mode": "telepathy"})

    def test_jira_factory_unknown_mode_raises(self, monkeypatch) -> None:
        from framework.adapters.jira import make_jira_adapter
        monkeypatch.setenv("KBF_ENV", "dev")
        with pytest.raises(ValueError, match="unknown jira mode"):
            make_jira_adapter({"mode": "telepathy"})


# ---------------------------------------------------------------------------
# Normalize edge cases
# ---------------------------------------------------------------------------

class TestConfluenceNormalize:
    def test_normalize_top_level_page(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        payload = {
            "id": "5",
            "title": "Direct Page",
            "body": {"storage": {}},
            "space": {"key": "DEMO"},
            "version": {"number": 1, "when": "2024-01-01T00:00:00Z"},
            "metadata": {"labels": {"results": [{"name": "alpha"}, {"name": "beta"}]}},
        }
        item = adapter.normalize(payload, "5")
        assert item.metadata["title"] == "Direct Page"
        assert item.metadata["labels"] == ["alpha", "beta"]

    def test_normalize_wrapped_content_text(self, toml_path: Path) -> None:
        from framework.adapters.confluence.codex_cli import ConfluenceCodexCliAdapter
        adapter = ConfluenceCodexCliAdapter({**CONFLUENCE_CFG, "config_path": str(toml_path)})
        inner = {
            "id": "6", "title": "Wrapped", "body": {},
            "space": {"key": "X"},
            "version": {"number": 2, "when": None},
            "metadata": {},
        }
        wrapped = {"content": [{"text": json.dumps(inner)}]}
        item = adapter.normalize(wrapped, "6")
        assert item.metadata["title"] == "Wrapped"


class TestJiraNormalize:
    def test_normalize_fields_present(self, toml_path: Path) -> None:
        from framework.adapters.jira.codex_cli import JiraCodexCliAdapter
        adapter = JiraCodexCliAdapter({**JIRA_CFG, "config_path": str(toml_path)})
        payload = {
            "key": "ABC-1",
            "fields": {
                "summary": "Fix it",
                "status": {"name": "Done"},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "Critical"},
                "creator": {"displayName": "Alice"},
                "assignee": None,
                "labels": ["urgent"],
                "components": [],
                "project": {"key": "ABC"},
                "created": "2024-01-01T00:00:00Z",
                "updated": "2024-06-01T00:00:00Z",
            },
        }
        item = adapter.normalize(payload)
        assert item.source_id == "ABC-1"
        assert item.metadata["status"] == "Done"
        assert item.metadata["priority"] == "Critical"
        assert item.metadata["assignee"] is None
