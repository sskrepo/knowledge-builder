"""MCP Streamable HTTP transport (JSON-RPC 2.0).

Implements MCP spec 2025-03-26 Streamable HTTP transport so that
Claude Code's native MCP HTTP client can connect directly.

Single endpoint: POST /mcp
Wire format: JSON-RPC 2.0 with optional SSE streaming.

Auth model
----------
Dev mode (KBF_ENV=laptop|dev|local):
  No Bearer token required for any method — the server returns a built-in
  anonymous consumer so local dev works out-of-the-box with any MCP client.

Production mode:
  tools/call requires a valid Bearer token.  All other methods (initialize,
  ping, tools/list, prompts/list, resources/list) are public per the MCP spec.

  When a tools/call arrives without auth the server returns HTTP 401 (not a
  JSON-RPC 200 error) with:
    WWW-Authenticate: Bearer realm="<server-url>"
  and a JSON body containing the exact .mcp.json snippet to paste, so MCP
  clients can surface actionable setup instructions to the user.

Discovery
---------
  GET /.well-known/oauth-protected-resource
    RFC 9728 resource-metadata endpoint — MCP clients that support auto-
    discovery use this to find auth requirements before sending any request.

The existing /mcp/tools/list and /mcp/tools/call REST routes remain for
backward compatibility with other clients.
"""
from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

# FastAPI/Starlette imports at module level so that FastAPI's annotation resolver
# (`get_type_hints`) can find `Request` in this module's __globals__.
# Wrapped in try/except so the module can be imported in non-FastAPI environments.
try:
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse
    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 error codes (spec §5.1)
# ---------------------------------------------------------------------------

_PARSE_ERROR     = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS  = -32602
_INTERNAL_ERROR  = -32603

# KBF_ENV values that bypass token auth — developer laptops only
_DEV_ENVS = {"laptop", "dev", "local", "test"}


def _is_dev_mode() -> bool:
    return os.environ.get("KBF_ENV", "dev").lower() in _DEV_ENVS


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


# ---------------------------------------------------------------------------
# Anon consumer (dev mode only)
# ---------------------------------------------------------------------------


def _make_anon_consumer():
    """Return a built-in anonymous consumer used when KBF_ENV is a dev value.

    Importing ConsumerManifest here (not at module top) avoids a hard
    dependency when this module is used in test environments that don't
    have the full auth package installed.
    """
    from framework.deploy.auth.consumer import ConsumerManifest
    return ConsumerManifest(
        name="anon-dev",
        token_hash="",
        scopes=["read", "write"],
        persona_allowlist=[],
        rpm_cap=120,
        token_budget_per_request=16000,
        user_id="anon-dev",
    )


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _authenticate(req: "Request"):
    """Validate the Bearer token from *req*.

    Returns:
        (consumer, None)        — authenticated; consumer is a ConsumerManifest
        (anon_consumer, None)   — dev mode, no token needed
        (None, Response)        — auth failed; Response is an HTTP 401 to return directly

    Dev mode (KBF_ENV in _DEV_ENVS):
        If no Authorization header is present, return the anonymous dev consumer
        so any MCP client works out-of-the-box without configuration.
        If a header IS present, still validate it (lets devs test with real tokens).

    Production mode:
        Missing or unrecognised token → HTTP 401 with WWW-Authenticate header
        and a JSON body containing the exact .mcp.json snippet to configure.
    """
    auth_header = req.headers.get("Authorization", "")

    # Dev mode with no token → anonymous passthrough
    if _is_dev_mode() and not auth_header:
        log.debug("mcp_transport: dev mode — anonymous consumer (no auth required)")
        return _make_anon_consumer(), None

    if not auth_header.startswith("Bearer "):
        return None, _unauth_response(req, "Authorization header missing or not Bearer")

    token = auth_header[len("Bearer "):]
    registry = getattr(req.app.state, "consumer_registry", None)
    if registry is None:
        return None, _unauth_response(req, "Consumer registry not initialised on server")

    consumer = registry.lookup(token)
    if consumer is None:
        return None, _unauth_response(req, "Bearer token not recognised")

    return consumer, None


