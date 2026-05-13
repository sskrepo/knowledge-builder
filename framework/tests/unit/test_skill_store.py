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


class TestAdbSkillStoreRequiresPool:
    """ADB is the source of truth — there is no stub-mode / no-op fallback.
    Constructing AdbSkillStore with pool=None must fail at construction so
    the app cannot silently degrade. This is the contract that prevents the
    synth-tpm-14a54555-class of data-loss bug.
    """

    def test_constructor_raises_on_none_pool(self):
        with pytest.raises(ValueError, match="pool is required"):
            AdbSkillStore(pool=None)


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

    def test_retries_transient_failure_then_succeeds(self, monkeypatch):
        """Transient ADB errors (network blip, deadlock) should be retried.

        Prevents silent data loss like the synth-tpm-6523a9c4 incident: the
        session reported "Committed" but ADB had no row because a single
        transient pool error was previously swallowed.
        """
        # Avoid the 0.5/2.0/5.0s real sleeps in the retry loop.
        monkeypatch.setattr("time.sleep", lambda _s: None)

        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        # First two attempts raise; third succeeds.
        attempts = {"n": 0}

        def execute_side_effect(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("transient ADB error")
            return None

        mock_cur.execute.side_effect = execute_side_effect

        store = AdbSkillStore(pool=mock_pool)
        # Should NOT raise — third attempt succeeds.
        store.write_artifacts(
            synth_id="s", persona="tpm", skill_name="skill",
            artifacts={"workflow_skill": "c"},
        )
        assert attempts["n"] == 3, "expected exactly 3 attempts (2 fail + 1 success)"

    def test_raises_after_exhausting_retries(self, monkeypatch):
        """When all 3 attempts fail, write_artifacts must re-raise — caller
        (skill_builder.conversation) needs the exception to keep the session
        at PREVIEW and refuse to advance to COMMITTED. Reporting success when
        ADB has nothing is the bug behind synth-tpm-6523a9c4.
        """
        monkeypatch.setattr("time.sleep", lambda _s: None)

        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.execute.side_effect = RuntimeError("ADB permanently down")

        store = AdbSkillStore(pool=mock_pool)
        with pytest.raises(RuntimeError, match="ADB permanently down"):
            store.write_artifacts(
                synth_id="s", persona="tpm", skill_name="skill",
                artifacts={"workflow_skill": "c"},
            )


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
        mock_cur.rowcount = 1  # must be >0 to satisfy the no-row-update guard
        store = AdbSkillStore(pool=mock_pool)

        store.promote("ops_eng", "incident_summary")

        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]

        assert "UPDATE KB_SHIM.KBF_SKILL_ARTIFACTS" in sql
        assert "status" in sql.lower() or "promoted" in sql.lower()
        assert params["persona"] == "ops_eng"
        assert params["skill_name"] == "incident_summary"
        mock_conn.commit.assert_called_once()

    def test_promote_raises_when_zero_rows_updated(self):
        """Hard-fail contract: promoting a skill that doesn't exist in ADB
        must raise — never silently no-op. This is the guard that catches
        the synth-tpm-14a54555 class of bug, where an earlier silent COMMIT
        failure leaves no row to promote.
        """
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.rowcount = 0
        store = AdbSkillStore(pool=mock_pool)

        with pytest.raises(ValueError, match="0 rows updated"):
            store.promote("tpm", "skill_that_was_never_committed")


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


# ---------------------------------------------------------------------------
# delete() — FilestoreSkillStore
# ---------------------------------------------------------------------------


class TestFilestoreSkillStoreDelete:
    def test_delete_removes_all_existing_artifacts(self, tmp_path):
        store = FilestoreSkillStore(repo_root=tmp_path)
        store.write_artifacts(
            synth_id="s1",
            persona="tpm",
            skill_name="my_skill",
            artifacts={t: f"content-{t}" for t in ARTIFACT_TYPES},
        )
        deleted = store.delete("tpm", "my_skill")
        assert set(deleted) == ARTIFACT_TYPES
        # Files are gone
        for t in ARTIFACT_TYPES:
            assert store.read_artifact("tpm", "my_skill", t) is None

    def test_delete_returns_empty_list_when_skill_not_found(self, tmp_path):
        store = FilestoreSkillStore(repo_root=tmp_path)
        deleted = store.delete("tpm", "nonexistent_skill")
        assert deleted == []

    def test_delete_only_removes_existing_files(self, tmp_path):
        store = FilestoreSkillStore(repo_root=tmp_path)
        # Only write 2 of 5 artifact types
        partial = {t: "content" for t in list(ARTIFACT_TYPES)[:2]}
        store.write_artifacts("s1", "ops_eng", "partial_skill", partial)
        deleted = store.delete("ops_eng", "partial_skill")
        assert set(deleted) == set(partial.keys())

    def test_delete_is_idempotent(self, tmp_path):
        store = FilestoreSkillStore(repo_root=tmp_path)
        store.write_artifacts("s1", "tpm", "skill", {"workflow_skill": "yaml"})
        store.delete("tpm", "skill")
        # Second delete — nothing to remove, should not raise
        deleted2 = store.delete("tpm", "skill")
        assert deleted2 == []


