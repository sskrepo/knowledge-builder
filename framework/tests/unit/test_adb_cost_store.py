"""Unit tests for AdbCostStore in framework/deploy/cost_store.py.

Coverage:
  - record() writes to both ADB (mock cursor) and local JSONL
  - ADB write failure does NOT suppress the JSONL write
  - pool=None falls back to pure JSONL (parent class behaviour)
  - Correct SQL and bind values for the INSERT statement
  - query() still works from JSONL cache after dual writes
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.deploy.cost_store import AdbCostStore, CostStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pool():
    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__enter__ = lambda s: mock_conn
    mock_pool.acquire.return_value.__exit__ = MagicMock(return_value=False)

    return mock_pool, mock_conn, mock_cur


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_store(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# record() dual-write
# ---------------------------------------------------------------------------


class TestAdbCostStoreRecord:
    def test_writes_to_jsonl(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(persona="tpm", operation="ingestion", prompt_tokens=100, completion_tokens=50)
        log_path = tmp_store / "cost_log.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["persona"] == "tpm"
        assert entry["operation"] == "ingestion"
        assert entry["prompt"] == 100
        assert entry["completion"] == 50
        assert entry["total"] == 150

    def test_inserts_into_adb(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(
            persona="ops_eng",
            operation="retrieval",
            prompt_tokens=200,
            completion_tokens=80,
            skill_name="incident_summary",
        )
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]
        assert "KBF_COST_LOG" in sql
        assert "INSERT" in sql
        assert params["persona"] == "ops_eng"
        assert params["operation"] == "retrieval"
        assert params["prompt_tokens"] == 200
        assert params["completion_tokens"] == 80
        assert params["total_tokens"] == 280
        assert params["skill_name"] == "incident_summary"

    def test_commit_called(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(persona="pm", operation="synthesis", prompt_tokens=10, completion_tokens=5)
        mock_conn.commit.assert_called_once()

    def test_adb_failure_does_not_suppress_jsonl_write(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.execute.side_effect = RuntimeError("ADB down")
        store = AdbCostStore(pool, tmp_store)
        # Must not raise
        store.record(persona="tpm", operation="ingestion", prompt_tokens=5, completion_tokens=2)
        # JSONL must still be written
        assert (tmp_store / "cost_log.jsonl").exists()

    def test_null_pool_falls_back_to_jsonl_only(self, tmp_store):
        store = AdbCostStore(pool=None, store_root=tmp_store)
        store.record(persona="tpm", operation="ingestion", prompt_tokens=10, completion_tokens=5)
        assert (tmp_store / "cost_log.jsonl").exists()

    def test_skill_name_defaults_to_empty_string(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(persona="tpm", operation="ingestion", prompt_tokens=10, completion_tokens=5)
        params = mock_cur.execute.call_args.args[1]
        assert params["skill_name"] == ""

    def test_multiple_records_accumulate_in_jsonl(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(persona="tpm", operation="ingestion", prompt_tokens=10, completion_tokens=5)
        store.record(persona="tpm", operation="retrieval", prompt_tokens=20, completion_tokens=8)
        lines = (tmp_store / "cost_log.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# query() still works from JSONL cache after dual writes
# ---------------------------------------------------------------------------


class TestAdbCostStoreQuery:
    def test_query_reads_from_jsonl(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(persona="ops_eng", operation="ingestion", prompt_tokens=100, completion_tokens=40)
        store.record(persona="ops_eng", operation="retrieval", prompt_tokens=50, completion_tokens=20)

        result = store.query(persona="ops_eng")
        assert result["total_tokens"] == 210
        assert "ops_eng" in result["by_persona"]

    def test_query_inherits_filter_behaviour_from_parent(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbCostStore(pool, tmp_store)
        store.record(persona="tpm", operation="ingestion", prompt_tokens=50, completion_tokens=10,
                     skill_name="weekly_report")
        store.record(persona="pm", operation="synthesis", prompt_tokens=30, completion_tokens=15)

        result = store.query(persona="tpm")
        assert "tpm" in result["by_persona"]
        assert "pm" not in result["by_persona"]
