"""TPM context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class TpmSkill(BasePersonaSkill):
    persona = "tpm"
    PROMPT_FRAGMENT = """
    You retrieve context for a Technical Program Manager consumer.
    Prefer weekly ops, ECARs, cross-team dependencies. Primary axis is program. Always cite.
    """
