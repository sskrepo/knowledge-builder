"""External MCP tool registry — exactly 2 tools (PDD V3 §7).

Internal retrieval tools (vector_search, get_incident_summary, etc.) remain
registered separately for in-process orchestrator use but are NOT exported
through the external MCP surface.

The two tools mirror the two REST route groups:
  askKnowledgeBase  → POST /api/v1/ask
  authorSkill       → POST /api/v1/kb/authorSkill

Both handlers accept a ``_consumer`` kwarg injected by the MCP dispatch layer
(an MCP-anonymous ConsumerManifest if the caller is unauthenticated).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema published on /mcp/tools/list
# ---------------------------------------------------------------------------

EXTERNAL_TOOLS_SCHEMA = [
    {
        "name": "reviewSkillSession",
        "description": (
            "Comprehensive LLM-powered quality review of an authorSkill session. "
            "Reads all committed artifacts for the given synth_id, cross-checks them "
            "for gaps across 7 dimensions (intent fidelity, schema completeness, "
            "KB wiring, routing descriptors, eval quality, artifact consistency, "
            "ASK-KB routing simulation), and files structured bug reports. "
            "Requires admin or write scope."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["synthId"],
            "properties": {
                "synthId": {
                    "type": "string",
                    "description": "The synth_id of the authorSkill session to review",
                },
                "depth": {
                    "type": "string",
                    "enum": ["structural", "semantic", "full"],
                    "default": "full",
                    "description": (
                        "Review depth: 'structural' = deterministic checks only (no LLM); "
                        "'semantic' / 'full' = structural + LLM critique"
                    ),
                },
                "fileBugs": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to write findings to KBF_BUG_REPORTS",
                },
            },
        },
    },
    {
        "name": "reportBug",
        "description": (
            "Report an error you received from any KBF tool. "
            "Include the requestId from the error response. "
            "The server will investigate."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["requestId", "tool", "description"],
            "properties": {
                "requestId": {
                    "type": "string",
                    "description": "The requestId field from the isError response",
                },
                "tool": {
                    "type": "string",
                    "description": "Which tool failed (e.g. authorSkill)",
                },
                "description": {
                    "type": "string",
                    "description": "What you were trying to do when the error occurred",
                },
                "input": {
                    "type": "object",
                    "description": "The input you passed to the failing tool (optional)",
                },
            },
        },
    },
    {
        "name": "askKnowledgeBase",
        "description": (
            "Single entry point for all knowledge queries. Routes through four-tier system: "
            "workflow skill → KB retrieval → multi-persona fanout → no-answer. "
            "Caller never specifies which KB, retriever, or persona skill to use."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["question"],
            "properties": {
                "question": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": "Natural language question",
                },
                "persona": {
                    "type": "string",
                    "description": "Optional persona hint (e.g. 'ops_eng', 'tpm')",
                },
                "serviceId": {
                    "type": "string",
                    "description": "Optional service ID filter",
                },
                "functionalArea": {
                    "type": "string",
                    "description": "Optional functional area filter",
                },
                "maxResults": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max citations to return",
                },
                "page_id": {
                    "type": "string",
                    "description": (
                        "Optional Confluence pageId or page URL for ask_parameterized "
                        "skills that fetch the source page at query time (ADR-032). "
                        "If omitted, the page reference is extracted from the question "
                        "text."
                    ),
                },
            },
        },
    },
    {
        "name": "authorSkill",
        "description": (
            "Single entry point for the knowledge builder flow. "
            "Pass-through pattern: call with no synthId to start a new session; "
            "pass the returned synthId on subsequent calls to advance the state machine. "
            "Repeat until done=true.\n\n"
            "CRITICAL: when mustShowHuman=true is in the response, you MUST display the "
            "full message field to the actual human user and wait for their typed response "
            "before calling authorSkill again. Do NOT summarise, paraphrase, auto-answer, "
            "or infer a response. The human must see and explicitly respond to this turn. "
            "Turns with mustShowHuman=true include: CLARIFY questions, REVIEW_DESIGN schema "
            "review, PREVIEW_EXTRACTION confirmation, and EVAL gap reports. Auto-answering "
            "any of these silently bypasses the human review loop and undermines the "
            "skill-authoring quality gates.\n\n"
            "IMPORTANT for client LLMs: pass the user's input VERBATIM. Do not "
            "summarize URLs, paraphrase Confluence/Jira links, or paste \"pageId=N\" "
            "in place of a link. The server's source parser needs the original "
            "text. If the user pastes 'https://confluence.example.com/pages/12345/Title', "
            "send that string unchanged. Stripping or rewriting it has caused real "
            "data-extraction failures (BUG-queue-d3ec0 / session synth-tpm-3bda58fe)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["input"],
            "properties": {
                "input": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": (
                        "User's natural language input or response to the last turn. "
                        "Pass user-supplied URLs and identifiers verbatim — do not "
                        "summarize, paraphrase, or convert links to alternate forms."
                    ),
                },
                "synthId": {
                    "type": "string",
                    "description": (
                        "Session ID from a previous call. Omit to start a new session."
                    ),
                },
            },
        },
    },
    {
        "name": "deleteSkill",
        "description": (
            "Hard-delete all stored artifacts for a skill (workflow YAML, persona builder delta, "
            "eval gold sets, extraction schema). This is IRREVERSIBLE — the skill is permanently "
            "removed from the skill store. Requires 'admin' scope AND the server-side deletion "
            "password in confirmationPassword. Does NOT remove already-ingested vector/graph content "
            "(run a re-index to propagate the removal)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["persona", "skillName", "confirmationPassword"],
            "properties": {
                "persona": {
                    "type": "string",
                    "description": "Persona slug (e.g. 'ops_eng', 'tpm')",
                },
                "skillName": {
                    "type": "string",
                    "description": "Skill slug (e.g. 'weekly_incident_summary')",
                },
                "confirmationPassword": {
                    "type": "string",
                    "description": (
                        "Server-side deletion password (KBF_SKILL_DELETE_PASSWORD). "
                        "Required in addition to the bearer token admin scope."
                    ),
                },
            },
        },
    },
    {
        "name": "listSkills",
        "description": (
            "List all authored skills stored in the skill store, optionally filtered "
            "by persona and/or status. Returns a lightweight summary — no artifact "
            "content. Use getSkill to fetch full detail for a specific skill."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "persona": {
                    "type": "string",
                    "description": (
                        "Optional persona slug to filter by (e.g. 'tpm', 'ops_eng'). "
                        "Omit to list skills for all personas."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": ["draft", "promoted", "production"],
                    "description": "Optional status filter.",
                },
            },
        },
    },
    {
        "name": "getSkill",
        "description": (
            "Fetch full detail for a specific skill: KB card, sources, retrieval tools, "
            "and eval gold-set line counts. Pass includeArtifacts=true to also receive "
            "the raw workflow YAML (requires write scope)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["persona", "skillName"],
            "properties": {
                "persona": {
                    "type": "string",
                    "description": "Persona slug (e.g. 'tpm')",
                },
                "skillName": {
                    "type": "string",
                    "description": "Skill slug (e.g. 'generate_weekly_exec_review_pptx')",
                },
                "includeArtifacts": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, include the full workflow YAML text in the response. "
                        "Requires write scope."
                    ),
                },
            },
        },
    },
    {
        "name": "uploadArtifact",
        "description": (
            "Upload a local file (PPT, DOCX, Markdown, text) to the server for "
            "analysis during an authorSkill session. Call this BEFORE providing "
            "an artifact reference in an authorSkill turn. "
            "Returns an artifactId — include it in the next authorSkill input as: "
            "'artifact:<filename> id:<artifactId>'\n\n"
            "Analyzer capability: extracts text-based structure (slide titles, "
            "headings, bullet text). Image-only slides (e.g. a JPG embedded in a "
            "PPTX) yield only placeholder field names — in that case, replace the "
            "stub fields with your real field list at the REVIEW_FIELDS step."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["content", "filename", "synthId"],
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Base64-encoded file bytes.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Original filename including extension "
                        "(e.g. q2.pptx). Extension selects the analyzer."
                    ),
                },
                "synthId": {
                    "type": "string",
                    "description": (
                        "The authorSkill session ID. "
                        "Scopes the upload for cleanup when the session completes."
                    ),
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


def build_external_tool_registry(app) -> dict[str, Any]:
    """Return the 2-tool registry for external MCP clients.

    Each value is a callable that accepts keyword arguments matching the tool's
    inputSchema properties plus an optional ``_consumer`` kwarg injected by the
    MCP dispatch layer.

    Args:
        app: The FastAPI application instance. Must have ``app.state`` populated
             (context_builder, session_store, llm) before this is called.

    Returns:
        dict mapping tool name → async callable.
    """
    return {
        "reviewSkillSession": _make_review_skill_session_handler(app),
        "reportBug": _make_report_bug_handler(app),
        "askKnowledgeBase": _make_ask_handler(app),
        "authorSkill": _make_author_skill_handler(app),
        "uploadArtifact": _make_upload_artifact_handler(app),
        "deleteSkill": _make_delete_skill_handler(app),
        "listSkills": _make_list_skills_handler(app),
        "getSkill": _make_get_skill_handler(app),
    }


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_ask_handler(app):
    """Build the askKnowledgeBase MCP tool handler."""

    from .routes.ask import _build_ask_response, maybe_render_artifact

    async def ask_handler(
        *,
        question: str,
        persona: str = "",
        serviceId: str = "",
        functionalArea: str = "",
        maxResults: int = 10,
        page_id: str = "",
        _consumer=None,
    ) -> dict:
        """MCP handler for askKnowledgeBase.

        Calls ContextBuilder.answer() with optional persona/service/area hints
        and returns a snake_case response dict (the MCP layer does NOT apply
        to_camel_response — callers receive the raw dict).

        Args:
            question:       Natural language query (1-4096 chars).
            persona:        Optional persona hint.
            serviceId:      Optional service ID filter.
            functionalArea: Optional functional area filter.
            maxResults:     Max citations to return (default 10).
            page_id:        Optional Confluence pageId or page URL for
                            ask_parameterized skills (ADR-032 D1 Priority-1).
                            When provided, threads the explicit page reference
                            into maybe_render_artifact via body= so Priority-1
                            is reached before the question-string regex fallback.
                            Default "" → fully backward-compatible (Priority-2
                            question-string extraction still applies as fallback).
            _consumer:      ConsumerManifest injected by MCP dispatch.
                            Falls back to a minimal anonymous consumer if None.
        """
        from ..orchestrator.budget import Budget

        consumer = _consumer or _anonymous_consumer()

        ctx = app.state.context_builder
        budget = Budget(
            max_tokens_in=consumer.token_budget_per_request,
            max_tokens_out=1500,
        )

        log.info(
            "mcp:askKnowledgeBase consumer=%s question_len=%d max_results=%d",
            consumer.name, len(question), maxResults,
        )

        result = ctx.answer(
            query=question,
            budget=budget,
            persona_hint=persona,
            service_id_hint=serviceId,
            func_area_hint=functionalArea,
            max_results=maxResults,
        )

        # Same render hook as the REST route — both paths go through this
        # so a tier-1 skill with response_mode=artifact_url actually produces
        # the PPT/DOCX/etc. Without this the MCP path silently returned only
        # the extracted text (the trap I fell into on the first fix attempt).
        #
        # ADR-032 D1 fix (MCP path): build a synthetic body dict so that
        # Priority-1 (explicit page_id field) in maybe_render_artifact is
        # reachable for ask_parameterized skills.  When page_id is provided by
        # the MCP caller, body={"page_id": page_id} lets the D1 Priority-1
        # branch (`if body and input_param in body and body[input_param]`) fire.
        # When page_id is empty (omitted), body=None is passed so Priority-2
        # (question-string regex) remains the sole fallback — identical to the
        # pre-fix behaviour.  author_fixed skills are unaffected: body is only
        # consulted inside the ask_parameterized branch.
        body = {"page_id": page_id} if page_id else None
        maybe_render_artifact(app.state, result, question, body=body)

        response = _build_ask_response(result, consumer)
        return response

    return ask_handler


def _make_author_skill_handler(app):
    """Build the authorSkill MCP tool handler."""

    import asyncio
    from .routes.author_skill import _start_or_continue_session

    async def author_skill_handler(
        *,
        input: str,
        synthId: str = "",
        _consumer=None,
    ) -> dict:
        """MCP handler for authorSkill.

        Starts a new authoring session (no synthId) or advances an existing one.
        Returns the turn envelope (snake_case dict) including synth_id, state,
        message, data, options, artifacts_preview, progress, done.

        The caller should repeat calls (passing the returned synth_id as synthId)
        until done == True.

        Args:
            input:    User's natural language input.
            synthId:  Session ID from a previous call; omit to start a new session.
            _consumer: ConsumerManifest injected by MCP dispatch.
        """
        consumer = _consumer or _anonymous_consumer()
        user_id = consumer.user_id if consumer.user_id else "mcp-anonymous"

        session_store = app.state.session_store
        llm = getattr(app.state, "llm", None)
        artifact_store = getattr(app.state, "artifact_store", None)
        # CRITICAL: must pass skill_store. Forgetting this means the conversation
        # runs with _skill_store=None, so _write_artifacts silently skips the ADB
        # write entirely (no exception, no log) and "Committed N" is reported to
        # the user while KBF_SKILL_ARTIFACTS gets nothing. This was the actual
        # root cause behind session synth-tpm-14a54555 and the recurring
        # BUG-queue-e8298 symptom — the retry-then-fail fix is fine, but it can
        # only protect writes that are actually attempted.
        skill_store = getattr(app.state, "skill_store", None)

        log.info(
            "mcp:authorSkill consumer=%s synth_id=%s skill_store=%s",
            consumer.name, synthId or "(new)",
            "present" if skill_store is not None else "MISSING",
        )

        # CRITICAL: _start_or_continue_session is fully synchronous. It chains
        # into _run_ingest → ConfluenceWikiIngestor.ingest_space → codex CLI
        # subprocess with a 180s timeout, and into ADB cursor.execute calls.
        # Running that inline on the asyncio event loop would freeze the entire
        # uvicorn worker — listSkills, getSkill, healthz, EVERYTHING stops
        # responding while INGEST waits on codex. That was the visible failure
        # mode in BUG-queue-d3ec0: PIDs 95859→96394→96629→96950 cycling as the
        # MCP client timed out, uvicorn keepalive killed unresponsive workers,
        # and subsequent requests got "Unable to connect" / HTTP 000.
        #
        # Fix: run the blocking call in a worker thread so the event loop stays
        # responsive. The handler is now properly cooperative — other tools
        # (listSkills etc.) continue to serve while INGEST is in flight.
        result = await asyncio.to_thread(
            _start_or_continue_session,
            session_store=session_store,
            llm=llm,
            artifact_store=artifact_store,
            user_id=user_id,
            synth_id=synthId if synthId else None,
            user_input=input,
            skill_store=skill_store,
        )

        return result

    return author_skill_handler


def _make_report_bug_handler(app):
    """Build the reportBug MCP tool handler.

    reportBug does NOT require write scope — it is callable by any consumer
    including anonymous (dev mode).  The handler:
      1. Generates a queue_id for the report.
      2. Writes to error_store.record_user_bug().
      3. Returns a confirmation dict.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    async def report_bug_handler(
        *,
        requestId: str,
        tool: str,
        description: str,
        input: dict | None = None,
        _consumer=None,
    ) -> dict:
        """MCP handler for reportBug.

        Args:
            requestId:   The requestId from the isError response.
            tool:        Name of the tool that failed.
            description: Brief description of what the user was trying to do.
            input:       Optional — the input passed to the failing tool.
            _consumer:   ConsumerManifest injected by MCP dispatch.
        """
        consumer = _consumer or _anonymous_consumer()
        user_id = consumer.user_id if consumer.user_id else "anon"

        queue_id = f"BUG-queue-{uuid4().hex[:5]}"

        log.info(
            "mcp:reportBug request_id=%s tool=%s queue_id=%s consumer=%s",
            requestId, tool, queue_id, consumer.name,
        )

        error_store = getattr(app.state, "error_store", None)
        if error_store:
            entry = {
                "request_id": requestId,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "tool": tool,
                "description": description,
                "input": input or {},
                "user_id": user_id,
                "queue_id": queue_id,
            }
            error_store.record_user_bug(entry)

        return {
            "queued": True,
            "queueId": queue_id,
            "message": (
                "Bug report received. The team has been notified and will investigate."
            ),
        }

    return report_bug_handler


