"""ADB connection pool factory with bastion auto-reconnect (ADR-019).

Two modes controlled by deployment_mode config field:

  laptop    — when the SSH bastion tunnel expires (ORA-12541, ORA-12170,
               ORA-12560, DPY-6005, socket errors), the wrapper automatically
               creates a new OCI Bastion session, restarts the SSH tunnel, and
               retries the original operation. Up to max_reconnect_attempts.

  vm / container / anything else — standard exponential backoff only
               (max 5 attempts, 2^n seconds capped at 30 s). No OCI CLI calls.

Consumers call create_adb_pool(config_dict) and receive either a raw
oracledb pool (non-laptop) or a RetryWrapper around one (laptop). The
pool.acquire() context-manager protocol is preserved, so callers like
adb_store.py need no changes.
"""
from __future__ import annotations

import atexit
import json
import logging
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional oracledb import
# ---------------------------------------------------------------------------

try:
    import oracledb  # type: ignore
    _ORACLEDB_AVAILABLE = True
except ImportError:
    oracledb = None  # type: ignore[assignment]
    _ORACLEDB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

_CONNECTIVITY_ORA_CODES = frozenset({"ORA-12541", "ORA-12170", "ORA-12560"})
_CONNECTIVITY_DPY_CODES = frozenset({"DPY-6005"})

# errno values that indicate a dead tunnel
_CONNECTIVITY_ERRNOS = frozenset({
    61,   # ECONNREFUSED
    110,  # ETIMEDOUT (Linux)
    60,   # ETIMEDOUT (macOS)
})


def _is_connectivity_error(exc: BaseException) -> bool:
    """Return True if exc is a transient connectivity error that warrants reconnect."""
    msg = str(exc)
    for code in _CONNECTIVITY_ORA_CODES:
        if code in msg:
            return True
    for code in _CONNECTIVITY_DPY_CODES:
        if code in msg:
            return True
    if isinstance(exc, OSError) and exc.errno in _CONNECTIVITY_ERRNOS:
        return True
    # Check chained causes
    if exc.__cause__ is not None and _is_connectivity_error(exc.__cause__):
        return True
    if exc.__context__ is not None and _is_connectivity_error(exc.__context__):
        return True
    return False

# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BastionConfig:
    bastion_ocid: str
    target_db_host: str
    target_db_port: int
    local_tunnel_port: int
    ssh_key_path: str
    session_ttl_seconds: int = 10800
    oci_cli_path: str = "/opt/homebrew/bin/oci"
    connect_timeout_seconds: int = 30
    max_reconnect_attempts: int = 3

    @classmethod
    def from_dict(cls, d: dict) -> BastionConfig:
        return cls(
            bastion_ocid=d["bastion_ocid"],
            target_db_host=d["target_db_host"],
            target_db_port=int(d["target_db_port"]),
            local_tunnel_port=int(d["local_tunnel_port"]),
            ssh_key_path=d["ssh_key_path"],
            session_ttl_seconds=int(d.get("session_ttl_seconds", 10800)),
            oci_cli_path=d.get("oci_cli_path", "/opt/homebrew/bin/oci"),
            connect_timeout_seconds=int(d.get("connect_timeout_seconds", 30)),
            max_reconnect_attempts=int(d.get("max_reconnect_attempts", 3)),
        )


@dataclass
class AdbPoolConfig:
    deployment_mode: str
    service_name: str
    wallet_path: str
    user: str
    password: str
    min_connections: int = 1
    max_connections: int = 5
    bastion: BastionConfig | None = None

    @classmethod
    def from_dict(cls, d: dict) -> AdbPoolConfig:
        deployment_mode = d.get("deployment_mode", "vm")
        adb = d.get("adb", {})
        bastion_cfg = None
        if deployment_mode == "laptop" and "bastion" in d:
            bastion_cfg = BastionConfig.from_dict(d["bastion"])
        return cls(
            deployment_mode=deployment_mode,
            service_name=adb.get("service_name", ""),
            wallet_path=adb.get("wallet_path", ""),
            user=adb.get("user", ""),
            password=adb.get("password", ""),
            min_connections=int(adb.get("min_connections", 1)),
            max_connections=int(adb.get("max_connections", 5)),
            bastion=bastion_cfg,
        )

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class BastionReconnectError(RuntimeError):
    """Raised when all reconnect attempts to the OCI Bastion are exhausted."""