def _unauth_response(req: "Request", detail: str = "Unauthorized") -> "JSONResponse":
    """HTTP 401 with WWW-Authenticate header and actionable setup instructions.

    MCP clients that surface HTTP errors to users will show the hint field,
    which contains the exact .mcp.json snippet to paste.
    """
    base_url = str(req.base_url).rstrip("/")
    mcp_url  = f"{base_url}/mcp"
    snippet  = (
        '{\n'
        '  "mcpServers": {\n'
        '    "kbf": {\n'
        '      "type": "http",\n'
        f'      "url": "{mcp_url}",\n'
        '      "headers": { "Authorization": "Bearer <your-token>" }\n'
        '    }\n'
        '  }\n'
        '}'
    )
    body = {
        "error": "unauthorized",
        "detail": detail,
        "hint": (
            "Add an Authorization: Bearer <token> header to your MCP client. "
            "Paste the following into your .mcp.json (or Claude Code MCP settings):"
        ),
        "mcpJsonSnippet": snippet,
        "tokenSource": (
            f"Get a token from your KBF admin, or use 'dev-only-token-replace-me' "
            f"for local development (KBF_ENV=laptop bypasses auth entirely)."
        ),
        "discovery": f"{base_url}/.well-known/oauth-protected-resource",
    }
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer realm="{base_url}"'},
        content=body,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_mcp_transport(app, state: dict) -> None:
    """Attach POST /mcp and GET /.well-known/oauth-protected-resource to *app*.

    Args:
        app:   FastAPI application instance (already has middleware wired).
        state: The mcp_server module-level state dict that holds
               ``external_registry`` and ``external_tools_schema``.
    """
    if not _FASTAPI_AVAILABLE:  # pragma: no cover
        log.warning("fastapi not installed — MCP transport not registered")
        return

    # ------------------------------------------------------------------
    # Discovery endpoint — RFC 9728 / MCP spec
    # ------------------------------------------------------------------

    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource(req: Request):
        """OAuth 2.0 Protected Resource Metadata (RFC 9728).

        MCP clients that support auto-discovery call this before sending
        any request to learn what auth the server requires.
        """
        base_url = str(req.base_url).rstrip("/")
        return JSONResponse(content={
            "resource": base_url,
            "authorization_servers": [base_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["read", "write", "admin"],
            "kbf_auth_hint": (
                f"Set Authorization: Bearer <token> in your MCP client headers. "
                f"For local dev with KBF_ENV=laptop, no token is required."
            ),
        })

    # ------------------------------------------------------------------
    # Primary MCP endpoint — JSON-RPC 2.0 over HTTP
    # ------------------------------------------------------------------

    @app.post("/mcp")
    async def mcp_jsonrpc(req: Request):
        """MCP Streamable HTTP transport (JSON-RPC 2.0).

        Auth model:
          - Dev mode (KBF_ENV=laptop/dev/local): no token needed, any client works.
          - Production: tools/call requires Bearer token; all other methods are public.
          - Auth failure returns HTTP 401 + WWW-Authenticate + setup instructions.
        """
        # Assign a unique request ID for tracing — included in all error responses
        request_id = f"req-{uuid4().hex[:8]}"

        # Parse body
        try:
            body = await req.json()
        except Exception:
            return _respond(_err(None, _PARSE_ERROR, "Parse error"), req)

        if not isinstance(body, dict):
            return _respond(_err(None, _INVALID_REQUEST, "Invalid Request"), req)

        method = body.get("method")
        params = body.get("params") or {}
        req_id = body.get("id")

        log.info("mcp request request_id=%s method=%s", request_id, method or "(none)")

        # Notifications (no id) — acknowledge silently
        if "id" not in body:
            log.debug("mcp_transport: notification method=%s", method)
            return JSONResponse(content={}, status_code=200)

        if not method:
            return _respond(_err(req_id, _INVALID_REQUEST, "method is required"), req)

        log.debug("mcp_transport: method=%s id=%s request_id=%s", method, req_id, request_id)

        # tools/call: auth check happens here — failure returns HTTP 401 directly
        if method == "tools/call":
            consumer, auth_err = _authenticate(req)
            if auth_err is not None:
                return auth_err   # HTTP 401, not a JSON-RPC response
            response = await _dispatch_tool_call(params, req_id, consumer, state, req, request_id)
            return _respond(response, req)

        # All other methods
        response = await _dispatch(method, params, req_id, state)
        return _respond(response, req)


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------


async def _dispatch(method: str, params: dict, req_id: Any, state: dict) -> dict:
    """Route non-tool-call methods.  No auth required."""

    if method == "initialize":
        dev_note = " (dev mode: no auth required)" if _is_dev_mode() else ""
        return _ok(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "kbf",
                "version": "1.0.0",
                "note": f"Knowledge Builder Framework{dev_note}",
            },
        })

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        tools = state.get("external_tools_schema", [])
        return _ok(req_id, {"tools": tools})

    if method == "prompts/list":
        from .skill_prompt import SKILL_PROMPT_NAME, SKILL_PROMPT_DESCRIPTION, SKILL_PROMPT_VERSION
        return _ok(req_id, {
            "prompts": [
                {"name": SKILL_PROMPT_NAME, "description": SKILL_PROMPT_DESCRIPTION, "version": SKILL_PROMPT_VERSION}
            ]
        })

    if method == "prompts/get":
        from .skill_prompt import SKILL_PROMPT_NAME, SKILL_PROMPT_DESCRIPTION, get_skill_prompt_messages
        prompt_name = params.get("name") if isinstance(params, dict) else None
        if prompt_name != SKILL_PROMPT_NAME:
            return _err(req_id, _INVALID_PARAMS,
                        f"Unknown prompt: {prompt_name!r}. Available: {SKILL_PROMPT_NAME!r}")
        return _ok(req_id, {"description": SKILL_PROMPT_DESCRIPTION, "messages": get_skill_prompt_messages()})

    if method == "resources/list":
        return _ok(req_id, {"resources": []})

    return _err(req_id, _METHOD_NOT_FOUND, f"Method not found: {method!r}")