def _make_upload_artifact_handler(app):
    """Build the uploadArtifact MCP tool handler (ADR-021).

    Accepts base64-encoded file bytes, validates the content, stores via the
    ArtifactStore, and returns an artifactId for use in the next authorSkill turn.

    Requires 'write' scope.
    """
    import asyncio
    import base64
    from datetime import datetime, timezone
    from pathlib import Path
    from uuid import uuid4

    _ACCEPTED_SUFFIXES = {".pptx", ".docx", ".md", ".txt"}
    _MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    async def upload_artifact_handler(
        *,
        content: str,
        filename: str,
        synthId: str,
        _consumer=None,
    ) -> dict:
        """MCP handler for uploadArtifact.

        Args:
            content:   Base64-encoded file bytes.
            filename:  Original filename including extension.
            synthId:   The authorSkill session ID.
            _consumer: ConsumerManifest injected by MCP dispatch.
        """
        consumer = _consumer or _anonymous_consumer()

        # Scope check
        if "write" not in consumer.scopes:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "uploadArtifact requires write scope."}],
            }

        # Validate filename / extension
        suffix = Path(filename).suffix.lower()
        if suffix not in _ACCEPTED_SUFFIXES:
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        f"Unsupported file type '{suffix}'. "
                        f"Accepted: {', '.join(sorted(_ACCEPTED_SUFFIXES))}"
                    ),
                }],
            }

        if not synthId:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "synthId is required."}],
            }

        # Decode base64
        try:
            data = base64.b64decode(content, validate=True)
        except Exception:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "content must be valid base64."}],
            }

        # Size check (after decode)
        if len(data) > _MAX_SIZE_BYTES:
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        f"File exceeds 10 MB limit "
                        f"(got {len(data) / 1024 / 1024:.1f} MB)."
                    ),
                }],
            }

        artifact_id = f"art-{uuid4().hex[:8]}"
        artifact_store = getattr(app.state, "artifact_store", None)

        if artifact_store is None:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Artifact store not available."}],
            }

        log.info(
            "mcp:uploadArtifact synth_id=%s artifact_id=%s filename=%s size=%d consumer=%s",
            synthId, artifact_id, filename, len(data), consumer.name,
        )

        # Run blocking I/O in a thread
        await asyncio.to_thread(
            artifact_store.upload,
            synthId,
            artifact_id,
            filename,
            data,
        )

        expires_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()

        return {
            "artifactId": artifact_id,
            "filename": filename,
            "sizeBytes": len(data),
            "expiresAt": expires_at,
        }

    return upload_artifact_handler


