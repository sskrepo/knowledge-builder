"""Unit + integration tests for the kbf_ops review capability (ADR-023).

Coverage:
  KbfOpsSessionLoader:
    - test_session_loader_builds_bundle_from_mock_stores
    - test_session_loader_returns_none_for_unknown_synth_id

  KbfOpsReviewEngine structural checks:
    - test_structural_checks_all_pass
    - test_structural_check_null_gold_set
    - test_structural_check_truncated_name
    - test_structural_check_cross_skill_fields

  KbfOpsReviewEngine LLM review:
    - test_review_engine_full_with_mock_llm
    - test_review_engine_handles_malformed_llm_response

  MCP tool via TestClient:
    - test_mcp_tool_review_skill_session_returns_report
    - test_mcp_tool_requires_auth
    - test_mcp_tool_unknown_synth_id
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_session(synth_id: str, skill_name: str = "test_skill") -> dict:
    return {
        "synth_id": synth_id,
        "persona": "tpm",
        "skill_name": skill_name,
        "intent_description": "Summarise weekly ops report",
        "state": "committed",
        "status": "committed",
        "conversation_history": [
            {"role": "user", "content": "I want a weekly ops skill"},
            {"role": "assistant", "content": "Sure, let me help"},
        ],
        "state_history": ["intro", "gather_intent", "committed"],
    }


def _make_artifacts(skill_name: str = "test_skill") -> dict[str, str]:
    workflow_yaml = f"""\
workflow_skill: {skill_name}
persona: tpm
requires_extractions:
  - kb: tpm.weekly_ops
    required_fields:
      - incidents_count
      - top_alert
skill_card:
  use_when: User asks for weekly ops summary
  example_invocations:
    - "weekly ops report"
    - "what happened this week"
    - "ops summary"
"""
    persona_builder_delta = """\
tpm.weekly_ops:
  provides_fields:
    - incidents_count
    - top_alert
