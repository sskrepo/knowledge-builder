---
title: PDD V3 Implementation Plan
status: ready-for-dev
created: 2026-05-10
owner: architect
references:
  - docs/wiki/pdd/PDD-Knowledge-Builder-Framework-v3.md
  - framework/deploy/openapi.yaml
  - framework/skill_builder/conversation.py
  - framework/orchestrator/context_builder.py
---

# PDD V3 Implementation Plan

This plan breaks PDD V3 into concrete, file-level tasks for a backend developer. Tracks are ordered by dependency; tasks within the same track can run in parallel unless a dependency is noted. Read alongside the PDD V3 document and `framework/deploy/openapi.yaml` — those two are the authoritative contracts; this plan is the how-to-build map.

## Pre-read: What already exists

Before coding, understand what is already built and what is new.

**Existing code to reuse (do not rewrite):**

| Component | Location | What it does |
|-----------|----------|--------------|
| `SkillBuilderConversation` | `framework/skill_builder/conversation.py` | Complete 14-state machine with `start()`, `respond()`, `to_dict()`, `from_dict()`. This is the core state logic — the REST layer wraps it, not replaces it. |
| `ContextBuilder.answer()` | `framework/orchestrator/context_builder.py` | Four-tier routing (classify → dispatch → cross-source → synthesize). Returns a dict. The `POST /api/v1/ask` handler wraps this call. |
| `ShimFaaas`, `ShimKb` | `framework/orchestrator/shim_faaas.py`, `shim_kb.py` | Loaded at startup, wired into `ContextBuilder`. No changes needed. |
| `Budget` | `framework/orchestrator/budget.py` | Already parameterizes max tokens. The auth middleware sets per-consumer `Budget` before calling `answer()`. |
| FastAPI app skeleton | `framework/deploy/mcp_server.py` | Existing `_load_app()` function with lifespan startup, `/healthz`, `/mcp/tools/call`. V3 adds routes to this app; the skeleton stays. |
| All retrievers, adapters, stores | `framework/retrievers/`, `framework/adapters/`, `framework/stores/` | No changes for V3. |

**What V3 adds (all new code):**

- `framework/deploy/routes/` — REST route handlers (Tracks A and F)
- `framework/deploy/auth/` — Bearer token middleware and consumer manifest loader (Track C)
- `framework/deploy/session/` — Session store interface + filestore + ADB implementations (Track D)
- `framework/deploy/serialization.py` — camelCase serializer/deserializer (Track E)
- `framework/deploy/mcp_tools.py` — The two external MCP tools replacing the current ad-hoc tool registry (Track B)
- Replacement lifespan block in `mcp_server.py` wiring everything together

---

## Track A — REST API Layer

**Goal:** FastAPI route handlers for the 6 REST endpoint groups specified in `openapi.yaml`. Each handler validates the request, calls internal services, serializes the response to camelCase.

**Dependency:** Track C (auth middleware) must be complete before routes can enforce scopes. Track E (serializer) must be complete before any handler returns a response. Track D (session store) must be complete before author-skill handlers can persist state.

### A-1: Create routes package

**File:** `framework/deploy/routes/__init__.py`

Empty package init. No logic.

**File:** `framework/deploy/routes/ask.py`

New file. Implements `POST /api/v1/ask`.

```python
from fastapi import APIRouter, Depends, Request
from ..auth.middleware import require_scope, get_consumer
from ..serialization import to_camel_response, from_camel_request
from ...orchestrator.context_builder import ContextBuilder
from ...orchestrator.budget import Budget

router = APIRouter()

@router.post("/api/v1/ask")
async def ask_knowledge_base(req: Request):
    consumer = get_consumer(req)            # set by auth middleware
    require_scope(consumer, "read")

    body = await req.json()
    # camelCase in → snake_case internally
    question    = body.get("question", "")
    persona     = body.get("persona")
    service_id  = body.get("serviceId")
    func_area   = body.get("functionalArea")
    max_results = body.get("maxResults", 10)

    if not question or len(question) > 4096:
        return _error_response(400, "invalid_argument",
                               "question must be 1–4096 characters")

    # Per-consumer token budget from manifest
    budget = Budget(
        max_tokens_in=consumer.token_budget_per_request,
        max_tokens_out=1500,
    )

    ctx: ContextBuilder = req.app.state.context_builder
    result = ctx.answer(
        query=question,
        budget=budget,
        persona_hint=persona,
        service_id_hint=service_id,
        func_area_hint=func_area,
        max_results=max_results,
    )

    # Map internal result dict → AskResponse schema (camelCase)
    return to_camel_response(_build_ask_response(result, consumer))
```

Key mapping from `context_builder.answer()` output to `AskResponse`:

| Internal key | camelCase field | Notes |
|---|---|---|
| `answer` | `answer` | direct |
| `passages[].text` + `.citation` + `.score` | `citations[]` | reshape to Citation schema |
| `intent.confidence` | `confidence` | |
| `intent.tier` | `tierUsed` | rename |
| `intent.tier` mapped to string | `tierDescription` | `{1:"workflow_skill", 2:"kb_retrieval", 3:"multi_persona_fanout", 4:"no_answer"}` |
| `cost` dict | `costTokens` | reshape to `{prompt, completion, total}` |
| skill suggestion if tier==4 | `skillSuggestion` | see §5.5 of PDD V3 |

`context_builder.answer()` currently returns `passages` as `[{text, citation, score}]`. The Citation schema in openapi.yaml additionally requires `contentId`, `chunkId`, and `metadata`. Add those fields to the passage dict when the store returns them — or default gracefully if missing (they will be empty in stub mode).

The existing `answer()` method signature is `answer(query, budget)`. It needs two new optional keyword arguments: `persona_hint`, `service_id_hint`, `func_area_hint`, `max_results`. These are routing hints passed through to the intent classifier. Modify `context_builder.py` to accept and forward them — this is a minimal, backward-compatible addition.

---

### A-2: Author-skill routes

**File:** `framework/deploy/routes/author_skill.py`

Implements the 5 session endpoints. All depend on the session store (Track D) and serializer (Track E).

