"""ADR-032 MCP ask_handler body=/page_id gap fix — unit tests.

Verifies that the MCP consumption path correctly threads page_id through to
maybe_render_artifact via body=, enabling ADR-032 D1 Priority-1 (explicit
page_id field) for MCP consumers of ask_parameterized skills.

Root cause fixed: _make_ask_handler in framework/deploy/mcp_tools.py
previously called maybe_render_artifact(app.state, result, question) with
no body= kwarg, making the D1 Priority-1 branch structurally unreachable on
the MCP path.  MCP callers also had no structured page_id parameter at all,
forcing exclusive reliance on Priority-2 (question-string regex extraction).

Fix (Option A):
  1. ask_handler gains page_id: str = "" parameter (backward-compatible).
  2. body = {"page_id": page_id} if page_id else None.
  3. maybe_render_artifact(..., body=body) — Priority-1 now reachable.
  4. askKnowledgeBase tool schema gains optional page_id string property.

Tests:
  A. Explicit page_id → body={"page_id": ...} passed to maybe_render_artifact
     (Priority-1 now reachable on MCP path).
  B. No page_id, question with URL → body=None passed; Priority-2 still
     operative (assert body is None / falsy, function still called with question).
  C. Tool schema introspection: page_id present as optional string parameter.
  D. Backward-compat: question+persona only (no page_id) → no error, body=None.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_ID = "18625350641"
QUESTION_WITH_URL = (
    f"Please draft a status email based on "
    f"https://confluence.example.com/spaces/FA/pages/{PAGE_ID}/My+Project"
)
QUESTION_NO_URL = "What are the key milestones for the FA DB upgrade?"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(*, context_builder_result: dict | None = None) -> MagicMock:
    """Build a minimal mock app with app.state.context_builder wired."""
    app = MagicMock()
    cb = MagicMock()
    default_result = {
        "tier": 1,
        "intent": {"workflow_skill": "test_skill", "persona": "tpm", "confidence": 0.9},
        "answer": "Draft produced.",
        "passages": [],
        "cost": {"prompt": 100, "completion": 50},
        "latency_ms": 800,
    }
    if context_builder_result is not None:
        default_result.update(context_builder_result)
    cb.answer.return_value = default_result
    app.state.context_builder = cb
    return app


def _run(coro) -> Any:
    """Run a coroutine synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# A. Explicit page_id → body={"page_id": ...} passed to maybe_render_artifact
# ---------------------------------------------------------------------------

class TestExplicitPageId:
    """MCP ask_handler with page_id → Priority-1 body threading."""

    def test_explicit_page_id_passed_as_body_to_maybe_render_artifact(self):
        """ask_handler called with explicit page_id → maybe_render_artifact
        receives body={"page_id": "<id>"}, making Priority-1 reachable."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()

        captured: dict = {}

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            captured["body"] = body
            captured["question"] = question

        def _fake_build_ask_response(result, consumer):
            return {"answer": "ok"}

        def _fake_budget(**kwargs):
            return MagicMock()

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", _fake_budget):
            handler = _make_ask_handler(app)
            _run(handler(
                question=QUESTION_WITH_URL,
                persona="tpm",
                page_id=PAGE_ID,
            ))

        assert "body" in captured, "maybe_render_artifact must have been called"
        body_received = captured["body"]
        assert body_received is not None, (
            f"body must not be None when page_id={PAGE_ID!r} is explicitly provided; "
            f"got body={body_received!r}"
        )
        assert isinstance(body_received, dict), (
            f"body must be a dict; got {type(body_received)}"
        )
        assert body_received.get("page_id") == PAGE_ID, (
            f"body['page_id'] must be {PAGE_ID!r}; got {body_received.get('page_id')!r}"
        )

    def test_priority_1_body_key_matches_default_input_param(self):
        """The body key set by the MCP handler is 'page_id', which is the default
        input_param in maybe_render_artifact's D1 branch for ask_parameterized skills.
        This confirms the key names align without any translation."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        captured: dict = {}

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            captured["body"] = body

        def _fake_build_ask_response(result, consumer):
            return {}

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", MagicMock()):
            handler = _make_ask_handler(app)
            _run(handler(question="Draft an email", page_id="99991111"))

        assert captured.get("body", {}).get("page_id") == "99991111"


# ---------------------------------------------------------------------------
# B. No page_id, question with URL → body=None; Priority-2 still works
# ---------------------------------------------------------------------------

