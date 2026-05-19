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
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)


def _configure_framework_logging() -> None:
    """Wire the root logger so framework.* log.info()/warning()/error() calls
    land in ~/.kbf/kbf-server.log alongside uvicorn's lines.

    Previously, anything emitted via `log = logging.getLogger(__name__)` and
    `log.info(...)` was silently dropped: uvicorn's --log-level=info only
    configures uvicorn's OWN loggers, not the framework's. That made
    incidents (BUG-queue-d3ec0, synth-tpm-14a54555, synth-tpm-3bda58fe)
    much harder to diagnose because the application-level signal — INGEST
    starting, codex calls, write_artifacts attempts, retry exhaustion —
    was never persisted.

    Configuration:
      - INFO level by default (override via KBF_LOG_LEVEL=DEBUG/WARNING/etc.)
      - Rotating file handler at ~/.kbf/kbf-server.log
      - 10 MB per file, 5 backups (~50 MB ceiling)
      - Idempotent: safe to call from lifespan even if uvicorn already added
        handlers — we attach to the root logger, not to uvicorn's specific ones.
    """
    log_dir = Path(os.environ.get("KBF_LOG_DIR", str(Path.home() / ".kbf")))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "kbf-server.log"

    level_name = os.environ.get("KBF_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    # Don't double-attach if this function ran already (e.g. uvicorn --reload).
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == str(log_path):
            root.setLevel(level)
            return

    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,               # keep 5 rotations (~50 MB total)
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(level)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down noisy third-party loggers; their INFO is mostly traffic detail.
    for noisy in ("urllib3", "httpx", "oracledb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log.info(
        "framework logging configured: level=%s file=%s rotation=10MB×5",
        level_name, log_path,
    )


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
    from ..retrievers.search_wiki import SearchWikiRetriever
    from ..stores.wiki_metadata_store import build_wiki_store
    from ..adapters.udap_adapter import UdapAdapter
    from ..retrievers.tools import register_v1_tools
    from ..workflow_runtime.skill_registry import register_workflow_skills_as_mcp_tools
    from ..workflow_runtime.executor import WorkflowExecutor

    # Sprint 2 — auth, session, cost
    from .auth.middleware import bearer_auth_middleware
    from .auth.registry import ConsumerRegistry
    from .session.factory import build_session_store
    from .cost_store import CostStore
    from .error_store import ErrorStore
    from .artifact_store import build_artifact_store
    from .skill_store import build_skill_store

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

        # Configure logging FIRST — every subsequent log.info() in this
        # function (and across framework.*) needs the file handler in place.
        # Anything emitted before this call would land on stderr only.
        _configure_framework_logging()

        # --- Auth + session + cost telemetry ---
        log.info("initialising auth/session/cost infrastructure…")
        store_root = os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))

        app.state.consumer_registry = ConsumerRegistry(CONSUMER_MANIFESTS_DIR)

        # ADB pool — required for all environments (laptop, staging, production).
        # ADB is always available; there is no filestore fallback.
        # For laptop: bastion auto-reconnect via adb-connect.sh (ADR-019).
        # For staging/prod: direct wallet connection (no bastion).
        # Raises RuntimeError on pool init failure — server must not start without DB.
        kbf_env = os.environ.get("KBF_ENV", "laptop")
        log.info("initialising ADB pool (env=%s)…", kbf_env)
        adb_pool = _init_adb_pool(REPO_ROOT, kbf_env)
        log.info("ADB pool ready (env=%s)", kbf_env)
        app.state.adb_pool = adb_pool

        # Bug DB pool (DECISION-009) — non-fatal; falls back to adb_pool if absent.
        log.info("initialising bug DB pool (env=%s)…", kbf_env)
        bug_pool = _init_bug_pool(REPO_ROOT, kbf_env)
        if bug_pool is None:
            log.warning(
                "bug DB pool unavailable — bug writes will use main adb_pool (DECISION-009)"
            )
            bug_pool = adb_pool   # fallback so AdbErrorStore always gets a pool
        app.state.bug_pool = bug_pool

        # All stores are ADB-backed — no filestore fallback.
        from .error_store import AdbErrorStore
        from .cost_store import AdbCostStore
        app.state.session_store = build_session_store(pool=adb_pool)
        app.state.artifact_store = build_artifact_store(pool=adb_pool, env=kbf_env)
        app.state.skill_store = build_skill_store(pool=adb_pool, env=kbf_env)
        app.state.error_store = AdbErrorStore(bug_pool, store_root)
        app.state.cost_store = AdbCostStore(adb_pool, store_root)

        app.state.startup_time = time.time()

        # --- Shims + LLM ---
        log.info("loading shim_faaas…")
        state["shim_faaas"] = ShimFaaas(SHIM_FAAAS_PATH)
        state["shim_kb"] = ShimKb(PERSONA_BUILDERS_DIR, skill_store=app.state.skill_store)
        app.state.shim_kb = state["shim_kb"]
        # Apply LLM overrides from env-specific config (laptop.yaml [llm] section etc.)
        # _load_env_llm_overrides reads the [llm] section from the active env YAML
        # and returns kwargs that override adapters/llm.yaml defaults.  This ensures
        # auth: config_file flows through on laptop rather than falling back to the
        # adapters/llm.yaml default of auth: instance_principal.
        llm_kwargs: dict = _load_env_llm_overrides(REPO_ROOT, kbf_env)
        state["llm"] = LLMClient(**llm_kwargs)
        app.state.llm = state["llm"]

        # --- Stores + retrievers ---
        log.info("initializing retrievers…")
        from ..stores.incident_vector_store import IncidentVectorStore

        # Load base URLs from adapter configs for real citation URLs (GAP-C1)
        jira_base_url = _load_adapter_base_url(
            REPO_ROOT / "framework" / "config" / "adapters" / "jira.yaml"
        )
        confluence_base_url = _load_adapter_base_url(
            REPO_ROOT / "framework" / "config" / "adapters" / "confluence.yaml"
        )
        store = IncidentVectorStore(
            adb_pool=None,
            llm=state["llm"],
            jira_base_url=jira_base_url,
            confluence_base_url=confluence_base_url,
        )
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

        # Wiki store — DECISION-022: ADB-backed when ADB pool is available so
        # promoted skills are portable across hosts.  build_wiki_store() logs
        # the selection explicitly (never silent: WARNING if filestore fallback).
        wiki_store = build_wiki_store(pool=adb_pool, env=kbf_env)
        state["wiki_store"] = wiki_store

        retrievers = {
            "vector_search": VectorSearchRetriever(state["stores"]),
            "get_incident_summary": GetIncidentSummaryRetriever(store),
            "list_sources": ListSourcesRetriever(state["shim_faaas"], state["shim_kb"]),
            # Phase 2 tools
            "query_fleet": QueryFleetRetriever(udap_adapter),
            "text_to_sql": TextToSqlRetriever(llm=state["llm"], udap_adapter=udap_adapter),
            "find_symbol": FindSymbolRetriever(store_root=store_root),
            "read_code_page": ReadCodePageRetriever(store_root=store_root),
            # search_wiki: lexical search over WikiMetadataStore records,
            # written by ConfluenceWikiIngestor when KB.kind == "wiki".
            # Closes the retrieval gap that left
            # synth-tpm-bcbc739d/weekly_exec_review_26ai with no answers.
            "search_wiki": SearchWikiRetriever(wiki_store=wiki_store),
        }
        state["retrievers"] = retrievers
        # Internal tool registry — NOT exposed on /mcp/tools
        state["tool_registry"] = register_v1_tools(retrievers)

        # --- Workflow skills (internal only) ---
        log.info("registering workflow skills as MCP tools…")
        workflow_registry = register_workflow_skills_as_mcp_tools(WORKFLOW_SKILLS_DIR)
        state["workflow_registry"] = workflow_registry

        # ADR-032 P2-Infra: Optional Confluence adapter for ask_parameterized
        # skill ephemeral fetch.  Graceful optional dependency: if the adapter
        # cannot be built (no creds / unreachable), the server still starts and
        # ask_parameterized skills hard-fail with an actionable message at
        # consumption time (never silent).
        #
        # FIX (synth-tpm-afcacfc5 / BUG-queue-<NEW>): build the adapter
        # UNCONDITIONALLY — do NOT gate on _any_promoted_skill_requires_ephemeral.
        # The prior gate caused a chicken-and-egg: the first in-authoring
        # ask_parameterized skill could never pass EVAL Path-A because the
        # adapter was None (no promoted skill yet → gate=False → adapter=None →
        # hard-fail).  build_confluence_adapter already returns None safely when
        # no Confluence config exists (fixture/dev), so unconditional construction
        # is safe and side-effect-free when unconfigured.
        # _any_promoted_skill_requires_ephemeral is kept (may be used elsewhere).
        from ..adapters.confluence.factory import build_confluence_adapter as _build_confluence_adapter_factory
        from ..workflow_runtime.executor import _any_promoted_skill_requires_ephemeral  # kept — do not remove

        confluence_adapter = None
        try:
            confluence_adapter = _build_confluence_adapter_factory(kbf_env, REPO_ROOT)
        except Exception as _ca_exc:
            log.warning(
                "ADR-032: Confluence adapter init raised unexpectedly (%s: %s) — "
                "ask_parameterized skills will hard-fail at consumption time.",
                type(_ca_exc).__name__, _ca_exc,
            )
            confluence_adapter = None

        if confluence_adapter is None:
            log.warning(
                "ADR-032: no Confluence adapter configured for env=%r — "
                "ask_parameterized skills with ingest_on_demand:true will hard-fail "
                "at consumption time with an actionable message (never silent). "
                "Configure framework/config/adapters/confluence.yaml to enable "
                "ephemeral fetch.",
                kbf_env,
            )
        else:
            _mode = getattr(confluence_adapter, "mode", None) or type(confluence_adapter).__name__
            log.info(
                "ADR-032: Confluence adapter initialized for ephemeral fetch (env=%s mode=%s).",
                kbf_env, _mode,
            )

        # Store on app.state so the consumption path (P2-Exec) can access it
        # via request.app.state.confluence_adapter.  None = not available.
        app.state.confluence_adapter = confluence_adapter

        # Wire retrievers + shim_kb into the executor so it can fetch actual
        # ingested content (search_wiki for wiki KBs, vector_search for
        # vector KBs, etc.) instead of falling back to fixture data.
        # Without this, on-request artifact_url skills got rendered with
        # the FIRST matching fixture file's content (weekly_exec_review_26ai
        # ended up filled with tpm_weekly_ops fixture data).
        #
        # ADR-032 P2-Exec (RECONCILIATION): confluence_adapter is passed so
        # ask_parameterized skills can fetch the consumer-supplied page ephemerally.
        # None = adapter not configured; ask_parameterized skills hard-fail
        # actionably (never silently fall back to wrong content).
        # Backward-compatible: author_fixed skills are unaffected by this param.
        state["workflow_executor"] = WorkflowExecutor(
            store=None,
            llm=state["llm"],
            retrievers=retrievers,
            shim_kb=state["shim_kb"],
            confluence_adapter=confluence_adapter,  # ADR-032 P2-Exec — may be None
            wiki_store=wiki_store,  # Issue-1a fix: Strategy 1b direct lookup by canonical_id
        )
        # Expose on app.state so the ask route + MCP handler can call it from
        # within a request (see _maybe_render_artifact in routes/ask.py).
        # Without this attribute the render hook silently no-ops with
        # "render: app.state.workflow_executor missing".
        app.state.workflow_executor = state["workflow_executor"]

        def _make_wf_callable_from_cfg(cfg: dict, executor):
            """Return a callable that executes the skill from an in-memory cfg dict.

            ADR-033: used for promoted skills whose cfg was read from ADB, so
            the internal tool registry also uses the ADB-authoritative definition.
            """
            def _call(inputs: dict) -> dict:
                return executor.execute_from_config(cfg, inputs)
            return _call

        def _make_wf_callable(skill_path: str, executor):
            """Return a callable that executes the skill from disk (laptop/no-store path)."""
            def _call(inputs: dict) -> dict:
                from pathlib import Path as _Path
                return executor.execute(_Path(skill_path), inputs)
            return _call

        # ADR-033: for promoted skills, register using ADB artifact cfg
        # (via shim_workflows ADB-sourced cards) rather than disk paths.
        # For the disk-only path (laptop/no-store), fall back to the path-based callable.
        _wf_skill_store = app.state.skill_store
        for tool_name, wf_tool in workflow_registry.items():
            persona = wf_tool.persona
            skill_name_local = wf_tool.skill_name
            # Try ADB artifact first (ADR-033)
            if _wf_skill_store is not None:
                try:
                    _content = _wf_skill_store.read_artifact(persona, skill_name_local, "workflow_skill")
                except Exception:
                    _content = None
                if _content is not None:
                    try:
                        import yaml as _yaml_local
                        _cfg_from_adb = _yaml_local.safe_load(_content) or {}
                        state["tool_registry"][tool_name] = _make_wf_callable_from_cfg(
                            _cfg_from_adb, state["workflow_executor"],
                        )
                        log.debug(
                            "internal tool registry: %s registered from ADB artifact (ADR-033)",
                            tool_name,
                        )
                        continue
                    except Exception:
                        pass
            # Disk fallback
            state["tool_registry"][tool_name] = _make_wf_callable(
                wf_tool._path or wf_tool.skill_config.get("skill_path", ""),
                state["workflow_executor"],
            )
        log.info(
            "registered %d workflow skills as internal MCP tools: %s",
            len(workflow_registry), list(workflow_registry.keys()),
        )

        # --- Persona skills ---
        from ..persona_skills.pm import PmSkill
        from ..persona_skills.tpm import TpmSkill
        from ..orchestrator.shim_workflows import ShimWorkflows

        shim_workflows = ShimWorkflows(
            WORKFLOW_SKILLS_DIR,
            skill_store=app.state.skill_store,
        )
        state["shim_workflows"] = shim_workflows
        app.state.shim_workflows = shim_workflows  # expose for hot-reload after PROMOTE

        ops_eng_skill = OpsEngSkill(
            llm=state["llm"], shim_kb=state["shim_kb"], retrievers=retrievers,
        )
        pm_skill = PmSkill(
            llm=state["llm"], shim_kb=state["shim_kb"], retrievers=retrievers,
        )
        tpm_skill = TpmSkill(
            llm=state["llm"], shim_kb=state["shim_kb"], retrievers=retrievers,
        )
        state["skills"] = {
            "ops_eng": ops_eng_skill,
            "pm":      pm_skill,
            "tpm":     tpm_skill,
        }

        # --- Orchestrator ---
        ctx_builder = ContextBuilder(
            llm=state["llm"], shim_faaas=state["shim_faaas"],
            shim_kb=state["shim_kb"], skills_by_persona=state["skills"],
            synthesizer=Synthesizer(state["llm"]),
            shim_workflows=shim_workflows,
            cost_store=app.state.cost_store,
        )
        state["context_builder"] = ctx_builder
        app.state.context_builder = ctx_builder

        # --- kbf_ops session loader (ADR-023) ---
        from ..retrievers.kbf_ops.session_loader import KbfOpsSessionLoader
        app.state.kbf_ops_loader = KbfOpsSessionLoader(
            pool=adb_pool,
            session_store=app.state.session_store,
            skill_store=app.state.skill_store,
            artifact_store=getattr(app.state, "artifact_store", None),
        )

        # --- External MCP tool registry (5 tools, including reviewSkillSession) ---
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
    # MCP Streamable HTTP transport — JSON-RPC 2.0 (MCP spec 2025-03-26)
    # This is the single endpoint that Claude Code's native MCP HTTP client
    # connects to.  The existing /mcp/tools/* REST routes remain unchanged
    # for backward compatibility with other clients.
    # ----------------------------------------------------------------
    from .mcp_transport import register_mcp_transport
    register_mcp_transport(app, state)

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