def _make_review_skill_session_handler(app):
    """Build the reviewSkillSession MCP tool handler (ADR-023).

    Performs a comprehensive quality review of an authorSkill session:
      1. Loads all committed artifacts via KbfOpsSessionLoader.
      2. Runs KbfOpsReviewEngine to score 7 quality dimensions.
      3. Optionally files each finding as a bug report to KBF_BUG_REPORTS.
      4. Persists a row in KBF_AUDIT_RUNS for operational tracking.
      5. Returns the full QualityReport as a JSON-serialisable dict.

    Requires 'admin' or 'write' scope.
    """
    import json as _json
    from dataclasses import asdict
    from datetime import datetime, timezone
    from uuid import uuid4

    async def review_skill_session_handler(
        *,
        synthId: str,
        depth: str = "full",
        fileBugs: bool = True,
        _consumer=None,
    ) -> dict:
        consumer = _consumer or _anonymous_consumer()

        if not ({"admin", "write"} & set(consumer.scopes)):
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": "reviewSkillSession requires admin or write scope.",
                }],
            }

        if not synthId:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "synthId is required."}],
            }

        log.info(
            "mcp:reviewSkillSession synth_id=%s depth=%s file_bugs=%s consumer=%s",
            synthId, depth, fileBugs, consumer.name,
        )

        loader = getattr(app.state, "kbf_ops_loader", None)
        if loader is None:
            from ..retrievers.kbf_ops.session_loader import KbfOpsSessionLoader
            pool = getattr(app.state, "adb_pool", None)
            session_store = getattr(app.state, "session_store", None)
            skill_store = getattr(app.state, "skill_store", None)
            artifact_store = getattr(app.state, "artifact_store", None)
            loader = KbfOpsSessionLoader(
                pool=pool,
                session_store=session_store,
                skill_store=skill_store,
                artifact_store=artifact_store,
            )

        bundle = loader.load(synthId)
        if bundle is None:
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": f"Session '{synthId}' not found.",
                }],
            }

        llm = getattr(app.state, "llm", None)
        if llm is None and depth != "structural":
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "LLM not configured — "
                        "use depth='structural' for deterministic checks only"
                    ),
                }],
            }

        from .ops.review_engine import KbfOpsReviewEngine
        engine = KbfOpsReviewEngine(llm=llm)
        report = engine.review(bundle, depth=depth)

        bugs_filed = 0
        if fileBugs and report.bugs_to_file:
            error_store = getattr(app.state, "error_store", None)
            if error_store is not None:
                now_iso = datetime.now(tz=timezone.utc).isoformat()
                for bug in report.bugs_to_file:
                    entry = {
                        "request_id":    f"ops-{report.review_id}-{uuid4().hex[:6]}",
                        "queue_id":      f"OPS-{uuid4().hex[:8].upper()}",
                        "timestamp":     now_iso,
                        "tool":          "reviewSkillSession",
                        "description":   bug.detail,
                        "source":        "ops_skill_auditor",
                        "check_name":    bug.check_name,
                        "severity":      bug.severity,
                        "suggested_fix": bug.suggested_fix,
                        "synth_id":      synthId,
                        "review_id":     report.review_id,
                    }
                    error_store.record_user_bug(entry)
                    bugs_filed += 1

        # Use the dedicated bug DB pool (DECISION-009); fall back to main adb_pool.
        bug_pool = getattr(app.state, "bug_pool", None) or getattr(app.state, "adb_pool", None)
        if bug_pool is not None:
            _persist_audit_run(
                pool=bug_pool,
                review_id=report.review_id,
                synth_id=synthId,
                depth=depth,
                overall_score=report.overall_score,
                recommendation=report.recommendation,
                bugs_filed=bugs_filed,
                triggered_by=consumer.name,
                report=report,
            )

        return _report_to_dict(report, bugs_filed=bugs_filed)

    return review_skill_session_handler


