"""shim_kb — aggregated KB cards across persona builders (ADR-006).

V2: distinguishes authoring scope (cards_owned_by) from read scope
(cards_visible_to, ACL-driven via metadata_defaults.persona_visibility).
Per ADR-007 amend 6.
"""
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
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception as e:
                log.warning("failed to load %s: %s", path, e)
                continue
            persona = cfg.get("persona")
            visibility = (cfg.get("metadata_defaults") or {}).get("persona_visibility") or [persona]
            for kb in cfg.get("knowledge_bases", []):
                card = dict(kb.get("kb_card") or {})
                card.update({
                    "name": kb["name"],
                    "persona": persona,                       # AUTHORING owner
                    "kind": kb["kind"],
                    "retrieval_tools": kb.get("retrieval_tools", []),
                    "provides_fields": kb.get("provides_fields", []),
                    "persona_visibility": visibility,         # READ scope
                })
                cards.append(card)
        self._cards = cards
        log.info("shim_kb loaded %d cards from %s", len(cards), self.dir)

    def all_cards(self) -> list[dict]:
        return list(self._cards)

    def cards_owned_by(self, persona: str) -> list[dict]:
        """KBs this persona AUTHORS. Used by skill builder during authoring (reuse detection)."""
        return [c for c in self._cards if c.get("persona") == persona]

    def cards_visible_to(self, persona: str) -> list[dict]:
        """KBs this persona can READ (ACL-driven). Used by persona context skills at retrieval time.
        Per ADR-007 amend 6: this is wider than authoring scope."""
        return [c for c in self._cards
                if persona in (c.get("persona_visibility") or [])]

    def find_kb(self, kb_name: str) -> dict | None:
        """Lookup by 'persona.kb_name' (e.g. 'tpm.weekly_project_status') or just 'kb_name'."""
        if "." in kb_name:
            persona, name = kb_name.split(".", 1)
            for c in self._cards:
                if c.get("persona") == persona and c.get("name") == name:
                    return c
            return None
        for c in self._cards:
            if c.get("name") == kb_name:
                return c
        return None

    def render_for_persona_prompt(self, persona: str) -> str:
        cards = self.cards_visible_to(persona)
        if not cards:
            return f"# (no KBs visible to {persona})"
        lines = [f"# Knowledge bases visible to {persona}"]
        for c in cards:
            owner_note = "" if c["persona"] == persona else f" (owned by {c['persona']})"
            lines.append(f"\n## {c['name']}{owner_note} ({c['kind']})")
            if c.get("summary"): lines.append(f"  summary: {c['summary']}")
            if c.get("use_when"): lines.append(f"  use_when: {c['use_when']}")
            if c.get("provides_fields"):
                lines.append(f"  provides_fields: {c['provides_fields']}")
            if c.get("retrieval_tools"):
                lines.append(f"  tools: {', '.join(c['retrieval_tools'])}")
        return "\n".join(lines)
