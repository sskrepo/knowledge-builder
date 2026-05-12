"""Unit tests for framework/deploy/mcp_transport.py.

Tests the MCP Streamable HTTP transport (JSON-RPC 2.0 over POST /mcp).

Coverage:
  - initialize returns correct shape
  - tools/list returns tools array
  - tools/call dispatches and returns JSON-RPC result
  - tools/call with unknown tool returns isError=true content (not JSON-RPC error)
  - Unknown method returns -32601 error
  - Notification (initialized, no id field) returns empty 200
  - SSE path: response is text/event-stream with data: prefix
  - Auth missing on tools/call returns -32603
  - Auth valid on tools/call calls handler and returns result
  - ping returns empty result
  - prompts/list returns empty prompts array
  - resources/list returns empty resources array
  - Parse error on malformed body returns -32700
  - tools/call with invalid arguments returns isError=true content
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _FASTAPI_AVAILABLE,
    reason="fastapi not installed",
)


# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_transport_app(tmp_path: Path, *, tools_schema=None, external_registry=None):
    """Build a minimal FastAPI app with the MCP transport registered.

    Wires a real ConsumerRegistry pointing at a tmp manifests dir and a
    configurable tools schema / external_registry so individual tests can
    control what tools are exposed.
    """
    from fastapi import FastAPI
    from framework.deploy.auth.consumer import ConsumerManifest
    from framework.deploy.auth.middleware import bearer_auth_middleware
    from framework.deploy.auth.registry import ConsumerRegistry
    from framework.deploy.mcp_transport import register_mcp_transport

    # Write a dev consumer manifest
    manifests_dir = tmp_path / "consumer_manifests"
    manifests_dir.mkdir(exist_ok=True)
    dev_token = "transport-test-token-xyz"
    dev_token_hash = hashlib.sha256(dev_token.encode()).hexdigest()
    (manifests_dir / "dev.yaml").write_text(
        textwrap.dedent(f"""
            name: transport-test-consumer
            tokenHash: {dev_token_hash}
            scopes:
              - read
              - write
              - admin
            personaAllowlist: []
            rpmCap: 120
            tokenBudgetPerRequest: 8000
            userId: transport-test-user
        """),
        encoding="utf-8",
    )

    if tools_schema is None:
        tools_schema = [
            {
                "name": "echo",
                "description": "Echo the input",
                "inputSchema": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string"}},
                },
            }
        ]

    if external_registry is None:
        async def _echo_handler(*, message: str, _consumer=None) -> dict:
            return {"echoed": message}

        external_registry = {"echo": _echo_handler}

    # Module-level state dict (mirrors mcp_server.py state)
    state: dict = {
        "external_tools_schema": tools_schema,
        "external_registry": external_registry,
    }

    app = FastAPI(title="Transport Test App")

    # The middleware skips /mcp (we added it to _AUTH_SKIP_PATHS).
    # We still wire it so other paths behave normally.
    app.middleware("http")(bearer_auth_middleware)

    @app.on_event("startup")
    async def _startup():
        app.state.consumer_registry = ConsumerRegistry(manifests_dir)

    register_mcp_transport(app, state)

    return app, dev_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def transport_client(tmp_path):
    app, dev_token = _make_transport_app(tmp_path)
    with TestClient(app) as client:
        client._dev_token = dev_token
        yield client


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {client._dev_token}"}


def _post(client, body: dict, *, headers: dict | None = None) -> dict:
    h = headers or {}
    resp = client.post("/mcp", json=body, headers=h)
    return resp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonrpc(method: str, params: dict | None = None, *, req_id=1) -> dict:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    def test_returns_200(self, transport_client):
        resp = _post(transport_client, _jsonrpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "1.0.0"},
        }))
        assert resp.status_code == 200

    def test_result_has_protocol_version(self, transport_client):
        resp = _post(transport_client, _jsonrpc("initialize", {}))
        body = resp.json()
        assert "result" in body
        assert body["result"]["protocolVersion"] == "2025-03-26"

    def test_result_has_capabilities_tools(self, transport_client):
        resp = _post(transport_client, _jsonrpc("initialize", {}))
        result = resp.json()["result"]
        assert "capabilities" in result
        assert "tools" in result["capabilities"]

    def test_result_has_server_info(self, transport_client):
        resp = _post(transport_client, _jsonrpc("initialize", {}))
        result = resp.json()["result"]
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "kbf"

    def test_no_auth_required(self, transport_client):
        """initialize must work without a Bearer token."""
        resp = _post(transport_client, _jsonrpc("initialize", {}), headers={})
        assert resp.status_code == 200
        assert "error" not in resp.json()

    def test_id_echoed_back(self, transport_client):
        resp = _post(transport_client, _jsonrpc("initialize", {}, req_id=42))
        assert resp.json()["id"] == 42


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


class TestPing:
    def test_returns_empty_result(self, transport_client):
        resp = _post(transport_client, _jsonrpc("ping"))
        assert resp.status_code == 200
        assert resp.json()["result"] == {}

    def test_no_auth_required(self, transport_client):
        resp = _post(transport_client, _jsonrpc("ping"), headers={})
        assert "error" not in resp.json()


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class TestToolsList:
    def test_returns_200(self, transport_client):
        resp = _post(transport_client, _jsonrpc("tools/list"))
        assert resp.status_code == 200

    def test_result_has_tools_key(self, transport_client):
        resp = _post(transport_client, _jsonrpc("tools/list"))
        result = resp.json()["result"]
        assert "tools" in result

    def test_tools_array_contains_echo(self, transport_client):
        resp = _post(transport_client, _jsonrpc("tools/list"))
        names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert "echo" in names

    def test_no_auth_required(self, transport_client):
        """tools/list must work without a Bearer token (MCP spec)."""
        resp = _post(transport_client, _jsonrpc("tools/list"), headers={})
        assert resp.status_code == 200
        assert "error" not in resp.json()

    def test_id_echoed(self, transport_client):
        resp = _post(transport_client, _jsonrpc("tools/list", req_id=99))
        assert resp.json()["id"] == 99


# ---------------------------------------------------------------------------
# prompts/list
# ---------------------------------------------------------------------------


class TestPromptsList:
    def test_returns_empty_prompts(self, transport_client):
        resp = _post(transport_client, _jsonrpc("prompts/list"))
        assert resp.status_code == 200
        assert resp.json()["result"]["prompts"] == []

    def test_no_auth_required(self, transport_client):
        resp = _post(transport_client, _jsonrpc("prompts/list"), headers={})
        assert "error" not in resp.json()


# ---------------------------------------------------------------------------
# resources/list
# ---------------------------------------------------------------------------


class TestResourcesList:
    def test_returns_empty_resources(self, transport_client):
        resp = _post(transport_client, _jsonrpc("resources/list"))
        assert resp.status_code == 200
        assert resp.json()["result"]["resources"] == []


# ---------------------------------------------------------------------------
# tools/call — auth missing (PRODUCTION mode: KBF_ENV=production)
#
# In dev mode (KBF_ENV=laptop/dev) the anonymous bypass is active, so
# missing tokens succeed.  These tests explicitly set KBF_ENV=production
# to exercise the auth-failure path, which now returns HTTP 401 (not a
# JSON-RPC 200 error) with a WWW-Authenticate header and setup snippet.
# ---------------------------------------------------------------------------


class TestToolsCallAuthMissing:
    def _prod_app(self, tmp_path):
        """Build a transport app and force production mode for this test."""
        return _make_transport_app(tmp_path)

    def test_no_token_returns_401(self, tmp_path):
        """tools/call without Bearer token in prod mode → HTTP 401."""
        app, _ = self._prod_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "production"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hi"}}),
                    headers={},
                )
        assert resp.status_code == 401

    def test_no_token_has_www_authenticate_header(self, tmp_path):
        app, _ = self._prod_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "production"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hi"}}),
                    headers={},
                )
        assert "www-authenticate" in {k.lower() for k in resp.headers}
        assert "Bearer" in resp.headers.get("www-authenticate", resp.headers.get("WWW-Authenticate", ""))

    def test_no_token_body_has_mcp_json_snippet(self, tmp_path):
        """401 body must include exact .mcp.json snippet so clients can self-configure."""
        app, _ = self._prod_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "production"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hi"}}),
                    headers={},
                )
        body = resp.json()
        assert "mcpJsonSnippet" in body
        assert "Authorization" in body["mcpJsonSnippet"]

    def test_no_token_body_has_discovery_link(self, tmp_path):
        app, _ = self._prod_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "production"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hi"}}),
                    headers={},
                )
        body = resp.json()
        assert "discovery" in body
        assert ".well-known" in body["discovery"]

    def test_bad_token_returns_401(self, tmp_path):
        app, _ = self._prod_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "production"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hi"}}),
                    headers={"Authorization": "Bearer totally-wrong-token"},
                )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Dev mode — no auth required for tools/call
# ---------------------------------------------------------------------------


class TestDevMode:
    """In dev mode (KBF_ENV=laptop/dev/local) tools/call works without any token."""

    def test_no_token_succeeds_in_laptop_mode(self, tmp_path):
        app, _ = _make_transport_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
                    headers={},  # no auth
                )
        assert resp.status_code == 200
        assert resp.json()["result"]["isError"] is False

    def test_no_token_succeeds_in_dev_mode(self, tmp_path):
        app, _ = _make_transport_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "dev"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
                    headers={},
                )
        assert resp.status_code == 200

    def test_valid_token_also_works_in_dev_mode(self, tmp_path):
        """Even in dev mode, a real registered token is still accepted."""
        app, dev_token = _make_transport_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
                    headers={"Authorization": f"Bearer {dev_token}"},
                )
        assert resp.status_code == 200
        assert resp.json()["result"]["isError"] is False

    def test_anon_consumer_returned_in_content(self, tmp_path):
        """Tool result must come back successfully with anon consumer in dev mode."""
        app, _ = _make_transport_app(tmp_path)
        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with TestClient(app) as client:
                resp = client.post(
                    "/mcp",
                    json=_jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "ping"}}),
                    headers={},
                )
        data = json.loads(resp.json()["result"]["content"][0]["text"])
        assert data["echoed"] == "ping"


# ---------------------------------------------------------------------------
# tools/call — happy path
# ---------------------------------------------------------------------------


class TestToolsCallHappyPath:
    def test_returns_jsonrpc_result(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
            headers=_auth(transport_client),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body
        assert "error" not in body

    def test_result_has_content_array(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
            headers=_auth(transport_client),
        )
        result = resp.json()["result"]
        assert "content" in result
        assert isinstance(result["content"], list)
        assert len(result["content"]) >= 1

    def test_result_is_not_error(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
            headers=_auth(transport_client),
        )
        assert resp.json()["result"]["isError"] is False

    def test_content_type_is_text(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
            headers=_auth(transport_client),
        )
        content_item = resp.json()["result"]["content"][0]
        assert content_item["type"] == "text"

    def test_content_text_contains_echoed_value(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "ping-pong"}}),
            headers=_auth(transport_client),
        )
        text = resp.json()["result"]["content"][0]["text"]
        # text is JSON-serialised result dict
        data = json.loads(text)
        assert data["echoed"] == "ping-pong"

    def test_id_echoed(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "echo", "arguments": {"message": "x"}}, req_id=7),
            headers=_auth(transport_client),
        )
        assert resp.json()["id"] == 7


# ---------------------------------------------------------------------------
# tools/call — unknown tool returns isError content (not JSON-RPC error)
# ---------------------------------------------------------------------------


class TestToolsCallUnknownTool:
    def test_unknown_tool_is_not_jsonrpc_error(self, transport_client):
        """Unknown tool must use isError=true in content, NOT a JSON-RPC error envelope."""
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "non_existent_tool", "arguments": {}}),
            headers=_auth(transport_client),
        )
        body = resp.json()
        assert "error" not in body, (
            "Unknown tool should NOT produce a JSON-RPC error envelope"
        )

    def test_unknown_tool_returns_is_error_true(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "non_existent_tool", "arguments": {}}),
            headers=_auth(transport_client),
        )
        result = resp.json()["result"]
        assert result["isError"] is True

    def test_unknown_tool_content_mentions_tool_name(self, transport_client):
        resp = _post(
            transport_client,
            _jsonrpc("tools/call", {"name": "non_existent_tool", "arguments": {}}),
            headers=_auth(transport_client),
        )
        text = resp.json()["result"]["content"][0]["text"]
        assert "non_existent_tool" in text


# ---------------------------------------------------------------------------
# tools/call — handler raises TypeError (bad args)
# ---------------------------------------------------------------------------


class TestToolsCallBadArgs:
    def test_bad_args_returns_is_error_true(self, tmp_path):
        """If the handler raises TypeError (wrong kwargs), return isError=true content."""

        async def _strict_handler(*, required_arg: str, _consumer=None) -> dict:
            return {"ok": required_arg}

        app, dev_token = _make_transport_app(
            tmp_path,
            tools_schema=[{"name": "strict", "description": "", "inputSchema": {}}],
            external_registry={"strict": _strict_handler},
        )
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                json=_jsonrpc("tools/call", {
                    "name": "strict",
                    "arguments": {"wrong_arg": "oops"},
                }),
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        body = resp.json()
        assert "error" not in body
        assert body["result"]["isError"] is True
        assert "Invalid arguments" in body["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Unknown method → -32601
# ---------------------------------------------------------------------------


class TestUnknownMethod:
    def test_unknown_method_returns_minus_32601(self, transport_client):
        resp = _post(transport_client, _jsonrpc("some/unknown/method"))
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32601

    def test_error_message_mentions_method(self, transport_client):
        resp = _post(transport_client, _jsonrpc("widgets/frobnicate"))
        assert "widgets/frobnicate" in resp.json()["error"]["message"]

    def test_no_auth_required_for_unknown_method(self, transport_client):
        """Protocol errors should always return a JSON-RPC error, not 401."""
        resp = _post(transport_client, _jsonrpc("some/unknown"), headers={})
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == _METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# Notification (no id) — return 200 empty body
# ---------------------------------------------------------------------------


class TestNotification:
    def test_initialized_notification_returns_200(self, transport_client):
        """initialized is a notification (no id field) — server must not respond."""
        body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = transport_client.post("/mcp", json=body)
        assert resp.status_code == 200

    def test_notification_body_is_empty_or_null(self, transport_client):
        body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = transport_client.post("/mcp", json=body)
        # Response body should be {} or null (no id, no error, no result)
        content = resp.json()
        assert content == {} or content is None

    def test_arbitrary_notification_no_error(self, transport_client):
        body = {"jsonrpc": "2.0", "method": "some/notification"}
        resp = transport_client.post("/mcp", json=body)
        assert resp.status_code == 200
        # Notifications never get an error response either
        assert "error" not in (resp.json() or {})


# ---------------------------------------------------------------------------
# SSE streaming path
# ---------------------------------------------------------------------------

_METHOD_NOT_FOUND = -32601  # local constant matching JSON-RPC spec


class TestSseStreaming:
    def test_sse_response_media_type(self, transport_client):
        """When Accept: text/event-stream, response Content-Type must be text/event-stream."""
        resp = transport_client.post(
            "/mcp",
            json=_jsonrpc("tools/list"),
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct

    def test_sse_body_has_data_prefix(self, transport_client):
        """SSE body must start with 'data: '."""
        resp = transport_client.post(
            "/mcp",
            json=_jsonrpc("tools/list"),
            headers={"Accept": "text/event-stream"},
        )
        # TestClient reads the full SSE stream
        body_text = resp.text
        assert body_text.startswith("data: "), (
            f"Expected SSE body to start with 'data: ', got: {body_text[:80]!r}"
        )

    def test_sse_body_contains_valid_json(self, transport_client):
        """The JSON payload after 'data: ' must be valid JSON."""
        resp = transport_client.post(
            "/mcp",
            json=_jsonrpc("tools/list"),
            headers={"Accept": "text/event-stream"},
        )
        # Extract JSON from first SSE line: "data: {...}\n\n"
        first_line = resp.text.strip().split("\n")[0]
        assert first_line.startswith("data: ")
        payload = json.loads(first_line[len("data: "):])
        assert "result" in payload

    def test_sse_tools_list_result(self, transport_client):
        resp = transport_client.post(
            "/mcp",
            json=_jsonrpc("tools/list"),
            headers={"Accept": "text/event-stream"},
        )
        first_line = resp.text.strip().split("\n")[0]
        payload = json.loads(first_line[len("data: "):])
        assert "tools" in payload["result"]

    def test_json_response_when_no_sse_accept(self, transport_client):
        """Without Accept: text/event-stream, respond with application/json."""
        resp = transport_client.post(
            "/mcp",
            json=_jsonrpc("tools/list"),
            headers={"Accept": "application/json"},
        )
        ct = resp.headers.get("content-type", "")
        assert "application/json" in ct


# ---------------------------------------------------------------------------
# Parse error
# ---------------------------------------------------------------------------


class TestParseError:
    def test_non_json_body_returns_parse_error(self, transport_client):
        """Malformed JSON body → -32700."""
        resp = transport_client.post(
            "/mcp",
            content=b"this is not json at all!!!",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32700

    def test_non_object_body_returns_invalid_request(self, transport_client):
        """JSON array at top level → -32600 Invalid Request."""
        resp = transport_client.post(
            "/mcp",
            json=[1, 2, 3],  # array, not object
        )
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# OAuth 2.0 Protected Resource Metadata discovery (RFC 9728)
# ---------------------------------------------------------------------------


class TestOauthDiscovery:
    def test_discovery_returns_200(self, transport_client):
        resp = transport_client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200

    def test_discovery_has_resource_field(self, transport_client):
        resp = transport_client.get("/.well-known/oauth-protected-resource")
        assert "resource" in resp.json()

    def test_discovery_has_bearer_methods(self, transport_client):
        resp = transport_client.get("/.well-known/oauth-protected-resource")
        body = resp.json()
        assert "bearer_methods_supported" in body
        assert "header" in body["bearer_methods_supported"]

    def test_discovery_has_scopes(self, transport_client):
        resp = transport_client.get("/.well-known/oauth-protected-resource")
        body = resp.json()
        assert "scopes_supported" in body
        assert isinstance(body["scopes_supported"], list)

    def test_discovery_has_auth_hint(self, transport_client):
        """Hint field guides users who read the discovery doc."""
        resp = transport_client.get("/.well-known/oauth-protected-resource")
        assert "kbf_auth_hint" in resp.json()

    def test_discovery_no_auth_required(self, transport_client):
        """Discovery endpoint must be public — no token needed."""
        resp = transport_client.get(
            "/.well-known/oauth-protected-resource",
            headers={},  # no auth
        )
        assert resp.status_code == 200