def _persist_audit_run(
    pool, review_id, synth_id, depth, overall_score, recommendation,
    bugs_filed, triggered_by, report
) -> None:
    """Insert a row into KBF_AUDIT_RUNS using the bug DB pool (DECISION-009).

    The caller should pass ``app.state.bug_pool`` (dedicated KBF_BUGS connection)
    rather than ``app.state.adb_pool``.  Silently ignores errors so a DB failure
    never surfaces to the MCP caller.
    """
    import json as _json

    _SQL_INSERT = """
        INSERT INTO KB_SHIM.KBF_AUDIT_RUNS
            (review_id, synth_id, depth, overall_score, recommendation,
             bugs_filed, triggered_by, report_json)
        VALUES
            (:review_id, :synth_id, :depth, :overall_score, :recommendation,
             :bugs_filed, :triggered_by, :report_json)
    """
    try:
        report_json = _json.dumps(_report_to_dict(report, bugs_filed=bugs_filed))
        params = {
            "review_id":      review_id,
            "synth_id":       synth_id,
            "depth":          depth,
            "overall_score":  overall_score,
            "recommendation": recommendation,
            "bugs_filed":     bugs_filed,
            "triggered_by":   triggered_by,
            "report_json":    report_json,
        }
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_INSERT, params)
            conn.commit()
        log.info(
            "audit_run persisted: review_id=%s synth_id=%s score=%.1f",
            review_id, synth_id, overall_score,
        )
    except Exception as exc:
        log.warning("failed to persist audit run: %s", exc)


