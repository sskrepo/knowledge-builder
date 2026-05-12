"""build_skill_store — factory function (DECISION-006 Option A).

ADB is always used — for laptop, staging, and production.
There is no filestore fallback. If pool is None the factory raises ValueError.

The env parameter is accepted for API symmetry with build_artifact_store but
is not currently used for selection — the pool presence is the sole signal.
"""
from __future__ import annotations

import logging

from ._base import SkillStore

log = logging.getLogger(__name__)


def build_skill_store(pool, env: str = "") -> SkillStore:
    """Return an AdbSkillStore for the given connection pool.

    Args:
        pool: oracledb connection pool (required — ADB is always available).
        env:  Environment name (informational only).

    Returns:
        AdbSkillStore instance.

    Raises:
        ValueError: if pool is None.
    """
    if pool is None:
        raise ValueError(
            "build_skill_store: pool is required. "
            "ADB is always available — there is no filestore fallback."
        )
    from .adb import AdbSkillStore
    log.info("SkillStore: ADB mode (env=%s)", env or "unset")
    return AdbSkillStore(pool)