# ---------------------------------------------------------------------------
# ADB pool helpers
# ---------------------------------------------------------------------------

def _resolve_secret(ref: str) -> str:
    """Resolve an ``env://VAR_NAME`` reference to its environment variable value.

    Returns the literal string unchanged if it is not an ``env://`` reference.
    Returns ``""`` for an empty ref.  Logs a warning when the env var is unset.
    """
    if not ref:
        return ""
    if ref.startswith("env://"):
        var = ref[len("env://"):]
        val = os.environ.get(var, "")
        if not val:
            log.warning("mcp_server: env var %s is not set (required for ADB auth)", var)
        return val
    return ref  # literal value — not recommended but supported


def _init_adb_pool(repo_root: Path, kbf_env: str):
    """Initialise and return an oracledb connection pool for any environment.

    Reads the env-specific config YAML (laptop.yaml, staging.yaml, prod.yaml),
    resolves ``env://`` secret references, and calls ``create_adb_pool``.

    For laptop: includes bastion auto-reconnect config (ADR-019).
    For staging/prod: direct wallet connection (no bastion).

    Raises:
        RuntimeError: if pool initialisation fails for any reason.
            ADB is always available — there is no filestore fallback.
    """
    cfg_name = {
        "laptop":     "laptop.yaml",
        "staging":    "staging.yaml",
        "production": "prod.yaml",
    }.get(kbf_env, f"{kbf_env}.yaml")
    cfg_path = repo_root / "framework" / "config" / cfg_name

    if not cfg_path.exists():
        raise RuntimeError(
            f"ADB pool init failed: config file {cfg_path} not found "
            f"(KBF_ENV={kbf_env!r}). Cannot start without ADB."
        )

    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "ADB pool init failed: PyYAML not installed. "
            "Run: pip install pyyaml"
        ) from exc

    try:
        with open(cfg_path) as fh:
            raw = yaml.safe_load(fh)

        adb_raw = raw.get("adb", {})
        bastion_raw = raw.get("bastion", {})

        admin_password = _resolve_secret(
            adb_raw.get("admin_password_secret") or adb_raw.get("password_secret", "")
        )
        wallet_password = _resolve_secret(
            adb_raw.get("wallet_password_secret", "")
        )
        wallet_path = str(Path(adb_raw.get("wallet_path", "")).expanduser())

        pool_config: dict = {
            "deployment_mode": raw.get("deployment_mode", kbf_env),
            "adb": {
                "service_name": adb_raw.get("dsn") or adb_raw.get("service_name", ""),
                "wallet_path": wallet_path,
                "user": adb_raw.get("admin_user") or adb_raw.get("user", "Admin"),
                "password": admin_password,
                "wallet_password": wallet_password,
            },
        }

        if bastion_raw:
            pool_config["bastion"] = {
                "bastion_ocid":            bastion_raw.get("bastion_ocid", ""),
                "target_db_host":          bastion_raw.get("target_db_host", ""),
                "target_db_port":          bastion_raw.get("target_db_port", 1522),
                "local_tunnel_port":       bastion_raw.get("local_tunnel_port", 1522),
                "ssh_key_path":            bastion_raw.get("ssh_key_path", "~/.ssh/id_rsa"),
                "session_ttl_seconds":     bastion_raw.get("session_ttl_seconds", 10800),
                "oci_cli_path":            bastion_raw.get("oci_cli_path", "/opt/homebrew/bin/oci"),
                "connect_timeout_seconds": bastion_raw.get("connect_timeout_seconds", 30),
                "max_reconnect_attempts":  bastion_raw.get("max_reconnect_attempts", 3),
                "script_path":             bastion_raw.get("script_path", ""),
                "oci_profile":             bastion_raw.get("oci_profile", ""),
            }

        from ..core.adb_pool import create_adb_pool
        pool = create_adb_pool(pool_config)
        if pool is None:
            raise RuntimeError(
                f"create_adb_pool returned None for env={kbf_env!r} — "
                "oracledb unavailable or pool config invalid. Cannot start without ADB."
            )
        return pool

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"ADB pool init failed for env={kbf_env!r} "
            f"({type(exc).__name__}: {exc}). Cannot start without ADB."
        ) from exc