```python
from fastapi import APIRouter, Request
from ..auth.middleware import require_scope, get_consumer
from ..session.store import SessionStore
from ..serialization import to_camel_response, from_camel_request, snake_to_camel
from ...skill_builder.conversation import SkillBuilderConversation

router = APIRouter()

# POST /api/v1/kb/authorSkill — start or resume
@router.post("/api/v1/kb/authorSkill")
async def start_author_skill_session(req: Request):
    consumer = get_consumer(req)
    require_scope(consumer, "write")

    body = await req.json()
    user_input = body.get("input", "")
    synth_id = body.get("synthId")           # optional resume

    store: SessionStore = req.app.state.session_store
    user_id = consumer.user_id

    if synth_id:
        # Resume: delegate to the continue handler logic
        return await _continue_session(req, synth_id, user_input, store, user_id)

    # New session
    conv = SkillBuilderConversation(user_id=user_id, llm=req.app.state.llm)
    turn = conv.start(intent_description=user_input)
    session_dict = conv.to_dict()
    session_dict["status"] = "in_progress"
    session_dict["intent"] = user_input
    store.save(session_dict, user_id=user_id, ttl_days=7)

    return to_camel_response(_turn_to_envelope(turn))


# POST /api/v1/kb/authorSkill/{synthId} — continue
@router.post("/api/v1/kb/authorSkill/{synth_id}")
async def continue_author_skill_session(synth_id: str, req: Request):
    consumer = get_consumer(req)
    require_scope(consumer, "write")

    body = await req.json()
    user_input = body.get("input", "")

    store: SessionStore = req.app.state.session_store
    return await _continue_session(req, synth_id, user_input, store, consumer.user_id)


async def _continue_session(req, synth_id, user_input, store, user_id):
    session_dict = store.load(synth_id, user_id=user_id)
    if session_dict is None:
        return _error_response(404, "session_not_found",
                               f"Session {synth_id} not found or access denied.")

    status = session_dict.get("status", "in_progress")
    if status == "expired":
        return _error_response(409, "session_expired",
                               "Session TTL elapsed. Start a new session.")
    if session_dict.get("state") == "DONE" and status not in ("committed", "in_progress"):
        return _error_response(409, "session_done",
                               "Session is in DONE state. No further input accepted.")

    conv = SkillBuilderConversation.from_dict(session_dict, llm=req.app.state.llm)
    turn = conv.respond(user_input)
    updated_dict = conv.to_dict()

    # Update status from state
    if turn.done:
        updated_dict["status"] = _derive_status(updated_dict["state"])
        updated_dict["expires_at"] = None
    store.save(updated_dict, user_id=user_id, ttl_days=7)

    return to_camel_response(_turn_to_envelope(turn))


# GET /api/v1/kb/authorSkill — list sessions
@router.get("/api/v1/kb/authorSkill")
async def list_author_skill_sessions(req: Request):
    consumer = get_consumer(req)
    require_scope(consumer, "read")

    store: SessionStore = req.app.state.session_store
    sessions = store.list_for_user(consumer.user_id)
    # sessions is list[dict] with snake_case keys from the store
    # Output: SessionListResponse
    items = [_session_to_list_item(s) for s in sessions]
    return to_camel_response({"sessions": items})


# GET /api/v1/kb/authorSkill/{synthId} — get session state
@router.get("/api/v1/kb/authorSkill/{synth_id}")
async def get_author_skill_session(synth_id: str, req: Request):
    consumer = get_consumer(req)
    require_scope(consumer, "read")

    store: SessionStore = req.app.state.session_store
    session_dict = store.load(synth_id, user_id=consumer.user_id)
    if session_dict is None:
        return _error_response(404, "session_not_found",
                               f"Session {synth_id} not found or access denied.")

    conv = SkillBuilderConversation.from_dict(session_dict, llm=req.app.state.llm)
    state_snapshot = conv.get_state()   # existing method returns snake_case dict
    envelope = _snapshot_to_envelope(state_snapshot, session_dict)
    return to_camel_response(envelope)


# DELETE /api/v1/kb/authorSkill/{synthId} — cancel
@router.delete("/api/v1/kb/authorSkill/{synth_id}")
async def cancel_author_skill_session(synth_id: str, req: Request):
    consumer = get_consumer(req)
    require_scope(consumer, "write")

    store: SessionStore = req.app.state.session_store
    session_dict = store.load(synth_id, user_id=consumer.user_id)
    if session_dict is None:
        return _error_response(404, "session_not_found",
                               f"Session {synth_id} not found or access denied.")

    committed_before = session_dict.get("state") in (
        "COMMITTED", "VALIDATE", "INGEST", "EVAL", "PROMOTE", "DONE"
    )
    store.abandon(synth_id, user_id=consumer.user_id)

    return to_camel_response({
        "synth_id": synth_id,       # serializer converts to synthId
        "status": "abandoned",
        "message": (
            "Session abandoned. Previously committed artifacts are retained in git."
            if committed_before else
            "Session abandoned. No files were committed."
        ),
        "committed_before_abandon": committed_before,
    })
```

Helper functions (in the same file):

```python
def _turn_to_envelope(turn: ConversationTurn) -> dict:
    """Map ConversationTurn → AuthorSkillResponse (snake_case; serializer converts to camelCase)."""
    return {
        "synth_id": turn.synth_id,
        "state": turn.state,
        "message": turn.message,
        "data": turn.data or {},
        "options": turn.options or [],
        "progress": turn.progress or {},
        "done": turn.done,
    }

def _snapshot_to_envelope(state_snapshot: dict, session_dict: dict) -> dict:
    """Build GET response envelope with sessionSummary."""
    # Re-produce the message + options from the current state
    # by temporarily calling _prompt_* — or store last_turn in session_dict.
    # Simpler: store last_turn in session_dict at save time (see Track D note).
    last_turn = session_dict.get("last_turn", {})
    return {
        **last_turn,
        "session_summary": {
            "persona": state_snapshot.get("persona"),
            "intent": state_snapshot.get("intent_description"),
            "artifact_path": state_snapshot.get("artifact_path"),
            "fields_confirmed": state_snapshot.get("fields", []),
            "sources_configured": [s.get("kind", str(s)) for s in state_snapshot.get("sources", [])],
            "triggers_configured": state_snapshot.get("trigger"),
        }
    }

def _session_to_list_item(s: dict) -> dict:
    """Reshape session store record → SessionListItem (snake_case)."""
    return {
        "synth_id": s["synth_id"],
        "persona": s.get("persona"),
        "skill_name": s.get("skill_name"),
        "intent": s.get("intent", ""),
        "state": s.get("state", ""),
        "progress": s.get("progress", {}),
        "created_at": s.get("created_at", ""),
        "updated_at": s.get("updated_at", ""),
        "expires_at": s.get("expires_at"),
        "status": s.get("status", "in_progress"),
    }

def _derive_status(state: str) -> str:
    if state == "DONE":
        return "promoted"       # assume promoted when DONE (check promote result for nuance)
    if state in ("COMMITTED", "VALIDATE", "INGEST", "EVAL"):
        return "committed"
    return "in_progress"

def _error_response(http_status: int, code: str, message: str) -> dict:
    # FastAPI JSONResponse wrapper (use fastapi.responses.JSONResponse in real code)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message, "details": {}}}
    )
```

**Note on `last_turn` storage:** The GET endpoint must return `message` and `options` for the current state. The cleanest way is to store the last `ConversationTurn` (as a dict) in the session record at save time. Add `session_dict["last_turn"] = turn_to_dict(turn)` in both start and continue handlers before calling `store.save()`. This avoids re-running state handlers on GET.

---

### A-3: Wire routes into the app

