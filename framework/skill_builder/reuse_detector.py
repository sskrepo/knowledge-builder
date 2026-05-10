"""Reuse detector — see SkillBuilder._detect_reuse(). Standalone for tests."""
from __future__ import annotations


def detect_reuse(required_fields: list[str], persona: str, shim_kb) -> dict:
    visible = shim_kb.cards_visible_to(persona)
    covered: dict[str, str] = {}
    gaps: list[str] = []
    for field in required_fields:
        match = next(
            (kb for kb in visible if field in kb.get("provides_fields", [])),
            None,
        )
        if match:
            covered[field] = f"{match['persona']}.{match['name']}"
        else:
            gaps.append(field)
    return {"covered": covered, "gaps": gaps}
