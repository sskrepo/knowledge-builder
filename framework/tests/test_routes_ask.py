"""Tests for POST /api/v1/ask route.

Uses FastAPI TestClient with a lightweight test app that:
  - Has a mock context_builder on app.state returning a canned result
  - Has a mock consumer attached via a simple middleware shim
  - Does NOT load real retrievers, LLM, or orchestrator deps

Coverage:
  - Happy path returns 200 with correct camelCase fields
  - Empty question returns 400
  - Whitespace-only question returns 400
  - Question exceeding 4096 chars returns 400
  - Tier 4 response includes skillSuggestion
  - Citations are mapped correctly
  - costTokens fields present (prompt, completion, total)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from framework.deploy.auth.consumer import ConsumerManifest
from framework.deploy.routes.ask import router as ask_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_consumer(scopes: list[str] | None = None) -> ConsumerManifest:
    return ConsumerManifest(
        name="test-consumer",
        token_hash="deadbeef",
        scopes=scopes if scopes is not None else ["read", "write"],
        persona_allowlist=[],
        rpm_cap=60,
        token_budget_per_request=8000,
        user_id="test-user-001",
    )


def _canned_result(
    answer: str = "The root cause was a misconfigured replica.",
    tier: int = 2,
    confidence: float = 0.72,
    passages: list | None = None,
) -> dict:
    """Build a canned ContextBuilder.answer() return dict."""
    if passages is None:
        passages = [
            {
                "text": "Replica lag exceeded 30 s causing pod DB failure.",
                "citation": "https://wiki.example.com/incidents/INC-001",
                "score": 0.88,
            }
        ]
    return {
        "answer": answer,
        "schema": "GENERIC_QA",
        "tier": tier,
        "intent": {
            "persona": "ops_eng",
            "personas": None,
            "confidence": confidence,
            "workflow_skill": None,
            "reasoning": "KB retrieval match",
        },
        "passages": passages,
        "citations": ["https://wiki.example.com/incidents/INC-001"],
        "used_kbs": ["ops_eng.incidents"],
        "used_tools": [],
        "cost": {"prompt": 120, "completion": 80, "total": 200, "tool_calls": 0},
        "latency_ms": 312,
    }


def _make_test_app(consumer: ConsumerManifest, context_builder=None) -> FastAPI:
    """Build a minimal FastAPI app with the ask router and mocked dependencies."""
    app = FastAPI()
    app.include_router(ask_router)

    # Attach mock context_builder
    if context_builder is None:
        mock_cb = MagicMock()
        mock_cb.answer.return_value = _canned_result()
        context_builder = mock_cb

    # Startup: wire state
    @app.on_event("startup")
    async def _startup():
        app.state.context_builder = context_builder

    # Middleware shim: attach consumer to request.state so get_consumer() works
    @app.middleware("http")
    async def _attach_consumer(request: Request, call_next):
        request.state.consumer = consumer
        return await call_next(request)

    return app


@pytest.fixture()
def client() -> TestClient:
    """Default test client with read+write scopes and a canned result."""
    app = _make_test_app(_make_consumer())
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def read_only_client() -> TestClient:
    """Test client with only read scope."""
    app = _make_test_app(_make_consumer(scopes=["read"]))
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestAskHappyPath:
    def test_returns_200(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "What broke last week?"})
        assert resp.status_code == 200

    def test_response_has_answer(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "What broke last week?"})
        body = resp.json()
        assert "answer" in body
        assert isinstance(body["answer"], str)
        assert len(body["answer"]) > 0

    def test_response_has_camel_case_fields(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "What broke last week?"})
        body = resp.json()
        # camelCase keys must be present (snake_case must not leak)
        assert "tierUsed" in body, f"tierUsed missing from {list(body.keys())}"
        assert "tierDescription" in body
        assert "costTokens" in body
        assert "latencyMs" in body
        # snake_case must NOT be present
        assert "tier_used" not in body
        assert "tier_description" not in body
        assert "cost_tokens" not in body

    def test_citations_are_returned(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "What broke last week?"})
        body = resp.json()
        assert "citations" in body
        assert isinstance(body["citations"], list)
        assert len(body["citations"]) == 1
        citation = body["citations"][0]
        assert "citationUrl" in citation
        assert "text" in citation
        assert "relevanceScore" in citation

    def test_cost_tokens_structure(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "Any question"})
        body = resp.json()
        cost = body["costTokens"]
        assert "prompt" in cost
        assert "completion" in cost
        assert "total" in cost
        assert cost["prompt"] == 120
        assert cost["completion"] == 80
        assert cost["total"] == 200

    def test_tier_and_confidence(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "Any question"})
        body = resp.json()
        assert body["tierUsed"] == 2
        assert body["tierDescription"] == "kb_retrieval"
        assert isinstance(body["confidence"], float)

    def test_max_results_accepted(self, client: TestClient):
        resp = client.post(
            "/api/v1/ask",
            json={"question": "What is the status?", "maxResults": 5},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tier 4 — skill suggestion
# ---------------------------------------------------------------------------


class TestAskTier4:
    def test_tier4_includes_skill_suggestion(self):
        tier4_result = _canned_result(
            answer="",
            tier=4,
            confidence=0.2,
            passages=[],
        )
        mock_cb = MagicMock()
        mock_cb.answer.return_value = tier4_result
        app = _make_test_app(_make_consumer(), context_builder=mock_cb)

        with TestClient(app) as c:
            resp = c.post("/api/v1/ask", json={"question": "Something we have no KB for"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["tierUsed"] == 4
        assert body["tierDescription"] == "no_answer"
        assert "skillSuggestion" in body
        suggestion = body["skillSuggestion"]
        assert "message" in suggestion

    def test_tier4_no_citations(self):
        tier4_result = _canned_result(answer="", tier=4, confidence=0.1, passages=[])
        mock_cb = MagicMock()
        mock_cb.answer.return_value = tier4_result
        app = _make_test_app(_make_consumer(), context_builder=mock_cb)

        with TestClient(app) as c:
            resp = c.post("/api/v1/ask", json={"question": "Unknown territory"})

        body = resp.json()
        assert body["citations"] == []


# ---------------------------------------------------------------------------
# Validation failures — 400
# ---------------------------------------------------------------------------


class TestAskValidation:
    def test_empty_question_returns_400(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": ""})
        assert resp.status_code == 400

    def test_whitespace_only_question_returns_400(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": "   "})
        assert resp.status_code == 400

    def test_missing_question_returns_400(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={})
        assert resp.status_code == 400

    def test_question_over_4096_chars_returns_400(self, client: TestClient):
        long_q = "x" * 4097
        resp = client.post("/api/v1/ask", json={"question": long_q})
        assert resp.status_code == 400

    def test_question_at_exactly_4096_chars_passes(self, client: TestClient):
        edge_q = "x" * 4096
        resp = client.post("/api/v1/ask", json={"question": edge_q})
        assert resp.status_code == 200

    def test_400_response_has_error_structure(self, client: TestClient):
        resp = client.post("/api/v1/ask", json={"question": ""})
        body = resp.json()
        assert "error" in body or "detail" in body  # HTTPException detail or structured error


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestAskScopeEnforcement:
    def test_read_scope_sufficient_for_ask(self, read_only_client: TestClient):
        """read scope is sufficient — no write required for /ask."""
        resp = read_only_client.post("/api/v1/ask", json={"question": "Hello?"})
        assert resp.status_code == 200

    def test_no_read_scope_returns_403(self):
        """Consumer without 'read' scope should get 403."""
        consumer_no_read = _make_consumer(scopes=["write"])
        app = _make_test_app(consumer_no_read)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/api/v1/ask", json={"question": "Hello?"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Budget is passed from consumer token_budget_per_request
# ---------------------------------------------------------------------------


class TestAskBudget:
    def test_budget_uses_consumer_token_budget(self):
        """Verify that context_builder.answer() is called with a Budget whose
        max_tokens_in matches the consumer's token_budget_per_request."""
        captured_budgets: list = []

        def fake_answer(query: str, budget=None):
            captured_budgets.append(budget)
            return _canned_result()

        mock_cb = MagicMock()
        mock_cb.answer.side_effect = fake_answer

        consumer = _make_consumer()
        consumer_budget = consumer.token_budget_per_request  # 8000

        app = _make_test_app(consumer, context_builder=mock_cb)
        with TestClient(app) as c:
            c.post("/api/v1/ask", json={"question": "Budget test?"})

        assert len(captured_budgets) == 1
        budget = captured_budgets[0]
        assert budget.max_tokens_in == consumer_budget