**File:** `framework/deploy/mcp_server.py` (modify existing)

In the `_load_app()` function, after constructing the `FastAPI` app:

```python
from .routes.ask import router as ask_router
from .routes.author_skill import router as author_skill_router
from .routes.ops import router as ops_router

app.include_router(ask_router)
app.include_router(author_skill_router)
app.include_router(ops_router)
```

Replace the existing `/answer` endpoint (internal convenience endpoint) — it is superseded by `POST /api/v1/ask`. The old endpoint can be kept under a feature flag (`KBF_LEGACY_ANSWER_ENDPOINT=true`) or removed.

Store new state references in `app.state` during lifespan startup:

```python
app.state.context_builder = ContextBuilder(...)   # already exists
app.state.session_store = _build_session_store()  # new (Track D)
app.state.consumer_registry = ConsumerRegistry(MANIFESTS_DIR)  # new (Track C)
app.state.llm = state["llm"]                      # already in state dict, promote to app.state
app.state.startup_time = time.time()              # for uptimeSeconds in health check
```

---

## Track B — MCP Tool Layer

**Goal:** Replace the current ad-hoc `tool_registry` (which exposes all internal tools to the MCP client) with a 2-tool external registry: `askKnowledgeBase` and `authorSkill`. All internal retrieval tools remain registered as internal tools only, not exported.

**Dependency:** Track A routes must exist. Track C must be complete (MCP calls need auth too).

### B-1: MCP tool handlers

**File:** `framework/deploy/mcp_tools.py` (new file)

```python
"""External MCP tool registry — exactly 2 tools (PDD V3 §7).

Internal retrieval tools (vector_search, get_incident_summary, etc.) are
registered separately and are not exported through the external MCP surface.
"""
from __future__ import annotations
from typing import Any


def build_external_tool_registry(app) -> dict[str, callable]:
    """Return the 2-tool registry for external MCP clients.

    app: the FastAPI app instance (provides app.state.context_builder, etc.)
    """
    return {
        "askKnowledgeBase": _make_ask_handler(app),
        "authorSkill": _make_author_skill_handler(app),
    }


def _make_ask_handler(app):
    async def ask_knowledge_base(
        question: str,
        persona: str | None = None,
        service_id: str | None = None,    # MCP parameter name: serviceId (camelCase)
        functional_area: str | None = None,  # MCP parameter name: functionalArea
        max_results: int = 10,            # MCP parameter name: maxResults
        _consumer=None,                   # injected by MCP middleware
    ) -> dict[str, Any]:
        """Single entry point for all knowledge queries.

        Routes through four-tier system: workflow skill → KB retrieval →
        multi-persona fanout → no-answer. Caller never specifies which KB,
        retriever, or persona skill to use.
        """
        from .routes.ask import _build_ask_response
        from ..orchestrator.budget import Budget

        consumer = _consumer
        budget = Budget(max_tokens_in=consumer.token_budget_per_request if consumer else 8000)

        ctx = app.state.context_builder
        result = ctx.answer(
            query=question,
            budget=budget,
            persona_hint=persona,
            service_id_hint=service_id,
            func_area_hint=functional_area,
            max_results=max_results,
        )
        return _build_ask_response(result, consumer)   # already camelCase

    return ask_knowledge_base


def _make_author_skill_handler(app):
    async def author_skill(
        input: str,                        # noqa: A002 — intentional MCP param name
        synth_id: str | None = None,       # MCP parameter name: synthId (camelCase)
        _consumer=None,                    # injected by MCP middleware
    ) -> dict[str, Any]:
        """Single entry point for the knowledge builder flow.

        Pass-through pattern: call with synthId=null to start a new session;
        pass the returned synthId on subsequent calls to advance the state machine.
        Repeat until done=true.
        """
        from .routes.author_skill import _start_or_continue_session

        consumer = _consumer
        return await _start_or_continue_session(
            input_text=input,
            synth_id=synth_id,
            store=app.state.session_store,
            llm=app.state.llm,
            user_id=consumer.user_id if consumer else "anonymous",
        )

    return author_skill
```

### B-2: Register tools in MCP server

**File:** `framework/deploy/mcp_server.py` (modify existing)

In the lifespan block, after building the app, replace the existing tool registry export:

```python
from .mcp_tools import build_external_tool_registry

# Internal tool registry (all retrievers + workflow skills) — unchanged
state["tool_registry"] = register_v1_tools(retrievers)
# ... workflow skills appended ...

# External MCP registry — only the 2 external tools
state["external_tool_registry"] = build_external_tool_registry(app)
```

Modify `POST /mcp/tools/call` to route based on tool name:

```python
@app.post("/mcp/tools/call")
async def tools_call(req: Request):
    body = await req.json()
    name = body.get("name")
    args = body.get("arguments", {})

    # External tools: only askKnowledgeBase and authorSkill
    ext_registry = state.get("external_tool_registry", {})
    if name in ext_registry:
        consumer = await _extract_consumer(req, app.state.consumer_registry)
        args["_consumer"] = consumer
        result = await ext_registry[name](**args)
        return {"content": result}

    # Internal tools: only accessible within the process, not from external clients.
    # Reject external MCP calls to internal tool names.
    raise HTTPException(404, f"Tool '{name}' is not available on the external MCP surface.")
```

Modify `POST /mcp/tools/list` to return only external tools:

```python
@app.post("/mcp/tools/list")
async def tools_list():
    return {
        "tools": [
            {
                "name": "askKnowledgeBase",
                "description": "Single entry point for all knowledge queries.",
                "inputSchema": {
                    "type": "object",
                    "required": ["question"],
                    "properties": {
                        "question": {"type": "string", "maxLength": 4096},
                        "persona": {"type": "string"},
                        "serviceId": {"type": "string"},
                        "functionalArea": {"type": "string"},
                        "maxResults": {"type": "integer", "default": 10},
                    }
                }
            },
            {
                "name": "authorSkill",
                "description": "Single entry point for the knowledge builder flow.",
                "inputSchema": {
                    "type": "object",
                    "required": ["input"],
                    "properties": {
                        "input": {"type": "string", "maxLength": 4096},
                        "synthId": {"type": "string"},
                    }
                }
            }
        ]
    }
```

---

## Track C — Auth and Middleware

**Goal:** Bearer token middleware that validates tokens against consumer manifests, enforces scopes, and attaches a consumer object to the request. Consumer manifests are YAML files loaded at startup.

**Dependency:** None. This track is independent and can start immediately. It is a prerequisite for all route handlers.

### C-1: Consumer manifest model

**File:** `framework/deploy/auth/consumer.py` (new file)

