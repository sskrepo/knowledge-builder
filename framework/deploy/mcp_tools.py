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
            },
        },
    },
    {
        "name": "authorSkill",
        "description": (
            "Single entry point for the knowledge builder flow. "
            "Pass-through pattern: call with no synthId to start a new session; "
            "pass the returned synthId on subsequent calls to advance the state machine. "
            "Repeat until done=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["input"],
            "properties": {
                "input": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": "User's natural language input or response to the last turn",
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
        "name": "uploadArtifact",
        "description": (
            "Upload a local file (PPT, DOCX, Markdown, text) to the server for "
            "analysis during an authorSkill session. Call this BEFORE providing "
            "an artifact reference in an authorSkill turn. "
            "Returns an artifactId — include it in the next authorSkill input as: "
            "'artifact:<filename> id:<artifactId>'"
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
        "reportBug": _make_report_bug_handler(app),
        "askKnowledgeBase": _make_ask_handler(app),
        "authorSkill": _make_author_skill_handler(app),
        "uploadArtifact": _make_upload_artifact_handler(app),
    }


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_ask_handler(app):
    """Build the askKnowledgeBase MCP tool handler."""

    from .routes.ask import _build_ask_response

    async def ask_handler(
        *,
        question: str,
        persona: str = "",
        serviceId: str = "",
        functionalArea: str = "",
        maxResults: int = 10,
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

        response = _build_ask_response(result, consumer)
        return response

    return ask_handler


def _make_author_skill_handler(app):
    """Build the authorSkill MCP tool handler."""

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

        log.info(
            "mcp:authorSkill consumer=%s synth_id=%s",
            consumer.name, synthId or "(new)",
        )

        result = _start_or_continue_session(
            session_store=session_store,
            llm=llm,
            artifact_store=artifact_store,
            user_id=user_id,
            synth_id=synthId if synthId else None,
            user_input=input,
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
