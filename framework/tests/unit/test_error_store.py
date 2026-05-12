"""Unit tests for framework/deploy/error_store.py.

Coverage:
  - record_error appends valid JSON line
  - record_user_bug appends valid JSON line
  - read_errors / read_user_bugs round-trip
  - Missing file returns empty list (graceful)
  - Concurrent appends do not corrupt (append mode + newline delimiter)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from framework.deploy.error_store import ErrorStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path) -> ErrorStore:
    return ErrorStore(tmp_path)


# ---------------------------------------------------------------------------
# Basic write / read round-trips
# ---------------------------------------------------------------------------


class TestRecordError:
    def test_appends_jsonl_line(self, store, tmp_path):
        store.record_error({"request_id": "req-001", "message": "test error"})
        errors_path = tmp_path / "errors.jsonl"
        assert errors_path.exists()
        lines = errors_path.read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["request_id"] == "req-001"
        assert parsed["message"] == "test error"

    def test_multiple_appends_yield_multiple_lines(self, store, tmp_path):
        store.record_error({"request_id": "req-001"})
        store.record_error({"request_id": "req-002"})
        store.record_error({"request_id": "req-003"})
        lines = (tmp_path / "errors.jsonl").read_text().splitlines()
        assert len(lines) == 3

    def test_each_line_is_valid_json(self, store, tmp_path):
        store.record_error({"a": 1})
        store.record_error({"b": 2})
        for line in (tmp_path / "errors.jsonl").read_text().splitlines():
            json.loads(line)  # should not raise


class TestRecordUserBug:
    def test_appends_jsonl_line(self, store, tmp_path):
        store.record_user_bug({"request_id": "req-abc", "description": "it broke"})
        bugs_path = tmp_path / "user_bugs.jsonl"
        assert bugs_path.exists()
        parsed = json.loads(bugs_path.read_text().splitlines()[0])
        assert parsed["request_id"] == "req-abc"
        assert parsed["description"] == "it broke"

    def test_multiple_bugs_appended(self, store, tmp_path):
        store.record_user_bug({"queue_id": "BUG-queue-00001"})
        store.record_user_bug({"queue_id": "BUG-queue-00002"})
        lines = (tmp_path / "user_bugs.jsonl").read_text().splitlines()
        assert len(lines) == 2


class TestReadErrors:
    def test_returns_empty_list_when_file_missing(self, tmp_path):
        store = ErrorStore(tmp_path / "nonexistent_subdir")
        # Directory auto-created but file not yet written
        result = store.read_errors()
        assert result == []

    def test_round_trips_entries(self, store):
        entry1 = {"request_id": "req-a", "tool": "authorSkill", "error_type": "ValueError"}
        entry2 = {"request_id": "req-b", "tool": "askKB", "error_type": "TypeError"}
        store.record_error(entry1)
        store.record_error(entry2)
        errors = store.read_errors()
        assert len(errors) == 2
        assert errors[0]["request_id"] == "req-a"
        assert errors[1]["request_id"] == "req-b"

    def test_skips_blank_lines(self, store, tmp_path):
        errors_path = tmp_path / "errors.jsonl"
        errors_path.write_text('{"request_id": "req-1"}\n\n{"request_id": "req-2"}\n')
        result = store.read_errors()
        assert len(result) == 2

    def test_skips_malformed_json(self, store, tmp_path):
        errors_path = tmp_path / "errors.jsonl"
        errors_path.write_text('{"ok": true}\nNOT JSON\n{"also": "ok"}\n')
        result = store.read_errors()
        assert len(result) == 2
        assert result[0]["ok"] is True
        assert result[1]["also"] == "ok"


class TestReadUserBugs:
    def test_returns_empty_list_when_file_missing(self, tmp_path):
        store = ErrorStore(tmp_path)
        result = store.read_user_bugs()
        assert result == []

    def test_round_trips_entries(self, store):
        store.record_user_bug({"queue_id": "BUG-queue-aaaaa", "request_id": "req-1"})
        bugs = store.read_user_bugs()
        assert len(bugs) == 1
        assert bugs[0]["queue_id"] == "BUG-queue-aaaaa"


# ---------------------------------------------------------------------------
# Concurrent appends — no corruption
# ---------------------------------------------------------------------------


class TestConcurrentAppends:
    def test_concurrent_errors_no_corruption(self, store):
        """Concurrent writes to errors.jsonl must produce valid, distinct lines."""
        n_threads = 20
        n_per_thread = 10
        errors_list: list[Exception] = []

        def _write(thread_id: int):
            try:
                for i in range(n_per_thread):
                    store.record_error({"thread": thread_id, "index": i})
            except Exception as exc:
                errors_list.append(exc)

        threads = [threading.Thread(target=_write, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors_list, f"Thread errors: {errors_list}"

        records = store.read_errors()
        # Every record must be parseable (ensured by read_errors skipping bad lines)
        assert len(records) == n_threads * n_per_thread

    def test_concurrent_user_bugs_no_corruption(self, store):
        """Concurrent writes to user_bugs.jsonl must not corrupt each other."""
        n_threads = 10
        errors_list: list[Exception] = []

        def _write(thread_id: int):
            try:
                store.record_user_bug({"queue_id": f"BUG-queue-{thread_id:05d}"})
            except Exception as exc:
                errors_list.append(exc)

        threads = [threading.Thread(target=_write, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors_list
        bugs = store.read_user_bugs()
        assert len(bugs) == n_threads


# ---------------------------------------------------------------------------
# ErrorStore constructor — directory auto-created
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_creates_store_root_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        assert not nested.exists()
        ErrorStore(nested)
        assert nested.exists()

    def test_errors_and_bugs_paths_under_root(self, tmp_path):
        store = ErrorStore(tmp_path)
        # Write one record to each file to materialise them
        store.record_error({"x": 1})
        store.record_user_bug({"y": 2})
        assert (tmp_path / "errors.jsonl").exists()
        assert (tmp_path / "user_bugs.jsonl").exists()
