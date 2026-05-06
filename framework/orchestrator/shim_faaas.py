"""shim_faaas — domain ontology loader (ADR-006).

Reads framework/config/shim_faaas.yaml at startup; refreshes on signal.
"""
from __future__ import annotations
import logging
from pathlib import Path
import yaml

log = logging.getLogger(__name__)

class ShimFaaas:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        with open(self.path) as f:
            self._data = yaml.safe_load(f)

    def snapshot(self) -> dict:
        return dict(self._data)

    def personas(self) -> list[dict]:        return self._data.get("personas", [])
    def services(self) -> list[dict]:        return self._data.get("services", [])
    def resources(self) -> list[dict]:       return self._data.get("resources", [])
    def functional_areas(self) -> list[dict]:return self._data.get("functional_areas", [])
    def kinds(self) -> list[dict]:           return self._data.get("kinds_of_knowledge", [])

    def producer_personas(self) -> list[str]:
        return [p["id"] for p in self.personas() if p.get("role") == "producer"]

    def is_valid_resource(self, rid: str) -> bool:
        return any(r["id"] == rid for r in self.resources())

    def is_valid_functional_area(self, fa: str) -> bool:
        return any(f["id"] == fa for f in self.functional_areas())

    def render_for_prompt(self) -> str:
        """Compact YAML-like rendering for orchestrator system prompt."""
        lines = ["# FAaaS Ontology"]
        lines.append("\n## Personas")
        for p in self.personas():
            role = p.get("role", "producer")
            focus = p.get("knowledge_focus", [])
            lines.append(f"- {p['id']} ({role}): {', '.join(focus) if focus else ''}")
        lines.append("\n## Services")
        for s in self.services():
            lines.append(f"- {s['id']}: {s.get('description', '')}")
        lines.append("\n## Resources (with hierarchy)")
        for r in self.resources():
            rels = []
            if r.get("contains"): rels.append(f"contains: {r['contains']}")
            if r.get("contained_in"): rels.append(f"contained_in: {r['contained_in']}")
            if r.get("runs_on"): rels.append(f"runs_on: {r['runs_on']}")
            lines.append(f"- {r['id']}: {r.get('description', '')} {'; '.join(rels)}")
        lines.append("\n## Functional Areas")
        for f in self.functional_areas():
            lines.append(f"- {f['id']}: {f.get('description', '')}")
        return "\n".join(lines)
