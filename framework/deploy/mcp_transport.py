"""MCP Streamable HTTP transport (JSON-RPC 2.0).

Implements MCP spec 2025-03-26 Streamable HTTP transport so that
Claude Code's native MCP HTTP client can connect directly.

Single endpoint: POST /mcp
Wire format: JSON-RPC 2.0 with optional SSE streaming.

The existing /mcp/tools/list and /mcp/tools/call REST routes remain
for backward compatibility with other clients.

Usage — register on a FastAPI app:
    from framework.deploy.mcp_transport import register_mcp_transport
    register_mcp_transport(app, state)

Config snippet for Claude Code (.mcp.json):
    {
      "mcpServers": {
        "kbf": {
          "type": "http",
          "url": "http://localhost:8080/mcp",
          "headers": {"Authorization": "Bearer dev-only-token-replace-me"}
        }
      }
    }
"""
from __future__ import annotations

import json
import logging
from typing import Any

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

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _ok(req_id: Any, result: Any) -> dict:
    """Build a successful JSON-RPC 2.0 response."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict:
    """Build a JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_mcp_transport(app, state: dict) -> None:
    """Attach the POST /mcp JSON-RPC dispatcher to *app*.

    Args:
        app:   FastAPI application instance (already has middleware wired).
        state: The mcp_server module-level state dict that holds
               ``external_registry`` and ``external_tools_schema``.
    """
    if not _FASTAPI_AVAILABLE:  # pragma: no cover
        log.warning("fastapi not installed — MCP transport not registered")
        return

    @app.post("/mcp")
    async def mcp_jsonrpc(req: Request):
        """MCP Streamable HTTP transport (JSON-RPC 2.0).

        This is the endpoint Claude Code's native MCP HTTP client connects to.
        Speaks MCP spec 2025-03-26 Streamable HTTP transport.

        Auth rules (per MCP spec):
          - initialize, initialized, ping, tools/list, prompts/list, resources/list
            do NOT require a bearer token.
          - tools/call REQUIRES a valid bearer token — returns JSON-RPC error
            -32603 "Unauthorized" if token is missing or unrecognised.
        """
        # Parse request body
        try:
            body = await req.json()
        except Exception:
            response = _err(None, _PARSE_ERROR, "Parse error")
            return _respond(response, req)

        if not isinstance(body, dict):
            response = _err(None, _INVALID_REQUEST, "Invalid Request")
            return _respond(response, req)

        method = body.get("method")
        params = body.get("params") or {}
        req_id = body.get("id")  # None for notifications

        # Notifications (no id field) — acknowledge with empty 200
        # The MCP spec says notifications MUST NOT have an id.
        if "id" not in body:
            log.debug("mcp_transport: notification method=%s (no response needed)", method)
            return JSONResponse(content={}, status_code=200)

        if not method:
            return _respond(_err(req_id, _INVALID_REQUEST, "method is required"), req)

        log.debug("mcp_transport: method=%s id=%s", method, req_id)

        # Dispatch
        response = await _dispatch(method, params, req_id, req, state)
        return _respond(response, req)


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


async def _dispatch(
    method: str,
    params: dict,
    req_id: Any,
    req: "Request",
    state: dict,
) -> dict:
    """Route a JSON-RPC method to its handler.  Returns a JSON-RPC 2.0 dict."""

    # ----------------------------------------------------------------
    # Methods that do NOT require auth
    # ----------------------------------------------------------------

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kbf", "version": "1.0.0"},
        })

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        tools = state.get("external_tools_schema", [])
        return _ok(req_id, {"tools": tools})

    if method == "prompts/list":
        return _ok(req_id, {"prompts": []})

    if method == "resources/list":
        return _ok(req_id, {"resources": []})

    # ----------------------------------------------------------------
    # tools/call — requires bearer token
    # ----------------------------------------------------------------

    if method == "tools/call":
        # Manual auth: the bearer_auth_middleware skips /mcp so auth can be
        # selectively applied only to tools/call here.
        consumer = _authenticate(req, state)
        if consumer is None:
            return _err(req_id, _INTERNAL_ERROR, "Unauthorized")

        tool_name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}

        if not tool_name:
            return _err(req_id, _INVALID_PARAMS, "params.name is required for tools/call")

        external_registry = state.get("external_registry", {})
        handler = external_registry.get(tool_name)

        if handler is None:
            # Per MCP spec, unknown tool → isError=true in content (not a protocol error)
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name!r}"}],
                "isError": True,
            })

        try:
            result = await handler(**arguments, _consumer=consumer)
        except TypeError as exc:
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"Invalid arguments: {exc}"}],
                "isError": True,
            })
        except Exception as exc:
            log.exception("mcp_transport: tools/call %s raised %s", tool_name, exc)
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"Tool execution error: {exc}"}],
                "isError": True,
            })

        # Successful result: wrap in MCP content envelope
        return _ok(req_id, {
            "content": [{"type": "text", "text": json.dumps(_serialize(result))}],
            "isError": False,
        })

    # ----------------------------------------------------------------
    # Unknown method
    # ----------------------------------------------------------------

    return _err(req_id, _METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _authenticate(req: "Request", state: dict):
    """Extract and validate the Bearer token from *req*.

    Returns the ConsumerManifest on success, or None on failure.

    Uses the same ConsumerRegistry that the rest-auth middleware uses,
    accessed via req.app.state.consumer_registry so the lookup is
    identical.
    """
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[len("Bearer "):]

    registry = getattr(req.app.state, "consumer_registry", None)
    if registry is None:
        return None

    return registry.lookup(token)


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
