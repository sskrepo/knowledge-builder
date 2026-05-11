"""Integration tests for the wired-up mcp_server FastAPI app.

Uses FastAPI TestClient against a lightweight version of the app that:
  - Uses a real FilestoreSessionStore (tmp_path)
  - Has a mock ContextBuilder on app.state
  - Has a real ConsumerRegistry pointing at a temp manifest directory
  - Registers the two external MCP tools via build_external_tool_registry()

Coverage:
  - /mcp/tools/list returns exactly 2 tools (askKnowledgeBase, authorSkill)
  - /mcp/tools/list returns correct schema for each tool
  - /mcp/tools/call with "askKnowledgeBase" calls the handler and returns content
  - /mcp/tools/call with "authorSkill" starts a session and returns synthId
  - /mcp/tools/call with internal tool "vector_search" returns 404
  - /healthz returns 200 without auth
  - /api/v1/version returns 200 without auth and apiVersion field
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _FASTAPI_AVAILABLE,
    reason="fastapi not installed",
)


# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_integration_app(tmp_path: Path) -> "FastAPI":
    """Build a minimal wired app without loading real retrievers."""
    from fastapi import FastAPI, HTTPException, Request
    from framework.deploy.auth.consumer import ConsumerManifest
    from framework.deploy.auth.middleware import bearer_auth_middleware
    from framework.deploy.auth.registry import ConsumerRegistry
    from framework.deploy.cost_store import CostStore
    from framework.deploy.mcp_tools import build_external_tool_registry, EXTERNAL_TOOLS_SCHEMA
    from framework.deploy.routes.ask import router as ask_router
    from framework.deploy.routes.author_skill import router as author_skill_router
    from framework.deploy.routes.ops import router as ops_router
    from framework.deploy.session.filestore import FilestoreSessionStore
    from framework.deploy.serialization import to_camel_response

    # --- Write a dev consumer manifest to temp directory ---
    manifests_dir = tmp_path / "consumer_manifests"
    manifests_dir.mkdir()
    dev_token = "integration-test-token-abc123"
    dev_token_hash = hashlib.sha256(dev_token.encode()).hexdigest()
    (manifests_dir / "dev.yaml").write_text(
        f"""
name: integration-test-consumer
tokenHash: {dev_token_hash}
scopes:
  - read
  - write
  - admin
