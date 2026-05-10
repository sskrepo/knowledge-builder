"""shim_workflows — aggregator of workflow skill cards.

Per ADR-006 amend 2 + ADR-016. Reads framework/workflow_skills/{persona}/*.yaml
and exposes per-persona skill_card dicts for Tier-1 routing.
"""
from __future__ import annotations
import logging
from pathlib import Path
import yaml

log = logging.getLogger(__name__)


class ShimWorkflows:
    def __init__(self, workflow_skills_dir: Path):
        self.dir = Path(workflow_skills_dir)
        self._cards: list[dict] = []
        self.load()

    def load(self) -> None:
        cards: list[dict] = []
        if not self.dir.exists():
            log.warning("workflow_skills dir not found: %s", self.dir)
            self._cards = []
            return
        for path in sorted(self.dir.rglob("*.yaml")):
            if path.name.startswith("_"):
                continue
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception as e:
                log.warning("failed to load %s: %s", path, e)
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
                "status": cfg.get("status", "draft"),
            })
        self._cards = cards
        log.info("shim_workflows loaded %d cards from %s", len(cards), self.dir)

    def all_cards(self) -> list[dict]:
        return list(self._cards)

    def cards_for(self, persona: str) -> list[dict]:
        return [c for c in self._cards if c.get("persona") == persona]

    def request_invocable(self, persona: str | None = None) -> list[dict]:
        out = self._cards if not persona else self.cards_for(persona)
        return [c for c in out if c.get("on_request")]

    def render_for_persona_prompt(self, persona: str) -> str:
        cards = self.cards_for(persona)
        if not cards:
            return f"# (no workflow skills registered for persona {persona})"
        lines = [f"# Workflow skills available to {persona} (Tier 1)"]
        for c in cards:
            lines.append(f"\n## {c['name']}")
            if c.get("summary"): lines.append(f"  summary: {c['summary']}")
            if c.get("use_when"): lines.append(f"  use_when: {c['use_when']}")
            if c.get("example_invocations"):
                lines.append(f"  example_invocations:")
                for ex in c["example_invocations"]:
                    lines.append(f"    - {ex}")
            if c.get("do_not_use_for"): lines.append(f"  do_not_use_for: {c['do_not_use_for']}")
            if c.get("inputs"):
                ins = ", ".join(f"{i.get('name')} ({i.get('type','string')})" for i in c["inputs"])
                lines.append(f"  inputs: {ins}")
        return "\n".join(lines)