class TestNoPageIdPriority2Fallback:
    """When page_id is omitted, body=None → Priority-2 (question-string
    regex) remains the sole operative path — existing consumers unchanged."""

    def test_no_page_id_passes_body_none(self):
        """ask_handler with no page_id → body=None passed to maybe_render_artifact."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        captured: dict = {}

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            captured["body"] = body
            captured["question"] = question
            captured["called"] = True

        def _fake_build_ask_response(result, consumer):
            return {}

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", MagicMock()):
            handler = _make_ask_handler(app)
            _run(handler(question=QUESTION_WITH_URL, persona="tpm"))

        assert captured.get("called"), "maybe_render_artifact must still be called"
        assert captured["body"] is None, (
            f"body must be None when page_id is omitted; got {captured['body']!r}"
        )
        assert captured["question"] == QUESTION_WITH_URL, (
            "question must be passed unchanged to maybe_render_artifact"
        )

    def test_no_page_id_empty_string_same_as_omitted(self):
        """page_id='' (explicit empty string) must also yield body=None —
        same as omitting page_id entirely."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        captured: dict = {}

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            captured["body"] = body

        def _fake_build_ask_response(result, consumer):
            return {}

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", MagicMock()):
            handler = _make_ask_handler(app)
            _run(handler(question=QUESTION_WITH_URL, page_id=""))

        assert captured["body"] is None, (
            f"page_id='' must yield body=None; got {captured['body']!r}"
        )

    def test_question_still_passed_as_positional_arg(self):
        """question is always threaded through as the third positional arg to
        maybe_render_artifact regardless of whether page_id is supplied."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        calls: list = []

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            calls.append({"question": question, "body": body})

        def _fake_build_ask_response(result, consumer):
            return {}

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", MagicMock()):
            handler = _make_ask_handler(app)
            _run(handler(question=QUESTION_NO_URL))

        assert len(calls) == 1
        assert calls[0]["question"] == QUESTION_NO_URL
        assert calls[0]["body"] is None


# ---------------------------------------------------------------------------
# C. Tool schema: page_id present as optional string in askKnowledgeBase
# ---------------------------------------------------------------------------

class TestToolSchemaPageId:
    """The registered MCP askKnowledgeBase schema includes optional page_id."""

    def test_ask_tool_schema_has_page_id_property(self):
        """EXTERNAL_TOOLS_SCHEMA must include page_id as a string property
        in the askKnowledgeBase inputSchema.properties."""
        from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA

        ask_tool = next(
            (t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase"),
            None,
        )
        assert ask_tool is not None, "askKnowledgeBase must be in EXTERNAL_TOOLS_SCHEMA"

        props = ask_tool["inputSchema"]["properties"]
        assert "page_id" in props, (
            f"askKnowledgeBase schema must include page_id property; "
            f"got properties: {list(props.keys())}"
        )

    def test_ask_tool_page_id_is_string_type(self):
        """page_id property must declare type='string'."""
        from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA

        ask_tool = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        page_id_schema = ask_tool["inputSchema"]["properties"]["page_id"]
        assert page_id_schema.get("type") == "string", (
            f"page_id must have type='string'; got {page_id_schema.get('type')!r}"
        )

    def test_ask_tool_page_id_not_in_required(self):
        """page_id must NOT be in the required list — it is optional."""
        from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA

        ask_tool = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        required = ask_tool["inputSchema"].get("required", [])
        assert "page_id" not in required, (
            f"page_id must be optional (not in required); required={required!r}"
        )

    def test_ask_tool_page_id_description_mentions_adr_032(self):
        """page_id description must mention ADR-032 so callers understand its purpose."""
        from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA

        ask_tool = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        desc = ask_tool["inputSchema"]["properties"]["page_id"].get("description", "")
        assert "ADR-032" in desc, (
            f"page_id description must reference ADR-032; got {desc!r}"
        )

    def test_question_still_required_in_schema(self):
        """question must remain in required — backward-compat guard."""
        from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA

        ask_tool = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        required = ask_tool["inputSchema"].get("required", [])
        assert "question" in required, (
            "question must remain required in askKnowledgeBase schema"
        )


# ---------------------------------------------------------------------------
# D. Backward-compat: question+persona only, no page_id → no error
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    """Existing MCP consumers that pass only question/persona must be unaffected."""

    def test_question_and_persona_only_no_error(self):
        """ask_handler(question=..., persona=...) with no page_id → completes
        without error; body=None passed to maybe_render_artifact."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        captured: dict = {}

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            captured["body"] = body
            captured["done"] = True

        def _fake_build_ask_response(result, consumer):
            return {"answer": "ok", "tier": 2}

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", MagicMock()):
            handler = _make_ask_handler(app)
            response = _run(handler(question=QUESTION_NO_URL, persona="ops_eng"))

        assert captured.get("done"), "maybe_render_artifact must be called"
        assert captured["body"] is None
        assert response.get("answer") == "ok"

    def test_question_embedded_url_no_page_id_param_still_works(self):
        """Legacy MCP call with URL in question and no page_id param →
        body=None → Priority-2 path operative (no regression)."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        captured: dict = {}

        def _fake_maybe_render_artifact(app_state, result, question, body=None):
            captured["body"] = body
            captured["question"] = question

        def _fake_build_ask_response(result, consumer):
            return {}

        with patch("framework.deploy.routes.ask.maybe_render_artifact", _fake_maybe_render_artifact), \
             patch("framework.deploy.routes.ask._build_ask_response", _fake_build_ask_response), \
             patch("framework.orchestrator.budget.Budget", MagicMock()):
            handler = _make_ask_handler(app)
            _run(handler(question=QUESTION_WITH_URL))  # no page_id kwarg

        # body=None → Priority-2 in maybe_render_artifact will extract from question
        assert captured["body"] is None
        assert captured["question"] == QUESTION_WITH_URL

    def test_ask_handler_signature_has_page_id_with_empty_default(self):
        """ask_handler must accept page_id with default '' so existing callers
        that do not pass it are unaffected."""
        from framework.deploy.mcp_tools import _make_ask_handler

        app = _make_app()
        handler = _make_ask_handler(app)

        sig = inspect.signature(handler)
        assert "page_id" in sig.parameters, (
            "ask_handler must accept page_id parameter"
        )
        param = sig.parameters["page_id"]
        assert param.default == "", (
            f"page_id must default to '' for backward-compat; got {param.default!r}"
        )