async def _dispatch_tool_call(
    params: dict,
    req_id: Any,
    consumer,
    state: dict,
    req: "Request",
    request_id: str,
) -> dict:
    """Execute a tools/call request.  Consumer is already authenticated.

    On error, writes a structured record to error_store (if available) and
    includes ``requestId`` in the isError content item so the LLM client
    can call ``reportBug`` with the correct ID.
    """
    tool_name = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}

    if not tool_name:
        return _err(req_id, _INVALID_PARAMS, "params.name is required for tools/call")

    external_registry = state.get("external_registry", {})
    handler = external_registry.get(tool_name)

    if handler is None:
        return _ok(req_id, {
            "content": [{"type": "text", "text": f"Unknown tool: {tool_name!r}", "requestId": request_id}],
            "isError": True,
        })

    error_store = getattr(req.app.state, "error_store", None)
    user_id = getattr(consumer, "user_id", "anon") or "anon"
    synth_id = arguments.get("synthId", "") if isinstance(arguments, dict) else ""

    try:
        result = await handler(**arguments, _consumer=consumer)
    except TypeError as exc:
        tb = traceback.format_exc()
        log.warning("mcp_transport: tools/call %s bad arguments request_id=%s: %s", tool_name, request_id, exc)
        if error_store:
            _write_error_record(error_store, request_id, tool_name, synth_id, user_id, exc, tb, arguments)
        return _ok(req_id, {
            "content": [{"type": "text", "text": f"Invalid arguments: {exc}", "requestId": request_id}],
            "isError": True,
        })
    except Exception as exc:
        tb = traceback.format_exc()
        log.exception("mcp_transport: tools/call %s raised %s request_id=%s", tool_name, type(exc).__name__, request_id)
        if error_store:
            _write_error_record(error_store, request_id, tool_name, synth_id, user_id, exc, tb, arguments)
        return _ok(req_id, {
            "content": [{"type": "text", "text": f"Tool execution error: {exc}", "requestId": request_id}],
            "isError": True,
        })

    return _ok(req_id, {
        "content": [{"type": "text", "text": json.dumps(_serialize(result))}],
        "isError": False,
    })


def _write_error_record(error_store, request_id, tool_name, synth_id, user_id, exc, tb, arguments):
    """Write a structured error record to the error store."""
    from datetime import datetime, timezone
    try:
        error_store.record_error({
            "request_id": request_id,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "tool": tool_name,
            "synth_id": synth_id,
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": tb,
            "input_snapshot": _sanitise_input(arguments),
        })
    except Exception as store_exc:
        log.error("mcp_transport: failed to write to error_store: %s", store_exc)


def _sanitise_input(arguments: dict) -> dict:
    """Strip sensitive keys (token, password) from input before storing."""
    if not isinstance(arguments, dict):
        return {}
    sensitive = ("token", "password")
    return {k: v for k, v in arguments.items() if not any(s in k.lower() for s in sensitive)}


# ---------------------------------------------------------------------------
# SSE / response helpers
# ---------------------------------------------------------------------------


def _respond(jsonrpc_response: dict, req: "Request") -> "JSONResponse | StreamingResponse":
    """Return SSE StreamingResponse if client accepts text/event-stream, else JSONResponse."""
    accept = req.headers.get("accept", "")
    if "text/event-stream" in accept:
        return StreamingResponse(
            _sse_generator(jsonrpc_response),
            media_type="text/event-stream",
        )
    return JSONResponse(content=jsonrpc_response)


async def _sse_generator(jsonrpc_response: dict):
    """Yield one SSE event containing the JSON-RPC response, then close."""
    data = json.dumps(jsonrpc_response)
    yield f"data: {data}\n\n"


# ---------------------------------------------------------------------------
# Serialization helper (mirrors mcp_server._serialize)
# ---------------------------------------------------------------------------


def _serialize(obj):
    """Recursively convert dataclasses/dicts/lists to JSON-safe primitives."""
    from dataclasses import is_dataclass, asdict
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj
