"""Tests for Track C: auth middleware, consumer manifest model, and consumer registry.

Coverage:
  - ConsumerRegistry loads *.yaml manifests from a temp directory
  - registry.lookup() with a valid token returns the correct ConsumerManifest
  - registry.lookup() with an invalid token returns None
  - ConsumerManifest.has_scope() returns True/False correctly
  - ConsumerManifest.allows_persona() with empty allowlist returns True for any persona
  - ConsumerManifest.allows_persona() with a specific allowlist returns True/False
  - RPM enforcement: first N requests pass, N+1 is rejected (low rpm_cap for speed)
  - registry honours pre-hashed tokenHash field (production path)
  - require_scope raises HTTPException(403), not a JSONResponse
"""
from __future__ import annotations

import hashlib
import textwrap
import time
from pathlib import Path

import pytest

from framework.deploy.auth.consumer import ConsumerManifest
from framework.deploy.auth.registry import ConsumerRegistry
from framework.deploy.auth.middleware import (
    _check_rpm,
    _reset_rpm_counters_for_testing,
    require_scope,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_manifest(tmp_path: Path, filename: str, content: str) -> None:
    (tmp_path / filename).write_text(textwrap.dedent(content))


def _build_registry(tmp_path: Path) -> ConsumerRegistry:
    return ConsumerRegistry(manifests_dir=tmp_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manifests_dir(tmp_path: Path) -> Path:
    """Temp directory with two consumer manifests."""
    # Full-access dev consumer (plaintext token)
    _write_manifest(tmp_path, "dev-local.yaml", """
        name: dev-local
        token: "dev-only-secret"
        scopes: [read, write]
        personaAllowlist: []
        rpmCap: 120
        tokenBudgetPerRequest: 16000
    """)

    # Read-only consumer locked to two personas (pre-hashed token)
    token = "read-only-secret"
    token_hash = _sha256(token)
    _write_manifest(tmp_path, "readonly-consumer.yaml", f"""
        name: readonly-consumer
        tokenHash: "{token_hash}"
        scopes: [read]
        personaAllowlist: [pm, tpm]
        rpmCap: 10
        tokenBudgetPerRequest: 4000
        userId: explicit-user-id
    """)

    # Low-rpm consumer for RPM enforcement tests
    _write_manifest(tmp_path, "low-rpm.yaml", """
        name: low-rpm
        token: "low-rpm-token"
        scopes: [read]
        personaAllowlist: []
        rpmCap: 3
        tokenBudgetPerRequest: 8000
    """)

    return tmp_path


@pytest.fixture
def registry(manifests_dir: Path) -> ConsumerRegistry:
    return ConsumerRegistry(manifests_dir=manifests_dir)


# ---------------------------------------------------------------------------
# ConsumerRegistry loading tests
# ---------------------------------------------------------------------------

class TestConsumerRegistryLoading:
    def test_loads_all_yaml_files(self, registry: ConsumerRegistry):
        # 3 manifests were written by the fixture
        assert registry.consumer_count == 3

    def test_ignores_non_yaml_files(self, tmp_path: Path):
        _write_manifest(tmp_path, "dev.yaml", """
            name: dev
            token: "tok"
            scopes: [read]
            personaAllowlist: []
            rpmCap: 60
            tokenBudgetPerRequest: 8000
        """)
        (tmp_path / "notes.txt").write_text("ignored")
        (tmp_path / "schema.json").write_text("{}")
        reg = _build_registry(tmp_path)
        assert reg.consumer_count == 1

    def test_missing_dir_logs_warning_does_not_raise(self, tmp_path: Path):
        non_existent = tmp_path / "does_not_exist"
        reg = ConsumerRegistry(manifests_dir=non_existent)
        assert reg.consumer_count == 0

    def test_plaintext_token_is_hashed(self, manifests_dir: Path, registry: ConsumerRegistry):
        consumer = registry.lookup("dev-only-secret")
        assert consumer is not None
        assert consumer.token_hash == _sha256("dev-only-secret")

    def test_pre_hashed_token_used_directly(self, manifests_dir: Path, registry: ConsumerRegistry):
        consumer = registry.lookup("read-only-secret")
        assert consumer is not None
        assert consumer.token_hash == _sha256("read-only-secret")

    def test_fields_parsed_correctly(self, registry: ConsumerRegistry):
        consumer = registry.lookup("dev-only-secret")
        assert consumer is not None
        assert consumer.name == "dev-local"
        assert consumer.scopes == ["read", "write"]
        assert consumer.persona_allowlist == []
        assert consumer.rpm_cap == 120
        assert consumer.token_budget_per_request == 16000

    def test_explicit_user_id_honoured(self, registry: ConsumerRegistry):
        consumer = registry.lookup("read-only-secret")
        assert consumer is not None
        assert consumer.user_id == "explicit-user-id"

    def test_default_user_id_derived_from_stem(self, registry: ConsumerRegistry):
        consumer = registry.lookup("dev-only-secret")
        assert consumer is not None
        # Should be first 16 hex chars of SHA-1("dev-local")
        expected = hashlib.sha1("dev-local".encode()).hexdigest()[:16]
        assert consumer.user_id == expected


# ---------------------------------------------------------------------------
# ConsumerRegistry.lookup() tests
# ---------------------------------------------------------------------------

class TestConsumerRegistryLookup:
    def test_valid_token_returns_manifest(self, registry: ConsumerRegistry):
        result = registry.lookup("dev-only-secret")
        assert result is not None
        assert isinstance(result, ConsumerManifest)
        assert result.name == "dev-local"

    def test_invalid_token_returns_none(self, registry: ConsumerRegistry):
        result = registry.lookup("this-token-does-not-exist")
        assert result is None

    def test_empty_token_returns_none(self, registry: ConsumerRegistry):
        result = registry.lookup("")
        assert result is None

    def test_pre_hashed_token_lookup(self, registry: ConsumerRegistry):
        # The YAML has tokenHash; lookup with plaintext should still work
        result = registry.lookup("read-only-secret")
        assert result is not None
        assert result.name == "readonly-consumer"

    def test_lookup_is_case_sensitive(self, registry: ConsumerRegistry):
        # Tokens are case-sensitive (SHA-256 is byte-exact)
        assert registry.lookup("Dev-Only-Secret") is None
        assert registry.lookup("dev-only-secret") is not None


# ---------------------------------------------------------------------------
# ConsumerManifest.has_scope() tests
# ---------------------------------------------------------------------------

class TestHasScope:
    def _consumer(self, scopes: list[str]) -> ConsumerManifest:
        return ConsumerManifest(
            name="test",
            token_hash="deadbeef",
            scopes=scopes,
            persona_allowlist=[],
            rpm_cap=60,
            token_budget_per_request=8000,
            user_id="uid",
        )

    def test_has_scope_true_when_present(self):
        c = self._consumer(["read", "write"])
        assert c.has_scope("read") is True
        assert c.has_scope("write") is True

    def test_has_scope_false_when_absent(self):
        c = self._consumer(["read"])
        assert c.has_scope("write") is False
        assert c.has_scope("admin") is False

    def test_has_scope_empty_scopes(self):
        c = self._consumer([])
        assert c.has_scope("read") is False

    def test_has_scope_admin_only(self):
        c = self._consumer(["admin"])
        assert c.has_scope("admin") is True
        assert c.has_scope("read") is False


# ---------------------------------------------------------------------------
# ConsumerManifest.allows_persona() tests
# ---------------------------------------------------------------------------

class TestAllowsPersona:
    def _consumer(self, allowlist: list[str]) -> ConsumerManifest:
        return ConsumerManifest(
            name="test",
            token_hash="deadbeef",
            scopes=["read"],
            persona_allowlist=allowlist,
            rpm_cap=60,
            token_budget_per_request=8000,
            user_id="uid",
        )

    def test_empty_allowlist_allows_any_persona(self):
        c = self._consumer([])
        assert c.allows_persona("pm") is True
        assert c.allows_persona("tpm") is True
        assert c.allows_persona("ops-eng") is True
        assert c.allows_persona("unknown-persona") is True

    def test_specific_allowlist_allows_listed_personas(self):
        c = self._consumer(["pm", "tpm"])
        assert c.allows_persona("pm") is True
        assert c.allows_persona("tpm") is True

    def test_specific_allowlist_blocks_unlisted_personas(self):
        c = self._consumer(["pm", "tpm"])
        assert c.allows_persona("ops-eng") is False
        assert c.allows_persona("architect") is False
        assert c.allows_persona("") is False

    def test_single_entry_allowlist(self):
        c = self._consumer(["ops-eng"])
        assert c.allows_persona("ops-eng") is True
        assert c.allows_persona("pm") is False

    def test_registry_manifest_allowlist_parsed(self, registry: ConsumerRegistry):
        consumer = registry.lookup("read-only-secret")
        assert consumer is not None
        assert consumer.allows_persona("pm") is True
        assert consumer.allows_persona("tpm") is True
        assert consumer.allows_persona("ops-eng") is False


# ---------------------------------------------------------------------------
# RPM enforcement tests
# ---------------------------------------------------------------------------

class TestRpmEnforcement:
    """Uses the low-rpm consumer (rpmCap=3) to test the sliding window.

    _reset_rpm_counters_for_testing() clears the in-memory state so tests
    are hermetic even though the counter is module-level.
    """

    def _low_rpm_consumer(self) -> ConsumerManifest:
        return ConsumerManifest(
            name="low-rpm-test",
            token_hash="abc",
            scopes=["read"],
            persona_allowlist=[],
            rpm_cap=3,
            token_budget_per_request=8000,
            user_id="uid",
        )

    def setup_method(self):
        _reset_rpm_counters_for_testing()

    def test_first_n_requests_pass(self):
        c = self._low_rpm_consumer()
        for _ in range(3):
            assert _check_rpm(c) is True, "Expected request within rpm_cap to be allowed"

    def test_n_plus_one_request_is_rejected(self):
        c = self._low_rpm_consumer()
        for _ in range(3):
            _check_rpm(c)
        assert _check_rpm(c) is False, "Expected request beyond rpm_cap to be rejected"

    def test_different_consumers_have_independent_counters(self):
        c_a = ConsumerManifest(
            name="rpm-a", token_hash="aaa", scopes=["read"],
            persona_allowlist=[], rpm_cap=2, token_budget_per_request=8000, user_id="u1",
        )
        c_b = ConsumerManifest(
            name="rpm-b", token_hash="bbb", scopes=["read"],
            persona_allowlist=[], rpm_cap=2, token_budget_per_request=8000, user_id="u2",
        )
        # Exhaust c_a
        _check_rpm(c_a)
        _check_rpm(c_a)
        assert _check_rpm(c_a) is False  # c_a over limit

        # c_b still has capacity
        assert _check_rpm(c_b) is True

    def test_rpm_counter_via_registry_manifest(self, registry: ConsumerRegistry):
        consumer = registry.lookup("low-rpm-token")
        assert consumer is not None
        assert consumer.rpm_cap == 3

        for _ in range(3):
            assert _check_rpm(consumer) is True
        assert _check_rpm(consumer) is False


# ---------------------------------------------------------------------------
# require_scope() raises HTTPException (not a return value)
# ---------------------------------------------------------------------------

class TestRequireScope:
    def _consumer(self, scopes: list[str]) -> ConsumerManifest:
        return ConsumerManifest(
            name="test",
            token_hash="hash",
            scopes=scopes,
            persona_allowlist=[],
            rpm_cap=60,
            token_budget_per_request=8000,
            user_id="uid",
        )

    def test_require_scope_passes_silently_when_scope_present(self):
        from fastapi import HTTPException
        c = self._consumer(["read", "write"])
        # Should not raise
        require_scope(c, "read")
        require_scope(c, "write")

    def test_require_scope_raises_http_exception_403_when_missing(self):
        from fastapi import HTTPException
        c = self._consumer(["read"])
        with pytest.raises(HTTPException) as exc_info:
            require_scope(c, "write")
        assert exc_info.value.status_code == 403

    def test_require_scope_raises_http_exception_not_json_response(self):
        """Confirm the raise type is HTTPException, not JSONResponse (route handlers need this)."""
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse
        c = self._consumer(["read"])
        with pytest.raises(HTTPException):
            require_scope(c, "admin")
        # Validate it is NOT a JSONResponse by checking raise type
        try:
            require_scope(c, "admin")
        except HTTPException:
            pass  # correct
        except Exception as exc:
            pytest.fail(f"Expected HTTPException, got {type(exc).__name__}: {exc}")

    def test_require_scope_error_detail_structure(self):
        from fastapi import HTTPException
        c = self._consumer(["read"])
        with pytest.raises(HTTPException) as exc_info:
            require_scope(c, "write")
        detail = exc_info.value.detail
        assert "error" in detail
        assert detail["error"]["code"] == "permission_denied"
        assert "write" in detail["error"]["message"]