```python
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ConsumerManifest:
    """Parsed consumer manifest from consumer_manifests/{consumer}.yaml."""
    name: str                        # e.g. "sravan-laptop"
    token_hash: str                  # SHA-256 of the bearer token (stored hashed)
    scopes: list[str]                # ["read"] | ["read", "write"] | ["read", "write", "admin"]
    persona_allowlist: list[str]     # empty list = all personas allowed
    rpm_cap: int                     # requests per minute
    token_budget_per_request: int    # max tokens per single request
    user_id: str                     # derived from manifest name: sha1(name) or explicit field

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def allows_persona(self, persona: str) -> bool:
        if not self.persona_allowlist:
            return True
        return persona in self.persona_allowlist
```

**File:** `framework/deploy/auth/registry.py` (new file)

```python
from __future__ import annotations
import hashlib
import logging
from pathlib import Path
import yaml

from .consumer import ConsumerManifest

log = logging.getLogger(__name__)


class ConsumerRegistry:
    """Loads and caches consumer manifests from consumer_manifests/*.yaml.

    Manifests are loaded once at startup. In production, OCI Vault secrets
    are fetched at startup and compared here. In filestore/dev mode, plaintext
    tokens in the YAML are accepted.

    Token lookup: O(1) — keyed by token hash.
    """

    def __init__(self, manifests_dir: Path):
        self._dir = Path(manifests_dir)
        self._by_token_hash: dict[str, ConsumerManifest] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self._dir.exists():
            log.warning("consumer_manifests dir not found: %s", self._dir)
            return
        for path in self._dir.glob("*.yaml"):
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
                consumer = self._parse_manifest(path.stem, cfg)
                self._by_token_hash[consumer.token_hash] = consumer
                log.info("loaded consumer manifest: %s (scopes=%s)", consumer.name, consumer.scopes)
            except Exception as e:
                log.error("failed to load manifest %s: %s", path, e)

    def _parse_manifest(self, filename_stem: str, cfg: dict) -> ConsumerManifest:
        # YAML field names are camelCase (as specified in PDD V3 §9.1)
        token_raw = cfg.get("token", "")    # plaintext in dev; hash in prod
        token_hash = (
            cfg.get("tokenHash")            # pre-hashed (production)
            or hashlib.sha256(token_raw.encode()).hexdigest()
        )
        return ConsumerManifest(
            name=cfg.get("name", filename_stem),
            token_hash=token_hash,
            scopes=cfg.get("scopes", ["read"]),
            persona_allowlist=cfg.get("personaAllowlist", []),
            rpm_cap=cfg.get("rpmCap", 60),
            token_budget_per_request=cfg.get("tokenBudgetPerRequest", 8000),
            user_id=cfg.get("userId") or hashlib.sha1(filename_stem.encode()).hexdigest()[:16],
        )

    def lookup(self, bearer_token: str) -> ConsumerManifest | None:
        """Return manifest for token, or None if not found."""
        token_hash = hashlib.sha256(bearer_token.encode()).hexdigest()
        return self._by_token_hash.get(token_hash)
```

**File:** `framework/deploy/auth/__init__.py` — empty.

### C-2: Auth middleware

**File:** `framework/deploy/auth/middleware.py` (new file)

```python
from __future__ import annotations
import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse

from .consumer import ConsumerManifest
from .registry import ConsumerRegistry

log = logging.getLogger(__name__)
_REQUEST_KEY = "consumer"

# In-memory RPM counter (per worker process; for multi-worker deployments,
# use Redis or accept slight over-counting at the worker boundary)
_rpm_counters: dict[str, list[float]] = defaultdict(list)
_rpm_lock = Lock()


async def bearer_auth_middleware(request: Request, call_next):
    """FastAPI middleware: validate bearer token, attach consumer, enforce RPM."""
    # /healthz and /api/v1/version have security: [] in openapi.yaml — skip auth
    if request.url.path in ("/healthz", "/api/v1/version"):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthenticated",
                               "message": "Authorization header missing or invalid", "details": {}}}
        )

    token = auth_header[len("Bearer "):]
    registry: ConsumerRegistry = request.app.state.consumer_registry
    consumer = registry.lookup(token)

    if consumer is None:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthenticated",
                               "message": "Bearer token not recognized", "details": {}}}
        )

    # RPM enforcement
    if not _check_rpm(consumer):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "60"},
            content={"error": {"code": "rate_limited",
                               "message": "Rate limit exceeded. Retry after 60 seconds.",
                               "details": {"retryAfterSeconds": 60}}}
        )

    # Attach consumer to request state for route handlers
    request.state.consumer = consumer
    return await call_next(request)


def get_consumer(request: Request) -> ConsumerManifest:
    """Called from route handlers to retrieve the attached consumer."""
    return request.state.consumer


def require_scope(consumer: ConsumerManifest, scope: str) -> None:
    """Raise JSONResponse (403) if consumer lacks the required scope."""
    if not consumer.has_scope(scope):
        from fastapi.responses import JSONResponse as _JSONResponse
        raise _JSONResponse(
            status_code=403,
            content={"error": {"code": "permission_denied",
                               "message": f"Token lacks '{scope}' scope", "details": {}}}
        )


def _check_rpm(consumer: ConsumerManifest) -> bool:
    """Slide-window RPM check. Returns True if request is allowed."""
    now = time.time()
    window_start = now - 60.0
    with _rpm_lock:
        timestamps = _rpm_counters[consumer.name]
        # Evict old entries
        _rpm_counters[consumer.name] = [t for t in timestamps if t > window_start]
        if len(_rpm_counters[consumer.name]) >= consumer.rpm_cap:
            return False
        _rpm_counters[consumer.name].append(now)
        return True
```

Register middleware in `mcp_server.py` after the FastAPI app is created:

```python
from .auth.middleware import bearer_auth_middleware
app.middleware("http")(bearer_auth_middleware)
```

### C-3: Consumer manifest directory convention

**Directory:** `framework/config/consumer_manifests/` (create at project level)

**File:** `framework/config/consumer_manifests/README.md` — not committed to git for production. In production, manifests are fetched from OCI Vault at startup.

**Example dev manifest** (`framework/config/consumer_manifests/dev-local.yaml`):

```yaml
# Development-only consumer manifest — DO NOT commit tokens to git
name: dev-local
token: "dev-only-token-replace-me"   # plaintext only in dev/filestore mode
scopes: [read, write]
personaAllowlist: []                  # empty = all personas allowed
rpmCap: 120
tokenBudgetPerRequest: 16000
```

The gitignore should exclude `framework/config/consumer_manifests/*.yaml` in production branches. In filestore/dev mode, the example manifest is used.

---

## Track D — Session Persistence

**Goal:** Abstract session storage so `SkillBuilderConversation` sessions survive server restarts and can be listed per user. Two implementations: filestore (dev, default) and ADB (staging/prod).

**Dependency:** None. Independent track. Required by Track A author-skill routes.

### D-1: Session store interface

**File:** `framework/deploy/session/__init__.py` — empty.

**File:** `framework/deploy/session/_base.py` (new file)

