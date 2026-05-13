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

        # First pass: load all full persona builder YAMLs (*.yaml).
        # Build a persona → visibility mapping so *.yaml.new_kb entries can
        # inherit the owning persona's ACL defaults.
        persona_visibility_defaults: dict[str, list[str]] = {}
        full_yaml_paths = sorted(
            p for p in self.dir.glob("*.yaml") if not p.name.startswith("_")
        )

        for path in full_yaml_paths:
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception as e:
                log.warning("failed to load %s: %s", path, e)
                continue
            persona = cfg.get("persona")
            if not persona:
                continue
            visibility = (
                (cfg.get("metadata_defaults") or {}).get("persona_visibility")
                or [persona]
            )
            persona_visibility_defaults[persona] = visibility
            for kb in cfg.get("knowledge_bases", []):
                card = dict(kb.get("kb_card") or {})
                card.update({
                    "name": kb["name"],
                    "persona": persona,
                    "kind": kb["kind"],
                    "retrieval_tools": kb.get("retrieval_tools", []),
                    "provides_fields": kb.get("provides_fields", []),
                    "persona_visibility": visibility,
                })
                cards.append(card)

        # Second pass: *.yaml.new_kb files — raw KB entry dicts written by
        # COMMIT before they are merged into the full persona builder at PROMOTE
        # time.  These represent authored-but-not-yet-promoted KBs.  Include them
        # so that persona skill LLMs can select them during Tier 2 retrieval.
        # Without this, a newly promoted skill's KB is invisible to all retrievers
        # and every query returns the generic weekly-ops fixture.
        for path in sorted(self.dir.glob("*.yaml.new_kb")):
            if path.name.startswith("_"):
                continue
            try:
                with open(path) as f:
                    kb_entry = yaml.safe_load(f) or {}
            except Exception as e:
                log.warning("failed to load %s: %s", path, e)
                continue
            # Infer persona from filename: "tpm.yaml.new_kb" → "tpm"
            persona = path.name.split(".")[0]
            visibility = persona_visibility_defaults.get(persona) or [persona]
            kb_name = kb_entry.get("name")
            if not kb_name:
                continue
            card = dict(kb_entry.get("kb_card") or {})
            card.update({
                "name": kb_name,
                "persona": persona,
                "kind": kb_entry.get("kind", "vector"),
                "retrieval_tools": kb_entry.get("retrieval_tools", ["vector_search"]),
                "provides_fields": kb_entry.get("provides_fields", []),
                "persona_visibility": visibility,
            })
            cards.append(card)
            log.debug("shim_kb: loaded new_kb entry %s.%s from %s", persona, kb_name, path.name)

        self._cards = cards
        log.info(
            "shim_kb loaded %d cards from %s (%d full builders, %d new_kb entries)",
            len(cards),
            self.dir,
            len(full_yaml_paths),
            sum(1 for p in self.dir.glob("*.yaml.new_kb") if not p.name.startswith("_")),
        )

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
