"""shim_workflows — aggregator of workflow skill cards (ADB-aware).

Per ADR-006 amend 2 + ADR-016 + BUG-queue-2ad9a fix.

Reads framework/workflow_skills/{persona}/*.yaml for card bodies (name,
use_when, example_invocations, etc.) but resolves PROMOTION STATUS from the
skill_store when one is wired.  ADB (skill_store) is the single source of
truth for which workflow skills are promoted; disk YAML is the authoring
artifact only and its on-disk `status:` field is never relied upon for
routing decisions.

When a skill_store is provided (production / laptop with ADB):
  - all_cards() returns ONLY skills whose (persona, skill_name) pair is
    reported as promoted/production by skill_store.list_promoted_workflow_skills().
  - Drafts never reach the Tier-1 LLM classifier.

When skill_store is None (pure laptop/no-ADB):
  - all_cards() returns every on-disk card and logs at INFO (laptop mode).
  - This is an explicit, documented decision — NOT a silent fallback.
  - Use all_cards_including_draft() for tooling/introspection regardless of
    whether a skill_store is present.

Mirrors shim_kb.py (ADR-015 Option B) exactly.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from framework.deploy.skill_store._base import SkillStore

log = logging.getLogger(__name__)


class ShimWorkflows:
    def __init__(self, workflow_skills_dir: Path, skill_store=None):
        """Initialise ShimWorkflows.

        Args:
            workflow_skills_dir: Path to framework/workflow_skills/ directory.
            skill_store: Optional SkillStore instance.  When provided, ADB is
                the single source of truth for promotion status (mirrors ShimKb).
                When None, all on-disk cards are served with an INFO log
                explaining laptop-mode behaviour.
        """
        self.dir = Path(workflow_skills_dir)
        self._skill_store = skill_store
        self._cards: list[dict] = []          # all cards loaded from disk
        self._promoted: set[tuple[str, str]] = set()  # (persona, skill_name)
        self.load()

    # ------------------------------------------------------------------
    # Core loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Reload all disk cards and refresh the promoted set from skill_store."""
        cards: list[dict] = []
        if not self.dir.exists():
            log.warning("workflow_skills dir not found: %s", self.dir)
            self._cards = []
            self._promoted = set()
            return

        for path in sorted(self.dir.rglob("*.yaml")):
            if path.name.startswith("_"):
                continue
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception as e:
                log.warning("shim_workflows: failed to load %s: %s", path, e)
                continue

            persona = cfg.get("persona")
            sc = cfg.get("skill_card") or {}
            triggers = cfg.get("trigger") or {}
            on_request = bool((triggers.get("on_request") or {}).get("enabled"))
            on_schedule = bool((triggers.get("on_schedule") or {}).get("cron"))
            on_event = bool((triggers.get("on_event") or {}).get("enabled"))
            cards.append({
                "name": cfg.get("workflow_skill"),
                "persona": persona,
                "summary": sc.get("summary"),
                "use_when": sc.get("use_when"),
                "example_invocations": sc.get("example_invocations", []),
                "do_not_use_for": sc.get("do_not_use_for"),
                "inputs": (triggers.get("on_request") or {}).get("inputs", []),
                "output_format": (triggers.get("on_request") or {}).get("output_format"),
                "on_request": on_request,
                "on_schedule": on_schedule,
                "on_event": on_event,
                "_path": str(path),
                "status": cfg.get("status", "draft"),  # disk value, informational only
            })

        self._cards = cards

        # --- Resolve promoted set from ADB (single source of truth) ---
        if self._skill_store is not None:
            try:
                self._promoted = self._skill_store.list_promoted_workflow_skills()
            except Exception as exc:
                # WARNING — do not silently serve drafts to the classifier.
                # If the skill_store is wired but throws, we set promoted to
                # empty (no unknown-status cards reach the classifier) and log
                # prominently so operators can diagnose the store issue.
                log.warning(
                    "ShimWorkflows: list_promoted_workflow_skills FAILED — "
                    "all_cards() will return 0 cards to prevent drafts reaching "
                    "the Tier-1 LLM router until the store recovers. err=%s", exc,
                )
                self._promoted = set()
        else:
            # Laptop mode: no skill_store.  Explicit documented decision:
            # serve all on-disk cards.  This is intentional for dev/laptop
            # usage.  Log at INFO so it is visible but not alarming.
            log.info(
                "ShimWorkflows: no skill_store wired; serving all %d on-disk "
                "workflow cards (laptop mode — ADB not required).",
                len(cards),
            )
            self._promoted = set()  # not used in no-store path

        log.info(
            "shim_workflows loaded %d disk cards from %s; "
            "adb_backed=%s promoted=%d",
            len(cards), self.dir,
            self._skill_store is not None,
            len(self._promoted) if self._skill_store is not None else len(cards),
        )

    def reload(self) -> None:
        """Re-run load() — call after a PROMOTE to pick up newly promoted skills."""
        self.load()

    # ------------------------------------------------------------------
    # Public card accessors
    # ------------------------------------------------------------------

    def all_cards(self) -> list[dict]:
        """Return skill cards safe to feed to the Tier-1 LLM router.

        When a skill_store is wired:  returns ONLY cards whose (persona,
        skill_name) are reported promoted/production by ADB.  Drafts are
        excluded — they never reach the classifier.

        When no skill_store (laptop mode):  returns all on-disk cards
        (consistent with pre-fix behaviour; INFO-logged in load()).
        """
        if self._skill_store is None:
            # Laptop mode — serve all
            return list(self._cards)

        # ADB-backed — filter to promoted set only
        return [
            c for c in self._cards
            if (c.get("persona"), c.get("name")) in self._promoted
        ]

    def all_cards_including_draft(self) -> list[dict]:
        """Return ALL on-disk cards regardless of promotion status.

        For tooling, CLI introspection, and tests.  NOT used by the Tier-1
        LLM classifier.
        """
        return list(self._cards)

    def cards_for(self, persona: str) -> list[dict]:
        """Promoted cards for a specific persona (router-safe)."""
        return [c for c in self.all_cards() if c.get("persona") == persona]

    def request_invocable(self, persona: str | None = None) -> list[dict]:
        """Promoted on_request cards, optionally filtered by persona."""
        out = self.all_cards() if not persona else self.cards_for(persona)
        return [c for c in out if c.get("on_request")]

    def render_for_persona_prompt(self, persona: str) -> str:
        """Render promoted workflow skill cards for persona as a prompt block."""
        cards = self.cards_for(persona)
        if not cards:
            return f"# (no workflow skills registered for persona {persona})"
        lines = [f"# Workflow skills available to {persona} (Tier 1)"]
        for c in cards:
            lines.append(f"\n## {c['name']}")
            if c.get("summary"):
                lines.append(f"  summary: {c['summary']}")
            if c.get("use_when"):
                lines.append(f"  use_when: {c['use_when']}")
            if c.get("example_invocations"):
                lines.append(f"  example_invocations:")
                for ex in c["example_invocations"]:
                    lines.append(f"    - {ex}")
            if c.get("do_not_use_for"):
                lines.append(f"  do_not_use_for: {c['do_not_use_for']}")
            if c.get("inputs"):
                ins = ", ".join(
                    f"{i.get('name')} ({i.get('type', 'string')})"
                    for i in c["inputs"]
                )
                lines.append(f"  inputs: {ins}")
        return "\n".join(lines)
