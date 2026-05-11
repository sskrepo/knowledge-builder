"""FastAPI MCP server — exposes Phase 1 + Phase 2 retrieval tools.

Run:  uvicorn framework.deploy.mcp_server:app --host 0.0.0.0 --port 8080
Health: GET /healthz
MCP:    POST /mcp/tools/call  body: {"name": "vector_search", "arguments": {...}}

Phase 1 tools: vector_search, get_incident_summary, list_sources
Phase 2 tools: query_fleet, text_to_sql, find_symbol, read_code_page

All Phase 2 tools work in filestore mode (KBF_STORE_BACKEND=filestore).
"""
from __future__ import annotations

import logging
import os
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

    REPO_ROOT = Path(__file__).resolve().parents[2]
    SHIM_FAAAS_PATH = REPO_ROOT / "framework" / "config" / "shim_faaas.yaml"
    PERSONA_BUILDERS_DIR = REPO_ROOT / "framework" / "persona_builders"
    WORKFLOW_SKILLS_DIR = REPO_ROOT / "framework" / "workflow_skills"

    state: dict = {}

    @asynccontextmanager
    async def lifespan(app):
        # Startup: load shims, build registry
        log.info("loading shim_faaas…")
        state["shim_faaas"] = ShimFaaas(SHIM_FAAAS_PATH)
        state["shim_kb"] = ShimKb(PERSONA_BUILDERS_DIR)
        state["llm"] = LLMClient()

        # Lazy-init store + retrievers (real ADB pool needed for production use)
        log.info("initializing retrievers…")
        # NB: in real deploy, an oracledb pool is created and passed in.
        # Phase 1 scaffold leaves pool=None — vector_search returns []
        from ..stores.incident_vector_store import IncidentVectorStore
        store = IncidentVectorStore(adb_pool=None, llm=state["llm"])
        state["stores"] = {"ops_incidents": store}

        # Phase 2: UDAP adapter + fleet/code retrievers
        udap_cfg = {
            "connection": {},
            "allowlisted_views_file": str(REPO_ROOT / "framework" / "retrievers" / "fleet_views.yaml"),
            "text_to_sql": {"guardrails": {"max_rows": 1000}},
        }
        udap_adapter = UdapAdapter(udap_cfg)
        state["udap_adapter"] = udap_adapter

        store_root = os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))

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
        state["tool_registry"] = register_v1_tools(retrievers)

        # Workflow skills as MCP tools — registered at startup per ADR-016
        log.info("registering workflow skills as MCP tools…")
        workflow_registry = register_workflow_skills_as_mcp_tools(WORKFLOW_SKILLS_DIR)
        state["workflow_registry"] = workflow_registry
        state["workflow_executor"] = WorkflowExecutor(store=None, llm=state["llm"])
        for tool_name, wf_tool in workflow_registry.items():
            state["tool_registry"][tool_name] = wf_tool.to_mcp_tool_definition()
        log.info("registered %d workflow skills as MCP tools: %s",
                 len(workflow_registry), list(workflow_registry.keys()))

        # Persona skills
        ops_eng_skill = OpsEngSkill(
            llm=state["llm"], shim_kb=state["shim_kb"], retrievers=retrievers,
        )
        state["skills"] = {"ops_eng": ops_eng_skill}

        # Orchestrator
        state["context_builder"] = ContextBuilder(
            llm=state["llm"], shim_faaas=state["shim_faaas"],
            shim_kb=state["shim_kb"], skills_by_persona=state["skills"],
            synthesizer=Synthesizer(state["llm"]),
        )
        log.info("ready")
        yield
        log.info("shutting down")

    app = FastAPI(title="Knowledge Builder Framework MCP Server",
                  version="1.0.0-phase1", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "shim_faaas": "loaded" if state.get("shim_faaas") else "missing",
            "shim_kb_cards": len(state["shim_kb"].all_cards()) if state.get("shim_kb") else 0,
            "tools": list(state.get("tool_registry", {}).keys()),
        }

    @app.post("/mcp/tools/list")
    async def tools_list():
        return {"tools": [{"name": n} for n in state.get("tool_registry", {}).keys()]}

    @app.post("/mcp/tools/call")
    async def tools_call(req: Request):
        body = await req.json()
        name = body.get("name")
        args = body.get("arguments", {})
        tool = state.get("tool_registry", {}).get(name)
        if not tool:
            raise HTTPException(404, f"unknown tool: {name}")
        try:
            result = tool(**args)
        except TypeError as e:
            raise HTTPException(400, f"bad args: {e}")
        except NotImplementedError as e:
            raise HTTPException(501, str(e))
        # Convert dataclasses / Result objects to dict
        return {"content": _serialize(result)}

    @app.post("/answer")
    async def answer(req: Request):
        """Convenience endpoint: full orchestrator round-trip."""
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
