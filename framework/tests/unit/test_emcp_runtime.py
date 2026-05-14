"""Unit tests for EmcpRuntime — direct Streamable HTTP MCP client.

Mocks the subprocess (keychain read) and urllib (HTTP call) so the tests
run without macOS Keychain access or network.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from framework.core.emcp_runtime import EmcpAuthError, EmcpError, EmcpRuntime


_FAKE_CRED = {
    "server_name": "central_confluence",
    "url": "https://emcp.example.com/v2",
    "client_id": "abc123",
    "token_response": {
        "access_token": "tok_abc",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "rt_xyz",
        "scope": "read",
    },
    "expires_at": int((time.time() + 3600) * 1000),  # 1 hour from now
}


def _mock_urlopen(status_code: int, body_bytes: bytes):
    """Build a MagicMock that behaves like urllib's response context manager."""
    resp = MagicMock()
    resp.status = status_code
    resp.read.return_value = body_bytes
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


@pytest.fixture
def runtime(monkeypatch):
    """An EmcpRuntime with the keychain subprocess mocked."""
    # Mock auto-discovery (so we don't read the real keychain)
    monkeypatch.setattr(
        EmcpRuntime, "_discover_keychain_account_suffix",
        staticmethod(lambda _: "deadbeef"),
    )
    # Mock the credential read
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *a, **kw: json.dumps(_FAKE_CRED),
    )
    return EmcpRuntime(server_name="central_confluence", timeout_s=5.0)


class TestKeychainAndCredential:
    def test_auto_discovers_suffix_when_omitted(self, monkeypatch):
        called: dict = {}

        def fake_discover(server_name):
            called["server_name"] = server_name
            return "abc123"

        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(fake_discover),
        )
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: json.dumps(_FAKE_CRED),
        )
        r = EmcpRuntime(server_name="central_confluence")
        assert r._keychain_account == "central_confluence|abc123"
        assert called["server_name"] == "central_confluence"

    def test_explicit_suffix_skips_discovery(self, monkeypatch):
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: pytest.fail("must not be called")),
        )
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: json.dumps(_FAKE_CRED),
        )
        r = EmcpRuntime(server_name="central_confluence",
                        keychain_account_suffix="explicit")
        assert r._keychain_account == "central_confluence|explicit"

    def test_missing_keychain_raises_auth_error(self, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: "ff"),
        )
        def boom(*a, **kw):
            raise sp.CalledProcessError(1, "security", stderr="not found")
        monkeypatch.setattr("subprocess.check_output", boom)
        r = EmcpRuntime(server_name="central_confluence")
        with pytest.raises(EmcpAuthError, match="keychain read failed"):
            r._load_credential()

    def test_malformed_credential_raises_auth_error(self, monkeypatch):
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: "ff"),
        )
        monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "not json")
        r = EmcpRuntime(server_name="central_confluence")
        with pytest.raises(EmcpAuthError, match="not JSON"):
            r._load_credential()


class TestSseParsing:
    def test_parses_sse_format(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n\n'
        result = EmcpRuntime._parse_sse_or_json(body)
        assert result == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}

    def test_parses_plain_json(self):
        body = '{"jsonrpc":"2.0","id":7,"result":{"ok":true}}'
        result = EmcpRuntime._parse_sse_or_json(body)
        assert result == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