```python
from __future__ import annotations
from abc import ABC, abstractmethod


class SessionStore(ABC):
    """Abstract session persistence for author_skill sessions.

    All methods operate on session dicts with snake_case keys
    (matching Python convention and the to_dict()/from_dict() contract
    of SkillBuilderConversation).

    The API layer converts snake_case → camelCase before returning to clients.
    """

    @abstractmethod
    def save(self, session: dict, user_id: str, ttl_days: int = 7) -> None:
        """Persist (upsert) a session. Sets updated_at to now. Sets expires_at = now + ttl_days."""

    @abstractmethod
    def load(self, synth_id: str, user_id: str) -> dict | None:
        """Return session dict if it belongs to user_id, else None.
        Returns None for expired sessions (status=expired)."""

    @abstractmethod
    def list_for_user(self, user_id: str) -> list[dict]:
        """Return all sessions for user_id, ordered by updated_at descending."""

    @abstractmethod
    def abandon(self, synth_id: str, user_id: str) -> None:
        """Set status=abandoned on the session. No content removal."""

    @abstractmethod
    def expire_stale(self) -> int:
        """Set status=expired on sessions where expires_at < now() and status=in_progress.
        Returns count of sessions expired. Called by the TTL cleanup job."""
```

### D-2: Filestore implementation

**File:** `framework/deploy/session/filestore.py` (new file)

Stores sessions as JSON files under `{store_root}/sessions/{user_id}/{synth_id}.json`.

```python
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ._base import SessionStore

log = logging.getLogger(__name__)


class FilestoreSessionStore(SessionStore):
    """Filestore session store for dev/laptop mode.

    Layout: {store_root}/sessions/{user_id}/{synth_id}.json
    """

    def __init__(self, store_root: str | Path):
        self._root = Path(store_root) / "sessions"

    def _path(self, user_id: str, synth_id: str) -> Path:
        return self._root / user_id / f"{synth_id}.json"

    def _now(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def save(self, session: dict, user_id: str, ttl_days: int = 7) -> None:
        path = self._path(user_id, session["synth_id"])
        path.parent.mkdir(parents=True, exist_ok=True)

        session = dict(session)   # shallow copy — do not mutate caller's dict
        session["updated_at"] = self._now()
        if session.get("status", "in_progress") == "in_progress":
            expires = datetime.now(tz=timezone.utc) + timedelta(days=ttl_days)
            session["expires_at"] = expires.isoformat()

        with open(path, "w") as f:
            json.dump(session, f, indent=2)

    def load(self, synth_id: str, user_id: str) -> dict | None:
        path = self._path(user_id, synth_id)
        if not path.exists():
            return None
        with open(path) as f:
            session = json.load(f)
        # Check ownership
        if session.get("user_id") != user_id:
            return None
        # Auto-expire check
        expires_at = session.get("expires_at")
        if expires_at and session.get("status") == "in_progress":
            if datetime.fromisoformat(expires_at) < datetime.now(tz=timezone.utc):
                session["status"] = "expired"
                self.save(session, user_id=user_id, ttl_days=0)
                return None   # Treat expired as not found for resume
        return session

    def list_for_user(self, user_id: str) -> list[dict]:
        user_dir = self._root / user_id
        if not user_dir.exists():
            return []
        sessions: list[dict] = []
        for path in user_dir.glob("*.json"):
            try:
                with open(path) as f:
                    sessions.append(json.load(f))
            except Exception as e:
                log.warning("failed to read session file %s: %s", path, e)
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions

    def abandon(self, synth_id: str, user_id: str) -> None:
        session = self.load(synth_id, user_id=user_id)
        if session:
            session["status"] = "abandoned"
            session["expires_at"] = None
            self.save(session, user_id=user_id, ttl_days=0)

    def expire_stale(self) -> int:
        count = 0
        now = datetime.now(tz=timezone.utc)
        for path in self._root.rglob("*.json"):
            try:
                with open(path) as f:
                    session = json.load(f)
                expires_at = session.get("expires_at")
                if (expires_at
                        and session.get("status") == "in_progress"
                        and datetime.fromisoformat(expires_at) < now):
                    session["status"] = "expired"
                    with open(path, "w") as f:
                        json.dump(session, f, indent=2)
                    count += 1
            except Exception as e:
                log.warning("expire_stale failed for %s: %s", path, e)
        return count
```

### D-3: ADB implementation (stub + interface)

**File:** `framework/deploy/session/adb_store.py` (new file)

The ADB implementation reads/writes the `kb_shim.author_skill_sessions` table as specified in PDD V3 §16. The schema uses snake_case column names (DB convention). The store's dict contract is also snake_case. The API layer converts to camelCase.