def _report_to_dict(report, bugs_filed: int = 0) -> dict:
    """Convert a QualityReport to a JSON-serialisable dict."""
    from dataclasses import asdict
    d = asdict(report)
    d["synthId"] = d.pop("synth_id")
    d["reviewId"] = d.pop("review_id")
    d["skillNames"] = d.pop("skill_names")
    d["overallScore"] = d.pop("overall_score")
    d["bugsToFile"] = [
        {
            "checkName":    b["check_name"],
            "severity":     b["severity"],
            "detail":       b["detail"],
            "suggestedFix": b["suggested_fix"],
        }
        for b in d.pop("bugs_to_file")
    ]
    d["bugsFiledCount"] = bugs_filed
    return d


def _make_delete_skill_handler(app):
    """Build the deleteSkill MCP tool handler.

    Protection layers:
      1. Bearer token must have 'admin' scope.
      2. confirmationPassword must match KBF_SKILL_DELETE_PASSWORD env var.

    Both checks must pass — the password is a second factor on top of the token.
    """
    import os

    async def delete_skill_handler(
        *,
        persona: str,
        skillName: str,
        confirmationPassword: str,
        _consumer=None,
    ) -> dict:
        consumer = _consumer or _anonymous_consumer()

        # Layer 1: admin scope required.
        if "admin" not in consumer.scopes:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "deleteSkill requires admin scope."}],
            }

        # Layer 2: confirmation password check.
        delete_password = os.environ.get("KBF_SKILL_DELETE_PASSWORD", "")
        if not delete_password:
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "Server is not configured for skill deletion "
                        "(KBF_SKILL_DELETE_PASSWORD not set). Contact the framework team."
                    ),
                }],
            }
        if confirmationPassword != delete_password:
            log.warning(
                "mcp:deleteSkill rejected — wrong confirmationPassword consumer=%s persona=%s skill=%s",
                consumer.name, persona, skillName,
            )
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Invalid confirmationPassword."}],
            }

        # Input validation.
        persona = (persona or "").strip()
        skill_name = (skillName or "").strip()
        if not persona or not skill_name:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "persona and skillName are required."}],
            }

        skill_store = getattr(app.state, "skill_store", None)
        if skill_store is None:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Skill store not available."}],
            }

        log.warning(
            "mcp:deleteSkill persona=%s skill=%s consumer=%s — destructive delete initiated",
            persona, skill_name, consumer.name,
        )

        try:
            deleted_types = skill_store.delete(persona, skill_name)
        except Exception as exc:
            log.error("mcp:deleteSkill failed: %s", exc)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Delete failed: {exc}"}],
            }

        if not deleted_types:
            return {
                "isError": False,
                "content": [{
                    "type": "text",
                    "text": (
                        f"No artifacts found for {persona}.{skill_name} — nothing deleted."
                    ),
                }],
                "deletedArtifacts": [],
                "persona": persona,
                "skillName": skill_name,
                "status": "not_found",
            }

        # Mirror delete to filesystem so _list_available_personas() stays accurate
        # (BUG-queue-4fd5e — ADB delete alone leaves disk stale)
        from pathlib import Path as _Path
        _fs_root = _Path(__file__).resolve().parents[2]
        _FS_TEMPLATES = {
            "workflow_skill":  "framework/workflow_skills/{persona}/{skill_name}.yaml",
            "eval_extraction": "eval/gold_sets/{persona}-{skill_name}-extraction.jsonl",
            "eval_workflow":   "eval/gold_sets/{persona}-{skill_name}-workflow.jsonl",
        }
        for _tmpl in _FS_TEMPLATES.values():
            _fp = _fs_root / _tmpl.format(persona=persona, skill_name=skill_name)
            if _fp.exists():
                try:
                    _fp.unlink()
                    log.info("mcp:deleteSkill removed filesystem artifact: %s", _fp.name)
                except OSError as _exc:
                    log.warning("mcp:deleteSkill fs cleanup failed for %s: %s", _fp.name, _exc)

        # Remove the promoted KB entry from KBF_PERSONA_BUILDERS so ShimKb
        # stops routing queries to this skill's KB.
        pb_deleted = False
        try:
            pb_deleted = skill_store.delete_persona_builder_kb(persona, skill_name)
            if pb_deleted:
                log.info(
                    "mcp:deleteSkill removed KBF_PERSONA_BUILDERS entry "
                    "persona=%s kb_name=%s", persona, skill_name,
                )
        except Exception as exc:
            log.warning(
                "mcp:deleteSkill delete_persona_builder_kb failed "
                "(artifacts already deleted): %s", exc,
            )

        # Hot-reload ShimKb so the card is immediately gone from routing.
        shim_kb = getattr(app.state, "shim_kb", None)
        if shim_kb is not None:
            try:
                shim_kb.reload()
                log.info("mcp:deleteSkill ShimKb reloaded after deletion")
            except Exception as exc:
                log.warning("mcp:deleteSkill shim_kb.reload() failed: %s", exc)

        log.warning(
            "mcp:deleteSkill COMPLETED persona=%s skill=%s deleted=%s pb_deleted=%s consumer=%s",
            persona, skill_name, deleted_types, pb_deleted, consumer.name,
        )

        return {
            "isError": False,
            "content": [{
                "type": "text",
                "text": (
                    f"Deleted {len(deleted_types)} artifact(s) for {persona}.{skill_name}: "
                    f"{', '.join(sorted(deleted_types))}. "
                    f"KB routing entry {'removed' if pb_deleted else 'not found (already clean)'}. "
                    "Note: already-ingested vector/graph content is not removed — "
                    "run a re-index to propagate."
                ),
            }],
            "deletedArtifacts": sorted(deleted_types),
            "pbEntryDeleted": pb_deleted,
            "persona": persona,
            "skillName": skill_name,
            "status": "deleted",
        }

    return delete_skill_handler


