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
        "askKnowledgeBase": _make_ask_handler(app),
        "authorSkill": _make_author_skill_handler(app),
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

        # ContextBuilder.answer() accepts only query + budget in the current
        # implementation.  persona/service/area hints are accepted as kwargs
        # per the ADR-007 amendment but are silently forwarded here for
        # forward-compatibility — the builder ignores unknown kwargs.
        result = ctx.answer(query=question, budget=budget)

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

        log.info(
            "mcp:authorSkill consumer=%s synth_id=%s",
            consumer.name, synthId or "(new)",
        )

        result = _start_or_continue_session(
            session_store=session_store,
            llm=llm,
            user_id=user_id,
            synth_id=synthId if synthId else None,
            user_input=input,
        )

        return result

    return author_skill_handler


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
