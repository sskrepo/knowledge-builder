"""ADR-033 + BUG-queue-2ad9a — ShimWorkflows ADB-aware promotion + card-body resolution.

Tests:
  - ShimWorkflows ADR-033: card bodies resolved from ADB artifact, not disk
  - ShimWorkflows ADB-aware all_cards() filtering (promotion gating)
  - FilestoreSkillStore.list_promoted_workflow_skills focused test

ADR-033 core behaviour under test:
  When a skill_store is wired:
    - all_cards() bodies come from read_artifact(persona, skill_name, "workflow_skill")
    - disk-absent-but-ADB-promoted skill IS still routable
    - source_binding is carried from the ADB artifact into the card dict
  When skill_store=None (laptop mode):
    - all_cards() returns all on-disk cards (INFO logged, explicit decision)
  Store error path:
    - list_promoted_workflow_skills raises → all_cards() returns [] (no drafts)

Test matrix:
  ADR-033 specific:
    T1.  Card body from ADB artifact (not disk): name/summary/source_binding all from ADB
    T2.  Disk-absent-but-ADB-promoted: skill routable even with no disk YAML
    T3.  ADB artifact has source_binding.mode=ask_parameterized: card carries it through
    T4.  read_artifact returns None (artifact absent in ADB): skill skipped with WARNING

  Pre-ADR-033 promotion gating (still valid):
    T5.  store-backed, only A promoted: all_cards() returns ONLY A
    T6.  store-backed, empty promoted set: all_cards() returns []
    T7.  store-backed, multiple promoted: all cards returned
    T8.  all_cards_including_draft() returns all 3 disk cards regardless
    T9.  skill_store=None → laptop mode: all_cards() serves all (INFO logged)
    T10. skill_store raises → WARNING logged + all_cards() returns 0 cards
    T11. cards_for(persona) respects promotion filter
    T12. render_for_persona_prompt excludes draft skills
    T13. reload() re-queries the skill_store
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
# Fixtures and helpers
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

# ADB artifact bodies — may differ from disk (e.g., include source_binding)
_ADB_BODIES = {
    "A": {
        "workflow_skill": "A",
        "persona": _PERSONA,
        "status": "promoted",   # ADB artifact carries promoted status
        "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
        "skill_card": {
            "summary": "skill A summary (ADB version)",
            "use_when": "Use A when ADB says so ...",
            "example_invocations": ["make an A pptx (adb)"],
        },
        "source_binding": {
            "mode": "author_fixed",
        },
    },
    "B": {
        "workflow_skill": "B",
        "persona": _PERSONA,
        "status": "promoted",
        "trigger": {"on_request": {"enabled": True, "output_format": "eml",
                                   "inputs": [{"name": "page_id", "type": "string"}]}},
        "skill_card": {
            "summary": "skill B summary (ADB version)",
            "use_when": "Use B when ADB says so ...",
            "example_invocations": ["send B email (adb)"],
        },
        "source_binding": {
            "mode": "ask_parameterized",
            "source_type": "confluence",
            "input_param": "page_id",
            "ingest_on_demand": True,
            "space_allow_list": ["OCIFACP"],
            "ephemeral_ttl_seconds": 300,
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


def _make_store(
    promoted_pairs: set[tuple[str, str]],
    adb_bodies: dict = None,
    artifact_missing: set[tuple[str, str]] = None,
    artifact_raises: set[tuple[str, str]] = None,
) -> MagicMock:
    """Create a MagicMock skill_store with properly mocked read_artifact.

    ADR-033: the store must also mock read_artifact returning YAML text for
    each promoted skill, so yaml.safe_load receives a string, not a MagicMock.

    Args:
        promoted_pairs:   set of (persona, skill_name) tuples.
        adb_bodies:       dict of skill_name → cfg dict.  Defaults to _ADB_BODIES.
        artifact_missing: set of (persona, skill_name) pairs whose read_artifact
                          returns None (artifact absent in ADB).
        artifact_raises:  set of (persona, skill_name) pairs whose read_artifact
                          raises RuntimeError.
    """
    if adb_bodies is None:
        adb_bodies = _ADB_BODIES
    if artifact_missing is None:
        artifact_missing = set()
    if artifact_raises is None:
        artifact_raises = set()

    store = MagicMock()
    store.list_promoted_workflow_skills.return_value = promoted_pairs

    def _read_artifact(persona, skill_name, artifact_type):
        key = (persona, skill_name)
        if key in artifact_raises:
            raise RuntimeError(f"simulated read_artifact failure for {key}")
        if key in artifact_missing:
            return None
        # Return YAML text (string) if we have an ADB body; else return a
        # minimal YAML so yaml.safe_load gets a valid string.
        body = adb_bodies.get(skill_name) or {
            "workflow_skill": skill_name,
            "persona": persona,
            "status": "promoted",
            "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
            "skill_card": {"summary": f"{skill_name} from ADB", "use_when": "..."},
        }
        return yaml.safe_dump(body)

    store.read_artifact.side_effect = _read_artifact
    return store


# ---------------------------------------------------------------------------
# T1. ADR-033: Card body comes from ADB artifact, not disk
# ---------------------------------------------------------------------------

def test_card_body_from_adb_not_disk(tmp_path: Path):
    """ADR-033 core: when skill_store is wired, the card body (summary,
    use_when, etc.) is loaded from ADB artifact, NOT from the disk YAML.

    The ADB body has "(ADB version)" in the summary; the disk body does not.
    all_cards() must return the ADB version.
    """
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    assert len(cards) == 1
    card = cards[0]
    assert card["name"] == "A"
    assert card["_source"] == "adb", (
        "Card must be marked as ADB-sourced (_source='adb') per ADR-033."
    )
    assert "ADB version" in card["summary"], (
        "Card summary must come from ADB artifact, not disk YAML."
    )


# ---------------------------------------------------------------------------
# T2. ADR-033: Disk-absent-but-ADB-promoted skill is still routable
# ---------------------------------------------------------------------------

def test_disk_absent_but_adb_promoted_skill_is_routable(tmp_path: Path):
    """ADR-033 critical: a skill whose disk YAML does not exist is still
    routable via all_cards() when it is promoted in ADB.

    This is the EXACT failure mode ADR-033 fixes: disk byproduct was cleaned,
    but ADB has the authoritative promoted artifact.
    """
    # Create a workflow_skills dir that has NO yaml files for skill "promoted_only"
    ws_dir = tmp_path / "workflow_skills"
    ws_dir.mkdir(parents=True)
    # (no tpm/promoted_only.yaml on disk)

    adb_body = {
        "workflow_skill": "promoted_only",
        "persona": "tpm",
        "status": "promoted",
        "trigger": {"on_request": {"enabled": True, "output_format": "eml"}},
        "skill_card": {
            "summary": "This skill has no disk file",
            "use_when": "When the disk is clean but ADB has it",
        },
        "source_binding": {
            "mode": "ask_parameterized",
            "source_type": "confluence",
            "input_param": "page_id",
            "ingest_on_demand": True,
            "space_allow_list": ["OCIFACP"],
            "ephemeral_ttl_seconds": 300,
        },
    }
    store = _make_store(
        promoted_pairs={("tpm", "promoted_only")},
        adb_bodies={"promoted_only": adb_body},
    )
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    names = {c["name"] for c in cards}
    assert "promoted_only" in names, (
        "A skill promoted in ADB must appear in all_cards() even if its "
        "disk YAML is absent.  This is the ADR-033 fix."
    )
    assert len(cards) == 1
    # Verify all_cards_including_draft is empty (no disk files)
    assert shim.all_cards_including_draft() == [], (
        "all_cards_including_draft() reflects disk — should be empty when "
        "no disk files exist."
    )


# ---------------------------------------------------------------------------
# T3. ADR-033: source_binding carried through from ADB artifact
# ---------------------------------------------------------------------------

def test_source_binding_carried_from_adb_artifact(tmp_path: Path):
    """ADR-033: source_binding.mode and the full source_binding dict must be
    present on the card when loaded from an ADB artifact.

    This is critical for ask_parameterized skills: maybe_render_artifact and
    the executor need source_binding.input_param etc. from the card.
    """
    ws_dir = _make_disk_dir(tmp_path)
    # Skill B has ask_parameterized source_binding in the ADB body
    store = _make_store({(_PERSONA, "B")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    assert len(cards) == 1
    card = cards[0]
    assert card["name"] == "B"
    sb = card.get("source_binding") or {}
    assert sb.get("mode") == "ask_parameterized", (
        "source_binding.mode must be carried through from ADB artifact."
    )
    assert sb.get("input_param") == "page_id"
    assert sb.get("ingest_on_demand") is True
    assert "OCIFACP" in sb.get("space_allow_list", [])
    # The full cfg must also be in the card for executor and render paths
    cfg = card.get("_cfg") or {}
    assert cfg.get("source_binding", {}).get("mode") == "ask_parameterized"


# ---------------------------------------------------------------------------
# T4. ADR-033: read_artifact returns None → skill skipped with WARNING
# ---------------------------------------------------------------------------

def test_artifact_none_skill_skipped_with_warning(tmp_path: Path, caplog):
    """When read_artifact returns None for a promoted skill (artifact absent
    in ADB), that skill is skipped and a WARNING is logged.

    This indicates a data-integrity issue: promoted in ADB but no artifact row.
    """
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store(
        promoted_pairs={(_PERSONA, "A")},
        artifact_missing={(_PERSONA, "A")},
    )
    with caplog.at_level(logging.WARNING, logger="framework.orchestrator.shim_workflows"):
        shim = ShimWorkflows(ws_dir, skill_store=store)

    # Skill A was promoted but has no artifact → should be absent from all_cards()
    cards = shim.all_cards()
    assert cards == [], (
        "A promoted skill with no ADB artifact must be skipped (not added to "
        "all_cards). Data integrity issue."
    )
    warning_msgs = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    assert any("workflow_skill artifact" in m for m in warning_msgs), (
        "A WARNING must be logged when a promoted skill has no ADB artifact."
    )


# ---------------------------------------------------------------------------
# T5. Store-backed: only A promoted — all_cards() returns only A
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


# ---------------------------------------------------------------------------
# T6. Store-backed: empty promoted set → all_cards() returns []
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# T7. Store-backed: multiple promoted skills returned
# ---------------------------------------------------------------------------

def test_all_cards_store_backed_multiple_promoted(tmp_path: Path):
    """Multiple promoted skills are all returned by all_cards()."""
    ws_dir = _make_disk_dir(tmp_path)
    store = _make_store({(_PERSONA, "A"), (_PERSONA, "B")})
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    names = {c["name"] for c in cards}
    assert names == {"A", "B"}


# ---------------------------------------------------------------------------
# T8. all_cards_including_draft() returns all disk cards regardless of store
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
# T9. skill_store=None → laptop mode: all_cards() serves all (INFO logged)
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
# T10. skill_store raises → WARNING + all_cards() returns 0 cards
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
# T11. cards_for(persona) respects promotion filter
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
# T12. render_for_persona_prompt excludes draft skills
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
# T13. reload() re-queries the skill_store
# ---------------------------------------------------------------------------

def test_reload_picks_up_new_promotions(tmp_path: Path):
    """After reload(), all_cards() reflects the updated promoted set from ADB."""
    ws_dir = _make_disk_dir(tmp_path)

    # Initially only A promoted
    store = _make_store({(_PERSONA, "A")})
    shim = ShimWorkflows(ws_dir, skill_store=store)
    assert {c["name"] for c in shim.all_cards()} == {"A"}

    # Simulate ADB promoting B too — update both list_promoted_workflow_skills and read_artifact
    store.list_promoted_workflow_skills.return_value = {(_PERSONA, "A"), (_PERSONA, "B")}
    # read_artifact is still properly mocked via side_effect from _make_store
    shim.reload()

    names = {c["name"] for c in shim.all_cards()}
    assert names == {"A", "B"}, (
        "After reload(), the newly promoted skill B must appear in all_cards()."
    )


# ---------------------------------------------------------------------------
# Structural: 3 real tpm promoted skills pass through when promoted
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

    # ADB says all 3 are promoted (with ADB artifact bodies)
    promoted_pairs = {("tpm", n) for n in promoted_names}
    adb_bodies = {
        name: {
            "workflow_skill": name,
            "persona": "tpm",
            "status": "promoted",
            "trigger": {"on_request": {"enabled": True, "output_format": "pptx"}},
            "skill_card": {
                "summary": f"{name} summary (ADB)",
                "use_when": f"Use when you need {name}",
            },
        }
        for name in promoted_names
    }
    store = _make_store(promoted_pairs, adb_bodies=adb_bodies)
    shim = ShimWorkflows(ws_dir, skill_store=store)

    cards = shim.all_cards()
    returned_names = {c["name"] for c in cards}
    assert returned_names == set(promoted_names), (
        "All 3 genuinely-promoted tpm skills must appear in all_cards() when "
        "ADB reports them as promoted, regardless of disk status: draft."
    )
    # Verify cards are ADB-sourced
    for card in cards:
        assert card["_source"] == "adb", (
            f"Card for {card['name']} must be ADB-sourced (_source='adb')."
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
