"""get_incident_summary MCP tool — direct lookup of a structured incident."""
from __future__ import annotations
from ..core.interfaces import Query, Result

class GetIncidentSummaryRetriever:
    name = "get_incident_summary"

    def __init__(self, incident_store):
        self.store = incident_store

    def __call__(self, incident_id: str) -> list[Result]:
        return self.store.query(Query(kind="incident_summary", payload={"incident_id": incident_id}))