```python
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta

from ._base import SessionStore

log = logging.getLogger(__name__)


class AdbSessionStore(SessionStore):
    """ADB-backed session store for staging/production.

    Reads and writes kb_shim.author_skill_sessions.
    Column names match PDD V3 §16 (snake_case, DB convention).

    pool: oracledb.AsyncConnectionPool (or synchronous pool) — injected at construction.
    """

    def __init__(self, pool):
        self._pool = pool   # oracledb pool; None in stub mode

    def _now(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def save(self, session: dict, user_id: str, ttl_days: int = 7) -> None:
        if self._pool is None:
            log.warning("AdbSessionStore: no pool — save is a no-op (stub mode)")
            return
        now = self._now()
        expires_at = None
        if session.get("status", "in_progress") == "in_progress" and ttl_days > 0:
            expires_at = (
                datetime.now(tz=timezone.utc) + timedelta(days=ttl_days)
            ).isoformat()

        # Merge session dict into session_data JSON column
        session_data_json = json.dumps(session)

        # Upsert into kb_shim.author_skill_sessions
        sql = """
            MERGE INTO kb_shim.author_skill_sessions tgt
            USING DUAL ON (tgt.synth_id = :synth_id)
            WHEN MATCHED THEN UPDATE SET
                state = :state,
                persona = :persona,
                skill_name = :skill_name,
                session_data = :session_data,
                updated_at = :updated_at,
                expires_at = :expires_at,
                status = :status
            WHEN NOT MATCHED THEN INSERT
                (synth_id, user_id, persona, skill_name, intent, state,
                 session_data, created_at, updated_at, expires_at, status)
            VALUES
                (:synth_id, :user_id, :persona, :skill_name, :intent, :state,
                 :session_data, :created_at, :updated_at, :expires_at, :status)
        """
        with self._pool.acquire() as conn:
            conn.execute(sql, {
                "synth_id": session["synth_id"],
                "user_id": user_id,
                "persona": session.get("persona"),
                "skill_name": session.get("skill_name"),
                "intent": session.get("intent_description") or session.get("intent", ""),
                "state": session.get("state"),
                "session_data": session_data_json,
                "created_at": session.get("created_at", now),
                "updated_at": now,
                "expires_at": expires_at,
                "status": session.get("status", "in_progress"),
            })
            conn.commit()

    def load(self, synth_id: str, user_id: str) -> dict | None:
        if self._pool is None:
            return None
        sql = """
            SELECT synth_id, user_id, persona, skill_name, intent, state,
                   session_data, created_at, updated_at, expires_at, status
            FROM kb_shim.author_skill_sessions
            WHERE synth_id = :synth_id AND user_id = :user_id
        """
        with self._pool.acquire() as conn:
            row = conn.fetchone(sql, {"synth_id": synth_id, "user_id": user_id})
        if row is None:
            return None
        # Deserialize session_data JSON (the full accumulated dict)
        session = json.loads(row["session_data"])
        # Overlay top-level metadata columns (they may differ from session_data if updated)
        session["state"] = row["state"]
        session["status"] = row["status"]
        session["expires_at"] = row["expires_at"]
        session["updated_at"] = row["updated_at"]
        # Auto-expire check
        expires_at = row["expires_at"]
        if expires_at and session.get("status") == "in_progress":
            if datetime.fromisoformat(str(expires_at)) < datetime.now(tz=timezone.utc):
                self.abandon(synth_id, user_id)   # mark expired
                return None
        return session

    def list_for_user(self, user_id: str) -> list[dict]:
        if self._pool is None:
            return []
        sql = """
            SELECT synth_id, persona, skill_name, intent, state,
                   created_at, updated_at, expires_at, status,
                   JSON_VALUE(session_data, '$.progress') AS progress_json
            FROM kb_shim.author_skill_sessions
            WHERE user_id = :user_id
            ORDER BY updated_at DESC
        """
        with self._pool.acquire() as conn:
            rows = conn.fetchall(sql, {"user_id": user_id})
        sessions = []
        for row in rows:
            sessions.append({
                "synth_id": row["synth_id"],
                "persona": row["persona"],
                "skill_name": row["skill_name"],
                "intent": row["intent"],
                "state": row["state"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "expires_at": str(row["expires_at"]) if row["expires_at"] else None,
                "status": row["status"],
                "progress": json.loads(row["progress_json"]) if row["progress_json"] else {},
            })
        return sessions

    def abandon(self, synth_id: str, user_id: str) -> None:
        if self._pool is None:
            return
        sql = """
            UPDATE kb_shim.author_skill_sessions
            SET status = 'abandoned', expires_at = NULL, updated_at = :now
            WHERE synth_id = :synth_id AND user_id = :user_id
        """
        with self._pool.acquire() as conn:
            conn.execute(sql, {"synth_id": synth_id, "user_id": user_id,
                               "now": self._now()})
            conn.commit()

    def expire_stale(self) -> int:
        if self._pool is None:
            return 0
        sql = """
            UPDATE kb_shim.author_skill_sessions
            SET status = 'expired'
            WHERE status = 'in_progress'
              AND expires_at < SYSTIMESTAMP
        """
        with self._pool.acquire() as conn:
            cursor = conn.execute(sql)
            count = cursor.rowcount
            conn.commit()
        return count
```

### D-4: Session store factory

**File:** `framework/deploy/session/factory.py` (new file)

```python
import os
from pathlib import Path
from ._base import SessionStore


def build_session_store(pool=None) -> SessionStore:
    """Select session store implementation based on KBF_STORE_BACKEND env var.

    KBF_STORE_BACKEND=filestore (default for dev/laptop): FilestoreSessionStore
    KBF_STORE_BACKEND=adb:      AdbSessionStore (requires pool)
    """
    backend = os.environ.get("KBF_STORE_BACKEND", "filestore").lower()
    if backend == "adb":
        from .adb_store import AdbSessionStore
        return AdbSessionStore(pool=pool)
    else:
        from .filestore import FilestoreSessionStore
        store_root = os.environ.get(
            "KBF_STORE_ROOT",
            str(Path.home() / ".kbf" / "store")
        )
        return FilestoreSessionStore(store_root=store_root)
```

Call in the lifespan block:

```python
from .session.factory import build_session_store
app.state.session_store = build_session_store(pool=state.get("adb_pool"))
```

### D-5: TTL cleanup background task

**File:** `framework/deploy/session/cleanup_job.py` (new file)

```python
import asyncio
import logging
from ._base import SessionStore

log = logging.getLogger(__name__)


async def run_ttl_cleanup_loop(store: SessionStore, interval_seconds: int = 86400):
    """Background coroutine: expire stale sessions once per interval (default: daily)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            expired_count = store.expire_stale()
            log.info("TTL cleanup: expired %d sessions", expired_count)
        except Exception as e:
            log.error("TTL cleanup failed: %s", e)
```

Start in the lifespan block:

```python
import asyncio
from .session.cleanup_job import run_ttl_cleanup_loop

async with asyncio.TaskGroup() as tg:
    tg.create_task(run_ttl_cleanup_loop(app.state.session_store))
    yield   # lifespan continues; cleanup runs in background
```

---

## Track E — Serialization

**Goal:** Centralized camelCase serialization for all REST responses, and camelCase-to-snake_case deserialization for request bodies. All route handlers deal with snake_case internally and call `to_camel_response()` before returning.

**Dependency:** None. Independent. Required by all route handlers.

### E-1: Serialization module

**File:** `framework/deploy/serialization.py` (new file)

```python
"""camelCase ↔ snake_case serialization for PDD V3 external API surface.

Rules:
- DB column names: snake_case (PostgreSQL/Oracle convention) — internal only.
- Python dict keys: snake_case (Python convention) — internal only.
- External API JSON: camelCase — all REST responses and MCP tool return values.

Usage:
  # In a route handler:
  return to_camel_response({"synth_id": "...", "created_at": "..."})
  # Returns: {"synthId": "...", "createdAt": "..."}
"""
from __future__ import annotations
import re
from fastapi.responses import JSONResponse


def snake_to_camel(name: str) -> str:
    """Convert snake_case → camelCase. e.g. synth_id → synthId."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def camel_to_snake(name: str) -> str:
    """Convert camelCase → snake_case. e.g. synthId → synth_id."""
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()


def convert_keys(obj: object, converter) -> object:
    """Recursively apply key converter to all dict keys in obj."""
    if isinstance(obj, dict):
        return {converter(k): convert_keys(v, converter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_keys(item, converter) for item in obj]
    return obj


def to_camel_response(data: dict, status_code: int = 200) -> JSONResponse:
    """Convert snake_case dict to camelCase JSON response."""
    return JSONResponse(
        status_code=status_code,
        content=convert_keys(data, snake_to_camel),
    )


def from_camel_request(body: dict) -> dict:
    """Convert camelCase request body to snake_case for internal use."""
    return convert_keys(body, camel_to_snake)
```

**Invariant:** Any field that is a well-known compound name must pass through cleanly:
- `synth_id` → `synthId`
- `created_at` → `createdAt`
- `skill_name` → `skillName`
- `persona_allowlist` → `personaAllowlist`
- `tier_used` → `tierUsed`
- `citation_url` → `citationUrl`
- `source_sha` → `sourceSha`