def _init_bug_pool(repo_root: Path, kbf_env: str):
    """Initialise and return a dedicated oracledb connection pool for bug storage.

    Reads the same env-specific config YAML as ``_init_adb_pool``, then merges
    the ``adb`` section (base) with the ``bug_db`` section (overrides) per
    DECISION-009.  The resulting pool connects as the ``KBF_BUGS`` user (or
    whatever ``bug_db.user`` specifies).

    Inheritance rules (any key absent from ``bug_db`` falls back to ``adb``):
      - dsn / service_name  → adb.dsn / adb.service_name
      - wallet_path         → adb.wallet_path
      - wallet_password_secret → adb.wallet_password_secret
      - bastion             → always from the top-level ``bastion`` section

    Returns:
        oracledb pool on success.
        None if ``bug_db`` section is absent from config (non-fatal).
        None if pool creation fails (non-fatal; caller falls back to adb_pool).
    """
    cfg_name = {
        "laptop":     "laptop.yaml",
        "staging":    "staging.yaml",
        "production": "prod.yaml",
    }.get(kbf_env, f"{kbf_env}.yaml")
    cfg_path = repo_root / "framework" / "config" / cfg_name

    if not cfg_path.exists():
        log.warning(
            "bug DB pool: config file %s not found (KBF_ENV=%r) — skipping",
            cfg_path, kbf_env,
        )
        return None

    try:
        import yaml  # type: ignore[import]
    except ImportError:
        log.warning("bug DB pool: PyYAML not installed — skipping")
        return None

    try:
        with open(cfg_path) as fh:
            raw = yaml.safe_load(fh)

        bug_db_raw = raw.get("bug_db", {})
        if not bug_db_raw:
            log.warning(
                "bug DB pool: no 'bug_db' section in %s — skipping (DECISION-009)",
                cfg_name,
            )
            return None

        adb_raw = raw.get("adb", {})
        bastion_raw = raw.get("bastion", {})

        # Merge: adb provides defaults; bug_db overrides any key it specifies.
        # Connection parameters are resolved field-by-field so inheritance is
        # explicit rather than a blind dict merge.
        service_name = (
            bug_db_raw.get("dsn")
            or bug_db_raw.get("service_name")
            or adb_raw.get("dsn")
            or adb_raw.get("service_name", "")
        )
        wallet_path = str(
            Path(
                bug_db_raw.get("wallet_path") or adb_raw.get("wallet_path", "")
            ).expanduser()
        )
        wallet_password = _resolve_secret(
            bug_db_raw.get("wallet_password_secret")
            or adb_raw.get("wallet_password_secret", "")
        )
        user = bug_db_raw.get("user", "KBF_BUGS")
        password = _resolve_secret(
            bug_db_raw.get("password_secret", "")
        )

        pool_config: dict = {
            "deployment_mode": raw.get("deployment_mode", kbf_env),
            "adb": {
                "service_name":   service_name,
                "wallet_path":    wallet_path,
                "user":           user,
                "password":       password,
                "wallet_password": wallet_password,
            },
        }

        # Bastion config is always inherited from the top-level bastion section.
        if bastion_raw:
            pool_config["bastion"] = {
                "bastion_ocid":            bastion_raw.get("bastion_ocid", ""),
                "target_db_host":          bastion_raw.get("target_db_host", ""),
                "target_db_port":          bastion_raw.get("target_db_port", 1522),
                "local_tunnel_port":       bastion_raw.get("local_tunnel_port", 1522),
                "ssh_key_path":            bastion_raw.get("ssh_key_path", "~/.ssh/id_rsa"),
                "session_ttl_seconds":     bastion_raw.get("session_ttl_seconds", 10800),
                "oci_cli_path":            bastion_raw.get("oci_cli_path", "/opt/homebrew/bin/oci"),
                "connect_timeout_seconds": bastion_raw.get("connect_timeout_seconds", 30),
                "max_reconnect_attempts":  bastion_raw.get("max_reconnect_attempts", 3),
                "script_path":             bastion_raw.get("script_path", ""),
                "oci_profile":             bastion_raw.get("oci_profile", ""),
            }

        from ..core.adb_pool import create_adb_pool
        pool = create_adb_pool(pool_config)
        if pool is None:
            log.warning(
                "bug DB pool: create_adb_pool returned None for env=%r — skipping",
                kbf_env,
            )
            return None

        log.info("bug DB pool ready (env=%s user=%s)", kbf_env, user)
        return pool

    except Exception as exc:
        log.warning(
            "bug DB pool init failed for env=%r (%s: %s) — skipping",
            kbf_env, type(exc).__name__, exc,
        )
        return None