# ---------------------------------------------------------------------------
# Module-level tunnel subprocess handle (atexit-cleaned)
# ---------------------------------------------------------------------------

_tunnel_proc: subprocess.Popen | None = None


def _kill_tunnel() -> None:
    global _tunnel_proc
    if _tunnel_proc is not None and _tunnel_proc.poll() is None:
        log.info("adb_pool: killing SSH tunnel subprocess (pid=%d)", _tunnel_proc.pid)
        _tunnel_proc.terminate()
        try:
            _tunnel_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _tunnel_proc.kill()
        _tunnel_proc = None


atexit.register(_kill_tunnel)

# ---------------------------------------------------------------------------
# BastionReconnector
# ---------------------------------------------------------------------------


class BastionReconnector:
    """Creates OCI Bastion managed-SSH sessions and manages the SSH tunnel."""

    def __init__(self, cfg: BastionConfig) -> None:
        self._cfg = cfg

    def create_session(self) -> str:
        """Call OCI CLI to create a new bastion session.

        Returns the raw SSH tunnel command string from the OCI CLI JSON output.
        Raises subprocess.CalledProcessError on CLI failure.
        """
        cmd = [
            self._cfg.oci_cli_path,
            "bastion", "session", "create-managed-ssh",
            "--bastion-id", self._cfg.bastion_ocid,
            "--target-private-ip", self._cfg.target_db_host,
            "--target-port", str(self._cfg.target_db_port),
            "--session-ttl", str(self._cfg.session_ttl_seconds),
            "--ssh-public-key-file", str(Path(self._cfg.ssh_key_path).expanduser()) + ".pub",
            "--wait-for-state", "ACTIVE",
        ]
        log.debug("adb_pool: creating bastion session: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        # OCI CLI returns the ssh_metadata.command field with the tunnel command
        ssh_command: str = (
            data.get("data", {})
            .get("ssh_metadata", {})
            .get("command", "")
        )
        if not ssh_command:
            raise BastionReconnectError(
                f"OCI CLI did not return an SSH tunnel command. Raw output: {result.stdout[:500]}"
            )
        return ssh_command

    def start_tunnel(self, ssh_command: str) -> None:
        """Kill any existing tunnel, start a new one from the SSH command string."""
        global _tunnel_proc
        _kill_tunnel()

        key_path = str(Path(self._cfg.ssh_key_path).expanduser())
        # Inject our key path: replace the -i flag if present, or prepend it
        if "-i " not in ssh_command:
            ssh_command = f"ssh -i {key_path} " + ssh_command.removeprefix("ssh ")

        args = ssh_command.split()
        log.debug("adb_pool: starting SSH tunnel: %s", " ".join(args))
        _tunnel_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait_for_port(self) -> None:
        """Block until LOCAL_TUNNEL_PORT is accepting connections or timeout."""
        port = self._cfg.local_tunnel_port
        deadline = time.monotonic() + self._cfg.connect_timeout_seconds
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    log.debug("adb_pool: tunnel port %d is open", port)
                    return
            except OSError:
                time.sleep(1)
        raise BastionReconnectError(
            f"Tunnel port {port} did not open within {self._cfg.connect_timeout_seconds}s"
        )

    def reconnect(self) -> None:
        """Full reconnect cycle: create session → start tunnel → wait for port."""
        ssh_command = self.create_session()
        self.start_tunnel(ssh_command)
        self.wait_for_port()

# ---------------------------------------------------------------------------
# RetryWrapper
# ---------------------------------------------------------------------------


class RetryWrapper:
    """Wraps a raw oracledb pool and intercepts connectivity failures.

    In laptop mode:  reconnect via BastionReconnector, then retry.
    In non-laptop:   exponential backoff up to 5 attempts.

    Non-connectivity errors (ORA-0001, ORA-1400, etc.) are re-raised immediately.

    Usage mirrors the raw pool:
        with wrapped_pool.acquire() as conn:
            conn.execute(...)
    """

    def __init__(
        self,
        pool: Any,
        deployment_mode: str,
        reconnector: BastionReconnector | None = None,
        max_reconnect_attempts: int = 3,
    ) -> None:
        self._pool = pool
        self._deployment_mode = deployment_mode
        self._reconnector = reconnector
        self._max_reconnect_attempts = max_reconnect_attempts

    @contextmanager
    def acquire(self) -> Generator[Any, None, None]:
        if self._deployment_mode == "laptop":
            conn = self._acquire_laptop()
        else:
            conn = self._acquire_with_backoff()
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _acquire_laptop(self) -> Any:
        reconnect_attempt = 0
        while True:
            try:
                return self._pool.acquire()
            except Exception as exc:
                if not _is_connectivity_error(exc):
                    raise
                reconnect_attempt += 1
                if reconnect_attempt > self._max_reconnect_attempts:
                    log.error(
                        "adb_pool: bastion reconnect exhausted after %d attempts",
                        self._max_reconnect_attempts,
                        exc_info=exc,
                    )
                    raise BastionReconnectError(
                        f"Gave up after {self._max_reconnect_attempts} reconnect attempts"
                    ) from exc

                start = time.monotonic()
                log.warning(
                    "adb_pool: bastion_reconnect_attempt",
                    extra={
                        "event": "bastion_reconnect_attempt",
                        "attempt": reconnect_attempt,
                        "max_attempts": self._max_reconnect_attempts,
                        "tunnel_port": (
                            self._reconnector._cfg.local_tunnel_port
                            if self._reconnector else None
                        ),
                        "bastion_ocid": (
                            self._reconnector._cfg.bastion_ocid
                            if self._reconnector else None
                        ),
                    },
                )
                if self._reconnector is None:
                    raise BastionReconnectError(
                        "Connectivity error in laptop mode but no BastionReconnector configured"
                    ) from exc

                try:
                    self._reconnector.reconnect()
                    self._health_check()
                except Exception:
                    continue

                elapsed = time.monotonic() - start
                log.info(
                    "adb_pool: bastion reconnect succeeded",
                    extra={
                        "event": "bastion_reconnect_success",
                        "attempt": reconnect_attempt,
                        "elapsed_seconds": round(elapsed, 1),
                    },
                )

    def _acquire_with_backoff(self) -> Any:
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                return self._pool.acquire()
            except Exception as exc:
                if not _is_connectivity_error(exc):
                    raise
                if attempt >= max_attempts:
                    raise
                wait = min(2 ** attempt, 30)
                log.warning(
                    "adb_pool: connectivity error (attempt %d/%d), retrying in %ds",
                    attempt, max_attempts, wait,
                )
                time.sleep(wait)

    def _health_check(self) -> None:
        """Verify connectivity with SELECT 1 FROM DUAL after tunnel establishment."""
        conn = self._pool.acquire()
        try:
            conn.execute("SELECT 1 FROM DUAL")
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_adb_pool(config: dict) -> Any:
    """Build and return a (possibly wrapped) oracledb pool from a config dict.

    The returned object exposes pool.acquire() and is transparent to callers.

    If oracledb is not installed, returns None (stub mode).
    """
    if not _ORACLEDB_AVAILABLE:
        log.warning("adb_pool: oracledb not installed — returning None (stub mode)")
        return None

    cfg = AdbPoolConfig.from_dict(config)

    pool = oracledb.create_pool(
        user=cfg.user,
        password=cfg.password,
        dsn=cfg.service_name,
        config_dir=cfg.wallet_path,
        wallet_location=cfg.wallet_path,
        wallet_password=None,
        min=cfg.min_connections,
        max=cfg.max_connections,
    )

    if cfg.deployment_mode != "laptop":
        log.info(
            "adb_pool: deployment_mode=%s — using exponential backoff only",
            cfg.deployment_mode,
        )
        return RetryWrapper(pool, deployment_mode=cfg.deployment_mode)

    if cfg.bastion is None:
        log.warning(
            "adb_pool: deployment_mode=laptop but no [bastion] config — "
            "reconnect disabled; using backoff only"
        )
        return RetryWrapper(pool, deployment_mode="laptop", reconnector=None,
                            max_reconnect_attempts=3)

    reconnector = BastionReconnector(cfg.bastion)
    log.info(
        "adb_pool: deployment_mode=laptop — bastion auto-reconnect enabled "
        "(max_attempts=%d, port=%d)",
        cfg.bastion.max_reconnect_attempts,
        cfg.bastion.local_tunnel_port,
    )
    return RetryWrapper(
        pool,
        deployment_mode="laptop",
        reconnector=reconnector,
        max_reconnect_attempts=cfg.bastion.max_reconnect_attempts,
    )
