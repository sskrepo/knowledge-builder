"""Skill store package — ADB-backed artifact storage (DECISION-006).

ADB is always used — laptop, staging, and production all connect to ADB.
There is no filestore fallback.

Exports:
  SkillStore          — ABC defining the write/read/promote/list contract
  build_skill_store   — factory: returns AdbSkillStore (pool required)
"""
from ._base import SkillStore
from .factory import build_skill_store

__all__ = ["SkillStore", "build_skill_store"]
