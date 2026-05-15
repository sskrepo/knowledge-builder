"""Unit tests for AdbErrorStore in framework/deploy/error_store.py.

Coverage:
  - record_error writes to both ADB (mock cursor) and local JSONL
  - record_user_bug writes to both ADB and local JSONL
  - ADB write failure does NOT suppress the JSONL write
  - pool=None falls back to pure JSONL (parent class behaviour)
  - Correct SQL and bind values for each INSERT statement
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.deploy.error_store import AdbErrorStore, ErrorStore


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


@pytest.fixture()
def adb_store(tmp_store):
    pool, _, _ = _make_mock_pool()
    return AdbErrorStore(pool, tmp_store), pool


# ---------------------------------------------------------------------------
# record_error dual-write
# ---------------------------------------------------------------------------


class TestAdbErrorStoreRecordError:
    def test_writes_to_jsonl(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_error({"request_id": "req-001", "message": "boom"})
        lines = (tmp_store / "errors.jsonl").read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["request_id"] == "req-001"

    def test_inserts_into_adb(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_error({
            "request_id": "req-abc",
            "tool": "authorSkill",
            "error_type": "ValueError",
            "message": "bad input",
        })
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]
        assert "KBF_ERROR_LOG" in sql
        assert "INSERT" in sql
        assert params["request_id"] == "req-abc"
        assert params["tool"] == "authorSkill"
        assert params["error_type"] == "ValueError"
        assert params["message"] == "bad input"

    def test_commit_called(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_error({"request_id": "req-x"})
        mock_conn.commit.assert_called_once()

    def test_adb_failure_does_not_suppress_jsonl_write(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.execute.side_effect = Exception("DB down")
        store = AdbErrorStore(pool, tmp_store)
        # Should not raise — ADB failure is swallowed
        store.record_error({"request_id": "req-fail", "message": "test"})
        # JSONL must still be written
        assert (tmp_store / "errors.jsonl").exists()

    def test_extra_fields_stored_in_extra_json(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_error({
            "request_id": "r",
            "tool": "t",
            "custom_field": "custom_value",
            "another": 42,
        })
        params = mock_cur.execute.call_args.args[1]
        extra = json.loads(params["extra_json"])
        assert extra.get("custom_field") == "custom_value"
        assert extra.get("another") == 42

    def test_null_pool_falls_back_to_jsonl_only(self, tmp_store):
        store = AdbErrorStore(pool=None, store_root=tmp_store)
        store.record_error({"request_id": "req-001", "message": "test"})
        assert (tmp_store / "errors.jsonl").exists()


# ---------------------------------------------------------------------------
# record_user_bug dual-write
# ---------------------------------------------------------------------------


class TestAdbErrorStoreRecordUserBug:
    def test_writes_to_jsonl(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_user_bug({"request_id": "req-bug", "queue_id": "BUG-queue-00001"})
        lines = (tmp_store / "user_bugs.jsonl").read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["queue_id"] == "BUG-queue-00001"

    def test_inserts_into_adb(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_user_bug({
            "request_id": "req-xyz",
            "queue_id": "BUG-queue-00001",
            "tool": "reportBug",
            "description": "it crashed",
        })
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args.args[0]
        params = mock_cur.execute.call_args.args[1]
        assert "KBF_BUG_REPORTS" in sql
        assert params["queue_id"] == "BUG-queue-00001"
        assert params["description"] == "it crashed"

    def test_adb_failure_does_not_suppress_jsonl(self, tmp_store):
        pool, mock_conn, mock_cur = _make_mock_pool()
        mock_cur.execute.side_effect = RuntimeError("connection lost")
        store = AdbErrorStore(pool, tmp_store)
        store.record_user_bug({"request_id": "r", "description": "d"})
        assert (tmp_store / "user_bugs.jsonl").exists()

    def test_null_pool_falls_back_to_jsonl_only(self, tmp_store):
        store = AdbErrorStore(pool=None, store_root=tmp_store)
        store.record_user_bug({"request_id": "r", "queue_id": "BUG-0"})
        assert (tmp_store / "user_bugs.jsonl").exists()

    def test_inherits_read_methods_from_parent(self, tmp_store):
        pool, _, _ = _make_mock_pool()
        store = AdbErrorStore(pool, tmp_store)
        store.record_user_bug({"queue_id": "BUG-abc"})
        bugs = store.read_user_bugs()
        assert len(bugs) == 1
        assert bugs[0]["queue_id"] == "BUG-abc"


# ---------------------------------------------------------------------------
# CLOB binding — BUG-queue-440da
#
# AdbErrorStore.record_error and record_user_bug must call setinputsizes for
# CLOB columns (stack_trace, message, extra_json, description) so the
# oracledb thin driver uses LOB binding rather than VARCHAR2(4000).
# Stack traces for 32-field sessions easily exceed 4000 bytes.
# ---------------------------------------------------------------------------


class TestAdbErrorStoreClobBinding:
    """Regression tests for BUG-queue-440da: AdbErrorStore must declare
    CLOB columns via setinputsizes so that long stack traces and large
    bug descriptions don't trigger ORA-03146 on real ADB.
    """

    def _patch_clob_flag(self, error_mod, value: bool):
        original = error_mod._ORACLEDB_AVAILABLE
        error_mod._ORACLEDB_AVAILABLE = value
        return original

    def _patch_oracledb(self, error_mod, clob_sentinel):
        original = getattr(error_mod, "oracledb", None)
        mock_oracledb = MagicMock()
        mock_oracledb.DB_TYPE_CLOB = clob_sentinel
        error_mod.oracledb = mock_oracledb
        return original

    def _restore_oracledb(self, error_mod, original):
        if original is not None:
            error_mod.oracledb = original
        elif hasattr(error_mod, "oracledb"):
            del error_mod.oracledb

    def test_record_error_calls_setinputsizes_for_clob_columns(self, tmp_store):
        """record_error must call setinputsizes with message, stack_trace,
        and extra_json declared as DB_TYPE_CLOB before the INSERT execute.
        """
        import framework.deploy.error_store as error_mod
        pool, mock_conn, mock_cur = _make_mock_pool()

        clob_sentinel = object()
        orig_flag = self._patch_clob_flag(error_mod, True)
        orig_odb = self._patch_oracledb(error_mod, clob_sentinel)

        try:
            store = AdbErrorStore(pool, tmp_store)
            store.record_error({
                "request_id": "req-clob",
                "message": "m" * 5000,           # > 4000 bytes
                "stack_trace": "t" * 6000,        # > 4000 bytes
            })
        finally:
            self._patch_clob_flag(error_mod, orig_flag)
            self._restore_oracledb(error_mod, orig_odb)

        mock_cur.setinputsizes.assert_called_once()
        kwargs = mock_cur.setinputsizes.call_args.kwargs
        assert kwargs.get("message") is clob_sentinel, (
            f"Expected setinputsizes(message=DB_TYPE_CLOB), got {kwargs!r}"
        )
        assert kwargs.get("stack_trace") is clob_sentinel, (
            f"Expected setinputsizes(stack_trace=DB_TYPE_CLOB), got {kwargs!r}"
        )
        assert kwargs.get("extra_json") is clob_sentinel, (
            f"Expected setinputsizes(extra_json=DB_TYPE_CLOB), got {kwargs!r}"
        )

    def test_record_user_bug_calls_setinputsizes_for_clob_columns(self, tmp_store):
        """record_user_bug must declare description and extra_json as CLOB."""
        import framework.deploy.error_store as error_mod
        pool, mock_conn, mock_cur = _make_mock_pool()

        clob_sentinel = object()
        orig_flag = self._patch_clob_flag(error_mod, True)
        orig_odb = self._patch_oracledb(error_mod, clob_sentinel)

        try:
            store = AdbErrorStore(pool, tmp_store)
            store.record_user_bug({
                "request_id": "req-bug-clob",
                "queue_id": "BUG-queue-440da",
                "description": "d" * 5000,  # > 4000 bytes
            })
        finally:
            self._patch_clob_flag(error_mod, orig_flag)
            self._restore_oracledb(error_mod, orig_odb)

        mock_cur.setinputsizes.assert_called_once()
        kwargs = mock_cur.setinputsizes.call_args.kwargs
        assert kwargs.get("description") is clob_sentinel, (
            f"Expected setinputsizes(description=DB_TYPE_CLOB), got {kwargs!r}"
        )
        assert kwargs.get("extra_json") is clob_sentinel, (
            f"Expected setinputsizes(extra_json=DB_TYPE_CLOB), got {kwargs!r}"
        )

    def test_setinputsizes_not_called_when_oracledb_unavailable(self, tmp_store):
        """When _ORACLEDB_AVAILABLE is False, setinputsizes must NOT be called."""
        import framework.deploy.error_store as error_mod
        pool, mock_conn, mock_cur = _make_mock_pool()

        orig_flag = self._patch_clob_flag(error_mod, False)
        try:
            store = AdbErrorStore(pool, tmp_store)
            store.record_error({"request_id": "r", "message": "short"})
        finally:
            self._patch_clob_flag(error_mod, orig_flag)

        mock_cur.setinputsizes.assert_not_called()