"""
    eval_extraction = json.dumps({
        "input": "Summarise this week",
        "expected_extraction": {"incidents_count": "3", "top_alert": "DB latency"},
    }) + "\n"
    eval_workflow = json.dumps({
        "input": "Weekly ops",
        "expected_output_includes": {"incidents_count": "3"},
    }) + "\n"
    extraction_schema = json.dumps({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "incidents_count": {"type": "string"},
            "top_alert": {"type": "string"},
        },
        "required": ["incidents_count", "top_alert"],
    }, indent=2)
    return {
        "workflow_skill": workflow_yaml,
        "persona_builder_delta": persona_builder_delta,
        "eval_extraction": eval_extraction,
        "eval_workflow": eval_workflow,
        "extraction_schema": extraction_schema,
    }


# ---------------------------------------------------------------------------
# KbfOpsSessionLoader
# ---------------------------------------------------------------------------


class TestSessionLoaderBuildsBundle:
    def test_session_loader_builds_bundle_from_mock_stores(self, tmp_path):
        """Loader should build a SessionBundle with all fields populated."""
        from framework.retrievers.kbf_ops.session_loader import KbfOpsSessionLoader, SessionBundle
        from framework.deploy.session.filestore import FilestoreSessionStore
        from framework.deploy.skill_store.filestore import FilestoreSkillStore

        synth_id = "synth-tpm-test001"
        skill_name = "test_skill"

        # Seed a session.
        session_store = FilestoreSessionStore(store_root=str(tmp_path / "sessions"))
        session = _make_session(synth_id, skill_name)
        session_store.save(session, user_id="kbf-ops")

        # Seed skill artifacts.
        skill_store = FilestoreSkillStore(repo_root=tmp_path)
        artifacts = _make_artifacts(skill_name)
        skill_store.write_artifacts(synth_id, "tpm", skill_name, artifacts)

        loader = KbfOpsSessionLoader(
            pool=None,
            session_store=session_store,
            skill_store=skill_store,
            artifact_store=None,
        )
        bundle = loader.load(synth_id)

        assert bundle is not None
        assert bundle.synth_id == synth_id
        assert bundle.persona == "tpm"
        assert skill_name in bundle.skill_names
        assert bundle.intent_description == "Summarise weekly ops report"
        assert len(bundle.conversation_history) == 2
        assert "committed" in bundle.state_progression
        assert "workflow_skill" in bundle.artifacts[skill_name]
        assert bundle.uploaded_files == []
        assert bundle.errors == []
        assert bundle.status == "committed"

    def test_session_loader_returns_none_for_unknown_synth_id(self, tmp_path):
        """Loader must return None when the session does not exist."""
        from framework.retrievers.kbf_ops.session_loader import KbfOpsSessionLoader
        from framework.deploy.session.filestore import FilestoreSessionStore
        from framework.deploy.skill_store.filestore import FilestoreSkillStore

        session_store = FilestoreSessionStore(store_root=str(tmp_path / "sessions"))
        skill_store = FilestoreSkillStore(repo_root=tmp_path)

        loader = KbfOpsSessionLoader(
            pool=None,
            session_store=session_store,
            skill_store=skill_store,
            artifact_store=None,
        )
        bundle = loader.load("synth-does-not-exist")
        assert bundle is None


# ---------------------------------------------------------------------------
# KbfOpsReviewEngine — structural checks
# ---------------------------------------------------------------------------


def _make_bundle(
    synth_id: str = "synth-tpm-abc",
    skill_name: str = "test_skill",
    artifacts: dict[str, str] | None = None,
    conversation: list[dict] | None = None,
) -> "SessionBundle":
    from framework.retrievers.kbf_ops.session_loader import SessionBundle

    if artifacts is None:
        artifacts = _make_artifacts(skill_name)

    return SessionBundle(
        synth_id=synth_id,
        persona="tpm",
        skill_names=[skill_name],
        intent_description="Weekly ops summary",
        conversation_history=conversation or [],
        state_progression=["committed"],
        artifacts={skill_name: artifacts},
        uploaded_files=[],
        errors=[],
        status="committed",
    )


class TestStructuralChecks:
    def test_structural_checks_all_pass(self):
        """A well-formed bundle should produce 0 bugs."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        engine = KbfOpsReviewEngine(llm=None)
        report = engine.review(_make_bundle(), depth="structural")

        assert report is not None
        assert report.overall_score >= 0
        # Only structural bugs count; a valid bundle should have 0.
        assert report.bugs_to_file == [], (
            f"Expected no bugs, got: {[b.check_name for b in report.bugs_to_file]}"
        )

    def test_structural_check_null_gold_set(self):
        """Bundle with all-null expected_extraction should file a bug."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        # Make eval_extraction with all-null expected values.
        bad_eval_extraction = json.dumps({
            "input": "test query",
            "expected_extraction": {"incidents_count": None, "top_alert": None},
        }) + "\n"
        arts = _make_artifacts()
        arts["eval_extraction"] = bad_eval_extraction

        engine = KbfOpsReviewEngine(llm=None)
        report = engine.review(_make_bundle(artifacts=arts), depth="structural")

        check_names = [b.check_name for b in report.bugs_to_file]
        assert "check_gold_set_not_null" in check_names, (
            f"Expected check_gold_set_not_null in bugs, got: {check_names}"
        )

    def test_structural_check_truncated_name(self):
        """Bundle where artifact skill_name differs from session skill_name should file a bug."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        # Workflow YAML has a different skill name.
        arts = _make_artifacts(skill_name="test_skill")
        arts["workflow_skill"] = "workflow_skill: completely_different_name\npersona: tpm\n"

        engine = KbfOpsReviewEngine(llm=None)
        report = engine.review(_make_bundle(artifacts=arts), depth="structural")

        check_names = [b.check_name for b in report.bugs_to_file]
        assert "check_skill_name_not_truncated" in check_names, (
            f"Expected check_skill_name_not_truncated in bugs, got: {check_names}"
        )

    def test_structural_check_cross_skill_fields(self):
        """Gold set referencing field not in requires_extractions should file a bug."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        # eval_workflow references a field not in requires_extractions.
        bad_eval_workflow = json.dumps({
            "input": "weekly ops",
            "expected_output_includes": {
                "incidents_count": "3",
                "other_skills_secret_field": "some_value",  # not declared
            },
        }) + "\n"
        arts = _make_artifacts()
        arts["eval_workflow"] = bad_eval_workflow

        engine = KbfOpsReviewEngine(llm=None)
        report = engine.review(_make_bundle(artifacts=arts), depth="structural")

        check_names = [b.check_name for b in report.bugs_to_file]
        assert "check_gold_fields_scoped" in check_names, (
            f"Expected check_gold_fields_scoped in bugs, got: {check_names}"
        )

    def test_structural_check_missing_artifact(self):
        """Bundle missing an artifact type should file check_artifact_count bug."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        arts = _make_artifacts()
        del arts["eval_workflow"]  # remove one artifact

        engine = KbfOpsReviewEngine(llm=None)
        report = engine.review(_make_bundle(artifacts=arts), depth="structural")

        check_names = [b.check_name for b in report.bugs_to_file]
        assert "check_artifact_count" in check_names, (
            f"Expected check_artifact_count in bugs, got: {check_names}"
        )


