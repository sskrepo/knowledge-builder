"""BUG-queue-2ad9a — ShimWorkflows ADB-aware promotion filtering + filestore impl.

Tests:
  - ShimWorkflows ADB-aware all_cards() filtering (8 scenarios)
  - FilestoreSkillStore.list_promoted_workflow_skills focused test

Verifies that ShimWorkflows resolves promotion status from the skill_store
(ADB) rather than from the on-disk YAML `status:` field, exactly mirroring
the ShimKb pattern (ADR-015 Option B).

Test matrix (spec from bug report):
  1.  store-backed, only A promoted:
        all_cards() returns ONLY A (even though disk has A/B/C all draft)
  2.  all_cards_including_draft() returns all 3 disk cards regardless
  3.  skill_store=None → laptop mode: all_cards() serves all (INFO logged)
  4.  skill_store raises → WARNING logged + all_cards() returns 0 cards
        (NOT unknown-status cards — no drafts sneak to the classifier)
  5.  cards_for(persona) respects the promotion filter
  6.  render_for_persona_prompt excludes draft skills
  7.  reload() re-queries the skill_store
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from framework.orchestrator.shim_workflows import ShimWorkflows
from framework.deploy.skill_store.filestore import FilestoreSkillStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PERSONA = "tpm"

_CARD_BODIES = {
    "A": {
        "workflow_skill": "A",
        "persona": _PERSONA,
        "status": "draft",      # disk always says draft — ADB is authoritative
        "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
        "skill_card": {
            "summary": "skill A summary",
            "use_when": "Use A when ...",
            "example_invocations": ["make an A pptx"],
        },
    },
    "B": {
        "workflow_skill": "B",
        "persona": _PERSONA,
        "status": "draft",
        "trigger": {"on_request": {"enabled": True, "output_format": "eml"}},
        "skill_card": {
            "summary": "skill B summary",
            "use_when": "Use B when ...",
            "example_invocations": ["send B email"],
        },
    },
    "C": {
        "workflow_skill": "C",
        "persona": _PERSONA,
        "status": "draft",
        "trigger": {"on_schedule": {"cron": "0 16 * * 5"}},
        "skill_card": {
            "summary": "skill C summary",
            "use_when": "Use C for scheduled ...",
        },
    },
}


def _make_disk_dir(tmp_path: Path, cards: dict = None) -> Path:
    """Write workflow skill YAMLs to a temporary directory."""
    if cards is None:
        cards = _CARD_BODIES
    ws_dir = tmp_path / "workflow_skills" / _PERSONA
    ws_dir.mkdir(parents=True, exist_ok=True)
    for name, body in cards.items():
        (ws_dir / f"{name}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    # Return the parent (workflow_skills/)
    return tmp_path / "workflow_skills"


def _make_store(promoted_pairs: set[tuple[str, str]]) -> MagicMock:
    store = MagicMock()
    store.list_promoted_workflow_skills.return_value = promoted_pairs
    return store


# ---------------------------------------------------------------------------
# 1. Store-backed: only A promoted — all_cards() returns only A
# ---------------------------------------------------------------------------

def test_all_cards_store_backed_filters_to_promoted(tmp_path: Path):
    """When skill_store reports only {(tpm, A)} promoted, all_cards() returns
    ONLY card A — even though disk has A, B, C all with status: draft."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    names = {c["name"] for c in cards}

    assert names == {"A"}, (
        f"Expected only promoted skill 'A' but got: {names}. "
        "Draft skills B and C must NOT reach the Tier-1 classifier."
    )
    assert len(cards) == 1


def test_all_cards_store_backed_excludes_all_drafts(tmp_path: Path):
    """When skill_store reports no promotions, all_cards() returns empty list."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store(set())
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    assert cards == [], (
        "With no promoted skills in ADB, all_cards() must return [] — "
        "zero drafts should reach the classifier."
    )


def test_all_cards_store_backed_multiple_promoted(tmp_path: Path):
    """Multiple promoted skills are all returned by all_cards()."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A"), (_PERSONA, "B")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    names = {c["name"] for c in cards}
    assert names == {"A", "B"}


# ---------------------------------------------------------------------------
# 2. all_cards_including_draft() returns all 3 regardless of store
# ---------------------------------------------------------------------------