personaAllowlist: []
rpmCap: 120
tokenBudgetPerRequest: 8000
userId: integration-test-user
""",
        encoding="utf-8",
    )

    store_dir = tmp_path / "store"
    store_dir.mkdir()

    # --- Build the mock context builder ---
    ctx_builder = MagicMock()
    ctx_builder.answer.return_value = {
        "answer": "Integration test answer.",
        "tier": 2,
        "intent": {"persona": "ops_eng", "confidence": 0.9},
        "passages": [
            {
                "text": "Sample passage.",
                "citation": "https://example.com/wiki/INC-001",
                "score": 0.95,
            }
        ],
        "cost": {"prompt": 40, "completion": 20, "total": 60},
        "latency_ms": 50,
    }

    app = FastAPI(title="Integration Test MCP Server")

    # Wire up state before route registration
    @app.on_event("startup")
    async def _startup():
        app.state.consumer_registry = ConsumerRegistry(manifests_dir)
        app.state.session_store = FilestoreSessionStore(store_root=str(store_dir))
        app.state.cost_store = CostStore(str(store_dir))
        app.state.llm = None
        app.state.context_builder = ctx_builder

        # Wire external MCP tools
        external_registry = build_external_tool_registry(app)
        app.state._external_registry = external_registry
        app.state._external_tools_schema = EXTERNAL_TOOLS_SCHEMA

    # Auth middleware
    app.middleware("http")(bearer_auth_middleware)

    # REST routes
    app.include_router(ask_router)
    app.include_router(author_skill_router)
    app.include_router(ops_router)

    # MCP endpoints (mirrors mcp_server.py)
    @app.post("/mcp/tools/list")
    async def tools_list():
        return {"tools": app.state._external_tools_schema}

    @app.post("/mcp/tools/call")
    async def tools_call(req: Request):
        body = await req.json()
        name = body.get("name")
        args = body.get("arguments", {})
        registry = app.state._external_registry
        handler = registry.get(name)
        if handler is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown external tool: {name!r}",
            )
        consumer = getattr(req.state, "consumer", None)
        try:
            result = await handler(**args, _consumer=consumer)
        except TypeError as exc:
            raise HTTPException(400, f"bad args: {exc}")
        return {"content": result}

    return app, dev_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def integration_client(tmp_path):
    app, dev_token = _make_integration_app(tmp_path)
    with TestClient(app) as client:
        client._dev_token = dev_token
        yield client


def _auth_headers(client) -> dict:
    return {"Authorization": f"Bearer {client._dev_token}"}


# ---------------------------------------------------------------------------
# /mcp/tools/list
# ---------------------------------------------------------------------------


class TestMcpToolsList:
    def test_returns_200(self, integration_client):
        resp = integration_client.post("/mcp/tools/list", headers=_auth_headers(integration_client))
        assert resp.status_code == 200

    def test_returns_exactly_two_tools(self, integration_client):
        resp = integration_client.post("/mcp/tools/list", headers=_auth_headers(integration_client))
        tools = resp.json()["tools"]
        assert len(tools) == 2

    def test_tool_names_are_correct(self, integration_client):
        resp = integration_client.post("/mcp/tools/list", headers=_auth_headers(integration_client))
        names = {t["name"] for t in resp.json()["tools"]}
        assert names == {"askKnowledgeBase", "authorSkill"}

    def test_each_tool_has_input_schema(self, integration_client):
        resp = integration_client.post("/mcp/tools/list", headers=_auth_headers(integration_client))
        for tool in resp.json()["tools"]:
            assert "inputSchema" in tool, f"Missing inputSchema in {tool['name']}"

    def test_no_internal_tools_exposed(self, integration_client):
        resp = integration_client.post("/mcp/tools/list", headers=_auth_headers(integration_client))
        names = {t["name"] for t in resp.json()["tools"]}
        internal = {"vector_search", "get_incident_summary", "list_sources",
                    "query_fleet", "text_to_sql", "find_symbol", "read_code_page"}
        assert names.isdisjoint(internal), (
            f"Internal tools exposed on external surface: {names & internal}"
        )


# ---------------------------------------------------------------------------
# /mcp/tools/call — askKnowledgeBase
# ---------------------------------------------------------------------------


class TestMcpToolsCallAsk:
    def test_ask_returns_200(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "askKnowledgeBase", "arguments": {"question": "What failed?"}},
            headers=_auth_headers(integration_client),
        )
        assert resp.status_code == 200

    def test_ask_response_has_content(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "askKnowledgeBase", "arguments": {"question": "RCA for INC-999?"}},
            headers=_auth_headers(integration_client),
        )
        body = resp.json()
        assert "content" in body
        content = body["content"]
        assert "answer" in content

    def test_ask_citations_present(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "askKnowledgeBase", "arguments": {"question": "Who owns pod-33?"}},
            headers=_auth_headers(integration_client),
        )
        content = resp.json()["content"]
        assert "citations" in content

    def test_ask_tier_used_present(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "askKnowledgeBase", "arguments": {"question": "Any open incidents?"}},
            headers=_auth_headers(integration_client),
        )
        content = resp.json()["content"]
        assert "tier_used" in content


# ---------------------------------------------------------------------------
# /mcp/tools/call — authorSkill
# ---------------------------------------------------------------------------


class TestMcpToolsCallAuthorSkill:
    def test_author_skill_returns_200(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "authorSkill", "arguments": {"input": "I want to build a skill"}},
            headers=_auth_headers(integration_client),
        )
        assert resp.status_code == 200

    def test_author_skill_returns_synth_id(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "authorSkill", "arguments": {"input": "Build ops-eng skill"}},
            headers=_auth_headers(integration_client),
        )
        content = resp.json()["content"]
        assert "synth_id" in content
        assert content["synth_id"]  # non-empty

    def test_author_skill_returns_state(self, integration_client):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": "authorSkill", "arguments": {"input": "Start skill authoring"}},
            headers=_auth_headers(integration_client),
        )
        content = resp.json()["content"]
        assert "state" in content

    def test_author_skill_multi_turn(self, integration_client):
        """Start a session then continue it with the returned synthId."""
        # Turn 1: start
        resp1 = integration_client.post(
            "/mcp/tools/call",
            json={"name": "authorSkill", "arguments": {"input": "I want an incident triage skill"}},
            headers=_auth_headers(integration_client),
        )
        assert resp1.status_code == 200
        synth_id = resp1.json()["content"]["synth_id"]
        assert synth_id

        # Turn 2: continue with the synthId
        resp2 = integration_client.post(
            "/mcp/tools/call",
            json={
                "name": "authorSkill",
                "arguments": {"input": "ops_eng", "synthId": synth_id},
            },
            headers=_auth_headers(integration_client),
        )
        assert resp2.status_code == 200
        content2 = resp2.json()["content"]
        # Should carry the same synth_id
        assert content2.get("synth_id") == synth_id


# ---------------------------------------------------------------------------
# /mcp/tools/call — internal tool blocked
# ---------------------------------------------------------------------------


class TestMcpInternalToolsBlocked:
    @pytest.mark.parametrize("internal_tool", [
        "vector_search",
        "get_incident_summary",
        "list_sources",
        "query_fleet",
        "text_to_sql",
        "find_symbol",
        "read_code_page",
    ])
    def test_internal_tool_returns_404(self, integration_client, internal_tool):
        resp = integration_client.post(
            "/mcp/tools/call",
            json={"name": internal_tool, "arguments": {}},
            headers=_auth_headers(integration_client),
        )
        assert resp.status_code == 404, (
            f"Expected 404 for internal tool '{internal_tool}', got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Public endpoints (no auth required)
# ---------------------------------------------------------------------------


class TestPublicEndpoints:
    def test_healthz_returns_200(self, integration_client):
        resp = integration_client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_has_status_field(self, integration_client):
        resp = integration_client.get("/healthz")
        body = resp.json()
        # to_camel_response converts status → status (already camelCase-safe)
        assert "status" in body

    def test_version_returns_200(self, integration_client):
        resp = integration_client.get("/api/v1/version")
        assert resp.status_code == 200

    def test_version_has_api_version(self, integration_client):
        resp = integration_client.get("/api/v1/version")
        body = resp.json()
        # api_version → apiVersion after to_camel_response
        assert "apiVersion" in body
        assert body["apiVersion"] == "v1"

    def test_healthz_no_auth_required(self, integration_client):
        """Verify /healthz works without Authorization header."""
        resp = integration_client.get("/healthz")
        assert resp.status_code != 401

    def test_version_no_auth_required(self, integration_client):
        """Verify /api/v1/version works without Authorization header."""
        resp = integration_client.get("/api/v1/version")
        assert resp.status_code != 401
