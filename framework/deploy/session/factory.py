"""Session store factory — selects implementation from KBF_STORE_BACKEND env var.

Usage (in mcp_server.py lifespan block):
    from framework.deploy.session.factory import build_session_store
    app.state.session_store = build_session_store(pool=state.get("adb_pool"))
"""
from __future__ import annotations

import os
from pathlib import Path

from ._base import SessionStore


def build_session_store(pool=None) -> SessionStore:
    """Construct and return the appropriate SessionStore implementation.

    Selection is driven by the KBF_STORE_BACKEND environment variable:

    - ``filestore`` (default): FilestoreSessionStore rooted at KBF_STORE_ROOT
      (defaults to ~/.kbf/store). Used for dev/laptop mode — no external
      services required.
    - ``adb``: AdbSessionStore backed by the provided oracledb pool. When
      pool=None the store operates in stub mode (all operations are no-ops).

    Args:
        pool: oracledb connection pool for ADB backend; ignored for filestore.

    Returns:
        A concrete SessionStore instance ready for use.
    """
    backend = os.environ.get("KBF_STORE_BACKEND", "filestore").lower().strip()

    # When a pool is explicitly provided and no backend override is set,
    # infer ADB mode — callers that pass a pool want ADB session storage.
    if pool is not None and backend == "filestore":
        backend = "adb"

    if backend == "adb":
        from .adb_store import AdbSessionStore
        if pool is None:
            import logging
            logging.getLogger(__name__).warning(
                "build_session_store: backend=adb but no pool provided — "
                "AdbSessionStore will run in stub mode (all operations no-ops)"
            )
        return AdbSessionStore(pool=pool)

    # Default: filestore
    store_root = os.environ.get(
        "KBF_STORE_ROOT",
        str(Path.home() / ".kbf" / "store"),
    )
    from .filestore import FilestoreSessionStore
    return FilestoreSessionStore(store_root=store_root)