def test_all_cards_including_draft_returns_all(tmp_path: Path):
    """all_cards_including_draft() always returns every disk card."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A")})    # only A promoted
    shim = ShimWorkflows(ws_dir, skill_store=store)

    draft_cards = shim.all_cards_including_draft()
    names = {c["name"] for c in draft_cards}
    assert names == {"A", "B", "C"}, (
        "all_cards_including_draft() must return all 3 on-disk cards "
        "regardless of promotion status — used for tooling/introspection."
    )


def test_all_cards_including_draft_no_store(tmp_path: Path):
    """all_cards_including_draft() works in laptop mode too."""
    ws_dir = _make_disk_dir(tmp_path)
    shim = ShimWorkflows(ws_dir, skill_store=None)

    draft_cards = shim.all_cards_including_draft()
    assert len(draft_cards) == 3


# ---------------------------------------------------------------------------
# 3. skill_store=None → laptop mode: all_cards() serves all (INFO logged)
# ---------------------------------------------------------------------------

def test_laptop_mode_serves_all_cards(tmp_path: Path, caplog):
    """When skill_store=None, all_cards() returns all on-disk cards AND
    logs at INFO to make the laptop-mode decision explicit."""
    ws_dir = _make_disk_dir(tmp_path)
    with caplog.at_level(logging.INFO, logger="framework.orchestrator.shim_workflows"):
        shim = ShimWorkflows(ws_dir, skill_store=None)

    cards = shim.all_cards()
    names = {c["name"] for c in cards}
    assert names == {"A", "B", "C"}, (
        "Laptop mode (no skill_store) must serve all on-disk cards."
    )

    # Verify INFO log was emitted — this makes the laptop-mode decision explicit
    info_msgs = [
        r.message for r in caplog.records
        if r.levelno == logging.INFO and "laptop mode" in r.message.lower()
    ]
    assert info_msgs, (
        "ShimWorkflows must log at INFO when skill_store=None (laptop mode) "
        "so the decision to serve all cards is explicit, not silent."
    )


# ---------------------------------------------------------------------------
# 4. skill_store raises → WARNING + all_cards() returns 0 cards
# ---------------------------------------------------------------------------

def test_store_failure_returns_zero_cards_not_drafts(tmp_path: Path, caplog):
    """If list_promoted_workflow_skills raises, all_cards() returns [] — NOT
    unknown-status drafts.  The failure is logged at WARNING."""
    ws_dir = _make_disk_dir(tmp_path)
    store = MagicMock()
    store.list_promoted_workflow_skills.side_effect = RuntimeError("ADB unreachable")

    with caplog.at_level(logging.WARNING, logger="framework.orchestrator.shim_workflows"):
        shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    assert cards == [], (
        "When the skill_store raises, all_cards() must return [] — "
        "it MUST NOT silently include unknown-status (draft) cards."
    )

    warning_msgs = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "list_promoted_workflow_skills" in r.message
    ]
    assert warning_msgs, (
        "A WARNING must be logged when list_promoted_workflow_skills fails."
    )


# ---------------------------------------------------------------------------
# 5. cards_for(persona) respects promotion filter
# ---------------------------------------------------------------------------

def test_cards_for_respects_promotion(tmp_path: Path):
    """cards_for() delegates to all_cards(), so it also excludes drafts."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.cards_for(_PERSONA)
    assert len(cards) == 1
    assert cards[0]["name"] == "A"

    # Different persona → no cards (A, B, C all belong to _PERSONA)
    cards_other = shim.cards_for("ops_eng")
    assert cards_other == []


# ---------------------------------------------------------------------------
# 6. render_for_persona_prompt excludes draft skills
# ---------------------------------------------------------------------------