# ---------------------------------------------------------------------------
# listSkills / getSkill handlers
# ---------------------------------------------------------------------------


def _make_list_skills_handler(app):
    """Build the listSkills MCP tool handler.

    Returns a lightweight summary of all skills in the skill store.
    Requires read scope (or anonymous dev mode).
    """

    async def list_skills_handler(
        *,
        persona: str = "",
        status: str = "",
        _consumer=None,
    ) -> dict:
        """MCP handler for listSkills.

        Args:
            persona:   Optional persona slug filter.
            status:    Optional status filter ("draft" | "promoted" | "production").
            _consumer: ConsumerManifest injected by MCP dispatch.
        """
        skill_store = getattr(app.state, "skill_store", None)
        if skill_store is None:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Skill store not available."}],
            }

        log.info(
            "mcp:listSkills persona=%s status=%s",
            persona or "(all)", status or "(all)",
        )

        try:
            raw = skill_store.list_skills(persona=persona or None)
        except Exception as exc:
            log.error("mcp:listSkills list_skills failed: %s", exc)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Failed to list skills: {exc}"}],
            }

        # Client-side status filter (list_skills only accepts persona filter)
        if status:
            raw = [s for s in raw if s.get("status") == status]

        skills = [
            {
                "persona":       s.get("persona", ""),
                "skillName":     s.get("skill_name", ""),
                "status":        s.get("status", "draft"),
                "artifactCount": s.get("artifact_count", 0),
                "updatedAt":     s.get("updated_at", ""),
            }
            for s in raw
        ]

        return {"skills": skills, "total": len(skills)}

    return list_skills_handler