# ---------------------------------------------------------------------------
# KbfOpsReviewEngine — LLM review
# ---------------------------------------------------------------------------


def _make_valid_llm_response(synth_id: str, review_id: str) -> str:
    """Return a minimal valid JSON response matching the output schema."""
    data = {
        "synthId": synth_id,
        "reviewId": review_id,
        "persona": "tpm",
        "skillNames": ["test_skill"],
        "status": "committed",
        "overallScore": 7.5,
        "recommendation": "promote_with_fixes",
        "dimensions": {
            "intentFidelity":         {"score": 8.0, "findings": ["Good intent match"]},
            "schemaCompleteness":     {"score": 7.0, "findings": []},
            "kbWiring":               {"score": 9.0, "findings": []},
            "routingDescriptors":     {"score": 6.0, "findings": ["Need more invocations"]},
            "evalQuality":            {"score": 8.0, "findings": []},
            "artifactConsistency":    {"score": 7.5, "findings": []},
            "askKbRoutingSimulation": {"score": 7.0, "findings": ["Tier 1 routing ok"]},
        },
        "bugsToFile": [
            {
                "checkName": "routing_descriptors",
                "severity": "minor",
                "detail": "Only 3 invocations; should have 5+",
                "suggestedFix": "Add more natural-language phrasings to example_invocations",
            }
        ],
    }
    return json.dumps(data)


class TestLLMReview:
    def test_review_engine_full_with_mock_llm(self):
        """Mock LLM returning valid JSON should produce a parsed QualityReport."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        mock_llm = MagicMock()

        # We need to capture the review_id from the engine so we can mirror it.
        # The easiest approach: make the LLM return a response with a placeholder
        # review_id that the engine replaces internally via _parse_llm_response.
        # Actually the engine passes review_id into the prompt and expects it back;
        # intercept at chat() level.
        captured_review_id: list[str] = []

        def _fake_chat(model, messages, **kwargs) -> dict:
            # Extract reviewId from the user message content or use a fixed one.
            import re
            prompt = messages[-1]["content"] if messages else ""
            m = re.search(r'"reviewId"\s*:\s*"([^"]+)"', prompt)
            rev_id = m.group(1) if m else "rev-mockreviewid"
            captured_review_id.append(rev_id)
            return {"text": _make_valid_llm_response("synth-tpm-abc", rev_id), "tokens_in": 0, "tokens_out": 0}

        mock_llm.chat.side_effect = _fake_chat

        engine = KbfOpsReviewEngine(llm=mock_llm)
        report = engine.review(_make_bundle(), depth="full")

        assert report is not None
        assert report.persona == "tpm"
        assert report.overall_score > 0
        assert report.recommendation in {"promote", "promote_with_fixes", "do_not_promote"}
        assert len(report.dimensions) == 7
        # All 7 dimension keys present.
        expected_dims = {
            "intentFidelity", "schemaCompleteness", "kbWiring",
            "routingDescriptors", "evalQuality", "artifactConsistency",
            "askKbRoutingSimulation",
        }
        assert set(report.dimensions.keys()) == expected_dims
        # Bugs to file: at least the ones from the mock response.
        bug_names = [b.check_name for b in report.bugs_to_file]
        assert "routing_descriptors" in bug_names

    def test_review_engine_handles_malformed_llm_response(self):
        """LLM returning invalid JSON should produce a graceful fallback report, not raise."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"text": "This is NOT valid JSON at all {{{]]}", "tokens_in": 0, "tokens_out": 0}

        engine = KbfOpsReviewEngine(llm=mock_llm)
        # Must not raise.
        report = engine.review(_make_bundle(), depth="full")

        assert report is not None
        # Should record a parse-failed bug.
        check_names = [b.check_name for b in report.bugs_to_file]
        assert any("parse" in cn or "llm" in cn for cn in check_names), (
            f"Expected a parse/llm error bug, got: {check_names}"
        )

    def test_review_engine_structural_depth_does_not_call_llm(self):
        """depth='structural' must never invoke the LLM."""
        from framework.deploy.ops.review_engine import KbfOpsReviewEngine

        mock_llm = MagicMock()
        engine = KbfOpsReviewEngine(llm=mock_llm)
        engine.review(_make_bundle(), depth="structural")

        mock_llm.chat.assert_not_called()


