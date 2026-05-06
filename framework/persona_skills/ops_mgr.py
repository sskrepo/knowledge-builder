"""Ops Manager context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class OpsMgrSkill(BasePersonaSkill):
    persona = "ops_mgr"
    PROMPT_FRAGMENT = """
    You retrieve context for an Operations Manager consumer.
    Prefer SLAs, escalation paths, exec-summary incident views, compliance state.
    Always cite. Functional area + service filters narrow strongly.
    """
