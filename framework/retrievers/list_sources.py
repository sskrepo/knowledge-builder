"""list_sources MCP tool — exposes the shim_index for orchestrator/consumers."""
from __future__ import annotations

class ListSourcesRetriever:
    name = "list_sources"

    def __init__(self, shim_faaas, shim_kb):
        self.shim_faaas = shim_faaas
        self.shim_kb = shim_kb

    def __call__(self, persona: str | None = None) -> dict:
        return {
            "shim_faaas": self.shim_faaas.snapshot(),
            "knowledge_bases": self.shim_kb.cards_for(persona) if persona else self.shim_kb.all_cards(),
        }