Write a unit test file `framework/tests/test_serialization.py` covering at least:
- Nested dict conversion
- List of dicts conversion
- Round-trip: snake → camel → snake returns original
- Known field names from the openapi.yaml schemas

---

## Track F — Operational Endpoints

**Goal:** Implement the three operational endpoints: `/healthz`, `/api/v1/version`, `/api/v1/metrics/cost`. All are in a separate routes module. `/healthz` and `/api/v1/version` have `security: []` in the OpenAPI spec (no auth required).

**Dependency:** Track C for the cost endpoint (requires admin scope). The health check is independent.

### F-1: Operational routes

**File:** `framework/deploy/routes/ops.py` (new file)

```python
import os
import time
from fastapi import APIRouter, Request
from ..auth.middleware import get_consumer, require_scope
from ..serialization import to_camel_response

router = APIRouter()

_BUILD_SHA = os.environ.get("KBF_BUILD_SHA", "unknown")
_API_VERSION = "v1"
_SCHEMA_VERSION = "1.0.0"


@router.get("/healthz")
async def health_check(req: Request):
    """Health check. No authentication required (security: [] per openapi.yaml)."""
    checks = {}
    overall = "healthy"

    # ADB check
    try:
        pool = getattr(req.app.state, "adb_pool", None)
        if pool:
            pool.ping()
            checks["adb"] = "ok"
        else:
            checks["adb"] = "not_configured"
    except Exception as e:
        checks["adb"] = f"error: {e}"
        overall = "degraded"

    # OCI GenAI — ping the LLM client
    try:
        llm = getattr(req.app.state, "llm", None)
        if llm and hasattr(llm, "ping"):
            llm.ping()
        checks["ociGenai"] = "ok"
    except Exception as e:
        checks["ociGenai"] = f"error: {e}"
        overall = "degraded"

    # Vault — currently no OCI Vault client; check for placeholder
    checks["vault"] = "not_configured"

    # Git — check repo root is accessible
    try:
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]
        if (repo_root / ".git").exists():
            checks["git"] = "ok"
        else:
            checks["git"] = "not_a_git_repo"
    except Exception as e:
        checks["git"] = f"error: {e}"
        overall = "degraded"

    # Confluence + Jira adapters — check adapter configs exist
    checks["confluenceAdapter"] = "ok"   # placeholder until adapter has a ping method
    checks["jiraAdapter"] = "ok"         # placeholder

    startup_time = getattr(req.app.state, "startup_time", time.time())
    uptime = int(time.time() - startup_time)

    http_status = 200 if overall == "healthy" else 503
    return to_camel_response({
        "status": overall,
        "checks": checks,
        "uptime_seconds": uptime,
        "version": _SCHEMA_VERSION,
    }, status_code=http_status)


@router.get("/api/v1/version")
async def get_version():
    """API version. No authentication required."""
    return to_camel_response({
        "api_version": _API_VERSION,
        "schema_version": _SCHEMA_VERSION,
        "build_sha": _BUILD_SHA,
    })


@router.get("/api/v1/metrics/cost")
async def get_cost_metrics(
    req: Request,
    persona: str | None = None,
    skill_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    """Cost telemetry. Requires admin scope."""
    consumer = get_consumer(req)
    require_scope(consumer, "admin")

    # Read from cost telemetry store (see cost_store below)
    cost_store = getattr(req.app.state, "cost_store", None)
    if cost_store is None:
        # Stub: return zeroed response
        return to_camel_response({
            "period": {"start": start_date or "2026-01-01", "end": end_date or "2026-12-31"},
            "total_tokens": 0,
            "by_persona": {},
            "by_operation": {"ingestion": 0, "retrieval": 0, "synthesis": 0},
        })

    data = cost_store.query(
        persona=persona,
        skill_name=skill_name,
        start_date=start_date,
        end_date=end_date,
    )
    return to_camel_response(data)
```

### F-2: Cost telemetry store

**File:** `framework/deploy/cost_store.py` (new file)

The cost store is an append-only log of token usage events. In filestore mode it writes JSON lines to `{store_root}/cost_log.jsonl`. In ADB mode it writes to `kb_shim.cost_events`.

```python
from __future__ import annotations
import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class CostStore:
    """Append-only token usage log.

    Records are: {timestamp, persona, skill_name, operation, prompt_tokens, completion_tokens}
    operation: "ingestion" | "retrieval" | "synthesis"
    """

    def __init__(self, store_root: str | Path):
        self._log_path = Path(store_root) / "cost_log.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        persona: str,
        operation: str,
        prompt_tokens: int,
        completion_tokens: int,
        skill_name: str = "",
    ) -> None:
        entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "persona": persona,
            "skill_name": skill_name,
            "operation": operation,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        with open(self._log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def query(
        self,
        persona: str | None = None,
        skill_name: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """Aggregate cost log into CostMetricsResponse structure."""
        if not self._log_path.exists():
            return self._empty_response(start_date, end_date)

        total = by_persona: dict = {}
        by_operation = {"ingestion": 0, "retrieval": 0, "synthesis": 0}
        total_tokens = 0

        with open(self._log_path) as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if start_date and ts[:10] < start_date:
                    continue
                if end_date and ts[:10] > end_date:
                    continue
                if persona and entry.get("persona") != persona:
                    continue
                if skill_name and entry.get("skill_name") != skill_name:
                    continue

                p = entry["persona"]
                op = entry.get("operation", "retrieval")
                pt = entry.get("prompt_tokens", 0)
                ct = entry.get("completion_tokens", 0)
                t = pt + ct

                if p not in by_persona:
                    by_persona[p] = {"prompt": 0, "completion": 0, "total": 0}
                by_persona[p]["prompt"] += pt
                by_persona[p]["completion"] += ct
                by_persona[p]["total"] += t

                if op in by_operation:
                    by_operation[op] += t

                total_tokens += t

        return {
            "period": {"start": start_date or "", "end": end_date or ""},
            "total_tokens": total_tokens,
            "by_persona": by_persona,
            "by_operation": by_operation,
        }

    @staticmethod
    def _empty_response(start_date, end_date) -> dict:
        return {
            "period": {"start": start_date or "", "end": end_date or ""},
            "total_tokens": 0,
            "by_persona": {},
            "by_operation": {"ingestion": 0, "retrieval": 0, "synthesis": 0},
        }
```

Cost telemetry is written from two call sites:

1. `context_builder.answer()` — after synthesis completes, call `cost_store.record(...)` with the token counts from the budget/LLM response. The `ContextBuilder` constructor should accept an optional `cost_store` parameter.
2. Inside the `SkillBuilderConversation` state handlers (ANALYZE_ARTIFACT, PREVIEW) — these make LLM calls. Record cost from `ConversationTurn.data["cost_tokens"]` after each turn.

