"""Bearer token auth middleware for the PDD V3 REST API.

Responsibilities:
  1. Skip auth for unauthenticated paths (/healthz, /api/v1/version).
  2. Extract the Bearer token from the Authorization header.
  3. Look up the token in ConsumerRegistry (attached to app.state.consumer_registry).
  4. Reject with 401 if the token is missing or unrecognised.
  5. Enforce per-consumer RPM cap with a sliding-window in-memory counter.
  6. Reject with 429 + Retry-After: 60 if the consumer is over-limit.
  7. Attach the ConsumerManifest to request.state.consumer for route handlers.

Helper callables for route handlers:
  get_consumer(request)          — retrieve the attached ConsumerManifest
  require_scope(consumer, scope) — raise HTTPException(403) if consumer lacks scope

RPM enforcement note (v1):
  The counter is in-process.  For multi-worker deployments the counter is
  approximate (each worker maintains its own window).  Upgrading to Redis-backed
  rate limiting is deferred to a future release per the implementation plan.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .consumer import ConsumerManifest
from .registry import ConsumerRegistry

log = logging.getLogger(__name__)

# Paths that bypass authentication (security: [] in openapi.yaml)
# /mcp is skipped here because the MCP Streamable HTTP transport (mcp_transport.py)
# handles auth internally — only tools/call requires a token, all other MCP methods
# (initialize, ping, tools/list, prompts/list, resources/list) are intentionally
# public per the MCP spec 2025-03-26.
_AUTH_SKIP_PATHS = frozenset({"/healthz", "/api/v1/version", "/mcp"})

# In-memory sliding-window RPM counters: consumer_name → list[timestamp]
_rpm_counters: dict[str, list[float]] = defaultdict(list)
_rpm_lock = Lock()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


async def bearer_auth_middleware(request: Request, call_next):
    """FastAPI middleware: validate bearer token, enforce RPM, attach consumer."""

    if request.url.path in _AUTH_SKIP_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _unauthenticated("Authorization header missing or not a Bearer token")

    token = auth_header[len("Bearer "):]

    registry: ConsumerRegistry | None = getattr(
        request.app.state, "consumer_registry", None
    )
    if registry is None:
        # Registry not wired yet (e.g. during startup or in tests without middleware).
        # Fail closed.
        return _unauthenticated("Consumer registry not initialised")

    consumer = registry.lookup(token)
    if consumer is None:
        return _unauthenticated("Bearer token not recognised")

    if not _check_rpm(consumer):
        log.warning("RPM limit exceeded for consumer '%s'", consumer.name)
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "60"},
            content={
                "error": {
                    "code": "rate_limited",
                    "message": "Rate limit exceeded. Retry after 60 seconds.",
                    "details": {"retryAfterSeconds": 60},
                }
            },
        )

    request.state.consumer = consumer
    log.debug("authenticated consumer '%s' for %s %s", consumer.name,
              request.method, request.url.path)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Route-handler helpers
# ---------------------------------------------------------------------------


def get_consumer(request: Request) -> ConsumerManifest:
    """Return the ConsumerManifest attached by the auth middleware.

    Call from route handlers *after* the middleware has run.  If the middleware
    was somehow bypassed (e.g. a test that skips middleware) this will raise
    AttributeError.
    """
    return request.state.consumer


def require_scope(consumer: ConsumerManifest, scope: str) -> None:
    """Raise HTTPException(403) if *consumer* does not hold *scope*.

    Route handlers call this immediately after ``get_consumer()``.  Raising
    HTTPException (not returning a JSONResponse) allows FastAPI exception
    handlers to format the response consistently.
    """
    if not consumer.has_scope(scope):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "permission_denied",
                    "message": f"Token lacks '{scope}' scope",
                    "details": {},
                }
            },
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unauthenticated(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": "unauthenticated",
                "message": message,
                "details": {},
            }
        },
    )


def _check_rpm(consumer: ConsumerManifest) -> bool:
    """Sliding-window RPM check.  Returns True if the request is allowed.

    Evicts timestamps older than 60 s before deciding.  Thread-safe via
    ``_rpm_lock`` (single lock across all consumers; contention is negligible
    at v1 scale).
    """
    now = time.monotonic()
    window_start = now - 60.0

    with _rpm_lock:
        timestamps = _rpm_counters[consumer.name]
        # Evict expired entries in-place
        _rpm_counters[consumer.name] = [t for t in timestamps if t > window_start]
        if len(_rpm_counters[consumer.name]) >= consumer.rpm_cap:
            return False
        _rpm_counters[consumer.name].append(now)
        return True


def _reset_rpm_counters_for_testing() -> None:
    """Clear all in-memory RPM counters.  Only for use in tests."""
    with _rpm_lock:
        _rpm_counters.clear()
