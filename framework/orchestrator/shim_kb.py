"""shim_kb — aggregated KB cards across persona builders (ADR-006)."""
from __future__ import annotations
import logging
from pathlib import Path
import yaml

log = logging.getLogger(__name__)

class ShimKb:
    def __init__(self, persona_builders_dir: Path):
        self.dir = Path(persona_builders_dir)
        self._cards: list[dict] = []
        self.load()

    def load(self) -> None:
        cards: list[dict] = []
        for path in sorted(self.dir.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            with open(path) as f:
                cfg = yaml.safe_load(f)
            persona = cfg.get("persona")
            for kb in cfg.get("knowledge_bases", []):
                card = dict(kb.get("kb_card") or {})
                card.update({
                    "name": kb["name"],
                    "persona": persona,
                    "kind": kb["kind"],
                    "retrieval_tools": kb.get("retrieval_tools", []),
                })
                cards.append(card)
        self._cards = cards

    def all_cards(self) -> list[dict]:
        return list(self._cards)

    def cards_for(self, persona: str) -> list[dict]:
        return [c for c in self._cards if c.get("persona") == persona]

    def render_for_persona_prompt(self, persona: str) -> str:
        cards = self.cards_for(persona)
        if not cards:
            return f"# (no KBs registered for persona {persona})"
        lines = [f"# Knowledge bases available to {persona}"]
        for c in cards:
            lines.append(f"\n## {c['name']} ({c['kind']})")
            if c.get("summary"): lines.append(f"  summary: {c['summary']}")
            if c.get("use_when"): lines.append(f"  use_when: {c['use_when']}")
            if c.get("input_shape"): lines.append(f"  input_shape: {c['input_shape']}")
            if c.get("output_shape"): lines.append(f"  output_shape: {c['output_shape']}")
            if c.get("retrieval_tools"):
                lines.append(f"  tools: {', '.join(c['retrieval_tools'])}")
        return "\n".join(lines)
