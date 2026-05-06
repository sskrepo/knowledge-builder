"""Architect context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class ArchitectSkill(BasePersonaSkill):
    persona = "architect"
    PROMPT_FRAGMENT = """
    You retrieve context for an Architect consumer.
    Prefer design docs, ADRs, system maps, integration playbooks.
    Functional area is primary axis with service_id strong secondary. Always cite.
    """
