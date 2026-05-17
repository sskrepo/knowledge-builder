"""shim_workflows — aggregator of workflow skill cards (ADB-aware).

Per ADR-006 amend 2 + ADR-016 + ADR-033 + BUG-queue-2ad9a fix.

ADR-033 (promoted workflow skill definitions resolved from ADB, not disk):
  When a skill_store is wired, BOTH the promotion status AND the card body
  (name, use_when, example_invocations, inputs, output_format, trigger,
  source_binding, etc.) are resolved from the ADB-committed workflow_skill
  artifact in KBF_SKILL_ARTIFACTS.  Disk YAML is consulted ONLY in the
  no-skill-store laptop path.

  This closes the inconsistency where:
    - ADB held the definitive promoted artifact (with correct source_binding)
    - shim_workflows built the card body from a disk byproduct that could be
      stale, absent, or lack source_binding — causing promoted skills to be
      invisible to the Tier-1 router whenever the disk file was cleaned.

  Mirrors ADR-015 Option B (shim_kb reads promoted KB entries from ADB, not
  from persona_builders/*.yaml).

When a skill_store is provided (production / laptop with ADB):
  - list_promoted_workflow_skills() yields the (persona, skill_name) pairs.
  - For each pair, read_artifact(persona, skill_name, "workflow_skill")
    provides the committed YAML text — parsed to build the card dict.
  - Disk YAML is not consulted.  A skill promoted in ADB but absent on disk
    is still correctly routed.
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


def _cfg_to_card(cfg: dict, source: str = "disk", path: str = "") -> dict:
    """Parse a workflow skill YAML dict into a routing card dict.

    Shared by both the disk path (laptop mode) and the ADB artifact path
    (promoted skills when skill_store is wired).  Every key that the Tier-1
    classifier, render path, and executor need must be extracted here.

    Args:
        cfg:    Parsed YAML dict (from disk or ADB artifact).
        source: "disk" or "adb" — informational, stored in card["_source"].
        path:   Original disk path (empty for ADB-sourced cards).
    """
    persona = cfg.get("persona")
    sc = cfg.get("skill_card") or {}
    triggers = cfg.get("trigger") or {}
    on_request = bool((triggers.get("on_request") or {}).get("enabled"))
    on_schedule = bool((triggers.get("on_schedule") or {}).get("cron"))
    on_event = bool((triggers.get("on_event") or {}).get("enabled"))
    # source_binding is load-bearing for ask_parameterized skills — must be
    # included in the card so both shim_workflows and maybe_render_artifact
    # can determine the skill's mode without touching disk.
    source_binding = cfg.get("source_binding") or {}
    return {
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
        "_path": path,
        "_source": source,
        # Store full cfg so maybe_render_artifact and _any_promoted_skill_requires_ephemeral
        # can read source_binding / trigger / delivery without re-reading disk.
        "_cfg": cfg,
        "source_binding": source_binding,
        "status": cfg.get("status", "draft"),  # disk/ADB value, informational only
    }


class ShimWorkflows:
    def __init__(self, workflow_skills_dir: Path, skill_store=None):
        """Initialise ShimWorkflows.

        Args:
            workflow_skills_dir: Path to framework/workflow_skills/ directory.
            skill_store: Optional SkillStore instance.  When provided, ADB is
                the single source of truth for BOTH promotion status AND card
                body (ADR-033).  When None, all on-disk cards are served with
                an INFO log explaining laptop-mode behaviour.
        """
        self.dir = Path(workflow_skills_dir)
        self._skill_store = skill_store
        self._cards: list[dict] = []          # promoted cards (ADB-sourced or disk)
        self._disk_cards: list[dict] = []     # all disk cards (for all_cards_including_draft)
        self._promoted: set[tuple[str, str]] = set()  # (persona, skill_name)
        self.load()

    # ------------------------------------------------------------------
    # Core loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Reload promoted card bodies from ADB (or disk in laptop mode).

        ADR-033: when skill_store is wired, card bodies are built from the
        committed ADB workflow_skill artifact — NOT from disk YAML.  The disk
        is scanned separately only to populate _disk_cards for
        all_cards_including_draft() (introspection / tooling).
        """
        # --- Always scan disk for all_cards_including_draft() ---
        disk_cards: list[dict] = []
        if self.dir.exists():
            for path in sorted(self.dir.rglob("*.yaml")):
                if path.name.startswith("_"):
                    continue
                try:
                    with open(path) as f:
                        cfg = yaml.safe_load(f) or {}
                except Exception as e:
                    log.warning("shim_workflows: failed to load %s: %s", path, e)
                    continue
                disk_cards.append(_cfg_to_card(cfg, source="disk", path=str(path)))
        else:
            log.warning("workflow_skills dir not found: %s", self.dir)

        self._disk_cards = disk_cards

        # --- ADB-backed path (production / laptop with ADB) ---
        if self._skill_store is not None:
            try:
                promoted_pairs = self._skill_store.list_promoted_workflow_skills()
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
                self._cards = []
                log.info(
                    "shim_workflows loaded %d disk cards from %s; "
                    "adb_backed=True promoted=0 (store error)",
                    len(disk_cards), self.dir,
                )
                return

            self._promoted = promoted_pairs

            # ADR-033: build card bodies from ADB artifact, not disk.
            adb_cards: list[dict] = []
            for (persona, skill_name) in sorted(promoted_pairs):
                try:
                    content = self._skill_store.read_artifact(
                        persona, skill_name, "workflow_skill"
                    )
                except Exception as exc:
                    log.warning(
                        "ShimWorkflows: read_artifact FAILED for promoted skill "
                        "%s.%s — skipping (err=%s). "
                        "Promoted in ADB but artifact unreadable.",
                        persona, skill_name, exc,
                    )
                    continue

                if content is None:
                    # Promoted in ADB (list_promoted_workflow_skills returned it)
                    # but the workflow_skill artifact row is absent.  This is a
                    # data-integrity issue — log prominently, skip.
                    log.warning(
                        "ShimWorkflows: promoted skill %s.%s has no "
                        "workflow_skill artifact in ADB — skipping.  "
                        "Re-commit the skill to fix this.",
                        persona, skill_name,
                    )
                    continue

                try:
                    cfg = yaml.safe_load(content) or {}
                except Exception as exc:
                    log.warning(
                        "ShimWorkflows: could not parse ADB workflow_skill "
                        "artifact for %s.%s: %s — skipping.",
                        persona, skill_name, exc,
                    )
                    continue

                card = _cfg_to_card(cfg, source="adb", path="")
                adb_cards.append(card)
                log.debug(
                    "ShimWorkflows: loaded ADB card for promoted skill %s.%s "
                    "(source_binding.mode=%s)",
                    persona, skill_name,
                    (cfg.get("source_binding") or {}).get("mode", "author_fixed"),
                )

            self._cards = adb_cards
            log.info(
                "shim_workflows: ADB-backed load complete. "
                "promoted=%d loaded_from_adb=%d disk_cards=%d dir=%s",
                len(promoted_pairs), len(adb_cards), len(disk_cards), self.dir,
            )

        else:
            # Laptop mode: no skill_store.  Explicit documented decision:
            # serve all on-disk cards.  This is intentional for dev/laptop
            # usage.  Log at INFO so it is visible but not alarming.
            log.info(
                "ShimWorkflows: no skill_store wired; serving all %d on-disk "
                "workflow cards (laptop mode — ADB not required).",
                len(disk_cards),
            )
            self._cards = disk_cards
            self._promoted = set()  # not used in no-store path
            log.info(
                "shim_workflows loaded %d disk cards from %s; "
                "adb_backed=False promoted=%d",
                len(disk_cards), self.dir, len(disk_cards),
            )

    def reload(self) -> None:
        """Re-run load() — call after a PROMOTE to pick up newly promoted skills."""
        self.load()

    # ------------------------------------------------------------------
    # Public card accessors
    # ------------------------------------------------------------------

    def all_cards(self) -> list[dict]:
        """Return skill cards safe to feed to the Tier-1 LLM router.

        When a skill_store is wired (ADR-033):  returns ONLY cards built from
        the ADB-committed workflow_skill artifact for promoted skills.  Drafts
        are excluded.  Card bodies (incl. source_binding) reflect the ADB
        artifact, not disk.

        When no skill_store (laptop mode):  returns all on-disk cards
        (consistent with pre-fix behaviour; INFO-logged in load()).
        """
        # self._cards is populated in load():
        #   - ADB-backed: ADB artifact bodies for promoted (persona,skill_name) pairs
        #   - Laptop: all disk cards
        return list(self._cards)

    def all_cards_including_draft(self) -> list[dict]:
        """Return ALL on-disk cards regardless of promotion status.

        For tooling, CLI introspection, and tests.  NOT used by the Tier-1
        LLM classifier.  Returns disk cards even in ADB-backed mode.
        """
        return list(self._disk_cards)

    def cards_for(self, persona: str) -> list[dict]:
        """Promoted cards for a specific persona (router-safe)."""
        return [c for c in self.all_cards() if c.get("persona") == persona]

    def request_invocable(self, persona: str | None = None) -> list[dict]:
        """Promoted on_request cards, optionally filtered by persona."""
        out = self.all_cards() if not persona else self.cards_for(persona)
        return [c for c in out if c.get("on_request")]

    def render_for_persona_prompt(self, persona: str) -> str:
        """Render promoted workflow skill cards for persona as a prompt block.

        ADR-038 §D: includes routing_queries from the skill_card as exemplar
        matching signal for the Tier-1 classifier.  The routing_queries are
        curated consumer queries produced at DESIGN_SKILL time and carried
        through synthesis into the committed ADB workflow_skill artifact.

        CRITICAL: this method ONLY returns promoted skills (all_cards() is
        promoted-only per ADR-033 / BUG-queue-2ad9a invariant).  Adding
        routing_queries here does NOT weaken the promoted-only guarantee —
        it only enriches the matching signal for already-promoted skills.
        """
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
            # ADR-038 §D: include routing_queries from the skill_card as
            # exemplar matching signal.  These are curated positive consumer
            # queries that SHOULD route to this skill.  Negative queries are
            # not included here (they are used only in EVAL Path-B self-test).
            # routing_queries is sourced from the committed ADB artifact's
            # skill_card.routing_queries field.
            skill_card = (c.get("_cfg") or {}).get("skill_card") or {}
            rq = skill_card.get("routing_queries") or {}
            positives = rq.get("positive") or []
            if positives:
                lines.append(f"  routing_queries:")
                for rq_q in positives[:5]:  # cap to 5 for prompt size
                    lines.append(f"    - {rq_q}")
        return "\n".join(lines)

    def resolve_only(self, query: str, scope: str = "promoted_only") -> dict:
        """Router resolve-only mode for EVAL Path-B routing self-test.

        ADR-038 §B.3: Returns which skill + tier WOULD be selected for the
        given query without executing the skill.  No side effects.  No output.
        No artifact produced.

        When scope="ingest_or_later" (used by EVAL Path-B), considers all
        on-disk skills (all_cards_including_draft()), not only promoted skills.
        The default scope="promoted_only" returns only promoted skills.

        CRITICAL: this method MUST NOT modify all_cards() behaviour.  The
        default consumption path (/api/v1/ask) is unaffected — it always calls
        all_cards() which returns only promoted skills per ADR-033.  This
        resolve_only method is a separate invocation path used only internally
        by EVAL Path-B.

        Args:
            query:  The consumer query to resolve.
            scope:  "promoted_only" (default) or "ingest_or_later" (EVAL Path-B).

        Returns:
            dict with keys: skill_id, skill_name, tier, confidence, matched
        """
        if scope == "ingest_or_later":
            candidate_cards = self.all_cards_including_draft()
        else:
            candidate_cards = self.all_cards()

        if not candidate_cards:
            return {
                "skill_id": None,
                "skill_name": None,
                "tier": 2,
                "confidence": 0.0,
                "matched": False,
            }

        # Simple embedding-free matching: score each card by term overlap between
        # the query and the card's routing_queries.positive + example_invocations +
        # summary + use_when.  This is sufficient for EVAL Path-B self-test where
        # the positive queries are curated to be highly specific to the skill.
        # A real vector search can be plugged in here when available.
        query_lower = query.lower()
        query_tokens = set(query_lower.split())

        best_score = 0.0
        best_card = None

        for card in candidate_cards:
            card_tokens: set[str] = set()

            # Extract tokens from all routing signal fields
            skill_card = (card.get("_cfg") or {}).get("skill_card") or {}
            rq = skill_card.get("routing_queries") or {}

            for pos_q in (rq.get("positive") or []):
                card_tokens.update(pos_q.lower().split())
            for ex in (card.get("example_invocations") or []):
                card_tokens.update(ex.lower().split())
            if card.get("summary"):
                card_tokens.update(card["summary"].lower().split())
            if card.get("use_when"):
                card_tokens.update(card["use_when"].lower().split())

            if not card_tokens:
                continue

            overlap = len(query_tokens & card_tokens)
            score = overlap / max(len(query_tokens), 1)

            if score > best_score:
                best_score = score
                best_card = card

        if best_card and best_score > 0:
            return {
                "skill_id": f"{best_card.get('persona', '?')}.{best_card.get('name', '?')}",
                "skill_name": best_card.get("name"),
                "persona": best_card.get("persona"),
                "tier": 1,
                "confidence": round(best_score, 3),
                "matched": True,
            }

        return {
            "skill_id": None,
            "skill_name": None,
            "tier": 2,
            "confidence": 0.0,
            "matched": False,
        }
