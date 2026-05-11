"""FastAPI MCP server — exposes Phase 1 + Phase 2 retrieval tools (internal)
and the two PDD V3 external MCP tools (askKnowledgeBase, authorSkill).

Run:  uvicorn framework.deploy.mcp_server:app --host 0.0.0.0 --port 8080
Health: GET /healthz
MCP:    POST /mcp/tools/call  body: {"name": "askKnowledgeBase", "arguments": {...}}

External MCP surface (PDD V3 §7):
  askKnowledgeBase  — four-tier knowledge query entry point
  authorSkill       — stateful skill authoring session

Internal-only tools (NOT exposed through /mcp/tools endpoints):
  vector_search, get_incident_summary, list_sources (Phase 1)
  query_fleet, text_to_sql, find_symbol, read_code_page (Phase 2)

REST routes (Sprint 2, all behind bearer_auth_middleware):
  POST /api/v1/ask
  POST/GET/DELETE /api/v1/kb/authorSkill[/{synth_id}]
  GET  /healthz, /api/v1/version, /api/v1/metrics/cost
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger(__name__)


def _load_app():
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ImportError:
        log.warning("fastapi not installed; mcp_server is a stub")
        return None

    from ..core.llm import LLMClient
    from ..orchestrator.shim_faaas import ShimFaaas
    from ..orchestrator.shim_kb import ShimKb
    from ..orchestrator.synthesizer import Synthesizer
    from ..orchestrator.context_builder import ContextBuilder
    from ..persona_skills.ops_eng import OpsEngSkill
    from ..retrievers.vector_search import VectorSearchRetriever
    from ..retrievers.get_incident_summary import GetIncidentSummaryRetriever
    from ..retrievers.list_sources import ListSourcesRetriever
    from ..retrievers.query_fleet import QueryFleetRetriever
    from ..retrievers.text_to_sql import TextToSqlRetriever
    from ..retrievers.find_symbol import FindSymbolRetriever
    from ..retrievers.read_code_page import ReadCodePageRetriever
    from ..adapters.udap_adapter import UdapAdapter
    from ..retrievers.tools import register_v1_tools
    from ..workflow_runtime.skill_registry import register_workflow_skills_as_mcp_tools
    from ..workflow_runtime.executor import WorkflowExecutor

    # Sprint 2 — auth, session, cost
    from .auth.middleware import bearer_auth_middleware
    from .auth.registry import ConsumerRegistry
    from .session.factory import build_session_store
    from .cost_store import CostStore

    # Sprint 3 — route modules
    from .routes.ask import router as ask_router
    from .routes.author_skill import router as author_skill_router
    from .routes.ops import router as ops_router

    REPO_ROOT = Path(__file__).resolve().parents[2]
    SHIM_FAAAS_PATH = REPO_ROOT / "framework" / "config" / "shim_faaas.yaml"
    PERSONA_BUILDERS_DIR = REPO_ROOT / "framework" / "persona_builders"
    WORKFLOW_SKILLS_DIR = REPO_ROOT / "framework" / "workflow_skills"
    CONSUMER_MANIFESTS_DIR = REPO_ROOT / "framework" / "config" / "consumer_manifests"

    state: dict = {}

    @asynccontextmanager
    async def lifespan(app):
        # ----------------------------------------------------------------
        # Startup: infrastructure first, then retrieval layer, then wiring
        # ----------------------------------------------------------------

        # --- Auth + session + cost telemetry ---
        log.info("initialising auth/session/cost infrastructure…")
        store_root = os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))

        app.state.consumer_registry = ConsumerRegistry(CONSUMER_MANIFESTS_DIR)
        app.state.session_store = build_session_store()
        app.state.cost_store = CostStore(store_root)
        app.state.startup_time = time.time()

        # --- Shims + LLM ---
        log.info("loading shim_faaas…")
        state["shim_faaas"] = ShimFaaas(SHIM_FAAAS_PATH)
        state["shim_kb"] = ShimKb(PERSONA_BUILDERS_DIR)
        state["llm"] = LLMClient()
        app.state.llm = state["llm"]

        # --- Stores + retrievers ---
        log.info("initializing retrievers…")
        from ..stores.incident_vector_store import IncidentVectorStore
        store = IncidentVectorStore(adb_pool=None, llm=state["llm"])
        state["stores"] = {"ops_incidents": store}

        udap_cfg = {
            "connection": {},
            "allowlisted_views_file": str(
                REPO_ROOT / "framework" / "retrievers" / "fleet_views.yaml"
            ),
            "text_to_sql": {"guardrails": {"max_rows": 1000}},
        }
        udap_adapter = UdapAdapter(udap_cfg)
        state["udap_adapter"] = udap_adapter

        retrievers = {
            "vector_search": VectorSearchRetriever(state["stores"]),
            "get_incident_summary": GetIncidentSummaryRetriever(store),
            "list_sources": ListSourcesRetriever(state["shim_faaas"], state["shim_kb"]),
            # Phase 2 tools
            "query_fleet": QueryFleetRetriever(udap_adapter),
            "text_to_sql": TextToSqlRetriever(llm=state["llm"], udap_adapter=udap_adapter),
            "find_symbol": FindSymbolRetriever(store_root=store_root),
            "read_code_page": ReadCodePageRetriever(store_root=store_root),
        }
        state["retrievers"] = retrievers
        # Internal tool registry — NOT exposed on /mcp/tools
        state["tool_registry"] = register_v1_tools(retrievers)

        # --- Workflow skills (internal only) ---
        log.info("registering workflow skills as MCP tools…")
        workflow_registry = register_workflow_skills_as_mcp_tools(WORKFLOW_SKILLS_DIR)
        state["workflow_registry"] = workflow_registry
        state["workflow_executor"] = WorkflowExecutor(store=None, llm=state["llm"])
        for tool_name, wf_tool in workflow_registry.items():
            state["tool_registry"][tool_name] = wf_tool.to_mcp_tool_definition()
        log.info(
            "registered %d workflow skills as internal MCP tools: %s",
            len(workflow_registry), list(workflow_registry.keys()),
        )

        # --- Persona skills ---
        ops_eng_skill = OpsEngSkill(
            llm=state["llm"], shim_kb=state["shim_kb"], retrievers=retrievers,
        )
        state["skills"] = {"ops_eng": ops_eng_skill}

        # --- Orchestrator ---
        ctx_builder = ContextBuilder(
            llm=state["llm"], shim_faaas=state["shim_faaas"],
            shim_kb=state["shim_kb"], skills_by_persona=state["skills"],
            synthesizer=Synthesizer(state["llm"]),
        )
        state["context_builder"] = ctx_builder
        app.state.context_builder = ctx_builder

        # --- External MCP tool registry (2 tools, PDD V3 §7) ---
        from .mcp_tools import build_external_tool_registry, EXTERNAL_TOOLS_SCHEMA
        state["external_registry"] = build_external_tool_registry(app)
        state["external_tools_schema"] = EXTERNAL_TOOLS_SCHEMA

        log.info("ready — external MCP tools: %s", list(state["external_registry"].keys()))
        yield
        log.info("shutting down")

    app = FastAPI(
        title="Knowledge Builder Framework MCP Server",
        version="1.0.0-v3",
        lifespan=lifespan,
    )

    # ----------------------------------------------------------------
    # Middleware — bearer auth for all non-public paths
    # ----------------------------------------------------------------
    app.middleware("http")(bearer_auth_middleware)

    # ----------------------------------------------------------------
    # REST route groups (Sprint 2)
    # ----------------------------------------------------------------
    app.include_router(ask_router)
    app.include_router(author_skill_router)
    app.include_router(ops_router)

    # ----------------------------------------------------------------
    # MCP endpoints — external surface only
    # ----------------------------------------------------------------

    @app.post("/mcp/tools/list")
    async def tools_list():
        """Return the 2 externally-exposed MCP tool schemas (PDD V3 §7).

        Internal tools (vector_search, workflow skills, etc.) are NOT included.
        /mcp/tools/list does not require auth (mirroring the MCP protocol spec).
        """
        return {"tools": state.get("external_tools_schema", [])}

    @app.post("/mcp/tools/call")
    async def tools_call(req: Request):
        """Call an external MCP tool by name.

        Body: {"name": "askKnowledgeBase", "arguments": {...}}

        Only the 2 external tools are reachable here.  Requests for internal
        tool names (vector_search, incident_summary, workflow skills) return
        404 — they are not part of the external API surface.
        """
        body = await req.json()
        name = body.get("name")
        args = body.get("arguments", {})

        external_registry = state.get("external_registry", {})
        handler = external_registry.get(name)
        if handler is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown external tool: {name!r}. "
                       f"Available: {list(external_registry.keys())}",
            )

        # Inject _consumer from request state if middleware ran
        consumer = getattr(req.state, "consumer", None)

        try:
            result = await handler(**args, _consumer=consumer)
        except TypeError as exc:
            raise HTTPException(400, f"bad args: {exc}")
        except NotImplementedError as exc:
            raise HTTPException(501, str(exc))

        return {"content": _serialize(result)}

    # ----------------------------------------------------------------
    # Legacy /answer convenience endpoint (preserved for backward compat)
    # ----------------------------------------------------------------

    @app.post("/answer")
    async def answer(req: Request):
        """Convenience endpoint: full orchestrator round-trip.

        Preserved from Phase 1/2 for backward compatibility.
        New callers should use POST /api/v1/ask or the MCP askKnowledgeBase tool.
        """
        body = await req.json()
        query = body.get("query")
        if not query:
            raise HTTPException(400, "query required")
        ctx_builder = state.get("context_builder")
        if not ctx_builder:
            raise HTTPException(503, "context_builder not initialized")
        return ctx_builder.answer(query)

    return app


def _serialize(obj):
    """Recursively convert dataclasses to dicts."""
    from dataclasses import is_dataclass, asdict
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


app = _load_app()
