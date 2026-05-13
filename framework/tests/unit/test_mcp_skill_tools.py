"""Unit tests for listSkills, getSkill, and deleteSkill MCP tool handlers.

Coverage:
  listSkills
    - returns all skills when no filters given
    - filters by persona
    - filters by status (client-side)
    - returns empty list gracefully
    - returns isError when skill_store absent
    - list_skills exception returns isError

  getSkill
    - returns full detail with kbCard parsed from persona_builder_delta
    - evalCounts reflect JSONL line counts
    - includeArtifacts=False omits workflowYaml key
    - includeArtifacts=True returns workflowYaml (write scope)
    - includeArtifacts=True rejected without write scope
    - skill not found returns isError
    - missing persona or skillName returns isError
    - skill_store absent returns isError

  deleteSkill (persona_builder cleanup)
    - delete_persona_builder_kb called after artifacts deleted
    - pbEntryDeleted=True in response when KB entry existed
    - pbEntryDeleted=False in response when KB entry was already absent
    - shim_kb.reload() called after successful delete
    - shim_kb.reload() failure does not crash the handler
    - delete_persona_builder_kb failure does not crash (artifacts still deleted)
    - persona_builder_delta absent → empty kbCard
    - malformed persona_builder_delta YAML → empty kbCard (no crash)
    - list_skills exception returns isError
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import yaml

from framework.deploy.mcp_tools import build_external_tool_registry
from framework.deploy.auth.consumer import ConsumerManifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app(skill_store):
    app = MagicMock()
    app.state.skill_store = skill_store
    app.state.session_store = None
    app.state.llm = None
    app.state.artifact_store = None
    app.state.error_store = None
    app.state.adb_pool = None
    app.state.bug_pool = None
    app.state.context_builder = None
    app.state.shim_kb = None
    app.state.kbf_ops_loader = None
    return app


def _consumer(scopes=("read", "write")):
    return ConsumerManifest(
        name="test-consumer",
        token_hash="x",
        scopes=list(scopes),
        persona_allowlist=[],
        rpm_cap=60,
        token_budget_per_request=8000,
        user_id="test-user",
    )


def _make_skill_row(persona="tpm", skill_name="weekly_ops", status="production", count=4):
    return {
        "persona": persona,
        "skill_name": skill_name,
        "status": status,
        "artifact_count": count,
        "updated_at": "2026-05-12T10:00:00+00:00",
    }


def _delta_yaml(kb_name="weekly_ops"):
    return yaml.safe_dump({
        "name": kb_name,
        "kind": "vector",
        "provides_fields": ["project_name", "overall_rag"],
        "sources": [{"kind": "confluence", "space": "PROJX"}],
        "retrieval_tools": ["vector_search"],
        "kb_card": {
            "summary": "Weekly ops summary.",
            "use_when": "Questions about weekly ops.",
        },
    })


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# listSkills tests
# ---------------------------------------------------------------------------

class TestListSkills:
    def _handler(self, skill_store):
        registry = build_external_tool_registry(_app(skill_store))
        return registry["listSkills"]

    def test_returns_all_skills(self):
        store = MagicMock()
        store.list_skills.return_value = [
            _make_skill_row("tpm", "weekly_ops"),
            _make_skill_row("pm", "briefs", "draft"),
        ]
        result = _run(self._handler(store)(_consumer=_consumer()))
        assert result["total"] == 2
        names = [s["skillName"] for s in result["skills"]]
        assert "weekly_ops" in names
        assert "briefs" in names

    def test_filters_by_persona(self):
        store = MagicMock()
        store.list_skills.return_value = [_make_skill_row("tpm", "weekly_ops")]
        result = _run(self._handler(store)(persona="tpm", _consumer=_consumer()))
        store.list_skills.assert_called_once_with(persona="tpm")
        assert result["total"] == 1
        assert result["skills"][0]["persona"] == "tpm"

    def test_filters_by_status_client_side(self):
        store = MagicMock()
        store.list_skills.return_value = [
            _make_skill_row("tpm", "weekly_ops", "production"),
            _make_skill_row("tpm", "old_skill", "draft"),
        ]
        result = _run(self._handler(store)(status="production", _consumer=_consumer()))
        assert result["total"] == 1
        assert result["skills"][0]["skillName"] == "weekly_ops"

    def test_empty_list_ok(self):
        store = MagicMock()
        store.list_skills.return_value = []
        result = _run(self._handler(store)(_consumer=_consumer()))
        assert result == {"skills": [], "total": 0}

    def test_camel_case_response_keys(self):
        store = MagicMock()
        store.list_skills.return_value = [_make_skill_row()]
        result = _run(self._handler(store)(_consumer=_consumer()))
        skill = result["skills"][0]
        assert "skillName" in skill
        assert "artifactCount" in skill
        assert "updatedAt" in skill
        assert "skill_name" not in skill

    def test_no_skill_store_returns_error(self):
        app = _app(None)
        app.state.skill_store = None
        registry = build_external_tool_registry(app)
        result = _run(registry["listSkills"](_consumer=_consumer()))
        assert result.get("isError") is True

    def test_list_skills_exception_returns_error(self):
        store = MagicMock()
        store.list_skills.side_effect = RuntimeError("DB down")
        result = _run(self._handler(store)(_consumer=_consumer()))
        assert result.get("isError") is True
        assert "DB down" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# getSkill tests
# ---------------------------------------------------------------------------

class TestGetSkill:
    def _handler(self, skill_store):
        registry = build_external_tool_registry(_app(skill_store))
        return registry["getSkill"]

    def _store_with_skill(self, delta=None, workflow=None, eval_ext=None, eval_wf=None):
        store = MagicMock()
        store.list_skills.return_value = [_make_skill_row()]

        def _read(persona, skill_name, artifact_type):
            return {
                "persona_builder_delta": delta if delta is not None else _delta_yaml(),
                "workflow_skill":        workflow or "workflow: yaml content",
                "eval_extraction":       eval_ext or "line1\nline2\n",
                "eval_workflow":         eval_wf or "line1\n",
            }.get(artifact_type)

        store.read_artifact.side_effect = _read
        return store

    def test_returns_full_detail(self):
        store = self._store_with_skill()
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert result["persona"] == "tpm"
        assert result["skillName"] == "weekly_ops"
        assert result["status"] == "production"
        assert result["artifactCount"] == 4

    def test_kb_card_parsed_from_delta(self):
        store = self._store_with_skill()
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        kb = result["kbCard"]
        assert kb["summary"] == "Weekly ops summary."
        assert kb["useWhen"] == "Questions about weekly ops."
        assert "project_name" in kb["providesFields"]
        assert kb["kind"] == "vector"
        assert kb["sources"] == [{"kind": "confluence", "space": "PROJX"}]
        assert "vector_search" in kb["retrievalTools"]

    def test_eval_counts_from_jsonl_lines(self):
        store = self._store_with_skill(
            eval_ext="ex1\nex2\nex3\n",
            eval_wf="wf1\nwf2\n",
        )
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert result["evalCounts"]["extraction"] == 3
        assert result["evalCounts"]["workflow"] == 2

    def test_no_artifacts_by_default(self):
        store = self._store_with_skill()
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert "workflowYaml" not in result

    def test_include_artifacts_returns_workflow_yaml(self):
        store = self._store_with_skill(workflow="steps:\n  - run_query\n")
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops",
            includeArtifacts=True, _consumer=_consumer(["read", "write"])
        ))
        assert result["workflowYaml"] == "steps:\n  - run_query\n"

    def test_include_artifacts_requires_write_scope(self):
        store = self._store_with_skill()
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops",
            includeArtifacts=True, _consumer=_consumer(["read"])
        ))
        assert result.get("isError") is True
        assert "write scope" in result["content"][0]["text"]

    def test_skill_not_found_returns_error(self):
        store = MagicMock()
        store.list_skills.return_value = []
        result = _run(self._handler(store)(
            persona="tpm", skillName="nonexistent", _consumer=_consumer()
        ))
        assert result.get("isError") is True
        assert "not found" in result["content"][0]["text"]

    def test_missing_persona_returns_error(self):
        store = self._store_with_skill()
        result = _run(self._handler(store)(
            persona="", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert result.get("isError") is True

    def test_missing_skill_name_returns_error(self):
        store = self._store_with_skill()
        result = _run(self._handler(store)(
            persona="tpm", skillName="", _consumer=_consumer()
        ))
        assert result.get("isError") is True

    def test_no_skill_store_returns_error(self):
        app = _app(None)
        app.state.skill_store = None
        registry = build_external_tool_registry(app)
        result = _run(registry["getSkill"](
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert result.get("isError") is True

    def test_missing_delta_yields_empty_kb_card(self):
        store = self._store_with_skill(delta=None)

        # Override so delta returns None
        def _read(persona, skill_name, artifact_type):
            if artifact_type == "persona_builder_delta":
                return None
            return "content"
        store.read_artifact.side_effect = _read

        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert result["kbCard"] == {}

    def test_malformed_delta_yields_empty_kb_card(self):
        store = self._store_with_skill(delta="{bad yaml{{{{")
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        # Must not crash; kbCard defaults to empty dict
        assert result.get("isError") is not True
        assert result["kbCard"] == {}

    def test_list_skills_exception_returns_error(self):
        store = MagicMock()
        store.list_skills.side_effect = RuntimeError("timeout")
        result = _run(self._handler(store)(
            persona="tpm", skillName="weekly_ops", _consumer=_consumer()
        ))
        assert result.get("isError") is True
        assert "timeout" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# deleteSkill — persona_builder cleanup tests
# ---------------------------------------------------------------------------

class TestDeleteSkillPersonaBuilderCleanup:
    """deleteSkill must also clean up KBF_PERSONA_BUILDERS and reload ShimKb."""

    def _handler(self, skill_store, shim_kb=None):
        app = _app(skill_store)
        app.state.shim_kb = shim_kb
        registry = build_external_tool_registry(app)
        return registry["deleteSkill"]

    def _admin_consumer(self):
        return _consumer(scopes=["read", "write", "admin"])

    def _store_with_skill(self, pb_deleted=True):
        store = MagicMock()
        store.delete.return_value = ["workflow_skill", "persona_builder_delta"]
        store.delete_persona_builder_kb.return_value = pb_deleted
        return store

    def test_delete_persona_builder_kb_called(self):
        """delete_persona_builder_kb must be called with the right persona + skill_name."""
        import os
        store = self._store_with_skill()
        handler = self._handler(store)
        with _patched_delete_pw("kbf-delete-dev"):
            _run(handler(
                persona="tpm", skillName="weekly_ops",
                confirmationPassword="kbf-delete-dev",
                _consumer=self._admin_consumer(),
            ))
        store.delete_persona_builder_kb.assert_called_once_with("tpm", "weekly_ops")

    def test_pb_entry_deleted_true_in_response(self):
        """pbEntryDeleted=True when the KB entry existed and was removed."""
        store = self._store_with_skill(pb_deleted=True)
        with _patched_delete_pw("pw"):
            result = _run(self._handler(store)(
                persona="tpm", skillName="weekly_ops",
                confirmationPassword="pw", _consumer=self._admin_consumer(),
            ))
        assert result["pbEntryDeleted"] is True
        assert "removed" in result["content"][0]["text"]

    def test_pb_entry_deleted_false_when_not_found(self):
        """pbEntryDeleted=False when KB entry was already absent (no error)."""
        store = self._store_with_skill(pb_deleted=False)
        with _patched_delete_pw("pw"):
            result = _run(self._handler(store)(
                persona="tpm", skillName="weekly_ops",
                confirmationPassword="pw", _consumer=self._admin_consumer(),
            ))
        assert result["pbEntryDeleted"] is False
        assert result.get("isError") is not True

    def test_shim_kb_reload_called(self):
        """shim_kb.reload() must be called after a successful delete."""
        store = self._store_with_skill()
        shim_kb = MagicMock()
        with _patched_delete_pw("pw"):
            _run(self._handler(store, shim_kb=shim_kb)(
                persona="tpm", skillName="weekly_ops",
                confirmationPassword="pw", _consumer=self._admin_consumer(),
            ))
        shim_kb.reload.assert_called_once()

    def test_shim_kb_reload_failure_does_not_crash(self):
        """shim_kb.reload() raising must not propagate — delete still succeeds."""
        store = self._store_with_skill()
        shim_kb = MagicMock()
        shim_kb.reload.side_effect = RuntimeError("reload failed")
        with _patched_delete_pw("pw"):
            result = _run(self._handler(store, shim_kb=shim_kb)(
                persona="tpm", skillName="weekly_ops",
                confirmationPassword="pw", _consumer=self._admin_consumer(),
            ))
        assert result.get("isError") is not True
        assert result["status"] == "deleted"

    def test_delete_persona_builder_kb_failure_does_not_crash(self):
        """delete_persona_builder_kb raising must not cancel the overall delete."""
        store = self._store_with_skill()
        store.delete_persona_builder_kb.side_effect = RuntimeError("ADB timeout")
        with _patched_delete_pw("pw"):
            result = _run(self._handler(store)(
                persona="tpm", skillName="weekly_ops",
                confirmationPassword="pw", _consumer=self._admin_consumer(),
            ))
        # Artifacts were still deleted
        assert result.get("isError") is not True
        assert result["status"] == "deleted"


# ---------------------------------------------------------------------------
# Helpers for deleteSkill tests
# ---------------------------------------------------------------------------

from contextlib import contextmanager
from unittest.mock import patch

@contextmanager
def _patched_delete_pw(password: str):
    """Patch KBF_SKILL_DELETE_PASSWORD env var for the duration of the block."""
    with patch.dict("os.environ", {"KBF_SKILL_DELETE_PASSWORD": password}):
        yield
