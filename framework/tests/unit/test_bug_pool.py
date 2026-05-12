"""Unit tests for _init_bug_pool (DECISION-009).

Tests:
  - Returns None when bug_db section is absent from config (non-fatal)
  - Returns None when config file is missing (non-fatal)
  - Merge logic: bug_db.user overrides adb.admin_user; dsn falls back to adb.dsn
  - Merge logic: bug_db.wallet_path overrides adb.wallet_path when set
  - Merge logic: wallet_password_secret inherits from adb when not in bug_db
  - Returns None and does not raise when create_adb_pool fails
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, data: dict, env: str = "test") -> Path:
    """Write a YAML config file at <tmp_path>/framework/config/<env>.yaml.

    _init_bug_pool resolves config as:
        repo_root / "framework" / "config" / f"{env}.yaml"
    so we mirror that layout under tmp_path so that tmp_path acts as repo_root.
    Returns tmp_path (the repo_root).
    """
    cfg_dir = tmp_path / "framework" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / f"{env}.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    return tmp_path


def _call_init_bug_pool(repo_root: Path, kbf_env: str = "test",
                        create_pool_return=None, create_pool_raise=None):
    """Call _init_bug_pool with a mocked create_adb_pool.

    _resolve_secret is also mocked to return its argument unchanged so env
    vars do not need to be set in the test environment.

    Returns (result, captured_pool_config).  captured_pool_config is the
    dict passed to the mocked create_adb_pool call, or {} if not called.
    """
    from framework.deploy.mcp_server import _init_bug_pool

    captured: dict = {}

    def _mock_create(pool_config):
        captured.update(pool_config)
        if create_pool_raise:
            raise create_pool_raise
        return create_pool_return

    with patch("framework.deploy.mcp_server._resolve_secret",
               side_effect=lambda ref: ref or ""):
        with patch("framework.core.adb_pool.create_adb_pool",
                   side_effect=_mock_create):
            result = _init_bug_pool(repo_root, kbf_env)

    return result, captured


# ---------------------------------------------------------------------------
# Tests: absent / missing config
# ---------------------------------------------------------------------------

class TestBugPoolAbsentConfig:
    def test_returns_none_when_config_file_missing(self, tmp_path):
        """No config YAML at all → None (non-fatal)."""
        from framework.deploy.mcp_server import _init_bug_pool

        result = _init_bug_pool(tmp_path, "nonexistent")
        assert result is None

    def test_returns_none_when_bug_db_section_absent(self, tmp_path):
        """Config exists but has no bug_db key → None."""
        cfg = {
            "deployment_mode": "test",
            "adb": {
                "dsn":                    "my_service_low",
                "wallet_path":            "~/.adb/wallet",
                "wallet_password_secret": "env://WALLET_PASSWORD",
                "admin_user":             "Admin",
                "admin_password_secret":  "env://ADMIN_PW",
            },
        }
        _write_config(tmp_path, cfg)
        result, _ = _call_init_bug_pool(tmp_path, "test")
        assert result is None

    def test_returns_none_when_bug_db_section_empty(self, tmp_path):
        """Config has bug_db: {} (empty mapping) → None."""
        cfg = {
            "deployment_mode": "test",
            "adb": {"dsn": "svc_low", "admin_user": "Admin"},
            "bug_db": {},
        }
        _write_config(tmp_path, cfg)
        result, _ = _call_init_bug_pool(tmp_path, "test")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: merge logic
# ---------------------------------------------------------------------------

class TestBugPoolMergeLogic:
    """Verify that _init_bug_pool correctly inherits adb fields when bug_db
    does not specify them, and overrides them when it does."""

    def _setup(self, tmp_path: Path, adb: dict, bug_db: dict):
        """Write config and run _init_bug_pool, returning (result, captured)."""
        cfg = {
            "deployment_mode": "test",
            "adb": adb,
            "bug_db": bug_db,
        }
        _write_config(tmp_path, cfg)
        mock_pool = MagicMock()
        return _call_init_bug_pool(tmp_path, "test", create_pool_return=mock_pool)

    def test_dsn_falls_back_to_adb_when_not_in_bug_db(self, tmp_path):
        """bug_db has user but no dsn → dsn comes from adb."""
        adb = {
            "dsn":                    "adb_service_low",
            "wallet_path":            "~/.adb/wallet",
            "admin_user":             "Admin",
            "admin_password_secret":  "env://ADMIN_PW",
            "wallet_password_secret": "env://WALLET_PW",
        }
        bug_db = {
            "user":            "KBF_BUGS",
            "password_secret": "env://KBF_BUGS_PASSWORD",
        }
        result, captured = self._setup(tmp_path, adb, bug_db)

        assert result is not None, "Expected a pool to be returned"
        assert captured.get("adb", {}).get("service_name") == "adb_service_low"

    def test_user_overridden_by_bug_db(self, tmp_path):
        """bug_db.user overrides adb.admin_user."""
        adb = {
            "dsn":                    "svc_low",
            "wallet_path":            "~/.adb/wallet",
            "admin_user":             "Admin",
            "admin_password_secret":  "env://ADMIN_PW",
            "wallet_password_secret": "env://WALLET_PW",
        }
        bug_db = {
            "user":            "KBF_BUGS",
            "password_secret": "env://KBF_BUGS_PASSWORD",
        }
        result, captured = self._setup(tmp_path, adb, bug_db)

        assert result is not None
        assert captured.get("adb", {}).get("user") == "KBF_BUGS"

    def test_bug_db_dsn_overrides_adb_dsn(self, tmp_path):
        """When bug_db specifies its own dsn, it wins over adb.dsn."""
        adb = {
            "dsn":                    "adb_service_low",
            "wallet_path":            "~/.adb/wallet",
            "admin_user":             "Admin",
            "admin_password_secret":  "env://ADMIN_PW",
            "wallet_password_secret": "env://WALLET_PW",
        }
        bug_db = {
            "user":            "KBF_BUGS",
            "password_secret": "env://KBF_BUGS_PASSWORD",
            "dsn":             "bug_dedicated_service_low",
        }
        result, captured = self._setup(tmp_path, adb, bug_db)

        assert result is not None
        assert captured.get("adb", {}).get("service_name") == "bug_dedicated_service_low"

    def test_wallet_password_falls_back_to_adb(self, tmp_path):
        """bug_db has no wallet_password_secret → inherits from adb."""
        adb = {
            "dsn":                    "svc_low",
            "wallet_path":            "~/.adb/wallet",
            "admin_user":             "Admin",
            "admin_password_secret":  "env://ADMIN_PW",
            "wallet_password_secret": "env://WALLET_PW",
        }
        bug_db = {
            "user":            "KBF_BUGS",
            "password_secret": "env://KBF_BUGS_PASSWORD",
        }
        result, captured = self._setup(tmp_path, adb, bug_db)

        # _resolve_secret is mocked to return its argument unchanged, so the
        # wallet_password in pool_config["adb"] should equal the secret ref.
        assert result is not None
        assert captured.get("adb", {}).get("wallet_password") == "env://WALLET_PW"

    def test_wallet_path_falls_back_to_adb(self, tmp_path):
        """bug_db has no wallet_path → inherits from adb.wallet_path."""
        adb = {
            "dsn":                    "svc_low",
            "wallet_path":            "~/.adb/wallet",
            "admin_user":             "Admin",
            "admin_password_secret":  "env://ADMIN_PW",
            "wallet_password_secret": "env://WALLET_PW",
        }
        bug_db = {
            "user":            "KBF_BUGS",
            "password_secret": "env://KBF_BUGS_PASSWORD",
        }
        result, captured = self._setup(tmp_path, adb, bug_db)

        assert result is not None
        # wallet_path goes through Path.expanduser() — check the expanded value
        expected = str(Path("~/.adb/wallet").expanduser())
        assert captured.get("adb", {}).get("wallet_path") == expected

    def test_bug_db_wallet_path_overrides_adb(self, tmp_path):
        """bug_db.wallet_path overrides adb.wallet_path when set."""
        adb = {
            "dsn":                    "svc_low",
            "wallet_path":            "~/.adb/wallet",
            "admin_user":             "Admin",
            "admin_password_secret":  "env://ADMIN_PW",
            "wallet_password_secret": "env://WALLET_PW",
        }
        bug_db = {
            "user":            "KBF_BUGS",
            "password_secret": "env://KBF_BUGS_PASSWORD",
            "wallet_path":     "~/.adb/bug_wallet",
        }
        result, captured = self._setup(tmp_path, adb, bug_db)

        assert result is not None
        expected = str(Path("~/.adb/bug_wallet").expanduser())
        assert captured.get("adb", {}).get("wallet_path") == expected


# ---------------------------------------------------------------------------
# Tests: failure resilience
# ---------------------------------------------------------------------------

class TestBugPoolFailureResilience:
    def test_returns_none_when_create_pool_raises(self, tmp_path):
        """If create_adb_pool raises, _init_bug_pool returns None (non-fatal)."""
        cfg = {
            "deployment_mode": "test",
            "adb": {
                "dsn":                    "svc_low",
                "wallet_path":            "~/.adb/wallet",
                "admin_user":             "Admin",
                "admin_password_secret":  "env://ADMIN_PW",
                "wallet_password_secret": "env://WALLET_PW",
            },
            "bug_db": {
                "user":            "KBF_BUGS",
                "password_secret": "env://KBF_BUGS_PASSWORD",
            },
        }
        _write_config(tmp_path, cfg)

        result, _ = _call_init_bug_pool(
            tmp_path, "test",
            create_pool_raise=RuntimeError("oracledb unavailable"),
        )
        assert result is None

    def test_returns_none_when_create_pool_returns_none(self, tmp_path):
        """If create_adb_pool returns None, _init_bug_pool returns None."""
        cfg = {
            "deployment_mode": "test",
            "adb": {
                "dsn":                    "svc_low",
                "wallet_path":            "~/.adb/wallet",
                "admin_user":             "Admin",
                "admin_password_secret":  "env://ADMIN_PW",
                "wallet_password_secret": "env://WALLET_PW",
            },
            "bug_db": {
                "user":            "KBF_BUGS",
                "password_secret": "env://KBF_BUGS_PASSWORD",
            },
        }
        _write_config(tmp_path, cfg)

        result, _ = _call_init_bug_pool(
            tmp_path, "test",
            create_pool_return=None,  # explicitly None
        )
        assert result is None

    def test_does_not_raise(self, tmp_path):
        """_init_bug_pool must never raise regardless of what create_adb_pool does."""
        _write_config(tmp_path, {
            "deployment_mode": "test",
            "adb": {"dsn": "svc_low"},
            "bug_db": {"user": "KBF_BUGS", "password_secret": "env://X"},
        })

        # Should not raise even when create_adb_pool blows up
        result, _ = _call_init_bug_pool(
            tmp_path, "test",
            create_pool_raise=Exception("unexpected failure"),
        )
        assert result is None
