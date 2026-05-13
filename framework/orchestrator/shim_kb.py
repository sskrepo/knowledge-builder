"""shim_kb — aggregated KB cards across persona builders (ADR-006).

V2: distinguishes authoring scope (cards_owned_by) from read scope
(cards_visible_to, ACL-driven via metadata_defaults.persona_visibility).
Per ADR-007 amend 6.

V3 (Option B — migration 008): when a skill_store is provided, promoted KB
entries from KBF_PERSONA_BUILDERS (status='production') are merged on top of
the seed *.yaml files at startup and after each PROMOTE via reload().
The *.yaml files are seed/bootstrap only — they define the initial KBs that
ship with the codebase.  Any skill authored via authorSkill and promoted lives
in ADB.  The .yaml.new_kb second pass has been removed.
"""
from __future__ import annotations
import logging
from pathlib import Path
import yaml

log = logging.getLogger(__name__)


class ShimKb:
    def __init__(self, persona_builders_dir: Path, skill_store=None):
        self.dir = Path(persona_builders_dir)
        self._skill_store = skill_store
        self._cards: list[dict] = []
        self.load()

    def load(self) -> None:
        cards: list[dict] = []

        # --- Pass 1: seed *.yaml files (ship with the codebase) ---
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
                    "_source": "yaml",
                })
                cards.append(card)

        # --- Pass 2: ADB-promoted KB entries (Option B) ---
        if self._skill_store is not None:
            try:
                pb_rows = self._skill_store.list_persona_builder_kbs(status="production")
            except Exception as exc:
                log.warning("ShimKb: list_persona_builder_kbs failed: %s", exc)
                pb_rows = []

            # Build a key-set of (persona, name) already loaded from YAML so we
            # can overwrite rather than duplicate if both exist.
            existing_keys: dict[tuple[str, str], int] = {
                (c["persona"], c["name"]): i for i, c in enumerate(cards)
            }

            for row in pb_rows:
                persona = row["persona"]
                kb_name = row["kb_name"]
                content_yaml = row.get("content_yaml", "")
                try:
                    entry = yaml.safe_load(content_yaml)
                except Exception as exc:
                    log.warning(
                        "ShimKb: could not parse content_yaml for %s.%s: %s",
                        persona, kb_name, exc,
                    )
                    continue

                if not isinstance(entry, dict):
                    log.warning(
                        "ShimKb: content_yaml for %s.%s did not parse to a dict (got %s) "
                        "— skipping",
                        persona, kb_name, type(entry).__name__,
                    )
                    continue

                # If it's a bare KB entry dict (from synthesize_persona_builder_diff),
                # wrap it to match the expected card shape.
                name = entry.get("name", kb_name)
                kind = entry.get("kind", "vector")
                retrieval_tools = entry.get("retrieval_tools", [])
                provides_fields = entry.get("provides_fields", [])
                kb_card = entry.get("kb_card") or {}

                # Derive persona_visibility: check entry or fall back to [persona]
                visibility = entry.get("persona_visibility") or [persona]

                card = dict(kb_card)
                card.update({
                    "name": name,
                    "persona": persona,
                    "kind": kind,
                    "retrieval_tools": retrieval_tools,
                    "provides_fields": provides_fields,
                    "persona_visibility": visibility,
                    "_source": "adb",
                })

                key = (persona, name)
                if key in existing_keys:
                    # ADB overrides YAML seed for promoted skills
                    cards[existing_keys[key]] = card
                else:
                    existing_keys[key] = len(cards)
                    cards.append(card)

        self._cards = cards
        log.info(
            "shim_kb loaded %d cards from %s (adb_backed=%s)",
            len(cards), self.dir, self._skill_store is not None,
        )

    def reload(self) -> None:
        """Re-run load() — call after a PROMOTE to pick up newly promoted KBs."""
        self.load()

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