---

## File map summary

All new files (none of these exist yet):

```
framework/deploy/
├── auth/
│   ├── __init__.py
│   ├── consumer.py          # ConsumerManifest dataclass
│   ├── middleware.py        # bearer_auth_middleware, get_consumer, require_scope, RPM check
│   └── registry.py         # ConsumerRegistry — loads consumer_manifests/*.yaml
├── session/
│   ├── __init__.py
│   ├── _base.py             # SessionStore ABC
│   ├── filestore.py         # FilestoreSessionStore
│   ├── adb_store.py         # AdbSessionStore (stub until pool is wired)
│   ├── factory.py           # build_session_store() — selects backend from env
│   └── cleanup_job.py       # run_ttl_cleanup_loop()
├── routes/
│   ├── __init__.py
│   ├── ask.py               # POST /api/v1/ask
│   ├── author_skill.py      # POST/GET/DELETE /api/v1/kb/authorSkill[/{synthId}]
│   └── ops.py               # GET /healthz, /api/v1/version, /api/v1/metrics/cost
├── mcp_tools.py             # External 2-tool MCP registry
├── serialization.py         # snake_to_camel, to_camel_response, from_camel_request
└── cost_store.py            # CostStore — append-only token usage log
```

Modified files:

```
framework/deploy/
└── mcp_server.py            # Add: routes, auth middleware, session_store, mcp_tools wiring;
                             #      replace /answer with /api/v1/ask; restrict /mcp/tools/list

framework/orchestrator/
└── context_builder.py       # Add: persona_hint, service_id_hint, func_area_hint, max_results
                             #      params to answer(); optional cost_store injection

framework/config/
└── consumer_manifests/
    └── dev-local.yaml       # Example dev consumer manifest (gitignored in production)
```

---

## Dependency graph

```
Track E (serialization)           — no deps; start first
Track C (auth)                    — no deps; start first (parallel with E)
Track D (session store)           — no deps; start first (parallel with C, E)
Track A-1 (ask route)             — needs C + E
Track A-2 (author-skill routes)   — needs C + D + E
Track A-3 (wire routes)           — needs A-1 + A-2
Track B-1 (mcp_tools.py)          — needs A-1 + A-2 (reuses _build_ask_response, _start_or_continue)
Track B-2 (register in server)    — needs B-1 + A-3
Track F (ops routes)              — needs C (for cost endpoint scope check); F-1 can start with E
```

Recommended sequencing:

1. Sprint 1 (parallel): E + C + D
2. Sprint 2 (parallel): A-1 + A-2 + F-1
3. Sprint 3: A-3 + B-1 + F-2
4. Sprint 4: B-2 (integration) + context_builder.py modifications

---

## Modifications to existing framework code

### `framework/orchestrator/context_builder.py`

The `answer()` method needs four new optional keyword arguments:

```python
def answer(
    self,
    query: str,
    budget: Budget | None = None,
    persona_hint: str | None = None,       # new
    service_id_hint: str | None = None,    # new
    func_area_hint: str | None = None,     # new
    max_results: int = 10,                 # new
) -> dict:
```

Pass `persona_hint` through to `IntentClassifier.classify()` as a soft bias (not a hard override). The classifier implementation already handles the routing logic — the hint is metadata that the classifier can weight but can override.

Also add an optional `cost_store` constructor parameter:

```python
def __init__(self, ..., cost_store=None):
    ...
    self.cost_store = cost_store
```

After `answer()` completes, call:

```python
if self.cost_store:
    self.cost_store.record(
        persona=classification.persona or "unknown",
        operation="retrieval",
        prompt_tokens=packet.cost.get("prompt", 0),
        completion_tokens=packet.cost.get("completion", 0),
    )
```

### `framework/skill_builder/conversation.py`

Two small additions:

1. `_make_synth_id()` currently generates `synth-{persona}-{8hex}`. Update to include a date component matching PDD V3 §16: `synth-{persona}-{skill_slug}-{yyyymmdd}-{4hex}`. This requires `skill_name` to be set before generating the synth_id — the current code generates it before skill_name is known. Either defer ID generation to IDENTIFY_PERSONA completion (where persona + slug are both known), or generate a stable ID at session start and regenerate at IDENTIFY_PERSONA completion (the current code already does this: `self._data.synth_id = _make_synth_id(persona_candidate, ...)` inside `_handle_identify_persona`).

2. The `to_dict()` method returns `{"state": ..., "persona": ..., ...get_state()}`. Add `"intent": self._data.intent_description` to this output so the session store list endpoint can return the `intent` field without unpacking `session_data`.

No other changes to `conversation.py` are needed — the state machine logic is complete.

---

## What is NOT in scope for V3

These are internal details explicitly excluded:

- ADB DDL migration for `kb_shim.author_skill_sessions` — the developer writes a migration script from the schema in PDD V3 §16. The Architect does not write DDL.
- OCI Vault integration for production token storage — the `ConsumerRegistry` has a stub that accepts plaintext tokens in dev. Vault integration is a deployment concern.
- Rate limiting with Redis for multi-worker correctness — the in-memory RPM counter in `middleware.py` is correct for single-worker deployments (which is v1 scope). Upgrade to Redis-backed rate limiting when adding workers.
- The ingestion job status endpoint (`GET /api/v1/kb/ingest/{jobId}/status`) — this is called internally by the INGEST state handler, not by external MCP clients. It should be implemented as an internal method on the ingestion pipeline, not as a REST endpoint.
- Per-KB-content ACL enforcement (`persona_visibility`) — deferred to Phase 4 per PDD V3 §9.1.

---

## Test coverage required (per CLAUDE.md eval discipline)

For each track, the developer must write tests before marking the task done:

| Track | Test file | What to cover |
|-------|-----------|---------------|
| E | `framework/tests/test_serialization.py` | snake→camel, camel→snake, nested dicts, lists, round-trip |
| C | `framework/tests/test_auth_middleware.py` | valid token → consumer attached; invalid token → 401; missing scope → 403; RPM cap enforcement |
| D | `framework/tests/test_session_store.py` | save/load/list/abandon/expire for FilestoreSessionStore; expired session returns None on load |
| A | `framework/tests/test_routes_ask.py` | valid request → 200 with camelCase fields; empty question → 400; wrong scope → 403 |
| A | `framework/tests/test_routes_author_skill.py` | new session → 200 with synthId; continue → state advances; GET returns sessionSummary; DELETE → abandoned |
| B | `framework/tests/test_mcp_tools.py` | tools_list returns exactly 2 tools with correct camelCase names; tools_call with internal tool name → 404 |
| F | `framework/tests/test_routes_ops.py` | /healthz returns checks dict; /api/v1/version returns build sha; /api/v1/metrics/cost requires admin scope |

All tests run with `KBF_STORE_BACKEND=filestore` and stub LLM — no real ADB or OCI GenAI required.
