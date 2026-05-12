"""Unit tests for framework/deploy/skill_store/.

Coverage:
  FilestoreSkillStore:
    - write_artifacts creates correct files under REPO_ROOT-relative paths
    - read_artifact returns content / None when missing
    - promote is a no-op (does not raise)
    - list_skills returns entries for existing workflow_skills

  AdbSkillStore (mock cursor):
    - write_artifacts issues correct MERGE SQL with right bind values
    - read_artifact issues SELECT and returns CLOB content
    - read_artifact returns None when no row found
    - promote issues UPDATE with correct bind values
    - All operations are safe no-ops when pool=None
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from framework.deploy.skill_store._base import ARTIFACT_TYPES, make_artifact_id
from framework.deploy.skill_store.filestore import FilestoreSkillStore
from framework.deploy.skill_store.adb import AdbSkillStore
from framework.deploy.skill_store.factory import build_skill_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_repo(tmp_path):
    """A minimal repo-like directory tree under tmp_path."""
    (tmp_path / "framework" / "workflow_skills").mkdir(parents=True)
    (tmp_path / "framework" / "persona_builders").mkdir(parents=True)
    (tmp_path / "eval" / "gold_sets").mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def filestore(tmp_repo):
    return FilestoreSkillStore(repo_root=tmp_repo)


# ---------------------------------------------------------------------------
# FilestoreSkillStore
# ---------------------------------------------------------------------------


class TestFilestoreSkillStoreWrite:
    def test_write_workflow_skill_creates_yaml_file(self, filestore, tmp_repo):
        content = "skill: weekly_status\n"
        filestore.write_artifacts(
            synth_id="synth-001",
            persona="tpm",
            skill_name="weekly_status",
            artifacts={"workflow_skill": content},
        )
        dest = tmp_repo / "framework" / "workflow_skills" / "tpm" / "weekly_status.yaml"
        assert dest.exists()
        assert dest.read_text() == content

    def test_write_all_four_artifact_types(self, filestore, tmp_repo):
        artifacts = {
            "workflow_skill":        "skill yaml content",
            "persona_builder_delta": "pb delta content",
            "eval_extraction":       "extraction jsonl",
            "eval_workflow":         "workflow jsonl",
        }
        filestore.write_artifacts(
            synth_id="synth-002",
            persona="ops_eng",
            skill_name="incident_summary",
            artifacts=artifacts,
        )
        expected_paths = [
            tmp_repo / "framework" / "workflow_skills" / "ops_eng" / "incident_summary.yaml",
            tmp_repo / "framework" / "persona_builders" / "ops_eng.yaml.new_kb",
            tmp_repo / "eval" / "gold_sets" / "ops_eng-incident_summary-extraction.jsonl",
            tmp_repo / "eval" / "gold_sets" / "ops_eng-incident_summary-workflow.jsonl",
        ]
        for p in expected_paths:
            assert p.exists(), f"Expected {p} to exist"

    def test_write_unknown_artifact_type_raises(self, filestore):
        with pytest.raises(ValueError, match="Unknown artifact_type"):
            filestore.write_artifacts(
                synth_id="s",
                persona="tpm",
                skill_name="x",
                artifacts={"bad_type": "content"},
            )


class TestFilestoreSkillStoreRead:
    def test_read_returns_content_after_write(self, filestore, tmp_repo):
        content = "---\nskill: test\n"
        filestore.write_artifacts(
            synth_id="s",
            persona="pm",
            skill_name="status_report",
            artifacts={"workflow_skill": content},
        )
        result = filestore.read_artifact("pm", "status_report", "workflow_skill")
        assert result == content

    def test_read_returns_none_when_file_missing(self, filestore):
        result = filestore.read_artifact("tpm", "nonexistent", "workflow_skill")
        assert result is None

    def test_read_unknown_artifact_type_returns_none(self, filestore):
        result = filestore.read_artifact("tpm", "skill", "bad_type")
        assert result is None


class TestFilestoreSkillStorePromote:
    def test_promote_does_not_raise(self, filestore):
        # promote is a no-op in filestore mode; must not raise
        filestore.promote("tpm", "weekly_status")


class TestFilestoreSkillStoreList:
    def test_list_returns_entries_for_existing_skills(self, filestore, tmp_repo):
        # Create two skills for tpm persona
        skills_dir = tmp_repo / "framework" / "workflow_skills" / "tpm"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "weekly_status.yaml").write_text("skill: weekly_status")
        (skills_dir / "risk_report.yaml").write_text("skill: risk_report")

        result = filestore.list_skills(persona="tpm")
        names = [s["skill_name"] for s in result]
        assert "weekly_status" in names
        assert "risk_report" in names

    def test_list_all_personas(self, filestore, tmp_repo):
        for p in ("tpm", "pm"):
            d = tmp_repo / "framework" / "workflow_skills" / p
            d.mkdir(parents=True, exist_ok=True)
            (d / "a_skill.yaml").write_text("skill: a")

        result = filestore.list_skills()
        personas = {s["persona"] for s in result}
        assert "tpm" in personas
        assert "pm" in personas

    def test_list_empty_when_no_skills(self, filestore):
        result = filestore.list_skills(persona="nonexistent")
        assert result == []


# ---------------------------------------------------------------------------
# AdbSkillStore (mock pool / cursor)
# ---------------------------------------------------------------------------


def _make_mock_pool():
    """Build a mock oracledb pool that supports context manager + cursor."""
    mock_cur = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = lambda s: s
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    # cursor() as context manager
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__enter__ = lambda s: mock_conn
    mock_pool.acquire.return_value.__exit__ = MagicMock(return_value=False)

    return mock_pool, mock_conn, mock_cur


class TestAdbSkillStoreNullPool:
    def test_write_no_pool_is_noop(self):
        store = AdbSkillStore(pool=None)
        # Should not raise
        store.write_artifacts("s", "tpm", "skill", {"workflow_skill": "content"})

    def test_read_no_pool_returns_none(self):
        store = AdbSkillStore(pool=None)
        result = store.read_artifact("tpm", "skill", "workflow_skill")
        assert result is None

    def test_promote_no_pool_is_noop(self):
        store = AdbSkillStore(pool=None)
        store.promote("tpm", "skill")

    def test_list_no_pool_returns_empty(self):
        store = AdbSkillStore(pool=None)
        result = store.list_skills()
        assert result == []


class TestAdbSkillStoreWriteArtifacts:
    def test_merge_sql_called_for_each_artifact(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)

        artifacts = {
            "workflow_skill":        "wf content",
            "persona_builder_delta": "pb content",
        }
        store.write_artifacts(
            synth_id="synth-test",
            persona="ops_eng",
            skill_name="incident_summary",
            artifacts=artifacts,
        )

        # execute should be called once per artifact
        assert mock_cur.execute.call_count == 2

        # Each call should use a MERGE statement
        for c in mock_cur.execute.call_args_list:
            sql_arg = c.args[0]
            assert "MERGE INTO KB_SHIM.KBF_SKILL_ARTIFACTS" in sql_arg

    def test_correct_artifact_id_format(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)

        store.write_artifacts(
            synth_id="s",
            persona="tpm",
            skill_name="weekly_report",
            artifacts={"workflow_skill": "text"},
        )

        # Extract the params dict from the first execute call
        params = mock_cur.execute.call_args.args[1]
        expected_id = "tpm.weekly_report.workflow_skill"
        assert params["artifact_id"] == expected_id

    def test_correct_bind_values(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)

        store.write_artifacts(
            synth_id="synth-abc",
            persona="pm",
            skill_name="exec_summary",
            artifacts={"eval_extraction": "jsonl content"},
        )

        params = mock_cur.execute.call_args.args[1]
        assert params["synth_id"] == "synth-abc"
        assert params["persona"] == "pm"
        assert params["skill_name"] == "exec_summary"
        assert params["artifact_type"] == "eval_extraction"
        assert params["content"] == "jsonl content"
        assert params["status"] == "draft"
        assert "eval/gold_sets/pm-exec_summary-extraction.jsonl" in params["rel_path"]

    def test_commit_called(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)
        store.write_artifacts("s", "tpm", "skill", {"workflow_skill": "c"})
        mock_conn.commit.assert_called_once()

    def test_unknown_artifact_type_raises(self):
        mock_pool, _, _ = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)
        with pytest.raises(ValueError, match="Unknown artifact_type"):
            store.write_artifacts("s", "tpm", "skill", {"bad_type": "content"})


class TestAdbSkillStoreRead:
    def test_read_returns_content_from_row(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()

        # Simulate fetchone returning a row with content column
        mock_cur.description = [("CONTENT",)]
        mock_cur.fetchone.return_value = {"content": "wf yaml text"}

        store = AdbSkillStore(pool=mock_pool)
        result = store.read_artifact("tpm", "weekly_report", "workflow_skill")
        assert result == "wf yaml text"

        # Verify correct SQL and bind value
        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]
        assert "SELECT content" in sql
        assert "KBF_SKILL_ARTIFACTS" in sql
        assert params["artifact_id"] == "tpm.weekly_report.workflow_skill"

    def test_read_returns_none_when_not_found(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.description = [("CONTENT",)]
        mock_cur.fetchone.return_value = None

        store = AdbSkillStore(pool=mock_pool)
        result = store.read_artifact("tpm", "nonexistent", "workflow_skill")
        assert result is None

    def test_read_materialises_lob_objects(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()

        lob = MagicMock()
        lob.read.return_value = "lob content"
        mock_cur.description = [("CONTENT",)]
        mock_cur.fetchone.return_value = {"content": lob}

        store = AdbSkillStore(pool=mock_pool)
        result = store.read_artifact("pm", "exec_summary", "eval_extraction")
        assert result == "lob content"
        lob.read.assert_called_once()


class TestAdbSkillStorePromote:
    def test_promote_issues_update_with_correct_binds(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)

        store.promote("ops_eng", "incident_summary")

        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]

        assert "UPDATE KB_SHIM.KBF_SKILL_ARTIFACTS" in sql
        assert "status" in sql.lower() or "promoted" in sql.lower()
        assert params["persona"] == "ops_eng"
        assert params["skill_name"] == "incident_summary"
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestBuildSkillStore:
    def test_raises_when_pool_is_none(self):
        """ADB is always available — no filestore fallback."""
        with pytest.raises(ValueError, match="pool is required"):
            build_skill_store(pool=None)

    def test_returns_adb_store_when_pool_provided(self):
        mock_pool = MagicMock()
        store = build_skill_store(pool=mock_pool)
        assert isinstance(store, AdbSkillStore)


# ---------------------------------------------------------------------------
# make_artifact_id helper
# ---------------------------------------------------------------------------


class TestMakeArtifactId:
    def test_format_is_persona_dot_skill_dot_type(self):
        result = make_artifact_id("tpm", "weekly_report", "workflow_skill")
        assert result == "tpm.weekly_report.workflow_skill"

    def test_all_five_types_generate_unique_ids(self):
        ids = [make_artifact_id("ops_eng", "incident_summary", t) for t in ARTIFACT_TYPES]
        assert len(set(ids)) == len(ARTIFACT_TYPES)  # one unique id per artifact type