def _make_get_skill_handler(app):
    """Build the getSkill MCP tool handler.

    Returns full skill detail: KB card parsed from persona_builder_delta,
    eval gold-set line counts, and optionally the raw workflow YAML.
    Requires write scope when includeArtifacts=True.
    """
    import yaml as _yaml

    async def get_skill_handler(
        *,
        persona: str,
        skillName: str,
        includeArtifacts: bool = False,
        _consumer=None,
    ) -> dict:
        """MCP handler for getSkill.

        Args:
            persona:          Persona slug (e.g. "tpm").
            skillName:        Skill slug (e.g. "generate_weekly_exec_review_pptx").
            includeArtifacts: When True, include full workflow YAML in response
                              (requires write scope).
            _consumer:        ConsumerManifest injected by MCP dispatch.
        """
        consumer = _consumer or _anonymous_consumer()

        persona = (persona or "").strip()
        skill_name = (skillName or "").strip()
        if not persona or not skill_name:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "persona and skillName are required."}],
            }

        if includeArtifacts and "write" not in consumer.scopes:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "includeArtifacts=true requires write scope."}],
            }

        skill_store = getattr(app.state, "skill_store", None)
        if skill_store is None:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Skill store not available."}],
            }

        log.info(
            "mcp:getSkill persona=%s skill=%s include_artifacts=%s consumer=%s",
            persona, skill_name, includeArtifacts, consumer.name,
        )

        # --- Summary row ---------------------------------------------------
        try:
            all_skills = skill_store.list_skills(persona=persona)
        except Exception as exc:
            log.error("mcp:getSkill list_skills failed: %s", exc)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Failed to fetch skill list: {exc}"}],
            }

        summary = next(
            (s for s in all_skills if s.get("skill_name") == skill_name),
            None,
        )
        if summary is None:
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": f"Skill '{persona}.{skill_name}' not found.",
                }],
            }

        # --- Artifacts -----------------------------------------------------
        def _read(artifact_type: str) -> str | None:
            try:
                return skill_store.read_artifact(persona, skill_name, artifact_type)
            except Exception as exc:
                log.warning(
                    "mcp:getSkill read_artifact %s failed: %s", artifact_type, exc
                )
                return None

        delta_yaml  = _read("persona_builder_delta")
        workflow_yaml = _read("workflow_skill") if includeArtifacts else None
        eval_ext    = _read("eval_extraction")
        eval_wf     = _read("eval_workflow")

        # --- Parse KB card from persona_builder_delta ----------------------
        kb_card: dict = {}
        if delta_yaml:
            try:
                delta = _yaml.safe_load(delta_yaml) or {}
                raw_card = delta.get("kb_card") or {}
                kb_card = {
                    "summary":        raw_card.get("summary", ""),
                    "useWhen":        raw_card.get("use_when", ""),
                    "providesFields": delta.get("provides_fields", []),
                    "kind":           delta.get("kind", ""),
                    "sources":        delta.get("sources", []),
                    "retrievalTools": delta.get("retrieval_tools", []),
                }
            except Exception as exc:
                log.warning("mcp:getSkill failed to parse persona_builder_delta: %s", exc)

        # --- Eval counts (line count ≈ example count for JSONL) -----------
        def _line_count(text: str | None) -> int:
            if not text:
                return 0
            return sum(1 for ln in text.splitlines() if ln.strip())

        response: dict = {
            "persona":       persona,
            "skillName":     skill_name,
            "status":        summary.get("status", "draft"),
            "artifactCount": summary.get("artifact_count", 0),
            "updatedAt":     summary.get("updated_at", ""),
            "kbCard":        kb_card,
            "evalCounts": {
                "extraction": _line_count(eval_ext),
                "workflow":   _line_count(eval_wf),
            },
        }

        if includeArtifacts:
            response["workflowYaml"] = workflow_yaml or ""

        return response

    return get_skill_handler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _anonymous_consumer():
    """Return a minimal ConsumerManifest for unauthenticated MCP callers."""
    from .auth.consumer import ConsumerManifest

    return ConsumerManifest(
        name="mcp-anonymous",
        token_hash="",
        scopes=["read", "write"],
        persona_allowlist=[],
        rpm_cap=60,
        token_budget_per_request=8000,
        user_id="mcp-anonymous",
    )
