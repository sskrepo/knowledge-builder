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


class TestTransientRetry:
    """Retry-with-backoff on transient HTTP/network errors.

    Real-world trigger (session synth-tpm-bcbc739d): emcp.oracle.com returned
    HTTP 504 Gateway Timeout for a get_page call that worked fine 30 min earlier
    and again 30 min later. Single-shot failure would unnecessarily kill an
    ingest that a 2-second retry would have completed. Same for transient
    network blips (DNS hiccup, TCP reset, etc.).
    """

    @pytest.fixture
    def runtime(self, monkeypatch):
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: "deadbeef"),
        )
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: json.dumps(_FAKE_CRED),
        )
        # Suppress real sleeps so tests run fast.
        monkeypatch.setattr("time.sleep", lambda _s: None)
        r = EmcpRuntime(
            server_name="central_confluence", timeout_s=5.0,
            max_attempts=3, retry_backoffs_s=(0.01, 0.01),
        )
        r._initialized = True  # skip the init handshake for unit tests
        return r

    def test_504_then_200_succeeds_on_second_attempt(self, runtime, monkeypatch):
        """Transient 504 from emcp gateway → retry → success."""
        import urllib.error
        call_n = {"i": 0}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            if call_n["i"] == 1:
                raise urllib.error.HTTPError(
                    "https://emcp.test/v2", 504, "Gateway Timeout", {},
                    MagicMock(read=lambda: b'{"code":504,"message":"Gateway Timeout"}'),
                )
            return _mock_urlopen(
                200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n\n',
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        status, body = runtime._post({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        assert status == 200
        assert call_n["i"] == 2, "should have retried exactly once after 504"

    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
    def test_each_transient_status_triggers_retry(
        self, runtime, monkeypatch, status_code,
    ):
        """Every status in the transient-retry set must trigger a retry."""
        import urllib.error
        call_n = {"i": 0}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            if call_n["i"] == 1:
                raise urllib.error.HTTPError(
                    "https://emcp.test/v2", status_code, "err", {},
                    MagicMock(read=lambda: b"upstream slow"),
                )
            return _mock_urlopen(
                200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        status, _ = runtime._post({"jsonrpc":"2.0","id":1,"method":"tools/list"})
        assert status == 200
        assert call_n["i"] == 2, f"HTTP {status_code} must be retried"

    @pytest.mark.parametrize("status_code", [400, 403, 404, 405, 409, 422])
    def test_non_transient_status_does_not_retry(
        self, runtime, monkeypatch, status_code,
    ):
        """4xx errors that are NOT in the transient set must fail immediately
        — retrying a 404 forever wastes time and may hit rate limits."""
        import urllib.error
        call_n = {"i": 0}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            raise urllib.error.HTTPError(
                "https://emcp.test/v2", status_code, "err", {},
                MagicMock(read=lambda: b"client error"),
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(EmcpError, match=f"HTTP {status_code}"):
            runtime._post({"jsonrpc":"2.0","id":1,"method":"tools/list"})
        assert call_n["i"] == 1, (
            f"non-transient HTTP {status_code} must NOT be retried "
            f"(got {call_n['i']} attempts)"
        )

    def test_network_error_triggers_retry(self, runtime, monkeypatch):
        """URLError (DNS, connection refused, socket timeout) is transient."""
        import urllib.error, socket
        call_n = {"i": 0}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            if call_n["i"] < 3:
                raise urllib.error.URLError(socket.timeout("timed out"))
            return _mock_urlopen(
                200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        status, _ = runtime._post({"jsonrpc":"2.0","id":1,"method":"tools/list"})
        assert status == 200
        assert call_n["i"] == 3, "should have retried twice then succeeded"

    def test_exhausted_retries_raise_with_diagnostic(self, runtime, monkeypatch):
        """After max_attempts of 504, raise EmcpError with status+body context."""
        import urllib.error
        call_n = {"i": 0}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            raise urllib.error.HTTPError(
                "https://emcp.test/v2", 504, "Gateway Timeout", {},
                MagicMock(read=lambda: b'{"code":504,"message":"Gateway Timeout"}'),
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(EmcpError, match=r"HTTP 504.*after 3 attempts"):
            runtime._post({"jsonrpc":"2.0","id":1,"method":"tools/list"})
        assert call_n["i"] == 3, "exactly max_attempts (3) attempts made"

    def test_401_then_transient_then_success_composes_correctly(
        self, runtime, monkeypatch,
    ):
        """If the very first response is 401 (run refresh) and the post-refresh
        retry hits a 504 (transient), the runtime should refresh AND apply
        transient retries — both budgets compose. Server-side 401 with a
        keychain bundle that LOOKS valid (the case force=True handles)."""
        import urllib.error
        runtime._token_endpoint_cache = "https://idp.test/token"
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stderr=""),
        )

        call_n = {"i": 0, "log": []}
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            call_n["log"].append(req.full_url)
            url = req.full_url
            # Call 1: tools/list returns 401
            if call_n["i"] == 1:
                raise urllib.error.HTTPError(
                    url, 401, "Unauthorized", {},
                    MagicMock(read=lambda: b'{"error":"invalid_token"}'),
                )
            # Call 2: refresh-token grant returns new tokens
            if "/token" in url:
                return _mock_urlopen(200, json.dumps({
                    "access_token":"tok_new","refresh_token":"rt_new",
                    "token_type":"Bearer","expires_in":3600,"scope":"",
                }).encode())
            # Call 3: post-refresh retry of tools/list returns 504 (transient)
            if call_n["i"] == 3:
                raise urllib.error.HTTPError(
                    url, 504, "Gateway Timeout", {},
                    MagicMock(read=lambda: b'{"code":504}'),
                )
            # Call 4: transient retry returns 200
            return _mock_urlopen(
                200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n',
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        status, _ = runtime._post({"jsonrpc":"2.0","id":1,"method":"tools/list"})
        assert status == 200
        # 1 initial (401) + refresh-grant + 1 retry (504) + 1 retry (200) = 4
        assert call_n["i"] == 4, (
            f"expected 4 HTTP calls (init + refresh + 2 retries), got {call_n}"
        )


class TestOAuthRefresh:
    """OAuth refresh-token grant flow — the IdP rotates refresh tokens
    on every use, so the runtime must (a) serialise refreshes, (b) write
    the new bundle back to keychain, and (c) handle 401s by running the
    grant rather than just re-reading keychain."""

    @pytest.fixture
    def runtime_with_real_refresh(self, monkeypatch):
        """A runtime whose _refresh_access_token is the real implementation,
        but with HTTP and keychain subprocesses mocked."""
        monkeypatch.setattr(
            EmcpRuntime, "_discover_keychain_account_suffix",
            staticmethod(lambda _: "deadbeef"),
        )
        return EmcpRuntime(server_name="central_confluence", timeout_s=5.0)

    def _make_expired_bundle(self):
        b = json.loads(json.dumps(_FAKE_CRED))
        b["expires_at"] = int((time.time() - 60) * 1000)  # already expired
        return b

    def test_401_triggers_real_oauth_refresh_with_keychain_writeback(
        self, runtime_with_real_refresh, monkeypatch,
    ):
        """The canonical happy path:
          1. tools/call returns 401
          2. runtime runs refresh_token grant against the IdP
          3. new bundle is written back to keychain
          4. retry succeeds with new access_token
        """
        runtime = runtime_with_real_refresh
        runtime._initialized = True

        # Keychain reads return an expired bundle (forcing refresh).
        keychain_reads = {"n": 0}
        keychain_writes: list[dict] = []
        def fake_check_output(*a, **kw):
            keychain_reads["n"] += 1
            return json.dumps(self._make_expired_bundle())
        def fake_subprocess_run(*a, **kw):
            # capture the new bundle being written via `security add-... -w <json>`
            argv = a[0] if a else kw.get("args")
            # argv: ["security","add-generic-password","-U","-s",svc,"-a",acct,"-w",json]
            idx_w = argv.index("-w")
            keychain_writes.append(json.loads(argv[idx_w + 1]))
            return MagicMock(returncode=0, stderr="")
        monkeypatch.setattr("subprocess.check_output", fake_check_output)
        monkeypatch.setattr("subprocess.run", fake_subprocess_run)
        # Cache token endpoint so we skip the well-known discovery roundtrips.
        runtime._token_endpoint_cache = "https://idp.example/token"

        import urllib.error
        call_n = {"i": 0}
        new_access = "tok_new_123"
        def fake_urlopen(req, timeout):
            call_n["i"] += 1
            url = req.full_url
            if "/token" in url and call_n["i"] == 1:
                # First call is the refresh_token grant — return new bundle.
                body = json.dumps({
                    "access_token": new_access,
                    "refresh_token": "rt_new",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "",
                }).encode()
                return _mock_urlopen(200, body)
            if call_n["i"] == 2:
                # Second call is the retry of the original tools/list.
                return _mock_urlopen(
                    200,
                    b'event: message\ndata: {"jsonrpc":"2.0","id":42,"result":{"ok":true}}\n\n',
                )
            pytest.fail(f"unexpected extra urlopen call #{call_n['i']} to {url}")
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        # Pre-seed the cache so the FIRST _post (tools/list) doesn't itself trigger
        # the proactive refresh in _ensure_token — we want to test the 401 path.
        runtime._cached_url = _FAKE_CRED["url"]
        runtime._cached_token = "tok_old"
        runtime._cached_expires_at_ms = int((time.time() + 3600) * 1000)

        # Inject a 401 into the FIRST tools/list call.
        original_post = runtime._post
        first_post_call = {"done": False}
        def first_post_returns_401(body, *, retried=False):
            if not first_post_call["done"] and not retried:
                first_post_call["done"] = True
                # simulate 401 the way real urlopen would
                err = urllib.error.HTTPError(
                    runtime._cached_url or "u", 401, "Unauthorized", {},
                    MagicMock(read=lambda: b'{"error":"invalid_token"}'),
                )
                # delegate to the real _post handler so it goes through the
                # 401 → refresh → retry path
                # … here we instead directly invoke the real flow by raising
                # via a fake urlopen — already set up above. So we just call
                # the real _post.
                return original_post(body, retried=False)
            return original_post(body, retried=retried)

        # Re-enable the real flow. The fake_urlopen above doesn't fire 401 on
        # the first call (it fires success twice). Instead simulate the 401
        # by intercepting at the urlopen layer:
        call_n["i"] = 0
        def fake_urlopen_with_401(req, timeout):
            call_n["i"] += 1
            url = req.full_url
            if call_n["i"] == 1 and "/token" not in url:
                # First tools/list call → 401
                raise urllib.error.HTTPError(
                    url, 401, "Unauthorized", {},
                    MagicMock(read=lambda: b'{"error":"invalid_token"}'),
                )
            if "/token" in url:
                # Refresh-token grant
                body = json.dumps({
                    "access_token": new_access, "refresh_token": "rt_new",
                    "token_type": "Bearer", "expires_in": 3600, "scope": "",
                }).encode()
                return _mock_urlopen(200, body)
            # Retry — succeed
            return _mock_urlopen(
                200,
                b'event: message\ndata: {"jsonrpc":"2.0","id":42,"result":{"ok":true}}\n\n',
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen_with_401)

        status, body = runtime._post({"jsonrpc":"2.0","id":42,"method":"tools/list"})

        assert status == 200, "retry should have succeeded"
        # Verify keychain was written with the new access_token + refresh_token
        assert len(keychain_writes) >= 1, "must write new bundle back to keychain"
        written = keychain_writes[-1]
        assert written["token_response"]["access_token"] == new_access
        assert written["token_response"]["refresh_token"] == "rt_new"
        # And the cache picked up the new token
        assert runtime._cached_token == new_access

    def test_refresh_failure_raises_with_helpful_message(
        self, runtime_with_real_refresh, monkeypatch,
    ):
        """If the OAuth IdP rejects the refresh_token (e.g. already
        consumed), we must raise EmcpAuthError pointing at
        `codex mcp login` as the recovery path."""
        runtime = runtime_with_real_refresh
        # Bundle must be expired so the early-return inside _refresh_access_token
        # doesn't short-circuit to adopting it.
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: json.dumps(self._make_expired_bundle()),
        )
        runtime._token_endpoint_cache = "https://idp.example/token"

        import urllib.error
        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                "https://idp.example/token", 400, "Bad Request", {},
                MagicMock(read=lambda: b'{"error":"invalid_grant"}'),
            )
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(EmcpAuthError, match="codex mcp login"):
            runtime._refresh_access_token()

    def test_keychain_writeback_failure_raises(
        self, runtime_with_real_refresh, monkeypatch,
    ):
        """If we can't write the new bundle back to keychain, raise — never
        proceed with a refresh that consumed the old refresh_token without
        persisting the new one."""
        runtime = runtime_with_real_refresh
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: json.dumps(self._make_expired_bundle()),
        )
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stderr="ACL denied"),
        )
        runtime._token_endpoint_cache = "https://idp.example/token"

        def fake_urlopen(req, timeout):
            return _mock_urlopen(200, json.dumps({
                "access_token":"x","refresh_token":"y",
                "token_type":"Bearer","expires_in":3600,"scope":"",
            }).encode())
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(EmcpAuthError, match="keychain write failed"):
            runtime._refresh_access_token()

    def test_concurrent_refresh_is_serialised(
        self, runtime_with_real_refresh, monkeypatch,
    ):
        """If two threads hit 401 simultaneously, only ONE refresh-token
        grant must run — otherwise the first one would consume the
        refresh_token and the second would fail with HTTP 400."""
        import threading
        runtime = runtime_with_real_refresh

        # After first refresh writes back, subsequent reads return a
        # bundle that is NOT yet expired — second thread should adopt.
        bundles = [self._make_expired_bundle()]
        def fake_check_output(*a, **kw):
            return json.dumps(bundles[-1])
        def fake_subprocess_run(*a, **kw):
            argv = a[0] if a else kw.get("args")
            idx_w = argv.index("-w")
            bundles.append(json.loads(argv[idx_w + 1]))
            return MagicMock(returncode=0, stderr="")
        monkeypatch.setattr("subprocess.check_output", fake_check_output)
        monkeypatch.setattr("subprocess.run", fake_subprocess_run)
        runtime._token_endpoint_cache = "https://idp.example/token"

        grants = {"n": 0}
        def fake_urlopen(req, timeout):
            grants["n"] += 1
            return _mock_urlopen(200, json.dumps({
                "access_token":f"tok_{grants['n']}","refresh_token":f"rt_{grants['n']}",
                "token_type":"Bearer","expires_in":3600,"scope":"",
            }).encode())
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        # Two threads race into _refresh_access_token.
        errors = []
        def call_refresh():
            try:
                runtime._refresh_access_token()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=call_refresh)
        t2 = threading.Thread(target=call_refresh)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors, f"refresh threads should not error: {errors}"
        assert grants["n"] == 1, (
            f"expected exactly ONE refresh_token grant under contention "
            f"(server rotates refresh_token), got {grants['n']}"
        )