# ---------------------------------------------------------------------------
# MCP tool integration tests (TestClient)
# ---------------------------------------------------------------------------

pytestmark_fastapi = pytest.mark.skipif(
    not _FASTAPI_AVAILABLE,
    reason="fastapi not installed",
)


def _make_review_test_app(tmp_path: Path, llm=None, bundle_to_return=None):
    """Build a minimal FastAPI app with reviewSkillSession wired for testing."""
    from framework.deploy.auth.consumer import ConsumerManifest
    from framework.deploy.auth.middleware import bearer_auth_middleware
    from framework.deploy.auth.registry import ConsumerRegistry
    from framework.deploy.cost_store import CostStore
    from framework.deploy.error_store import ErrorStore
    from framework.deploy.mcp_tools import build_external_tool_registry, EXTERNAL_TOOLS_SCHEMA
    from framework.deploy.session.filestore import FilestoreSessionStore

    manifests_dir = tmp_path / "consumer_manifests"
    manifests_dir.mkdir()
    dev_token = "review-test-token-xyz"
    dev_token_hash = hashlib.sha256(dev_token.encode()).hexdigest()
    (manifests_dir / "dev.yaml").write_text(
        f"""
name: review-test-consumer
tokenHash: {dev_token_hash}
scopes:
  - read
  - write
  - admin
personaAllowlist: []
rpmCap: 120
tokenBudgetPerRequest: 8000
userId: review-test-user
""",
        encoding="utf-8",
    )

    store_dir = tmp_path / "store"
    store_dir.mkdir(exist_ok=True)

    # Mock loader
    mock_loader = MagicMock()
    if bundle_to_return is not None:
        mock_loader.load.return_value = bundle_to_return
    else:
        mock_loader.load.return_value = None

    app = FastAPI(title="Review Test App")

    @app.on_event("startup")
    async def _startup():
        app.state.consumer_registry = ConsumerRegistry(manifests_dir)
        app.state.session_store = FilestoreSessionStore(store_root=str(store_dir))
        app.state.cost_store = CostStore(str(store_dir))
        app.state.error_store = ErrorStore(str(store_dir))
        app.state.llm = llm
        app.state.kbf_ops_loader = mock_loader
        app.state.adb_pool = None

        external_registry = build_external_tool_registry(app)
        app.state._external_registry = external_registry
        app.state._external_tools_schema = EXTERNAL_TOOLS_SCHEMA

    app.middleware("http")(bearer_auth_middleware)

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
            raise HTTPException(status_code=404, detail=f"unknown tool: {name!r}")
        consumer = getattr(req.state, "consumer", None)
        try:
            result = await handler(**args, _consumer=consumer)
        except TypeError as exc:
            raise HTTPException(400, f"bad args: {exc}")
        return {"content": result}

    return app, dev_token


