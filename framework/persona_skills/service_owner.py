"""Service Owner context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class ServiceOwnerSkill(BasePersonaSkill):
    persona = "service_owner"
    PROMPT_FRAGMENT = """
    You retrieve context for a Service Owner consumer.
    Service catalog, ownership, SLOs, decisions per service. Service id is primary axis.
    Always cite.
    """
