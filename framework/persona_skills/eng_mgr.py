"""Eng Manager context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class EngMgrSkill(BasePersonaSkill):
    persona = "eng_mgr"
    PROMPT_FRAGMENT = """
    You retrieve context for an Engineering Manager consumer.
    Prefer functional-area-scoped content (REFRESH/PROVISIONING/PATCHING/DR).
    Always cite. Apply resources filters when present.
    """