# ---------------------------------------------------------------------------
# delete() — AdbSkillStore
# ---------------------------------------------------------------------------


class TestAdbSkillStoreDelete:
    def _make_pool(self, existing_types: list[str]):
        """Build a mock pool that returns existing_types from the SELECT query."""
        pool = MagicMock()
        conn = MagicMock()
        cur = MagicMock()

        pool.acquire.return_value.__enter__ = lambda s: conn
        pool.acquire.return_value.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # First execute call → SELECT (returns existing types)
        # Second execute call → DELETE (no return value needed)
        cur.fetchall.return_value = [{"artifact_type": t} for t in existing_types]
        cur.description = [("artifact_type",)]

        return pool, cur

    def test_delete_executes_select_then_delete(self):
        existing = ["workflow_skill", "eval_extraction"]
        pool, cur = self._make_pool(existing)
        store = AdbSkillStore(pool=pool)
        # Patch rowfactory install (description already set above)
        deleted = store.delete("tpm", "my_skill")
        assert set(deleted) == set(existing)
        # DELETE was called
        assert cur.execute.call_count == 2

    def test_delete_skips_delete_sql_when_no_rows(self):
        pool, cur = self._make_pool([])
        store = AdbSkillStore(pool=pool)
        deleted = store.delete("tpm", "ghost_skill")
        assert deleted == []
        # Only SELECT was called, no DELETE
        assert cur.execute.call_count == 1

    # test_delete_is_noop_when_pool_is_none removed — AdbSkillStore(pool=None)
    # now raises at construction; see TestAdbSkillStoreRequiresPool above.


# ---------------------------------------------------------------------------
# upsert_persona_builder_kb / list_persona_builder_kbs — FilestoreSkillStore
# ---------------------------------------------------------------------------


class TestFilestorePersonaBuilderKbs:
    def test_upsert_writes_file(self, tmp_path, monkeypatch):
        store = FilestoreSkillStore(repo_root=tmp_path)
        # Redirect the global _PB_STORE_ROOT so we don't pollute ~/.kbf
        pb_root = tmp_path / ".kbf" / "persona_builders"
        import framework.deploy.skill_store.filestore as fs_mod
        monkeypatch.setattr(fs_mod, "_PB_STORE_ROOT", pb_root)

        store.upsert_persona_builder_kb(
            persona="tpm",
            kb_name="weekly_status",
            content_yaml="name: weekly_status\nkind: vector\n",
            status="production",
        )

        dest = pb_root / "tpm" / "weekly_status.yaml"
        assert dest.exists()
        wrapper = yaml.safe_load(dest.read_text())
        assert wrapper["persona"] == "tpm"
        assert wrapper["kb_name"] == "weekly_status"
        assert wrapper["status"] == "production"
        assert "weekly_status" in wrapper["content_yaml"]

    def test_list_returns_matching_entries(self, tmp_path, monkeypatch):
        store = FilestoreSkillStore(repo_root=tmp_path)
        pb_root = tmp_path / ".kbf" / "persona_builders"
        import framework.deploy.skill_store.filestore as fs_mod
        monkeypatch.setattr(fs_mod, "_PB_STORE_ROOT", pb_root)

        store.upsert_persona_builder_kb("tpm", "weekly_status", "name: ws\n", "production")
        store.upsert_persona_builder_kb("tpm", "risk_report", "name: rr\n", "draft")
        store.upsert_persona_builder_kb("ops_eng", "incident_summary", "name: is\n", "production")

        all_kbs = store.list_persona_builder_kbs()
        assert len(all_kbs) == 3

        prod_kbs = store.list_persona_builder_kbs(status="production")
        assert len(prod_kbs) == 2
        assert all(k["status"] == "production" for k in prod_kbs)

        tpm_kbs = store.list_persona_builder_kbs(persona="tpm")
        assert len(tpm_kbs) == 2
        assert all(k["persona"] == "tpm" for k in tpm_kbs)

        tpm_prod = store.list_persona_builder_kbs(persona="tpm", status="production")
        assert len(tpm_prod) == 1
        assert tpm_prod[0]["kb_name"] == "weekly_status"

    def test_list_returns_empty_when_no_kbs(self, tmp_path, monkeypatch):
        store = FilestoreSkillStore(repo_root=tmp_path)
        pb_root = tmp_path / ".kbf" / "persona_builders"
        import framework.deploy.skill_store.filestore as fs_mod
        monkeypatch.setattr(fs_mod, "_PB_STORE_ROOT", pb_root)

        result = store.list_persona_builder_kbs()
        assert result == []

    def test_upsert_is_idempotent(self, tmp_path, monkeypatch):
        store = FilestoreSkillStore(repo_root=tmp_path)
        pb_root = tmp_path / ".kbf" / "persona_builders"
        import framework.deploy.skill_store.filestore as fs_mod
        monkeypatch.setattr(fs_mod, "_PB_STORE_ROOT", pb_root)

        store.upsert_persona_builder_kb("tpm", "weekly_status", "v1 content\n", "draft")
        store.upsert_persona_builder_kb("tpm", "weekly_status", "v2 content\n", "production")

        results = store.list_persona_builder_kbs(persona="tpm")
        assert len(results) == 1
        assert results[0]["status"] == "production"
        assert "v2 content" in results[0]["content_yaml"]


