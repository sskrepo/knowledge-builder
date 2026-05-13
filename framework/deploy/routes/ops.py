"""Operational endpoints — /healthz, /api/v1/version, /api/v1/metrics/cost.

Implements the Operations tag from the OpenAPI 3.1 spec (framework/deploy/openapi.yaml):
  GET /healthz              — no auth; health check for all components
  GET /api/v1/version       — no auth; API/schema/build version
  GET /api/v1/metrics/cost  — admin scope required; token usage telemetry

Auth notes:
  - /healthz and /api/v1/version are in the _AUTH_SKIP_PATHS set in
    auth/middleware.py so the bearer middleware never runs for them.
  - These route handlers therefore do NOT call get_consumer(). Calling
    get_consumer() on an unauthenticated request would raise AttributeError.
  - /api/v1/metrics/cost: middleware runs, attaches consumer, handler calls
    get_consumer() then require_scope(consumer, "admin").

All responses use to_camel_response() per the camelCase external-API rule.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Query, Request

from ..auth.middleware import get_consumer, require_scope
from ..serialization import to_camel_response
from ...version import API_VERSION, GIT_SHA, BUILD_REF, SCHEMA_VERSION

log = logging.getLogger(__name__)

# Emit the build ref once at import time so it appears in the server startup log
# alongside uvicorn's "Application startup complete" message.
log.info("KBF build: %s", BUILD_REF)

# Wall-clock start time — used for uptimeSeconds computation.
_PROCESS_START = time.monotonic()

# Repository root: three parents above framework/deploy/routes/ops.py
# ops.py → routes/ → deploy/ → framework/ → repo_root/
_REPO_ROOT = Path(__file__).resolve().parents[3]

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------


@router.get("/healthz", tags=["Operations"])
async def healthz(request: Request):
    """Health check — no auth required.

    Checks:
      adb           — ADB connection pool (app.state.adb_pool)
      llm           — LLM client (app.state.llm)
      git           — .git directory present in repo root
      confluenceAdapter — placeholder; not yet wired
      jiraAdapter       — placeholder; not yet wired

    Returns HTTP 200 if status=="healthy", 503 if "degraded".
    """
    checks: dict[str, str] = {}
    any_error = False

    # -- ADB pool --
    adb_pool = getattr(request.app.state, "adb_pool", None)
    if adb_pool is None:
        checks["adb"] = "not_configured"
    else:
        try:
            # oracledb pools expose .ping(); treat any exception as failure.
            if hasattr(adb_pool, "ping"):
                adb_pool.ping()
            checks["adb"] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.warning("healthz: adb ping failed: %s", exc)
            checks["adb"] = f"error: {exc}"
            any_error = True

    # -- LLM client --
    llm = getattr(request.app.state, "llm", None)
    if llm is None:
        checks["llm"] = "not_configured"
    else:
        # No universal ping method exists; treat presence as ok.
        checks["llm"] = "ok"

    # -- Git repo --
    git_dir = _REPO_ROOT / ".git"
    if git_dir.exists():
        checks["git"] = "ok"
    else:
        checks["git"] = "error: .git not found"
        any_error = True

    # -- Adapters (placeholders — not wired in v1) --
    checks["confluenceAdapter"] = "not_configured"
    checks["jiraAdapter"] = "not_configured"

    status = "degraded" if any_error else "healthy"
    uptime_seconds = int(time.monotonic() - _PROCESS_START)

    payload = {
        "status": status,
        "checks": checks,
        "uptime_seconds": uptime_seconds,
        "version": SCHEMA_VERSION,
        "git_sha": GIT_SHA,
        "build_ref": BUILD_REF,
    }
    http_status = 503 if any_error else 200
    return to_camel_response(payload, status_code=http_status)


# ---------------------------------------------------------------------------
# GET /api/v1/version
# ---------------------------------------------------------------------------


@router.get("/api/v1/version", tags=["Operations"])
async def get_version():
    """Return API/schema/build version — no auth required."""
    payload = {
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
        "git_sha": GIT_SHA,
        "build_ref": BUILD_REF,
    }
    return to_camel_response(payload)


# ---------------------------------------------------------------------------
# GET /api/v1/metrics/cost
# ---------------------------------------------------------------------------


@router.get("/api/v1/metrics/cost", tags=["Operations"])
async def get_cost_metrics(
    request: Request,
    persona: Optional[str] = Query(default=None),
    skill_name: Annotated[Optional[str], Query(alias="skillName")] = None,
    start_date: Annotated[Optional[str], Query(alias="startDate")] = None,
    end_date: Annotated[Optional[str], Query(alias="endDate")] = None,
):
    """Token usage telemetry — requires admin scope.

    Query params (all optional):
      persona     — filter by persona slug (e.g. "ops_eng")
      skill_name  — filter by skill name
      start_date  — ISO-8601 date YYYY-MM-DD (inclusive lower bound)
      end_date    — ISO-8601 date YYYY-MM-DD (inclusive upper bound)

    Reads from app.state.cost_store (CostStore instance). If cost_store is
    None (not configured), returns a zeroed response rather than 503.
    """
    consumer = get_consumer(request)
    require_scope(consumer, "admin")

    cost_store = getattr(request.app.state, "cost_store", None)
    if cost_store is None:
        # Cost store not wired — return zeroed response per spec.
        payload = {
            "period": {
                "start": start_date or "",
                "end": end_date or "",
            },
            "total_tokens": 0,
            "by_persona": {},
            "by_operation": {},
        }
        return to_camel_response(payload)

    result = cost_store.query(
        persona=persona,
        skill_name=skill_name,
        start_date=start_date,
        end_date=end_date,
    )
    return to_camel_response(result)
