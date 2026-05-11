"""Tests for Track F: CostStore and operational route handlers.

Coverage:
  CostStore:
    - record() creates the JSONL file on first write
    - record() appends subsequent entries (file grows)
    - query() with no filters returns all entries aggregated
    - query() with persona filter includes only matching entries
    - query() with date range filter (start_date / end_date) works
    - query() on an empty / non-existent file returns zeroed response
    - query() aggregates correctly: by_persona totals, by_operation totals, total_tokens
    - query() with both persona and skill_name filters works

  Ops route handlers (via FastAPI TestClient):
    - GET /healthz returns 200 with status=="healthy" and checks dict
    - GET /healthz returns camelCase keys (uptimeSeconds, etc.)
    - GET /healthz returns 503 + status=="degraded" when git check fails
    - GET /api/v1/version returns 200 with apiVersion, schemaVersion, buildSha
    - GET /api/v1/version reads KBF_BUILD_SHA from env
    - GET /api/v1/metrics/cost with valid admin token returns 200
    - GET /api/v1/metrics/cost with non-admin token returns 403
    - GET /api/v1/metrics/cost passes query params to cost_store.query()
    - GET /api/v1/metrics/cost with cost_store=None returns zeroed response
    - /healthz and /api/v1/version do NOT require auth (no Authorization header)

Run:
    cd /path/to/Knowledgebase
    python3 -m pytest framework/tests/test_routes_ops.py -v --tb=short
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.deploy.cost_store import CostStore


# ===========================================================================
# CostStore unit tests
# ===========================================================================


class TestCostStoreRecord:
    """record() creates and appends to cost_log.jsonl."""

    def test_record_creates_file(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        log_path = tmp_path / "cost_log.jsonl"
        assert not log_path.exists()

        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)

        assert log_path.exists()

    def test_record_appends_valid_json_line(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)

        log_path = tmp_path / "cost_log.jsonl"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["persona"] == "ops_eng"
        assert entry["operation"] == "ingestion"
        assert entry["prompt"] == 100
        assert entry["completion"] == 50
        assert entry["total"] == 150

    def test_record_appends_multiple_entries(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        store.record(persona="tpm", operation="retrieval",
                     prompt_tokens=200, completion_tokens=80)
        store.record(persona="ops_eng", operation="synthesis",
                     prompt_tokens=300, completion_tokens=120)

        log_path = tmp_path / "cost_log.jsonl"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_record_stores_skill_name(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="retrieval",
                     prompt_tokens=50, completion_tokens=20,
                     skill_name="incident_summary")

        log_path = tmp_path / "cost_log.jsonl"
        entry = json.loads(log_path.read_text().strip())
        assert entry["skill_name"] == "incident_summary"

    def test_record_default_skill_name_is_empty_string(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="tpm", operation="ingestion",
                     prompt_tokens=10, completion_tokens=5)

        log_path = tmp_path / "cost_log.jsonl"
        entry = json.loads(log_path.read_text().strip())
        assert entry["skill_name"] == ""

    def test_record_stores_iso_timestamp(self, tmp_path: Path):
        before = datetime.now(tz=timezone.utc)
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=10, completion_tokens=5)
        after = datetime.now(tz=timezone.utc)

        log_path = tmp_path / "cost_log.jsonl"
        entry = json.loads(log_path.read_text().strip())
        ts = datetime.fromisoformat(entry["timestamp"])
        assert before <= ts <= after

    def test_record_total_is_sum(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=840, completion_tokens=210)

        log_path = tmp_path / "cost_log.jsonl"
        entry = json.loads(log_path.read_text().strip())
        assert entry["total"] == 1050


class TestCostStoreQueryEmpty:
    """query() on an empty or non-existent file returns zeroed response."""

    def test_query_on_nonexistent_file_returns_zeroed(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        result = store.query()

        assert result["total_tokens"] == 0
        assert result["by_persona"] == {}
        assert result["by_operation"] == {}

    def test_query_on_nonexistent_file_period_fields(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        result = store.query(start_date="2026-05-01", end_date="2026-05-10")

        assert result["period"]["start"] == "2026-05-01"
        assert result["period"]["end"] == "2026-05-10"

    def test_query_with_no_filters_empty_period_strings(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        result = store.query()
        assert result["period"]["start"] == ""
        assert result["period"]["end"] == ""

    def test_query_after_record_then_filter_out_all(self, tmp_path: Path):
        """Filter that matches nothing returns zeroed aggregates."""
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        result = store.query(persona="tpm")
        assert result["total_tokens"] == 0
        assert result["by_persona"] == {}


class TestCostStoreQueryNoFilters:
    """query() with no filters aggregates all entries."""

    def test_total_tokens_summed(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        store.record(persona="tpm", operation="retrieval",
                     prompt_tokens=200, completion_tokens=80)

        result = store.query()
        assert result["total_tokens"] == 430  # (100+50) + (200+80)

    def test_by_persona_accumulates_per_persona(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        store.record(persona="ops_eng", operation="retrieval",
                     prompt_tokens=200, completion_tokens=80)
        store.record(persona="tpm", operation="ingestion",
                     prompt_tokens=300, completion_tokens=100)

        result = store.query()
        assert "ops_eng" in result["by_persona"]
        assert "tpm" in result["by_persona"]
        ops_eng = result["by_persona"]["ops_eng"]
        assert ops_eng["prompt"] == 300       # 100 + 200
        assert ops_eng["completion"] == 130   # 50 + 80
        assert ops_eng["total"] == 430        # 150 + 280
        tpm = result["by_persona"]["tpm"]
        assert tpm["total"] == 400

    def test_by_operation_accumulates_per_operation(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        store.record(persona="tpm", operation="ingestion",
                     prompt_tokens=200, completion_tokens=100)
        store.record(persona="ops_eng", operation="retrieval",
                     prompt_tokens=50, completion_tokens=20)

        result = store.query()
        assert result["by_operation"]["ingestion"] == 450   # 150 + 300
        assert result["by_operation"]["retrieval"] == 70    # 50 + 20

    def test_single_entry_round_trip(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="pm", operation="synthesis",
                     prompt_tokens=500, completion_tokens=200,
                     skill_name="roadmap_summary")

        result = store.query()
        assert result["total_tokens"] == 700
        assert result["by_persona"]["pm"]["prompt"] == 500
        assert result["by_persona"]["pm"]["completion"] == 200
        assert result["by_persona"]["pm"]["total"] == 700
        assert result["by_operation"]["synthesis"] == 700


class TestCostStoreQueryPersonaFilter:
    """query(persona=...) includes only matching entries."""

    def test_persona_filter_includes_matching(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        store.record(persona="tpm", operation="ingestion",
                     prompt_tokens=200, completion_tokens=80)

        result = store.query(persona="ops_eng")
        assert result["total_tokens"] == 150
        assert "ops_eng" in result["by_persona"]
        assert "tpm" not in result["by_persona"]

    def test_persona_filter_excludes_all_when_no_match(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)

        result = store.query(persona="architect")
        assert result["total_tokens"] == 0

    def test_persona_filter_with_multiple_operations(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="ingestion",
                     prompt_tokens=100, completion_tokens=50)
        store.record(persona="ops_eng", operation="retrieval",
                     prompt_tokens=200, completion_tokens=80)
        store.record(persona="tpm", operation="synthesis",
                     prompt_tokens=500, completion_tokens=200)

        result = store.query(persona="ops_eng")
        assert result["total_tokens"] == 430
        assert "ingestion" in result["by_operation"]
        assert "retrieval" in result["by_operation"]
        assert "synthesis" not in result["by_operation"]


class TestCostStoreQuerySkillNameFilter:
    """query(skill_name=...) includes only matching entries."""

    def test_skill_name_filter_works(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="retrieval",
                     prompt_tokens=100, completion_tokens=50,
                     skill_name="incident_summary")
        store.record(persona="ops_eng", operation="synthesis",
                     prompt_tokens=200, completion_tokens=80,
                     skill_name="fleet_report")

        result = store.query(skill_name="incident_summary")
        assert result["total_tokens"] == 150

    def test_persona_and_skill_name_combined(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        store.record(persona="ops_eng", operation="retrieval",
                     prompt_tokens=100, completion_tokens=50,
                     skill_name="incident_summary")
        store.record(persona="tpm", operation="retrieval",
                     prompt_tokens=200, completion_tokens=80,
                     skill_name="incident_summary")
        store.record(persona="ops_eng", operation="synthesis",
                     prompt_tokens=300, completion_tokens=100,
                     skill_name="fleet_report")

        result = store.query(persona="ops_eng", skill_name="incident_summary")
        assert result["total_tokens"] == 150
        assert result["by_persona"]["ops_eng"]["total"] == 150


class TestCostStoreQueryDateFilter:
    """query(start_date=..., end_date=...) respects date boundaries."""

    def _write_entry_with_ts(self, log_path: Path, ts: str, persona: str,
                              operation: str, prompt: int, completion: int) -> None:
        """Directly write an entry with a specific timestamp (bypasses record())."""
        entry = {
            "timestamp": ts,
            "persona": persona,
            "operation": operation,
            "skill_name": "",
            "prompt": prompt,
            "completion": completion,
            "total": prompt + completion,
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def test_start_date_excludes_earlier_entries(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        log_path = tmp_path / "cost_log.jsonl"
        # Entry before start_date
        self._write_entry_with_ts(log_path, "2026-04-30T23:59:59+00:00",
                                  "ops_eng", "ingestion", 100, 50)
        # Entry on/after start_date
        self._write_entry_with_ts(log_path, "2026-05-01T00:00:00+00:00",
                                  "ops_eng", "ingestion", 200, 80)

        result = store.query(start_date="2026-05-01")
        assert result["total_tokens"] == 280   # only the second entry

    def test_end_date_excludes_later_entries(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        log_path = tmp_path / "cost_log.jsonl"
        # Entry on/before end_date
        self._write_entry_with_ts(log_path, "2026-05-10T23:59:59+00:00",
                                  "ops_eng", "retrieval", 100, 50)
        # Entry after end_date
        self._write_entry_with_ts(log_path, "2026-05-11T00:00:00+00:00",
                                  "ops_eng", "retrieval", 200, 80)

        result = store.query(end_date="2026-05-10")
        assert result["total_tokens"] == 150   # only the first entry

    def test_date_range_both_bounds(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        log_path = tmp_path / "cost_log.jsonl"
        self._write_entry_with_ts(log_path, "2026-04-29T12:00:00+00:00",
                                  "tpm", "ingestion", 100, 50)
        self._write_entry_with_ts(log_path, "2026-05-05T12:00:00+00:00",
                                  "tpm", "ingestion", 200, 80)
        self._write_entry_with_ts(log_path, "2026-05-15T12:00:00+00:00",
                                  "tpm", "ingestion", 300, 100)

        result = store.query(start_date="2026-05-01", end_date="2026-05-10")
        assert result["total_tokens"] == 280   # only middle entry

    def test_no_date_filter_returns_all(self, tmp_path: Path):
        store = CostStore(store_root=tmp_path)
        log_path = tmp_path / "cost_log.jsonl"
        self._write_entry_with_ts(log_path, "2025-01-01T00:00:00+00:00",
                                  "ops_eng", "ingestion", 100, 50)
        self._write_entry_with_ts(log_path, "2026-12-31T23:59:59+00:00",
                                  "ops_eng", "ingestion", 200, 80)

        result = store.query()
        assert result["total_tokens"] == 430


# ===========================================================================
# Ops route handler tests — using FastAPI TestClient
# ===========================================================================


def _build_app(cost_store=None, git_dir_exists=True, monkeypatch=None, env_build_sha=None):
    """Build a minimal FastAPI app with the ops router mounted.

    Args:
        cost_store:     CostStore instance or None to attach to app.state.
        git_dir_exists: If False, patch _REPO_ROOT so .git is not found.
        monkeypatch:    pytest monkeypatch fixture for env var injection.
        env_build_sha:  Value to set for KBF_BUILD_SHA env var.
    """
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi not installed")

    from framework.deploy.routes.ops import router, _PROCESS_START
    from framework.deploy.auth.consumer import ConsumerManifest
    from framework.deploy.auth.registry import ConsumerRegistry
    from framework.deploy.auth.middleware import bearer_auth_middleware

    app = FastAPI()
    app.middleware("http")(bearer_auth_middleware)
    app.include_router(router)

    # Wire cost_store onto app.state
    app.state.cost_store = cost_store

    # Build a minimal consumer registry with two consumers:
    #   admin-token → scopes: [admin, read, write]
    #   read-token  → scopes: [read]
    import hashlib
    import textwrap
    import tempfile

    with tempfile.TemporaryDirectory() as manifests_dir:
        manifests_path = Path(manifests_dir)
        (manifests_path / "admin-consumer.yaml").write_text(textwrap.dedent("""
            name: admin-consumer
            token: "admin-secret"
            scopes: [read, write, admin]
            personaAllowlist: []
            rpmCap: 120
            tokenBudgetPerRequest: 16000
        """))
        (manifests_path / "read-consumer.yaml").write_text(textwrap.dedent("""
            name: read-consumer
            token: "read-secret"
            scopes: [read]
            personaAllowlist: []
            rpmCap: 120
            tokenBudgetPerRequest: 16000
        """))
        registry = ConsumerRegistry(manifests_dir=manifests_path)

    app.state.consumer_registry = registry

    if env_build_sha is not None and monkeypatch is not None:
        monkeypatch.setenv("KBF_BUILD_SHA", env_build_sha)

    return app


class TestHealthzRoute:
    """GET /healthz — no auth required."""

    def test_healthz_returns_200_when_healthy(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_status_is_healthy(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        body = resp.json()
        assert body["status"] == "healthy"

    def test_healthz_checks_dict_present(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        body = resp.json()
        assert "checks" in body
        assert isinstance(body["checks"], dict)

    def test_healthz_camel_case_keys(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        body = resp.json()
        assert "uptimeSeconds" in body
        assert "uptime_seconds" not in body

    def test_healthz_version_field_present(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        body = resp.json()
        assert "version" in body
        assert body["version"] == "1.0.0"

    def test_healthz_no_auth_required(self, tmp_path):
        """No Authorization header → should still return 200 (not 401)."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")  # no headers
        assert resp.status_code != 401

    def test_healthz_uptime_seconds_is_non_negative_int(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        body = resp.json()
        assert isinstance(body["uptimeSeconds"], int)
        assert body["uptimeSeconds"] >= 0

    def test_healthz_git_check_present_in_checks(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        body = resp.json()
        assert "git" in body["checks"]


class TestVersionRoute:
    """GET /api/v1/version — no auth required."""

    def test_version_returns_200(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        assert resp.status_code == 200

    def test_version_no_auth_required(self):
        """No Authorization header → 200, not 401."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        assert resp.status_code != 401

    def test_version_body_has_required_fields(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        body = resp.json()
        assert "apiVersion" in body
        assert "schemaVersion" in body
        assert "buildSha" in body

    def test_version_api_version_is_v1(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        body = resp.json()
        assert body["apiVersion"] == "v1"

    def test_version_schema_version(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        body = resp.json()
        assert body["schemaVersion"] == "1.0.0"

    def test_version_build_sha_default_unknown(self, monkeypatch):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        monkeypatch.delenv("KBF_BUILD_SHA", raising=False)
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        body = resp.json()
        assert body["buildSha"] == "unknown"

    def test_version_build_sha_from_env(self, monkeypatch):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        monkeypatch.setenv("KBF_BUILD_SHA", "a3f1b2c9d4e5f6a7")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        body = resp.json()
        assert body["buildSha"] == "a3f1b2c9d4e5f6a7"

    def test_version_camel_case_keys(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/version")
        body = resp.json()
        assert "api_version" not in body
        assert "schema_version" not in body
        assert "build_sha" not in body


class TestCostMetricsRoute:
    """GET /api/v1/metrics/cost — requires admin scope."""

    def test_cost_metrics_with_admin_token_returns_200(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost",
                              headers={"Authorization": "Bearer admin-secret"})
        assert resp.status_code == 200

    def test_cost_metrics_with_read_only_token_returns_403(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost",
                              headers={"Authorization": "Bearer read-secret"})
        assert resp.status_code == 403

    def test_cost_metrics_without_auth_returns_401(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost")
        assert resp.status_code == 401

    def test_cost_metrics_body_shape(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        store.record("ops_eng", "ingestion", 100, 50)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost",
                              headers={"Authorization": "Bearer admin-secret"})
        body = resp.json()
        assert "period" in body
        assert "totalTokens" in body
        assert "byPersona" in body
        assert "byOperation" in body

    def test_cost_metrics_camel_case_keys(self, tmp_path):
        """Keys must be camelCase per OpenAPI spec."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        store.record("ops_eng", "ingestion", 100, 50)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost",
                              headers={"Authorization": "Bearer admin-secret"})
        body = resp.json()
        assert "total_tokens" not in body
        assert "by_persona" not in body
        assert "by_operation" not in body

    def test_cost_metrics_passes_persona_filter(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        store.record("ops_eng", "ingestion", 100, 50)
        store.record("tpm", "ingestion", 200, 80)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost?persona=ops_eng",
                              headers={"Authorization": "Bearer admin-secret"})
        body = resp.json()
        # Only ops_eng entries; total = 150
        assert body["totalTokens"] == 150

    def test_cost_metrics_with_cost_store_none_returns_zeroed(self, tmp_path):
        """When cost_store is None (not configured), return zeroed response."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        app = _build_app(cost_store=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost",
                              headers={"Authorization": "Bearer admin-secret"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalTokens"] == 0
        assert body["byPersona"] == {}
        assert body["byOperation"] == {}

    def test_cost_metrics_passes_date_range(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/metrics/cost?startDate=2026-05-01&endDate=2026-05-10",
                headers={"Authorization": "Bearer admin-secret"},
            )
        body = resp.json()
        assert body["period"]["start"] == "2026-05-01"
        assert body["period"]["end"] == "2026-05-10"

    def test_cost_metrics_total_tokens_correct(self, tmp_path):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")
        store = CostStore(store_root=tmp_path)
        store.record("ops_eng", "ingestion", 840, 210)
        store.record("tpm", "retrieval", 500, 150)
        app = _build_app(cost_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/metrics/cost",
                              headers={"Authorization": "Bearer admin-secret"})
        body = resp.json()
        assert body["totalTokens"] == 1700  # (840+210) + (500+150)
