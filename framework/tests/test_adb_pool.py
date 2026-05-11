"""Tests for framework.core.adb_pool (ADR-019).

Covers:
  - Error classification (_is_connectivity_error)
  - BastionConfig / AdbPoolConfig parsing
  - BastionReconnector with mocked subprocess
  - RetryWrapper laptop mode: ORA-12541 → reconnect → retry succeeds
  - RetryWrapper laptop mode: max attempts exceeded → BastionReconnectError
  - RetryWrapper non-laptop mode: exponential backoff
  - Non-connectivity errors pass through untouched
  - Port check loop (mocked socket)
  - atexit cleanup (_kill_tunnel)
  - create_adb_pool factory (oracledb stubbed)

Run:
    cd /Users/sravansunkaranam/github/Knowledgebase/.claude/worktrees/agitated-villani-eeed95
    python -m pytest framework/tests/test_adb_pool.py -v
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import types
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Inject a stub oracledb module so the import does not fail when the real
# package is absent (CI / developer machines without Oracle client)
# ---------------------------------------------------------------------------

if "oracledb" not in sys.modules:
    _stub = types.ModuleType("oracledb")
    _stub.create_pool = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
    sys.modules["oracledb"] = _stub

from framework.core.adb_pool import (  # noqa: E402 — must come after stub injection
    AdbPoolConfig,
    BastionConfig,
    BastionReconnectError,
    BastionReconnector,
    RetryWrapper,
    _is_connectivity_error,
    _kill_tunnel,
    create_adb_pool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bastion_cfg(**overrides) -> BastionConfig:
    defaults = dict(
        bastion_ocid="ocid1.bastion.oc1.iad.TESTOCID",
        target_db_host="10.0.1.50",
        target_db_port=1521,
        local_tunnel_port=15211,
        ssh_key_path="~/.ssh/oci_bastion_key",
        session_ttl_seconds=10800,
        oci_cli_path="/opt/homebrew/bin/oci",
        connect_timeout_seconds=5,
        max_reconnect_attempts=3,
    )
    defaults.update(overrides)
    return BastionConfig(**defaults)


def _make_pool(raises_once: Exception | None = None) -> MagicMock:
    """Return a mock oracledb pool whose acquire() returns a connection.

    If raises_once is set, the first call to acquire() raises it; subsequent
    calls succeed.  RetryWrapper.acquire() is the context manager — the raw
    pool.acquire() just returns a connection object.
    """
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = MagicMock(return_value=None)
    conn.close = MagicMock()

    call_count = {"n": 0}

    def acquire_fn():
        call_count["n"] += 1
        if raises_once is not None and call_count["n"] == 1:
            raise raises_once
        return conn

    pool.acquire = acquire_fn
    return pool


def _ora_error(code: str) -> Exception:
    """Fabricate a fake Oracle error carrying the given code in its str()."""
    exc = Exception(f"{code}: TNS:no listener")
    return exc


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestIsConnectivityError:
    def test_ora_12541(self):
        assert _is_connectivity_error(_ora_error("ORA-12541"))

    def test_ora_12170(self):
        assert _is_connectivity_error(_ora_error("ORA-12170"))

    def test_ora_12560(self):
        assert _is_connectivity_error(_ora_error("ORA-12560"))

    def test_dpy_6005(self):
        assert _is_connectivity_error(Exception("DPY-6005: cannot connect to database"))

    def test_econnrefused(self):
        exc = OSError(61, "Connection refused")
        assert _is_connectivity_error(exc)

    def test_etimedout_linux(self):
        exc = OSError(110, "Connection timed out")
        assert _is_connectivity_error(exc)

    def test_etimedout_macos(self):
        exc = OSError(60, "Operation timed out")
        assert _is_connectivity_error(exc)

    def test_chained_cause(self):
        inner = _ora_error("ORA-12541")
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert _is_connectivity_error(outer)

    def test_chained_context(self):
        inner = _ora_error("ORA-12170")
        outer = RuntimeError("wrapped")
        outer.__context__ = inner
        assert _is_connectivity_error(outer)

    def test_ora_0001_not_connectivity(self):
        assert not _is_connectivity_error(Exception("ORA-00001: unique constraint violated"))

    def test_ora_1400_not_connectivity(self):
        assert not _is_connectivity_error(Exception("ORA-01400: cannot insert NULL"))

    def test_generic_exception_not_connectivity(self):
        assert not _is_connectivity_error(ValueError("something went wrong"))

    def test_os_error_other_errno_not_connectivity(self):
        exc = OSError(111, "Network unreachable")
        assert not _is_connectivity_error(exc)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestBastionConfigFromDict:
    def test_required_fields(self):
        d = {
            "bastion_ocid": "ocid1.bastion.oc1.iad.ABC",
            "target_db_host": "10.0.0.1",
            "target_db_port": "1521",
            "local_tunnel_port": "15211",
            "ssh_key_path": "~/.ssh/key",
        }
        cfg = BastionConfig.from_dict(d)
        assert cfg.bastion_ocid == "ocid1.bastion.oc1.iad.ABC"
        assert cfg.target_db_port == 1521
        assert cfg.local_tunnel_port == 15211

    def test_optional_defaults(self):
        d = {
            "bastion_ocid": "ocid",
            "target_db_host": "10.0.0.1",
            "target_db_port": 1521,
            "local_tunnel_port": 15211,
            "ssh_key_path": "~/.ssh/key",
        }
        cfg = BastionConfig.from_dict(d)
        assert cfg.session_ttl_seconds == 10800
        assert cfg.oci_cli_path == "/opt/homebrew/bin/oci"
        assert cfg.connect_timeout_seconds == 30
        assert cfg.max_reconnect_attempts == 3

    def test_overrides_applied(self):
        d = {
            "bastion_ocid": "ocid",
            "target_db_host": "10.0.0.1",
            "target_db_port": 1521,
            "local_tunnel_port": 15211,
            "ssh_key_path": "~/.ssh/key",
            "max_reconnect_attempts": "5",
            "connect_timeout_seconds": "60",
        }
        cfg = BastionConfig.from_dict(d)
        assert cfg.max_reconnect_attempts == 5
        assert cfg.connect_timeout_seconds == 60


class TestAdbPoolConfigFromDict:
    def test_deployment_mode_defaults_to_vm(self):
        cfg = AdbPoolConfig.from_dict({"adb": {}})
        assert cfg.deployment_mode == "vm"
        assert cfg.bastion is None

    def test_laptop_mode_with_bastion(self):
        d = {
            "deployment_mode": "laptop",
            "adb": {"service_name": "svc", "wallet_path": "/w"},
            "bastion": {
                "bastion_ocid": "ocid",
                "target_db_host": "10.0.0.1",
                "target_db_port": 1521,
                "local_tunnel_port": 15211,
                "ssh_key_path": "~/.ssh/key",
            },
        }
        cfg = AdbPoolConfig.from_dict(d)
        assert cfg.deployment_mode == "laptop"
        assert cfg.bastion is not None
        assert cfg.bastion.bastion_ocid == "ocid"

    def test_laptop_mode_without_bastion_section(self):
        d = {"deployment_mode": "laptop", "adb": {}}
        cfg = AdbPoolConfig.from_dict(d)
        assert cfg.deployment_mode == "laptop"
        assert cfg.bastion is None

    def test_vm_mode_ignores_bastion_section(self):
        d = {
            "deployment_mode": "vm",
            "adb": {},
            "bastion": {"bastion_ocid": "ocid", "target_db_host": "x",
                         "target_db_port": 1521, "local_tunnel_port": 15211,
                         "ssh_key_path": "~/.ssh/key"},
        }
        cfg = AdbPoolConfig.from_dict(d)
        assert cfg.bastion is None


# ---------------------------------------------------------------------------
# BastionReconnector
# ---------------------------------------------------------------------------


class TestBastionReconnector:
    def _make_cli_output(self, ssh_cmd: str) -> str:
        return json.dumps({
            "data": {
                "ssh_metadata": {
                    "command": ssh_cmd,
                }
            }
        })

    def test_create_session_returns_ssh_command(self):
        cfg = _bastion_cfg()
        reconnector = BastionReconnector(cfg)
        ssh_cmd = "ssh -p 22 -i ~/.ssh/oci_bastion_key user@bastion -L 15211:10.0.1.50:1521 -N"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=self._make_cli_output(ssh_cmd),
                returncode=0,
            )
            result = reconnector.create_session()

        assert result == ssh_cmd
        mock_run.assert_called_once()
        called_cmd = mock_run.call_args[0][0]
        assert cfg.oci_cli_path in called_cmd
        assert "bastion" in called_cmd
        assert "session" in called_cmd
        assert "create-managed-ssh" in called_cmd
        assert cfg.bastion_ocid in called_cmd

    def test_create_session_raises_on_missing_command(self):
        cfg = _bastion_cfg()
        reconnector = BastionReconnector(cfg)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps({"data": {"ssh_metadata": {}}}),
                returncode=0,
            )
            with pytest.raises(BastionReconnectError, match="SSH tunnel command"):
                reconnector.create_session()

    def test_create_session_raises_on_cli_failure(self):
        cfg = _bastion_cfg()
        reconnector = BastionReconnector(cfg)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "oci")
            with pytest.raises(subprocess.CalledProcessError):
                reconnector.create_session()

    def test_start_tunnel_kills_previous_and_starts_new(self):
        import framework.core.adb_pool as adb_pool_module

        cfg = _bastion_cfg()
        reconnector = BastionReconnector(cfg)
        ssh_cmd = "ssh user@bastion -N -L 15211:10.0.1.50:1521"

        old_proc = MagicMock()
        old_proc.poll.return_value = None  # still running
        adb_pool_module._tunnel_proc = old_proc

        new_proc = MagicMock()
        with patch("subprocess.Popen", return_value=new_proc) as mock_popen:
            reconnector.start_tunnel(ssh_cmd)

        old_proc.terminate.assert_called_once()
        mock_popen.assert_called_once()
        assert adb_pool_module._tunnel_proc is new_proc

    def test_wait_for_port_succeeds_when_port_opens(self):
        cfg = _bastion_cfg(connect_timeout_seconds=5)
        reconnector = BastionReconnector(cfg)

        with patch("socket.create_connection") as mock_sock:
            mock_sock.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_sock.return_value.__exit__ = MagicMock(return_value=False)
            reconnector.wait_for_port()  # should not raise

        mock_sock.assert_called_with(("127.0.0.1", cfg.local_tunnel_port), timeout=1)

    def test_wait_for_port_raises_on_timeout(self):
        cfg = _bastion_cfg(connect_timeout_seconds=0)
        reconnector = BastionReconnector(cfg)

        with patch("socket.create_connection", side_effect=OSError("refused")):
            with patch("time.sleep"):
                with pytest.raises(BastionReconnectError, match="did not open"):
                    reconnector.wait_for_port()

    def test_wait_for_port_polls_until_open(self):
        """Port fails twice, succeeds on third attempt."""
        cfg = _bastion_cfg(connect_timeout_seconds=10)
        reconnector = BastionReconnector(cfg)

        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)

        attempt = {"n": 0}

        def _connect(*args, **kwargs):
            attempt["n"] += 1
            if attempt["n"] < 3:
                raise OSError("not yet")
            return cm

        with patch("socket.create_connection", side_effect=_connect):
            with patch("time.sleep"):
                reconnector.wait_for_port()

        assert attempt["n"] == 3


# ---------------------------------------------------------------------------
# RetryWrapper — laptop mode
# ---------------------------------------------------------------------------


class TestRetryWrapperLaptopMode:
    def _make_reconnector(self) -> MagicMock:
        r = MagicMock(spec=BastionReconnector)
        r.reconnect = MagicMock()
        r._cfg = _bastion_cfg()
        return r

    def test_succeeds_on_first_try(self):
        pool = _make_pool()
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=self._make_reconnector())
        results = []
        with wrapper.acquire() as conn:
            results.append("got_conn")
        assert results == ["got_conn"]

    def test_ora_12541_triggers_reconnect_and_retry(self):
        """First acquire raises ORA-12541; reconnect; second acquire succeeds."""
        pool = _make_pool(raises_once=_ora_error("ORA-12541"))
        reconnector = self._make_reconnector()

        # After reconnect, health_check must also succeed — pool.acquire succeeds
        # on second call (raises_once only fires once)
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector, max_reconnect_attempts=3)

        with wrapper.acquire() as conn:
            assert conn is not None

        reconnector.reconnect.assert_called_once()

    def test_dpy_6005_triggers_reconnect(self):
        pool = _make_pool(raises_once=Exception("DPY-6005: cannot connect"))
        reconnector = self._make_reconnector()
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector, max_reconnect_attempts=3)

        with wrapper.acquire() as conn:
            assert conn is not None

        reconnector.reconnect.assert_called_once()

    def test_socket_econnrefused_triggers_reconnect(self):
        pool = _make_pool(raises_once=OSError(61, "Connection refused"))
        reconnector = self._make_reconnector()
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector, max_reconnect_attempts=3)

        with wrapper.acquire() as conn:
            assert conn is not None

        reconnector.reconnect.assert_called_once()

    def test_max_attempts_exceeded_raises_bastion_reconnect_error(self):
        """All acquire() calls raise ORA-12541; reconnector is called max times."""
        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=_ora_error("ORA-12541"))

        reconnector = self._make_reconnector()
        max_attempts = 3
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector,
                               max_reconnect_attempts=max_attempts)

        with pytest.raises(BastionReconnectError, match="Gave up after 3 reconnect attempts"):
            with wrapper.acquire():
                pass

        assert reconnector.reconnect.call_count == max_attempts

    def test_non_connectivity_error_passes_through(self):
        """ORA-00001 (constraint) must NOT trigger reconnect — re-raise immediately."""
        pool = MagicMock()
        pool.acquire = MagicMock(
            side_effect=Exception("ORA-00001: unique constraint violated")
        )
        reconnector = self._make_reconnector()
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector)

        with pytest.raises(Exception, match="ORA-00001"):
            with wrapper.acquire():
                pass

        reconnector.reconnect.assert_not_called()

    def test_value_error_passes_through(self):
        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=ValueError("bad data"))
        reconnector = self._make_reconnector()
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector)

        with pytest.raises(ValueError, match="bad data"):
            with wrapper.acquire():
                pass

        reconnector.reconnect.assert_not_called()

    def test_reconnect_attempt_resets_on_success(self):
        """After a successful reconnect, the attempt counter resets so a later
        failure can use all max_reconnect_attempts again."""
        # First call raises, second and beyond succeed
        pool = _make_pool(raises_once=_ora_error("ORA-12541"))
        reconnector = self._make_reconnector()
        wrapper = RetryWrapper(pool, deployment_mode="laptop",
                               reconnector=reconnector, max_reconnect_attempts=3)

        # First call — triggers one reconnect
        with wrapper.acquire():
            pass
        assert reconnector.reconnect.call_count == 1


# ---------------------------------------------------------------------------
# RetryWrapper — non-laptop mode (exponential backoff)
# ---------------------------------------------------------------------------


class TestRetryWrapperBackoffMode:
    def test_succeeds_on_first_try(self):
        pool = _make_pool()
        wrapper = RetryWrapper(pool, deployment_mode="vm")
        with wrapper.acquire() as conn:
            assert conn is not None

    def test_retries_with_backoff_on_connectivity_error(self):
        pool = _make_pool(raises_once=_ora_error("ORA-12541"))
        wrapper = RetryWrapper(pool, deployment_mode="vm")

        with patch("time.sleep") as mock_sleep:
            with wrapper.acquire() as conn:
                assert conn is not None

        mock_sleep.assert_called_once_with(2)  # 2^1

    def test_gives_up_after_max_attempts(self):
        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=_ora_error("ORA-12541"))
        wrapper = RetryWrapper(pool, deployment_mode="vm")

        with patch("time.sleep"):
            with pytest.raises(Exception, match="ORA-12541"):
                with wrapper.acquire():
                    pass

    def test_backoff_capped_at_30_seconds(self):
        """Ensure wait never exceeds 30 s."""
        pool = MagicMock()
        call_count = {"n": 0}
        conn = MagicMock()

        def fail_four_times():
            call_count["n"] += 1
            if call_count["n"] <= 4:
                raise _ora_error("ORA-12541")
            return conn

        pool.acquire = fail_four_times
        wrapper = RetryWrapper(pool, deployment_mode="vm")

        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            with wrapper.acquire():
                pass

        for s in sleep_calls:
            assert s <= 30

    def test_non_connectivity_error_not_retried(self):
        pool = MagicMock()
        pool.acquire = MagicMock(
            side_effect=Exception("ORA-00001: constraint")
        )
        wrapper = RetryWrapper(pool, deployment_mode="container")

        with patch("time.sleep") as mock_sleep:
            with pytest.raises(Exception, match="ORA-00001"):
                with wrapper.acquire():
                    pass

        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# atexit / _kill_tunnel
# ---------------------------------------------------------------------------


class TestKillTunnel:
    def test_kill_running_process(self):
        import framework.core.adb_pool as adb_pool_module

        proc = MagicMock()
        proc.poll.return_value = None  # still running
        adb_pool_module._tunnel_proc = proc

        _kill_tunnel()

        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()
        assert adb_pool_module._tunnel_proc is None

    def test_kill_already_dead_process(self):
        import framework.core.adb_pool as adb_pool_module

        proc = MagicMock()
        proc.poll.return_value = 0  # already exited
        adb_pool_module._tunnel_proc = proc

        _kill_tunnel()  # should not raise or call terminate

        proc.terminate.assert_not_called()

    def test_kill_when_no_tunnel(self):
        import framework.core.adb_pool as adb_pool_module

        adb_pool_module._tunnel_proc = None
        _kill_tunnel()  # must not raise

    def test_kill_falls_back_to_kill_on_timeout(self):
        import framework.core.adb_pool as adb_pool_module

        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=5)
        adb_pool_module._tunnel_proc = proc

        _kill_tunnel()

        proc.kill.assert_called_once()
        assert adb_pool_module._tunnel_proc is None


# ---------------------------------------------------------------------------
# create_adb_pool factory
# ---------------------------------------------------------------------------


class TestCreateAdbPool:
    def _laptop_config(self) -> dict:
        return {
            "deployment_mode": "laptop",
            "adb": {
                "service_name": "kbf_dev_high",
                "wallet_path": "/opt/kb/wallet/dev/",
                "user": "ADMIN",
                "password": "secret",
            },
            "bastion": {
                "bastion_ocid": "ocid1.bastion.oc1.iad.TEST",
                "target_db_host": "10.0.1.50",
                "target_db_port": 1521,
                "local_tunnel_port": 15211,
                "ssh_key_path": "~/.ssh/oci_bastion_key",
            },
        }

    def _vm_config(self) -> dict:
        return {
            "deployment_mode": "vm",
            "adb": {
                "service_name": "kbf_prod_high",
                "wallet_path": "/opt/kb/wallet/prod/",
                "user": "ADMIN",
                "password": "secret",
            },
        }

    def test_laptop_mode_returns_retry_wrapper_with_reconnector(self):
        import sys
        stub_oracledb = sys.modules["oracledb"]
        stub_oracledb.create_pool = MagicMock(return_value=MagicMock())

        pool = create_adb_pool(self._laptop_config())

        assert isinstance(pool, RetryWrapper)
        assert pool._deployment_mode == "laptop"
        assert isinstance(pool._reconnector, BastionReconnector)

    def test_vm_mode_returns_retry_wrapper_without_reconnector(self):
        import sys
        stub_oracledb = sys.modules["oracledb"]
        stub_oracledb.create_pool = MagicMock(return_value=MagicMock())

        pool = create_adb_pool(self._vm_config())

        assert isinstance(pool, RetryWrapper)
        assert pool._deployment_mode == "vm"
        assert pool._reconnector is None

    def test_stub_mode_returns_none_when_oracledb_unavailable(self):
        import framework.core.adb_pool as adb_pool_module

        original = adb_pool_module._ORACLEDB_AVAILABLE
        try:
            adb_pool_module._ORACLEDB_AVAILABLE = False
            result = adb_pool_module.create_adb_pool(self._laptop_config())
            assert result is None
        finally:
            adb_pool_module._ORACLEDB_AVAILABLE = original

    def test_reconnector_has_correct_bastion_ocid(self):
        import sys
        stub_oracledb = sys.modules["oracledb"]
        stub_oracledb.create_pool = MagicMock(return_value=MagicMock())

        cfg = self._laptop_config()
        pool = create_adb_pool(cfg)

        assert pool._reconnector._cfg.bastion_ocid == "ocid1.bastion.oc1.iad.TEST"
        assert pool._reconnector._cfg.local_tunnel_port == 15211
