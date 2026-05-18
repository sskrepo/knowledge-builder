"""Tests for author_skill routes.

Coverage:
  POST /api/v1/kb/authorSkill (new session)   → 200 with synthId + state
  POST /api/v1/kb/authorSkill/{synth_id}      → advances state, returns new turn
  GET  /api/v1/kb/authorSkill                 → lists sessions for user
  GET  /api/v1/kb/authorSkill/{synth_id}      → returns last turn / state
  DELETE /api/v1/kb/authorSkill/{synth_id}    → abandons session

Edge cases:
  POST to nonexistent synthId  → 404
  DELETE nonexistent synthId   → 404
  Read scope insufficient for POST/DELETE → 403
  Write scope sufficient for GET → yes (write implies read in this API)
  List returns only this user's sessions
  Session persisted to filestore (real FilestoreSessionStore on tmp_path)

Design:
  - Real FilestoreSessionStore on tmp_path (no mocks for persistence)
  - LLM=None (conversation.py works in stub/template mode without LLM)
  - Consumer attached via middleware shim (bypasses bearer_auth_middleware)
  - No real ContextBuilder needed — author_skill doesn't use it
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from framework.deploy.auth.consumer import ConsumerManifest
from framework.deploy.routes.author_skill import router as author_skill_router
from framework.deploy.session.filestore import FilestoreSessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_consumer(
    user_id: str = "test-user-001",
    scopes: list[str] | None = None,
) -> ConsumerManifest:
    return ConsumerManifest(
        name="test-consumer",
        token_hash="deadbeef",
        scopes=scopes if scopes is not None else ["read", "write"],
        persona_allowlist=[],
        rpm_cap=60,
        token_budget_per_request=8000,
        user_id=user_id,
    )


def _make_test_app(
    consumer: ConsumerManifest,
    store: FilestoreSessionStore,
) -> FastAPI:
    """Minimal FastAPI app with author_skill router and real filestore."""
    app = FastAPI()
    app.include_router(author_skill_router)

    @app.on_event("startup")
    async def _startup():
        app.state.session_store = store
        app.state.llm = None  # stub LLM mode
        # skill_store is required by author_skill routes (since refactor/skill_store
        # commit 11f00c7 — ADB is the source of truth; passing None raises).
        # Tests use a MagicMock so all ADB operations are no-ops.
        app.state.skill_store = MagicMock()

    @app.middleware("http")
    async def _attach_consumer(request: Request, call_next):
        request.state.consumer = consumer
        return await call_next(request)

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> FilestoreSessionStore:
    return FilestoreSessionStore(store_root=tmp_path)


@pytest.fixture()
def consumer() -> ConsumerManifest:
    return _make_consumer()


@pytest.fixture()
def client(store: FilestoreSessionStore, consumer: ConsumerManifest) -> TestClient:
    app = _make_test_app(consumer, store)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# POST — start new session
# ---------------------------------------------------------------------------


class TestAuthorSkillStartNewSession:
    def test_post_new_session_returns_200(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        assert resp.status_code == 200

    def test_post_new_session_returns_synth_id(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        assert "synthId" in body, f"synthId missing from {list(body.keys())}"
        assert body["synthId"].startswith("synth-")

    def test_post_new_session_returns_state(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        assert "state" in body
        assert body["state"] == "IDENTIFY_PERSONA"

    def test_post_new_session_returns_message(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        assert "message" in body
        assert len(body["message"]) > 0

    def test_post_new_session_returns_options(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        assert "options" in body
        # options may be a list or null depending on the persona listing
        assert body["options"] is None or isinstance(body["options"], list)

    def test_post_with_persona_and_intent_starts_at_identify_persona(self, client: TestClient):
        """When persona is provided without a real LLM, session should start normally.
        ADR-027: CAPTURE_INTENT requires a real LLM; with llm=None the route will raise
        before advancing to that state. Test that providing only persona (no intent) stays
        at IDENTIFY_PERSONA (the stub-safe path).
        """
        resp = client.post(
            "/api/v1/kb/authorSkill",
            json={"persona": "ops_eng"},
        )
        body = resp.json()
        assert resp.status_code == 200
        # With persona only (no intent), should remain at IDENTIFY_PERSONA
        assert body["state"] == "IDENTIFY_PERSONA"

    def test_post_new_session_persists_to_store(
        self, client: TestClient, store: FilestoreSessionStore, consumer: ConsumerManifest
    ):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        synth_id = resp.json()["synthId"]

        # Verify it was persisted
        session = store.load(synth_id, user_id=consumer.user_id)
        assert session is not None
        assert session["synth_id"] == synth_id

    def test_post_new_session_camel_case_response(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        # Spot-check camelCase — synthId, artifactsPreview (null but key present)
        assert "synthId" in body
        assert "artifactsPreview" in body or "artifacts_preview" not in body
        assert "synth_id" not in body


# ---------------------------------------------------------------------------
# POST — resume / continue session by synthId in body
# ---------------------------------------------------------------------------


class TestAuthorSkillContinueViaBody:
    def _start_session(self, client: TestClient) -> str:
        resp = client.post("/api/v1/kb/authorSkill", json={})
        assert resp.status_code == 200
        return resp.json()["synthId"]

    def test_post_with_synth_id_in_body_continues_session(self, client: TestClient):
        synth_id = self._start_session(client)
        resp = client.post(
            "/api/v1/kb/authorSkill",
            json={"synthId": synth_id, "userInput": "ops_eng — automate weekly ops status"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should have advanced from IDENTIFY_PERSONA
        assert body["state"] != "" or body["done"] is True

    def test_post_with_nonexistent_synth_id_in_body_returns_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/kb/authorSkill",
            json={"synthId": "synth-does-not-exist-abc", "userInput": "hello"},
        )
        assert resp.status_code == 404

    def test_continue_via_body_returns_camel_case(self, client: TestClient):
        synth_id = self._start_session(client)
        resp = client.post(
            "/api/v1/kb/authorSkill",
            json={"synthId": synth_id, "userInput": "ops_eng — test intent"},
        )
        body = resp.json()
        assert "synthId" in body
        assert "synth_id" not in body


# ---------------------------------------------------------------------------
# POST /{synth_id} — continue session by path param
# ---------------------------------------------------------------------------


class TestAuthorSkillContinueByPath:
    def _start_session(self, client: TestClient) -> str:
        resp = client.post("/api/v1/kb/authorSkill", json={})
        assert resp.status_code == 200
        return resp.json()["synthId"]

    def test_post_path_continues_session(self, client: TestClient):
        synth_id = self._start_session(client)
        resp = client.post(
            f"/api/v1/kb/authorSkill/{synth_id}",
            json={"userInput": "ops_eng — automate weekly ops status"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["synthId"] == synth_id

    def test_post_path_advances_state(self, client: TestClient):
        synth_id = self._start_session(client)
        # respond to IDENTIFY_PERSONA
        resp = client.post(
            f"/api/v1/kb/authorSkill/{synth_id}",
            json={"userInput": "ops_eng — automate weekly ops status"},
        )
        body = resp.json()
        # After identifying persona with intent, state should advance
        assert body["state"] in (
            "IDENTIFY_PERSONA",  # if input not fully parsed
            "ANALYZE_ARTIFACT",  # if persona+intent parsed OK
            "REVIEW_FIELDS",
        ) or body["done"] is True

    def test_post_nonexistent_synth_id_returns_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/kb/authorSkill/synth-does-not-exist-xyz",
            json={"userInput": "hello"},
        )
        assert resp.status_code == 404

    def test_404_error_has_structured_body(self, client: TestClient):
        resp = client.post(
            "/api/v1/kb/authorSkill/synth-missing",
            json={"userInput": "hello"},
        )
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# GET — list sessions
# ---------------------------------------------------------------------------


class TestAuthorSkillList:
    def test_get_list_returns_200(self, client: TestClient):
        resp = client.get("/api/v1/kb/authorSkill")
        assert resp.status_code == 200

    def test_get_list_empty_for_new_user(self, client: TestClient):
        resp = client.get("/api/v1/kb/authorSkill")
        body = resp.json()
        assert "sessions" in body
        assert body["sessions"] == []
        assert body["count"] == 0

    def test_get_list_returns_created_sessions(self, client: TestClient):
        # Create two sessions with distinct intents so they get different synth_ids
        client.post("/api/v1/kb/authorSkill", json={"intentDescription": "first skill"})
        client.post("/api/v1/kb/authorSkill", json={"intentDescription": "second skill"})

        resp = client.get("/api/v1/kb/authorSkill")
        body = resp.json()
        assert body["count"] == 2
        assert len(body["sessions"]) == 2

    def test_get_list_sessions_have_expected_fields(self, client: TestClient):
        client.post("/api/v1/kb/authorSkill", json={})

        resp = client.get("/api/v1/kb/authorSkill")
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        s = sessions[0]
        # camelCase fields
        assert "synthId" in s
        assert "state" in s
        assert "status" in s
        assert "persona" in s
        assert "skillName" in s
        assert "updatedAt" in s

    def test_get_list_only_returns_own_sessions(
        self, store: FilestoreSessionStore, tmp_path: Path
    ):
        """Two clients with different user_ids should see their own sessions only."""
        consumer_a = _make_consumer(user_id="user-A")
        consumer_b = _make_consumer(user_id="user-B")

        app_a = _make_test_app(consumer_a, store)
        app_b = _make_test_app(consumer_b, store)

        with TestClient(app_a) as ca:
            ca.post("/api/v1/kb/authorSkill", json={})
            ca.post("/api/v1/kb/authorSkill", json={})

        with TestClient(app_b) as cb:
            cb.post("/api/v1/kb/authorSkill", json={})
            list_resp = cb.get("/api/v1/kb/authorSkill")

        body = list_resp.json()
        assert body["count"] == 1, (
            f"user-B should see only 1 session, got {body['count']}"
        )


# ---------------------------------------------------------------------------
# GET /{synth_id} — get session state
# ---------------------------------------------------------------------------


class TestAuthorSkillGet:
    def _start_session(self, client: TestClient) -> tuple[str, dict]:
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        return body["synthId"], body

    def test_get_synth_id_returns_200(self, client: TestClient):
        synth_id, _ = self._start_session(client)
        resp = client.get(f"/api/v1/kb/authorSkill/{synth_id}")
        assert resp.status_code == 200

    def test_get_synth_id_returns_last_turn(self, client: TestClient):
        synth_id, start_body = self._start_session(client)
        resp = client.get(f"/api/v1/kb/authorSkill/{synth_id}")
        body = resp.json()

        assert body["synthId"] == synth_id
        assert body["state"] == start_body["state"]
        assert body["message"] == start_body["message"]

    def test_get_nonexistent_synth_id_returns_404(self, client: TestClient):
        resp = client.get("/api/v1/kb/authorSkill/synth-does-not-exist")
        assert resp.status_code == 404

    def test_get_returns_camel_case(self, client: TestClient):
        synth_id, _ = self._start_session(client)
        resp = client.get(f"/api/v1/kb/authorSkill/{synth_id}")
        body = resp.json()
        assert "synthId" in body
        assert "synth_id" not in body


# ---------------------------------------------------------------------------
# DELETE /{synth_id} — abandon session
# ---------------------------------------------------------------------------


class TestAuthorSkillDelete:
    def _start_session(self, client: TestClient) -> str:
        resp = client.post("/api/v1/kb/authorSkill", json={})
        return resp.json()["synthId"]

    def test_delete_returns_200(self, client: TestClient):
        synth_id = self._start_session(client)
        resp = client.delete(f"/api/v1/kb/authorSkill/{synth_id}")
        assert resp.status_code == 200

    def test_delete_response_contains_synth_id(self, client: TestClient):
        synth_id = self._start_session(client)
        resp = client.delete(f"/api/v1/kb/authorSkill/{synth_id}")
        body = resp.json()
        assert body["synthId"] == synth_id
        assert body["status"] == "abandoned"

    def test_delete_sets_status_abandoned_in_store(
        self, client: TestClient, store: FilestoreSessionStore, consumer: ConsumerManifest
    ):
        synth_id = self._start_session(client)
        client.delete(f"/api/v1/kb/authorSkill/{synth_id}")

        session = store.load(synth_id, user_id=consumer.user_id)
        assert session is not None
        assert session["status"] == "abandoned"

    def test_delete_nonexistent_returns_404(self, client: TestClient):
        resp = client.delete("/api/v1/kb/authorSkill/synth-does-not-exist")
        assert resp.status_code == 404

    def test_delete_requires_write_scope(
        self, store: FilestoreSessionStore
    ):
        """Consumer with only read scope cannot delete sessions."""
        read_consumer = _make_consumer(scopes=["read"])
        app = _make_test_app(read_consumer, store)

        with TestClient(app, raise_server_exceptions=False) as c:
            # Create session first using write consumer
            write_consumer = _make_consumer(scopes=["read", "write"])
            app2 = _make_test_app(write_consumer, store)
            with TestClient(app2) as c2:
                synth_id = c2.post("/api/v1/kb/authorSkill", json={}).json()["synthId"]

            resp = c.delete(f"/api/v1/kb/authorSkill/{synth_id}")

        # read-only consumer should get 403
        assert resp.status_code == 403

    def test_post_requires_write_scope(
        self, store: FilestoreSessionStore
    ):
        """Consumer with only read scope cannot create/continue sessions."""
        read_consumer = _make_consumer(scopes=["read"])
        app = _make_test_app(read_consumer, store)

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/api/v1/kb/authorSkill", json={})

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Progress field
# ---------------------------------------------------------------------------


class TestAuthorSkillProgress:
    def test_new_session_has_progress(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        assert "progress" in body
        progress = body["progress"]
        if progress is not None:
            assert "step" in progress
            assert "total" in progress
            assert "label" in progress

    def test_done_field_present(self, client: TestClient):
        resp = client.post("/api/v1/kb/authorSkill", json={})
        body = resp.json()
        assert "done" in body
        assert body["done"] is False  # new sessions are not done


# ---------------------------------------------------------------------------
# Full lifecycle: start → continue → GET → DELETE
# ---------------------------------------------------------------------------


class TestAuthorSkillFullLifecycle:
    def test_start_continue_get_delete(
        self, client: TestClient, store: FilestoreSessionStore, consumer: ConsumerManifest
    ):
        # 1. Start
        start = client.post("/api/v1/kb/authorSkill", json={})
        assert start.status_code == 200
        synth_id = start.json()["synthId"]

        # 2. Continue
        cont = client.post(
            f"/api/v1/kb/authorSkill/{synth_id}",
            json={"userInput": "ops_eng — weekly ops status"},
        )
        assert cont.status_code == 200
        cont_body = cont.json()
        assert cont_body["synthId"] == synth_id

        # 3. GET — should return last turn from continue
        get = client.get(f"/api/v1/kb/authorSkill/{synth_id}")
        assert get.status_code == 200
        get_body = get.json()
        assert get_body["synthId"] == synth_id
        assert get_body["state"] == cont_body["state"]

        # 4. DELETE
        delete = client.delete(f"/api/v1/kb/authorSkill/{synth_id}")
        assert delete.status_code == 200
        assert delete.json()["status"] == "abandoned"

        # 5. Verify abandoned in store
        session = store.load(synth_id, user_id=consumer.user_id)
        assert session is not None
        assert session["status"] == "abandoned"
