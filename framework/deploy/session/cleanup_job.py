"""Background TTL cleanup task for author_skill sessions (PDD V3 Track D-5).

Start in the FastAPI lifespan block:

    from framework.deploy.session.cleanup_job import run_ttl_cleanup_loop

    @asynccontextmanager
    async def lifespan(app):
        cleanup_task = asyncio.create_task(
            run_ttl_cleanup_loop(app.state.session_store)
        )
        yield
        cleanup_task.cancel()
"""
from __future__ import annotations

import asyncio
import logging

from ._base import SessionStore

log = logging.getLogger(__name__)


async def run_ttl_cleanup_loop(
    store: SessionStore,
    interval_seconds: int = 86_400,
) -> None:
    """Expire stale sessions on a recurring interval (default: once per day).

    Runs indefinitely as a background asyncio task. Errors from expire_stale()
    are caught and logged — the loop continues regardless.

    Args:
        store: Any SessionStore implementation.
        interval_seconds: Seconds between cleanup runs. Defaults to 86 400 (1 day).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            expired_count = store.expire_stale()
            log.info("TTL cleanup: expired %d sessions", expired_count)
        except Exception as exc:
            log.error("TTL cleanup failed: %s", exc)