def test_render_for_persona_prompt_excludes_drafts(tmp_path: Path):
    """render_for_persona_prompt() only includes promoted skills."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    rendered = shim.render_for_persona_prompt(_PERSONA)
    assert "## A" in rendered, "Promoted skill A must appear in rendered prompt"
    assert "## B" not in rendered, "Draft skill B must NOT appear in rendered prompt"
    assert "## C" not in rendered, "Draft skill C must NOT appear in rendered prompt"


# ---------------------------------------------------------------------------
# 7. reload() re-queries the skill_store
# ---------------------------------------------------------------------------

def test_reload_picks_up_new_promotions(tmp_path: Path):
    """After reload(), all_cards() reflects the updated promoted set from ADB."""
    ws_dir = _make_disk_dir(tmp_path)

    # Initially only A promoted
    store = _make_store({(_PERSONA, "A")})
    shim = ShimWorkflows(ws_dir, skill_store=store)
    assert {c["name"] for c in shim.all_cards()} == {"A"}

    # Simulate ADB promoting B too
    store.list_promoted_workflow_skills.return_value = {(_PERSONA, "A"), (_PERSONA, "B")}
    shim.reload()

    names = {c["name"] for c in shim.all_cards()}
    assert names == {"A", "B"}, (
        "After reload(), the newly promoted skill B must appear in all_cards()."
    )


# ---------------------------------------------------------------------------
# 8. Confirm the 3 real tpm promoted skills would pass through
#    (structural test — disk files exist with these names, ADB promotes them)
# ---------------------------------------------------------------------------

def test_real_tpm_skills_pass_through_when_promoted(tmp_path: Path):
    """The 3 genuinely-promoted tpm skills (26ai_confluence_pptx,
    26ai_fa_db_upgrade_pptx, weekly_exec_review) would appear in all_cards()
    if ADB reports them as promoted — even though disk says status: draft.

    This is the core correctness assertion of the ADB-aware approach:
    disk status is irrelevant; ADB is authoritative.
    """
    promoted_names = [
        "26ai_confluence_pptx",
        "26ai_fa_db_upgrade_pptx",
        "weekly_exec_review",
    ]

    # Build a minimal disk directory mirroring the real workflow_skills/tpm/
    ws_dir = tmp_path / "workflow_skills"
    tpm_dir = ws_dir / "tpm"
    tpm_dir.mkdir(parents=True, exist_ok=True)
    for name in promoted_names:
        body = {
            "workflow_skill": name,
            "persona": "tpm",
            "status": "draft",   # disk always says draft — ADB is the source of truth
            "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
            "skill_card": {
                "summary": f"{name} summary",
                "use_when": f"Use when you need {name}",
            },
        }
        (tpm_dir / f"{name}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")

    # ADB says all 3 are promoted
    promoted_pairs = {("tpm", n) for n in promoted_names}
    store = _make_store(promoted_pairs)
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    returned_names = {c["name"] for c in cards}
    assert returned_names == set(promoted_names), (
        "All 3 genuinely-promoted tpm skills must appear in all_cards() when "
        "ADB reports them as promoted, regardless of disk status: draft."
    )


# ---------------------------------------------------------------------------
# FilestoreSkillStore.list_promoted_workflow_skills — focused tests
# ---------------------------------------------------------------------------

class TestFilestoreListPromotedWorkflowSkills:
    """Focused tests for the filestore implementation of
    list_promoted_workflow_skills, using a tmp_path-injected wf_promo_root
    so no real ~/.kbf is touched."""

    def _make_store(self, tmp_path: Path) -> tuple[FilestoreSkillStore, Path]:
        wf_promo_root = tmp_path / "workflow_promotions"
        store = FilestoreSkillStore(
            repo_root=tmp_path,
            wf_promo_root=wf_promo_root,
        )
        return store, wf_promo_root

    def _write_promo(
        self,
        wf_promo_root: Path,
        persona: str,
        skill_name: str,
        status: str = "promoted",
    ) -> None:
        persona_dir = wf_promo_root / persona
        persona_dir.mkdir(parents=True, exist_ok=True)
        entry = {"persona": persona, "skill_name": skill_name, "status": status}
        (persona_dir / f"{skill_name}.yaml").write_text(
            yaml.safe_dump(entry), encoding="utf-8"
        )

    def test_empty_root_returns_empty_set(self, tmp_path: Path):
        store, _ = self._make_store(tmp_path)
        result = store.list_promoted_workflow_skills()
        assert result == set()

    def test_missing_root_returns_empty_set(self, tmp_path: Path):
        """wf_promo_root doesn't exist at all → empty set, no crash."""
        store = FilestoreSkillStore(
            repo_root=tmp_path,
            wf_promo_root=tmp_path / "nonexistent",
        )
        assert store.list_promoted_workflow_skills() == set()

    def test_promoted_skill_returned(self, tmp_path: Path):
        store, wf_promo_root = self._make_store(tmp_path)
        self._write_promo(wf_promo_root, "tpm", "weekly_exec_review", "promoted")
        result = store.list_promoted_workflow_skills()
        assert ("tpm", "weekly_exec_review") in result

    def test_production_skill_returned(self, tmp_path: Path):
        store, wf_promo_root = self._make_store(tmp_path)
        self._write_promo(wf_promo_root, "tpm", "weekly_exec_review", "production")
        result = store.list_promoted_workflow_skills()
        assert ("tpm", "weekly_exec_review") in result

    def test_draft_skill_excluded(self, tmp_path: Path):
        """Draft skills are NOT returned."""
        store, wf_promo_root = self._make_store(tmp_path)
        self._write_promo(wf_promo_root, "tpm", "draft_skill", "draft")
        result = store.list_promoted_workflow_skills()
        assert ("tpm", "draft_skill") not in result
        assert result == set()

    def test_persona_filter_works(self, tmp_path: Path):
        store, wf_promo_root = self._make_store(tmp_path)
        self._write_promo(wf_promo_root, "tpm", "skill_a", "promoted")
        self._write_promo(wf_promo_root, "ops_eng", "skill_b", "promoted")

        tpm_result = store.list_promoted_workflow_skills(persona="tpm")
        assert ("tpm", "skill_a") in tpm_result
        assert ("ops_eng", "skill_b") not in tpm_result

        all_result = store.list_promoted_workflow_skills()
        assert ("tpm", "skill_a") in all_result
        assert ("ops_eng", "skill_b") in all_result

    def test_multiple_personas_multiple_skills(self, tmp_path: Path):
        store, wf_promo_root = self._make_store(tmp_path)
        self._write_promo(wf_promo_root, "tpm", "A", "promoted")
        self._write_promo(wf_promo_root, "tpm", "B", "promoted")
        self._write_promo(wf_promo_root, "ops_eng", "C", "production")
        self._write_promo(wf_promo_root, "ops_eng", "D", "draft")  # excluded

        result = store.list_promoted_workflow_skills()
        assert result == {("tpm", "A"), ("tpm", "B"), ("ops_eng", "C")}
        assert ("ops_eng", "D") not in result