class TestInitializeHandshake:
    def test_initialize_runs_once_and_sends_initialized_notification(self, runtime, monkeypatch):
        calls: list = []
        def fake_urlopen(req, timeout):
            body = req.data.decode()
            calls.append(json.loads(body))
            return _mock_urlopen(200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":0,"result":{"serverInfo":{"name":"X"}}}\n\n')
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        runtime._ensure_initialized()
        runtime._ensure_initialized()  # second call should be no-op

        methods = [c.get("method") for c in calls]
        assert methods == ["initialize", "notifications/initialized"]

    def test_initialize_failure_raises(self, runtime, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout: _mock_urlopen(
                200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":0,"error":{"code":-32603,"message":"nope"}}\n\n',
            ),
        )
        with pytest.raises(EmcpError, match="initialize failed"):
            runtime._ensure_initialized()


class TestCallTool:
    def test_call_tool_returns_result_dict(self, runtime, monkeypatch):
        responses = [
            # initialize
            b'event: message\ndata: {"jsonrpc":"2.0","id":0,"result":{"serverInfo":{}}}\n\n',
            # notifications/initialized
            b'',
            # tools/call response
            b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hi"}]}}\n\n',
        ]
        idx = {"i": 0}
        def fake_urlopen(req, timeout):
            i = idx["i"]; idx["i"] += 1
            return _mock_urlopen(200 if i != 1 else 202, responses[i])
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        result = runtime.call_tool("get_page", {"page_id": "123"})
        assert result == {"content": [{"type": "text", "text": "hi"}]}

    def test_call_tool_for_text_unwraps_text_block(self, runtime, monkeypatch):
        responses = [
            b'event: message\ndata: {"jsonrpc":"2.0","id":0,"result":{"serverInfo":{}}}\n\n',
            b'',
            b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"payload-string"}]}}\n\n',
        ]
        idx = {"i": 0}
        def fake_urlopen(req, timeout):
            i = idx["i"]; idx["i"] += 1
            return _mock_urlopen(200 if i != 1 else 202, responses[i])
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        text = runtime.call_tool_for_text("get_page", {"page_id": "123"})
        assert text == "payload-string"

    def test_call_tool_error_raises(self, runtime, monkeypatch):
        responses = [
            b'event: message\ndata: {"jsonrpc":"2.0","id":0,"result":{"serverInfo":{}}}\n\n',
            b'',
            b'event: message\ndata: {"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"bad request"}}\n\n',
        ]
        idx = {"i": 0}
        def fake_urlopen(req, timeout):
            i = idx["i"]; idx["i"] += 1
            return _mock_urlopen(200 if i != 1 else 202, responses[i])
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(EmcpError, match="bad request"):
            runtime.call_tool("get_page", {"page_id": "missing"})


class TestTokenRefreshOn401:
    def test_401_triggers_one_refresh_then_retries(self, monkeypatch):
        """If a tools/call returns 401, runtime should force-refresh from
        keychain (codex rotates the token) and retry exactly once."""
        # Track keychain reads — must happen twice (once initial, once after 401).
        keychain_reads = {"n": 0}
        def fake_check_output(*a, **kw):
            keychain_reads["n"] += 1
            return json.dumps(_FAKE_CRED)
        monkeypatch.setattr("subprocess.check_output", fake_check_output)
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: "deadbeef"),
        )
        runtime = EmcpRuntime(server_name="central_confluence", timeout_s=5.0)
        # Skip the initialize phase for this isolated _post test
        runtime._initialized = True

        # First call: 401. Second call: 200.
        import urllib.error
        call_n = {"i": 0}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            if call_n["i"] == 1:
                raise urllib.error.HTTPError(
                    runtime._cached_url, 401, "Unauthorized", {},
                    MagicMock(read=lambda: b"token expired"),
                )
            return _mock_urlopen(200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":42,"result":{"ok":true}}\n\n')
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        status, body = runtime._post({"jsonrpc":"2.0","id":42,"method":"tools/list"})
        assert status == 200
        assert call_n["i"] == 2, "should have retried exactly once after 401"
        # initial credential load (during _ensure_token) + force-refresh on 401 = 2
        assert keychain_reads["n"] == 2

    def test_second_401_does_not_loop(self, monkeypatch):
        """If even the post-refresh call also 401s, raise — don't loop."""
        monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: json.dumps(_FAKE_CRED))
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: "deadbeef"),
        )
        runtime = EmcpRuntime(server_name="central_confluence", timeout_s=5.0)
        runtime._initialized = True

        import urllib.error
        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                runtime._cached_url or "u", 401, "Unauthorized", {},
                MagicMock(read=lambda: b"expired"),
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(EmcpError, match="HTTP 401"):
            runtime._post({"jsonrpc":"2.0","id":1,"method":"tools/list"})
