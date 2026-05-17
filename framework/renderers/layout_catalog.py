"""Layout preset catalog — single source of truth for PPTX layout presets.

ADR-034: Provides the mapping between:
  - internal_id   — renderer dispatch key (used by PptxRenderer, never shown to users)
  - human_label   — plain-language name safe to surface to skill authors
  - description   — what the layout looks like and its purpose
  - when_to_use   — guidance for reasoning/selection (used in prompts)
  - output_format — which output_format this preset applies to
  - structural_shape — concise structural description for prompt injection

The prompt system receives the catalog DESCRIPTIONS (not the internal_ids) and
reasons over them to select the best fit.  The renderer dispatches on internal_id.
Users/authors only ever see human_label + description — never the internal identifier.

Adding a new preset:
  1. Add an entry to LAYOUT_PRESETS below.
  2. Add the dispatch branch in pptx_renderer.py::PptxRenderer.render().
  3. No prompt change required — the catalog is injected automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class LayoutPreset:
    """Immutable descriptor for one renderer layout preset."""

    internal_id: str       # renderer dispatch key — NOT user-facing
    human_label: str       # plain-language label safe to surface to users
    description: str       # one-sentence human description of the layout
    when_to_use: str       # guidance for LLM reasoning over user ask
    output_format: str     # "pptx" | "docx" | "markdown" | "email" | "slack"
    structural_shape: str  # concise structural shape for prompt injection


# ---------------------------------------------------------------------------
# Registered presets
# ---------------------------------------------------------------------------

LAYOUT_PRESETS: List[LayoutPreset] = [
    LayoutPreset(
        internal_id="weekly_exec_review_v1",
        human_label="Single-slide executive review (two-column Oracle style)",
        description=(
            "A single widescreen slide with an Oracle-branded header, "
            "a left column holding scope, assumptions, status, and next steps, "
            "and a right sidebar holding key milestones, ORM status, and risk/mitigation."
        ),
        when_to_use=(
            "Use when the skill produces a weekly or regular executive-facing "
            "project-status slide that must fit all information on one slide "
            "in a two-column layout with an Oracle branded look. "
            "Ideal for exec-review, steering-committee, or program-review PPTX outputs."
        ),
        output_format="pptx",
        structural_shape=(
            "1 slide | header band | left col: scope + assumptions + status + next steps | "
            "right sidebar: milestones + ORM + risk"
        ),
    ),
    LayoutPreset(
        internal_id="default",
        human_label="Standard multi-slide deck",
        description=(
            "A standard multi-slide PowerPoint deck with one slide per content section, "
            "each slide using a title-and-content layout."
        ),
        when_to_use=(
            "Use for any general-purpose PPTX output where the content spans multiple "
            "topics or sections and does not need to fit on a single slide. "
            "Also the fallback when no more specific layout fits the user ask."
        ),
        output_format="pptx",
        structural_shape=(
            "N slides | one slide per section | title + content placeholder per slide"
        ),
    ),
]

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

_BY_ID: dict[str, LayoutPreset] = {p.internal_id: p for p in LAYOUT_PRESETS}


def get_preset(internal_id: str) -> LayoutPreset | None:
    """Return the LayoutPreset for *internal_id*, or None if unknown."""
    return _BY_ID.get(internal_id)


def all_presets() -> List[LayoutPreset]:
    """Return all registered layout presets."""
    return list(LAYOUT_PRESETS)


def catalog_for_prompt(output_format: str | None = None) -> str:
    """Return a plain-language catalog description suitable for prompt injection.

    If *output_format* is supplied, only presets matching that format are
    included (avoids injecting docx presets into a pptx-only question).
    The catalog intentionally lists each entry's human_label, description,
    when_to_use, and structural_shape — but NEVER its internal_id.
    """
    presets = LAYOUT_PRESETS
    if output_format:
        presets = [p for p in presets if p.output_format == output_format]

    lines: list[str] = []
    for i, p in enumerate(presets, start=1):
        lines.append(
            f"Option {i}: {p.human_label}\n"
            f"  Description: {p.description}\n"
            f"  Best for: {p.when_to_use}\n"
            f"  Structure: {p.structural_shape}"
        )
    return "\n\n".join(lines) if lines else "(no layout presets available for this output format)"


def internal_ids() -> list[str]:
    """Return the list of all internal_ids — used for test assertions only."""
    return [p.internal_id for p in LAYOUT_PRESETS]
