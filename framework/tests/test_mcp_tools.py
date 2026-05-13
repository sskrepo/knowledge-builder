"""Tests for framework/deploy/mcp_tools.py.

Coverage:
  - build_external_tool_registry() returns exactly 5 tools (ADR-023: +reviewSkillSession)
  - Tool names are "askKnowledgeBase", "authorSkill", "reportBug", "uploadArtifact", "reviewSkillSession"
  - EXTERNAL_TOOLS_SCHEMA has exactly 5 entries with correct names
  - Each schema has an inputSchema with a required properties list
  - askKnowledgeBase schema requires "question"
  - authorSkill schema requires "input"
  - reportBug schema requires "requestId", "tool", "description"
  - Handlers are callable (async functions)
  - _anonymous_consumer() returns a valid ConsumerManifest with expected defaults
  - askKnowledgeBase handler calls ContextBuilder.answer() and _build_ask_response()
  - authorSkill handler calls _start_or_continue_session()
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.deploy.mcp_tools import (
    EXTERNAL_TOOLS_SCHEMA,
    build_external_tool_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_app(*, consumer_registry=None, session_store=None, llm=None, context_builder=None):
    """Build a minimal mock FastAPI app with the state attrs used by mcp_tools."""
    app = MagicMock()
    app.state.consumer_registry = consumer_registry or MagicMock()
    app.state.session_store = session_store or MagicMock()
    app.state.llm = llm
    app.state.context_builder = context_builder or MagicMock()
    return app


def _run(coro):
    """Run an awaitable synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# EXTERNAL_TOOLS_SCHEMA tests
# ---------------------------------------------------------------------------


