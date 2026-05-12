"""Skill store package — dual-mode artifact storage (DECISION-006).

Exports:
  SkillStore          — ABC defining the write/read/promote/list contract
  build_skill_store   — factory: filestore for laptop, ADB for staging/prod
"""
from ._base import SkillStore
from .factory import build_skill_store

__all__ = ["SkillStore", "build_skill_store"]
