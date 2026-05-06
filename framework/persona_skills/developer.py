"""Developer context skill (ADR-007 contract)."""
from __future__ import annotations
from ._base import BasePersonaSkill

class DeveloperSkill(BasePersonaSkill):
    persona = "developer"
    PROMPT_FRAGMENT = """
    You retrieve context for a Developer consumer.
    Prefer code wiki pages, OpenAPI specs, code-review patterns.
    Service id is primary axis. Always cite — exact paths and line numbers when available.
    """