def _load_env_llm_overrides(repo_root: Path, kbf_env: str) -> dict:
    """Read [llm] section from the active env config YAML and return kwargs for LLMClient().

    The env-specific YAML (laptop.yaml, staging.yaml, prod.yaml) is allowed to
    override any key that the generic adapters/llm.yaml provides.  This is the
    correct place for auth mode — adapters/llm.yaml defaults to
    ``auth: instance_principal`` for OCI Compute, but laptop.yaml must override
    with ``auth: config_file`` to use the local ~/.oci/config profile.

    Only the [llm] section keys present in the env YAML are forwarded as kwargs;
    absent keys fall back to adapters/llm.yaml defaults inside LLMClient().

    Returns empty dict on any error so LLMClient() falls back gracefully.
    """
    cfg_name = {
        "laptop":     "laptop.yaml",
        "staging":    "staging.yaml",
        "production": "prod.yaml",
    }.get(kbf_env, f"{kbf_env}.yaml")
    env_cfg_path = repo_root / "framework" / "config" / cfg_name
    if not env_cfg_path.exists():
        log.debug("_load_env_llm_overrides: %s not found — no LLM overrides", env_cfg_path)
        return {}
    try:
        import yaml  # type: ignore[import]
        with open(env_cfg_path) as fh:
            raw = yaml.safe_load(fh)
        llm_raw = raw.get("llm", {})
        if not llm_raw:
            return {}
        kwargs: dict = {}
        # Forward any recognised LLM init kwargs present in the env config.
        # This covers auth, config_profile, provider; endpoint/models stay in
        # adapters/llm.yaml to avoid duplication.
        for key in ("auth", "config_profile", "provider", "endpoint",
                    "compartment_ocid", "timeout_s"):
            if llm_raw.get(key):
                kwargs[key] = llm_raw[key]
        log.info(
            "env=%s: LLMClient overrides from %s: %s",
            kbf_env, cfg_name, kwargs,
        )
        return kwargs
    except Exception as exc:
        log.warning(
            "env=%s: could not load LLM overrides from %s (%s) — using adapter defaults",
            kbf_env, cfg_name, exc,
        )
        return {}


# Keep old name as an alias for any external callers that may reference it.
def _load_laptop_llm_overrides(repo_root: Path) -> dict:  # pragma: no cover
    """Deprecated alias — use _load_env_llm_overrides(repo_root, 'laptop') instead."""
    return _load_env_llm_overrides(repo_root, "laptop")


def _load_adapter_base_url(adapter_yaml_path: Path) -> str:
    """Read the ``native.base_url`` from an adapter YAML config.

    Returns empty string on any error (dev/filestore mode — citation fallback).
    """
    if not adapter_yaml_path.exists():
        return ""
    try:
        import yaml  # type: ignore[import]
        with open(adapter_yaml_path) as fh:
            raw = yaml.safe_load(fh)
        return raw.get("native", {}).get("base_url", "")
    except Exception as exc:
        log.debug("could not load base_url from %s: %s", adapter_yaml_path, exc)
        return ""


app = _load_app()