class TestExternalToolsSchema:
    def test_exactly_five_entries(self):
        # ADR-023 added reviewSkillSession — now 5 tools
        assert len(EXTERNAL_TOOLS_SCHEMA) == 5

    def test_names_are_correct(self):
        names = {t["name"] for t in EXTERNAL_TOOLS_SCHEMA}
        assert names == {
            "askKnowledgeBase", "authorSkill", "reportBug",
            "uploadArtifact", "reviewSkillSession",
        }

    def test_each_entry_has_name_description_inputschema(self):
        for tool in EXTERNAL_TOOLS_SCHEMA:
            assert "name" in tool, f"Missing 'name' in tool: {tool}"
            assert "description" in tool, f"Missing 'description' in {tool['name']}"
            assert "inputSchema" in tool, f"Missing 'inputSchema' in {tool['name']}"

    def test_ask_knowledge_base_requires_question(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        required = schema["inputSchema"].get("required", [])
        assert "question" in required

    def test_ask_knowledge_base_question_property_has_max_length(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        props = schema["inputSchema"]["properties"]
        assert "question" in props
        assert props["question"].get("maxLength") == 4096

    def test_ask_knowledge_base_optional_hints_present(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "askKnowledgeBase")
        props = schema["inputSchema"]["properties"]
        for hint in ("persona", "serviceId", "functionalArea", "maxResults"):
            assert hint in props, f"Missing optional property '{hint}' in askKnowledgeBase schema"

    def test_author_skill_requires_input(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "authorSkill")
        required = schema["inputSchema"].get("required", [])
        assert "input" in required

    def test_author_skill_has_synth_id_property(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "authorSkill")
        props = schema["inputSchema"]["properties"]
        assert "synthId" in props

    def test_author_skill_input_has_max_length(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "authorSkill")
        props = schema["inputSchema"]["properties"]
        assert props["input"].get("maxLength") == 4096

    def test_report_bug_requires_request_id_tool_description(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "reportBug")
        required = schema["inputSchema"].get("required", [])
        assert "requestId" in required
        assert "tool" in required
        assert "description" in required

    def test_report_bug_has_optional_input_property(self):
        schema = next(t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "reportBug")
        props = schema["inputSchema"]["properties"]
        assert "input" in props

    def test_input_schema_type_is_object(self):
        for tool in EXTERNAL_TOOLS_SCHEMA:
            assert tool["inputSchema"]["type"] == "object", (
                f"inputSchema.type should be 'object' for {tool['name']}"
            )


# ---------------------------------------------------------------------------
# build_external_tool_registry() tests
# ---------------------------------------------------------------------------


class TestBuildExternalToolRegistry:
    def test_returns_dict(self):
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert isinstance(registry, dict)

    def test_exactly_five_tools(self):
        # ADR-023 added reviewSkillSession — now 5 tools
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert len(registry) == 5

    def test_tool_names_match_schema(self):
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert set(registry.keys()) == {
            "askKnowledgeBase", "authorSkill", "reportBug",
            "uploadArtifact", "reviewSkillSession",
        }

    def test_ask_handler_is_callable(self):
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert callable(registry["askKnowledgeBase"])

    def test_author_skill_handler_is_callable(self):
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert callable(registry["authorSkill"])

    def test_report_bug_handler_is_callable(self):
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert callable(registry["reportBug"])

    def test_handlers_are_coroutine_functions(self):
        """Handlers must be async (awaitable) so the MCP dispatch can await them."""
        import inspect
        app = _make_mock_app()
        registry = build_external_tool_registry(app)
        assert inspect.iscoroutinefunction(registry["askKnowledgeBase"]), (
            "askKnowledgeBase handler must be async"
        )
        assert inspect.iscoroutinefunction(registry["authorSkill"]), (
            "authorSkill handler must be async"
        )
        assert inspect.iscoroutinefunction(registry["reportBug"]), (
            "reportBug handler must be async"
        )


# ---------------------------------------------------------------------------
# askKnowledgeBase handler behaviour
# ---------------------------------------------------------------------------


class TestAskKnowledgeBaseHandler:
    def _make_ctx_builder(self):
        """Return a mock ContextBuilder with a canned answer()."""
        ctx = MagicMock()
        ctx.answer.return_value = {
            "answer": "A test answer.",
            "tier": 2,
            "intent": {"persona": "ops_eng", "confidence": 0.8},
            "passages": [],
            "cost": {"prompt": 50, "completion": 30, "total": 80},
            "latency_ms": 100,
        }
        return ctx

    def test_happy_path_returns_dict(self):
        ctx = self._make_ctx_builder()
        app = _make_mock_app(context_builder=ctx)
        registry = build_external_tool_registry(app)
        handler = registry["askKnowledgeBase"]

        result = _run(handler(question="What is the incident RCA?"))

        assert isinstance(result, dict)
        assert "answer" in result

    def test_context_builder_answer_called(self):
        ctx = self._make_ctx_builder()
        app = _make_mock_app(context_builder=ctx)
        registry = build_external_tool_registry(app)
        handler = registry["askKnowledgeBase"]

        _run(handler(question="What happened in INC-001?"))

        ctx.answer.assert_called_once()
        call_kwargs = ctx.answer.call_args[1]
        assert call_kwargs["query"] == "What happened in INC-001?"

    def test_consumer_token_budget_respected(self):
        from framework.deploy.auth.consumer import ConsumerManifest

        ctx = self._make_ctx_builder()
        app = _make_mock_app(context_builder=ctx)
        registry = build_external_tool_registry(app)
        handler = registry["askKnowledgeBase"]

        consumer = ConsumerManifest(
            name="test",
            token_hash="abc",
            scopes=["read"],
            persona_allowlist=[],
            rpm_cap=60,
            token_budget_per_request=4000,
            user_id="user-1",
        )

        _run(handler(question="Test question", _consumer=consumer))

        # Budget should have been built with the consumer's token_budget_per_request
        call_kwargs = ctx.answer.call_args[1]
        budget = call_kwargs.get("budget")
        assert budget is not None
        assert budget.max_tokens_in == 4000

    def test_anonymous_consumer_used_when_not_provided(self):
        ctx = self._make_ctx_builder()
        app = _make_mock_app(context_builder=ctx)
        registry = build_external_tool_registry(app)
        handler = registry["askKnowledgeBase"]

        # No _consumer provided — should use anonymous consumer (no exception)
        result = _run(handler(question="Who is responsible for pod-99?"))
        assert isinstance(result, dict)

    def test_response_contains_tier_used(self):
        ctx = self._make_ctx_builder()
        app = _make_mock_app(context_builder=ctx)
        registry = build_external_tool_registry(app)
        handler = registry["askKnowledgeBase"]

        result = _run(handler(question="Summarise INC-042"))

        assert "tier_used" in result

    def test_response_contains_citations(self):
        ctx = self._make_ctx_builder()
        app = _make_mock_app(context_builder=ctx)
        registry = build_external_tool_registry(app)
        handler = registry["askKnowledgeBase"]

        result = _run(handler(question="What is the incident pattern?"))

        assert "citations" in result
        assert isinstance(result["citations"], list)


# ---------------------------------------------------------------------------
# authorSkill handler behaviour
# ---------------------------------------------------------------------------


class TestAuthorSkillHandler:
    def _make_session_store(self):
        """Return a minimal mock session store."""
        store = MagicMock()
        store.load.return_value = None
        store.save.return_value = None
        return store

    def test_new_session_returns_dict_with_synth_id(self):
        store = self._make_session_store()
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        # Start a new session (no synthId)
        result = _run(handler(input="I want to build a skill for incident triage"))

        assert isinstance(result, dict)
        # Result should have synth_id (new session created by SkillBuilderConversation)
        assert "synth_id" in result

    def test_new_session_sets_state(self):
        store = self._make_session_store()
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        result = _run(handler(input="Build me a weekly report skill"))

        assert "state" in result
        # Initial state should be the first state in the 15-state machine
        assert isinstance(result["state"], str)
        assert len(result["state"]) > 0

    def test_new_session_session_save_called(self):
        store = self._make_session_store()
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        _run(handler(input="Author a fleet-status skill"))

        store.save.assert_called_once()

    def test_consumer_user_id_used_for_session(self):
        from framework.deploy.auth.consumer import ConsumerManifest

        store = self._make_session_store()
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        consumer = ConsumerManifest(
            name="test-consumer",
            token_hash="xyz",
            scopes=["write"],
            persona_allowlist=[],
            rpm_cap=60,
            token_budget_per_request=8000,
            user_id="specific-user-99",
        )

        _run(handler(input="New skill request", _consumer=consumer))

        save_args = store.save.call_args
        assert save_args is not None
        # save(session_dict, user_id=...) — verify keyword user_id
        call_kwargs = save_args[1]
        assert call_kwargs.get("user_id") == "specific-user-99"

    def test_handler_passes_skill_store_to_session(self):
        """Regression for session synth-tpm-14a54555: MCP authorSkill handler
        previously failed to pass app.state.skill_store into
        _start_or_continue_session. The conversation ran with _skill_store=None,
        the ADB write was silently skipped, and the user saw "Committed N"
        while KBF_SKILL_ARTIFACTS got nothing. This test locks the wiring.
        """
        # Patch at the source module — _make_author_skill_handler does a local
        # `from .routes.author_skill import _start_or_continue_session` each
        # time it's called, so we must patch before build_external_tool_registry.
        with patch(
            "framework.deploy.routes.author_skill._start_or_continue_session",
            return_value={"synth_id": "x", "state": "X", "done": False},
        ) as mock_start:
            store = self._make_session_store()
            app = _make_mock_app(session_store=store)
            sentinel_skill_store = MagicMock(name="skill_store_sentinel")
            app.state.skill_store = sentinel_skill_store

            registry = build_external_tool_registry(app)
            handler = registry["authorSkill"]

            _run(handler(input="anything"))

        mock_start.assert_called_once()
        call_kwargs = mock_start.call_args.kwargs
        assert call_kwargs.get("skill_store") is sentinel_skill_store, (
            "MCP authorSkill handler must pass app.state.skill_store; "
            "forgetting this causes silent durable-write loss (BUG-queue-e8298 / "
            "session synth-tpm-14a54555)."
        )

    def test_anonymous_consumer_fallback(self):
        store = self._make_session_store()
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        # No _consumer — should use mcp-anonymous user_id without error
        result = _run(handler(input="Start anonymous session"))
        assert isinstance(result, dict)

    def test_existing_session_not_found_returns_error_dict(self):
        store = self._make_session_store()
        store.load.return_value = None  # session not found
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        result = _run(handler(input="continue", synthId="nonexistent-synth-id"))

        # _start_or_continue_session returns {"_error": {...}} for missing sessions
        assert "_error" in result
        assert result["_error"]["code"] == "not_found"

    def test_done_field_present_in_response(self):
        store = self._make_session_store()
        app = _make_mock_app(session_store=store)
        registry = build_external_tool_registry(app)
        handler = registry["authorSkill"]

        result = _run(handler(input="I need a skill"))
        assert "done" in result


# ---------------------------------------------------------------------------
# Anonymous consumer helper
# ---------------------------------------------------------------------------


class TestAnonymousConsumer:
    def test_anonymous_consumer_is_consumer_manifest(self):
        from framework.deploy.auth.consumer import ConsumerManifest
        from framework.deploy.mcp_tools import _anonymous_consumer

        consumer = _anonymous_consumer()
        assert isinstance(consumer, ConsumerManifest)

    def test_anonymous_consumer_name(self):
        from framework.deploy.mcp_tools import _anonymous_consumer

        consumer = _anonymous_consumer()
        assert consumer.name == "mcp-anonymous"

    def test_anonymous_consumer_user_id(self):
        from framework.deploy.mcp_tools import _anonymous_consumer

        consumer = _anonymous_consumer()
        assert consumer.user_id == "mcp-anonymous"

    def test_anonymous_consumer_has_read_write_scopes(self):
        from framework.deploy.mcp_tools import _anonymous_consumer

        consumer = _anonymous_consumer()
        assert "read" in consumer.scopes
        assert "write" in consumer.scopes

    def test_anonymous_consumer_reasonable_budget(self):
        from framework.deploy.mcp_tools import _anonymous_consumer

        consumer = _anonymous_consumer()
        assert consumer.token_budget_per_request > 0
        assert consumer.rpm_cap > 0


# ---------------------------------------------------------------------------
# reportBug handler behaviour (Sprint 1)
# ---------------------------------------------------------------------------


class TestReportBugHandler:
    def _make_mock_app_with_error_store(self, tmp_path=None):
        """Return a mock app with error_store attached."""
        import tempfile
        from pathlib import Path
        from framework.deploy.error_store import ErrorStore

        if tmp_path is None:
            tmp_path = Path(tempfile.mkdtemp())

        app = _make_mock_app()
        app.state.error_store = ErrorStore(tmp_path)
        return app, tmp_path

    def test_happy_path_returns_queued_true(self, tmp_path):
        app, _ = self._make_mock_app_with_error_store(tmp_path)
        registry = build_external_tool_registry(app)
        handler = registry["reportBug"]

        result = _run(handler(
            requestId="req-abc123",
            tool="authorSkill",
            description="Session failed on continue",
        ))

        assert result["queued"] is True

    def test_happy_path_returns_queue_id(self, tmp_path):
        app, _ = self._make_mock_app_with_error_store(tmp_path)
        registry = build_external_tool_registry(app)
        handler = registry["reportBug"]

        result = _run(handler(
            requestId="req-def456",
            tool="askKnowledgeBase",
            description="Query returned error",
        ))

        assert "queueId" in result
        assert result["queueId"].startswith("BUG-queue-")

    def test_returns_friendly_message(self, tmp_path):
        app, _ = self._make_mock_app_with_error_store(tmp_path)
        registry = build_external_tool_registry(app)
        handler = registry["reportBug"]

        result = _run(handler(
            requestId="req-ghi789",
            tool="authorSkill",
            description="It broke",
        ))

        assert "message" in result
        assert len(result["message"]) > 0

    def test_writes_to_error_store(self, tmp_path):
        from framework.deploy.error_store import ErrorStore

        app, store_path = self._make_mock_app_with_error_store(tmp_path)
        registry = build_external_tool_registry(app)
        handler = registry["reportBug"]

        _run(handler(
            requestId="req-store-check",
            tool="authorSkill",
            description="storing bug",
        ))

        store = ErrorStore(store_path)
        bugs = store.read_user_bugs()
        assert len(bugs) == 1
        assert bugs[0]["request_id"] == "req-store-check"

    def test_no_error_store_does_not_raise(self):
        """reportBug must not raise even when app.state.error_store is None."""
        app = _make_mock_app()
        app.state.error_store = None
        registry = build_external_tool_registry(app)
        handler = registry["reportBug"]

        # Should not raise
        result = _run(handler(
            requestId="req-no-store",
            tool="authorSkill",
            description="test",
        ))
        assert result["queued"] is True

    def test_optional_input_field_accepted(self, tmp_path):
        app, _ = self._make_mock_app_with_error_store(tmp_path)
        registry = build_external_tool_registry(app)
        handler = registry["reportBug"]

        result = _run(handler(
            requestId="req-with-input",
            tool="authorSkill",
            description="tried to continue",
            input={"synthId": "synth-abc", "input": "continue"},
        ))

        assert result["queued"] is True
