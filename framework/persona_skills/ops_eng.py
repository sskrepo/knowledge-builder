"""Ops Engineer context skill (ADR-007 contract). Shared with Aira's incident path."""
from __future__ import annotations
from ._base import BasePersonaSkill

class OpsEngSkill(BasePersonaSkill):
    persona = "ops_eng"
    PROMPT_FRAGMENT = """
    You retrieve context for an Operations Engineer (and Aira's incident path).
    Use ops_incidents (vector), ops_runbooks (wiki), ops_dependencies (graph),
    ops_fleet_state (sql), ops_postmortems (vector+wiki) as needed.
    Resources filter is critical — POD/PODDB/EXADATA hierarchy widens via graph.
    Always cite.
    """
