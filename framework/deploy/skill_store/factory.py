"""build_skill_store — factory function (DECISION-006 Option A).

Selection logic:
  pool is not None  → AdbSkillStore (staging / production / laptop with ADB)
  pool is None      → FilestoreSkillStore (dev mode, tests, laptop without ADB)

The env parameter is accepted for API symmetry with build_artifact_store but
is not currently used for selection — the pool presence is the sole signal.
"""
from __future__ import annotations

import logging

from ._base import SkillStore

log = logging.getLogger(__name__)


def build_skill_store(pool=None, env: str = "") -> SkillStore:
    """Return a SkillStore appropriate for the current environment.

    Args:
        pool: oracledb connection pool.  When not None, returns AdbSkillStore.
        env:  Environment name (informational only; does not affect selection).

    Returns:
        SkillStore instance (AdbSkillStore or FilestoreSkillStore).
    """
    if pool is not None:
        from .adb import AdbSkillStore
        log.info("SkillStore: ADB mode (env=%s)", env or "unset")
        return AdbSkillStore(pool)

    from .filestore import FilestoreSkillStore
    log.info("SkillStore: filestore mode (laptop/dev)")
    return FilestoreSkillStore()
