"""Session store factory — always ADB-backed.

ADB is always available (laptop, staging, production). There is no filestore
fallback. pool is required; passing None raises ValueError.

Usage (in mcp_server.py lifespan block):
    from framework.deploy.session.factory import build_session_store
    app.state.session_store = build_session_store(pool=adb_pool)
"""
from __future__ import annotations

import logging

from ._base import SessionStore

log = logging.getLogger(__name__)


def build_session_store(pool) -> SessionStore:
    """Return an AdbSessionStore backed by the given connection pool.

    Args:
        pool: oracledb connection pool (required — ADB is always available).

    Returns:
        AdbSessionStore instance.

    Raises:
        ValueError: if pool is None.
    """
    if pool is None:
        raise ValueError(
            "build_session_store: pool is required. "
            "ADB is always available — there is no filestore fallback."
        )
    from .adb_store import AdbSessionStore
    log.info("SessionStore: ADB mode")
    return AdbSessionStore(pool=pool)
