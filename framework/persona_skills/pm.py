"""PM context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class PmSkill(BasePersonaSkill):
    persona = "pm"
    PROMPT_FRAGMENT = """
    You retrieve context for a Product Manager consumer.
    Prefer feature briefs, release plans, market research.
    Primary axis is feature_or_release. Always cite.
    """