# ---------------------------------------------------------------------------
# upsert_persona_builder_kb / list_persona_builder_kbs — AdbSkillStore
# ---------------------------------------------------------------------------


class TestAdbPersonaBuilderKbs:
    def test_upsert_issues_merge_sql(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbSkillStore(pool=mock_pool)

        store.upsert_persona_builder_kb(
            persona="tpm",
            kb_name="weekly_status",
            content_yaml="name: weekly_status\nkind: vector\n",
            status="production",
        )

        assert mock_cur.execute.call_count == 1
        sql = mock_cur.execute.call_args.args[0]
        assert "MERGE INTO KB_SHIM.KBF_PERSONA_BUILDERS" in sql

        params = mock_cur.execute.call_args.args[1]
        assert params["persona"] == "tpm"
        assert params["kb_name"] == "weekly_status"
        assert params["status"] == "production"
        assert "weekly_status" in params["content_yaml"]
        mock_conn.commit.assert_called_once()

    # test_upsert_no_pool_is_noop removed — AdbSkillStore(pool=None) now raises
    # at construction; see TestAdbSkillStoreRequiresPool.

    def test_list_all_issues_select(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.description = [("persona",), ("kb_name",), ("content_yaml",), ("status",), ("updated_at",)]
        mock_cur.fetchall.return_value = [
            {"persona": "tpm", "kb_name": "ws", "content_yaml": "name: ws\n",
             "status": "production", "updated_at": "2026-01-01"},
        ]

        store = AdbSkillStore(pool=mock_pool)
        results = store.list_persona_builder_kbs()

        assert mock_cur.execute.call_count == 1
        sql = mock_cur.execute.call_args.args[0]
        assert "KBF_PERSONA_BUILDERS" in sql
        assert len(results) == 1
        assert results[0]["persona"] == "tpm"
        assert results[0]["kb_name"] == "ws"

    def test_list_by_persona_uses_persona_filter(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.description = [("persona",), ("kb_name",), ("content_yaml",), ("status",), ("updated_at",)]
        mock_cur.fetchall.return_value = []

        store = AdbSkillStore(pool=mock_pool)
        store.list_persona_builder_kbs(persona="tpm")

        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]
        assert "KBF_PERSONA_BUILDERS" in sql
        assert "persona" in sql.lower()
        assert params.get("persona") == "tpm"

    def test_list_by_status_uses_status_filter(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.description = [("persona",), ("kb_name",), ("content_yaml",), ("status",), ("updated_at",)]
        mock_cur.fetchall.return_value = []

        store = AdbSkillStore(pool=mock_pool)
        store.list_persona_builder_kbs(status="production")

        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]
        assert "KBF_PERSONA_BUILDERS" in sql
        assert params.get("status") == "production"

    # test_list_no_pool_returns_empty removed — AdbSkillStore(pool=None) now
    # raises at construction; see TestAdbSkillStoreRequiresPool.

    def test_list_materialises_lob_content_yaml(self):
        mock_pool, mock_conn, mock_cur = _make_mock_pool()
        lob = MagicMock()
        lob.read.return_value = "name: ws\n"
        mock_cur.description = [("persona",), ("kb_name",), ("content_yaml",), ("status",), ("updated_at",)]
        mock_cur.fetchall.return_value = [
            {"persona": "tpm", "kb_name": "ws", "content_yaml": lob,
             "status": "production", "updated_at": "2026-01-01"},
        ]

        store = AdbSkillStore(pool=mock_pool)
        results = store.list_persona_builder_kbs()

        assert results[0]["content_yaml"] == "name: ws\n"
        lob.read.assert_called_once()


# ---------------------------------------------------------------------------
# deleteSkill MCP tool
# ---------------------------------------------------------------------------


class TestDeleteSkillMcpTool:
    """Tests for the deleteSkill MCP handler (password + scope protection)."""

    def _make_app(self, deleted_types: list[str] | None = None):
        """Build a minimal mock app with skill_store."""
        from unittest.mock import MagicMock
        app = MagicMock()
        skill_store = MagicMock()
        skill_store.delete.return_value = deleted_types if deleted_types is not None else ["workflow_skill"]
        app.state.skill_store = skill_store
        return app, skill_store

    def _admin_consumer(self):
        from framework.deploy.auth.consumer import ConsumerManifest
        return ConsumerManifest(
            name="test-admin", token_hash="x", scopes=["admin"],
            persona_allowlist=[], rpm_cap=60, token_budget_per_request=8000,
            user_id="test-admin",
        )

    def _write_consumer(self):
        from framework.deploy.auth.consumer import ConsumerManifest
        return ConsumerManifest(
            name="test-writer", token_hash="x", scopes=["write"],
            persona_allowlist=[], rpm_cap=60, token_budget_per_request=8000,
            user_id="test-writer",
        )

    def test_rejects_non_admin_scope(self):
        import asyncio
        from framework.deploy.mcp_tools import _make_delete_skill_handler
        app, _ = self._make_app()
        handler = _make_delete_skill_handler(app)
        result = asyncio.get_event_loop().run_until_complete(
            handler(persona="tpm", skillName="skill", confirmationPassword="pw",
                    _consumer=self._write_consumer())
        )
        assert result["isError"] is True
        assert "admin" in result["content"][0]["text"]

    def test_rejects_wrong_password(self, monkeypatch):
        import asyncio
        from framework.deploy.mcp_tools import _make_delete_skill_handler
        monkeypatch.setenv("KBF_SKILL_DELETE_PASSWORD", "correct-password")
        app, _ = self._make_app()
        handler = _make_delete_skill_handler(app)
        result = asyncio.get_event_loop().run_until_complete(
            handler(persona="tpm", skillName="skill", confirmationPassword="wrong",
                    _consumer=self._admin_consumer())
        )
        assert result["isError"] is True
        assert "Invalid" in result["content"][0]["text"]

    def test_rejects_when_password_env_not_set(self, monkeypatch):
        import asyncio
        from framework.deploy.mcp_tools import _make_delete_skill_handler
        monkeypatch.delenv("KBF_SKILL_DELETE_PASSWORD", raising=False)
        app, _ = self._make_app()
        handler = _make_delete_skill_handler(app)
        result = asyncio.get_event_loop().run_until_complete(
            handler(persona="tpm", skillName="skill", confirmationPassword="any",
                    _consumer=self._admin_consumer())
        )
        assert result["isError"] is True
        assert "not configured" in result["content"][0]["text"]

    def test_deletes_skill_with_correct_credentials(self, monkeypatch):
        import asyncio
        from framework.deploy.mcp_tools import _make_delete_skill_handler
        monkeypatch.setenv("KBF_SKILL_DELETE_PASSWORD", "secret123")
        app, skill_store = self._make_app(deleted_types=["workflow_skill", "eval_extraction"])
        handler = _make_delete_skill_handler(app)
        result = asyncio.get_event_loop().run_until_complete(
            handler(persona="tpm", skillName="my_skill", confirmationPassword="secret123",
                    _consumer=self._admin_consumer())
        )
        assert result["isError"] is False
        assert result["status"] == "deleted"
        assert set(result["deletedArtifacts"]) == {"workflow_skill", "eval_extraction"}
        skill_store.delete.assert_called_once_with("tpm", "my_skill")

    def test_returns_not_found_when_no_artifacts(self, monkeypatch):
        import asyncio
        from framework.deploy.mcp_tools import _make_delete_skill_handler
        monkeypatch.setenv("KBF_SKILL_DELETE_PASSWORD", "secret123")
        app, _ = self._make_app(deleted_types=[])
        handler = _make_delete_skill_handler(app)
        result = asyncio.get_event_loop().run_until_complete(
            handler(persona="ops_eng", skillName="ghost", confirmationPassword="secret123",
                    _consumer=self._admin_consumer())
        )
        assert result["isError"] is False
        assert result["status"] == "not_found"
        assert result["deletedArtifacts"] == []