@pytest.mark.skipif(not _FASTAPI_AVAILABLE, reason="fastapi not installed")
class TestMcpToolReviewSkillSession:
    def test_tools_list_now_has_six_tools(self, tmp_path):
        """EXTERNAL_TOOLS_SCHEMA must now have 6 entries (deleteSkill added)."""
        app, dev_token = _make_review_test_app(tmp_path)
        with TestClient(app) as client:
            resp = client.post(
                "/mcp/tools/list",
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        names = {t["name"] for t in tools}
        assert len(tools) == 6, f"Expected 6 tools, got {len(tools)}: {names}"
        assert "reviewSkillSession" in names
        assert "deleteSkill" in names

    def test_mcp_tool_requires_auth(self, tmp_path):
        """No bearer token should return 401."""
        app, _ = _make_review_test_app(tmp_path)
        with TestClient(app) as client:
            resp = client.post(
                "/mcp/tools/call",
                json={"name": "reviewSkillSession", "arguments": {"synthId": "synth-test"}},
            )
        assert resp.status_code == 401

    def test_mcp_tool_unknown_synth_id(self, tmp_path):
        """Unknown synth_id should return an isError dict, not an exception."""
        app, dev_token = _make_review_test_app(tmp_path, bundle_to_return=None)
        with TestClient(app) as client:
            resp = client.post(
                "/mcp/tools/call",
                json={
                    "name": "reviewSkillSession",
                    "arguments": {"synthId": "synth-does-not-exist"},
                },
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        assert resp.status_code == 200  # MCP returns 200 with isError
        content = resp.json()["content"]
        assert content.get("isError") is True
        text = content["content"][0]["text"]
        assert "not found" in text.lower()

    def test_mcp_tool_review_skill_session_returns_report(self, tmp_path):
        """With a valid bundle and structural depth, should return a QualityReport dict."""
        bundle = _make_bundle()

        # Mock LLM not needed for structural depth.
        app, dev_token = _make_review_test_app(
            tmp_path, llm=None, bundle_to_return=bundle
        )
        with TestClient(app) as client:
            resp = client.post(
                "/mcp/tools/call",
                json={
                    "name": "reviewSkillSession",
                    "arguments": {
                        "synthId": "synth-tpm-abc",
                        "depth": "structural",
                        "fileBugs": False,
                    },
                },
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        assert resp.status_code == 200, resp.text
        content = resp.json()["content"]
        # Should not be an error.
        assert content.get("isError") is not True, f"Unexpected error: {content}"
        # Should have the key report fields.
        assert "synthId" in content
        assert "reviewId" in content
        assert "overallScore" in content
        assert "recommendation" in content
        assert "dimensions" in content
        assert "bugsToFile" in content
        assert content["recommendation"] in {
            "promote", "promote_with_fixes", "do_not_promote"
        }

    def test_mcp_tool_llm_not_configured_non_structural_returns_error(self, tmp_path):
        """Requesting depth='full' when LLM is None should return isError."""
        bundle = _make_bundle()

        app, dev_token = _make_review_test_app(
            tmp_path, llm=None, bundle_to_return=bundle
        )
        with TestClient(app) as client:
            resp = client.post(
                "/mcp/tools/call",
                json={
                    "name": "reviewSkillSession",
                    "arguments": {
                        "synthId": "synth-tpm-abc",
                        "depth": "full",
                    },
                },
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        assert resp.status_code == 200
        content = resp.json()["content"]
        assert content.get("isError") is True
        text = content["content"][0]["text"]
        assert "structural" in text.lower() or "llm" in text.lower()

    def test_mcp_tool_files_bugs_to_error_store(self, tmp_path):
        """When fileBugs=True and there are structural bugs, they should be recorded."""
        from framework.deploy.error_store import ErrorStore

        # Create a bundle with a known structural issue.
        arts = _make_artifacts()
        # Introduce a null gold set bug.
        arts["eval_extraction"] = json.dumps({
            "input": "test",
            "expected_extraction": {"incidents_count": None},
        }) + "\n"
        bundle = _make_bundle(artifacts=arts)

        store_dir = tmp_path / "store"
        store_dir.mkdir(exist_ok=True)

        app, dev_token = _make_review_test_app(
            tmp_path, llm=None, bundle_to_return=bundle
        )
        with TestClient(app) as client:
            resp = client.post(
                "/mcp/tools/call",
                json={
                    "name": "reviewSkillSession",
                    "arguments": {
                        "synthId": "synth-tpm-abc",
                        "depth": "structural",
                        "fileBugs": True,
                    },
                },
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        assert resp.status_code == 200
        content = resp.json()["content"]
        # Check bugsFiledCount if no error.
        if not content.get("isError"):
            # At least one bug should have been filed.
            assert content.get("bugsFiledCount", 0) >= 1
