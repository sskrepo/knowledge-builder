"""Unit tests for framework/orchestrator/shim_kb.py.

Coverage:
  ShimKb (YAML-only, no skill_store):
    - Loads cards from *.yaml seed files
    - cards_owned_by returns only the correct persona's cards
    - cards_visible_to respects persona_visibility
    - find_kb works by name and by persona.name
    - render_for_persona_prompt produces sensible output

  ShimKb (ADB-backed via skill_store):
    - list_persona_builder_kbs(status='production') results are merged on top
    - ADB entries override YAML seed cards with the same (persona, name) key
    - ADB entries for new KBs not in YAML are appended
    - skill_store failure falls back gracefully to YAML-only

  ShimKb.reload():
    - Calls list_persona_builder_kbs again to pick up newly promoted KBs
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from framework.orchestrator.shim_kb import ShimKb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_persona_yaml(pb_dir: Path, persona: str, kb_name: str, kind: str = "vector"):
    """Write a minimal persona builder YAML to pb_dir."""
    cfg = {
        "persona": persona,
        "metadata_defaults": {"persona_visibility": [persona]},
        "knowledge_bases": [
            {
                "name": kb_name,
                "kind": kind,
                "retrieval_tools": ["vector_search"],
                "provides_fields": ["title", "summary"],
                "kb_card": {
                    "summary": f"Seed KB {kb_name}",
                    "use_when": f"when you need {kb_name}",
                },
            }
        ],
    }
    path = pb_dir / f"{persona}.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# YAML-only (no skill_store)
# ---------------------------------------------------------------------------


class TestShimKbYamlOnly:
    def test_loads_cards_from_yaml(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")
        shim = ShimKb(tmp_path)
        assert len(shim.all_cards()) == 1
        card = shim.all_cards()[0]
        assert card["name"] == "weekly_status"
        assert card["persona"] == "tpm"

    def test_skips_underscore_files(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")
        (tmp_path / "_private.yaml").write_text("persona: internal\nknowledge_bases: []")
        shim = ShimKb(tmp_path)
        assert len(shim.all_cards()) == 1

    def test_cards_owned_by(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")
        _write_persona_yaml(tmp_path, "pm", "exec_summary")
        shim = ShimKb(tmp_path)

        tpm_cards = shim.cards_owned_by("tpm")
        assert len(tpm_cards) == 1
        assert tpm_cards[0]["persona"] == "tpm"

    def test_cards_visible_to(self, tmp_path):
        # tpm.yaml with visibility [tpm, pm]
        cfg = {
            "persona": "tpm",
            "metadata_defaults": {"persona_visibility": ["tpm", "pm"]},
            "knowledge_bases": [
                {
                    "name": "cross_visible",
                    "kind": "vector",
                    "retrieval_tools": [],
                    "provides_fields": [],
                    "kb_card": {},
                }
            ],
        }
        (tmp_path / "tpm.yaml").write_text(yaml.safe_dump(cfg))
        shim = ShimKb(tmp_path)

        # both tpm and pm can see it
        assert len(shim.cards_visible_to("tpm")) == 1
        assert len(shim.cards_visible_to("pm")) == 1
        # ops_eng cannot see it
        assert len(shim.cards_visible_to("ops_eng")) == 0

    def test_find_kb_by_name(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")
        shim = ShimKb(tmp_path)

        card = shim.find_kb("weekly_status")
        assert card is not None
        assert card["name"] == "weekly_status"

    def test_find_kb_by_persona_dot_name(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")
        shim = ShimKb(tmp_path)

        card = shim.find_kb("tpm.weekly_status")
        assert card is not None
        assert card["persona"] == "tpm"

    def test_find_kb_returns_none_when_not_found(self, tmp_path):
        shim = ShimKb(tmp_path)
        assert shim.find_kb("does_not_exist") is None
        assert shim.find_kb("tpm.does_not_exist") is None

    def test_render_for_persona_prompt(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")
        shim = ShimKb(tmp_path)

        output = shim.render_for_persona_prompt("tpm")
        assert "weekly_status" in output
        assert "vector" in output

    def test_render_for_persona_prompt_no_cards(self, tmp_path):
        shim = ShimKb(tmp_path)
        output = shim.render_for_persona_prompt("tpm")
        assert "no KBs" in output


# ---------------------------------------------------------------------------
# ADB-backed (with skill_store)
# ---------------------------------------------------------------------------


def _make_skill_store_with_kbs(pb_rows: list[dict]) -> MagicMock:
    mock_store = MagicMock()
    mock_store.list_persona_builder_kbs.return_value = pb_rows
    return mock_store


class TestShimKbAdbBacked:
    def test_adb_kbs_merged_on_top_of_yaml(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "seed_kb")

        adb_entry_yaml = yaml.safe_dump({
            "name": "promoted_skill",
            "kind": "vector",
            "retrieval_tools": ["vector_search"],
            "provides_fields": ["title"],
            "kb_card": {"summary": "Promoted via authorSkill"},
        })
        mock_store = _make_skill_store_with_kbs([{
            "persona": "tpm",
            "kb_name": "promoted_skill",
            "content_yaml": adb_entry_yaml,
            "status": "production",
            "updated_at": "2026-01-01",
        }])

        shim = ShimKb(tmp_path, skill_store=mock_store)
        cards = shim.all_cards()

        # Seed KB + promoted KB = 2 total
        assert len(cards) == 2
        names = {c["name"] for c in cards}
        assert "seed_kb" in names
        assert "promoted_skill" in names

    def test_adb_entry_overrides_yaml_seed_with_same_name(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "weekly_status")

        # Same KB name as the seed — ADB entry should win
        adb_entry_yaml = yaml.safe_dump({
            "name": "weekly_status",
            "kind": "relational",
            "retrieval_tools": ["text_to_sql"],
            "provides_fields": ["title", "updated_fields"],
            "kb_card": {"summary": "Upgraded to relational"},
        })
        mock_store = _make_skill_store_with_kbs([{
            "persona": "tpm",
            "kb_name": "weekly_status",
            "content_yaml": adb_entry_yaml,
            "status": "production",
            "updated_at": "2026-05-12",
        }])

        shim = ShimKb(tmp_path, skill_store=mock_store)
        cards = shim.all_cards()

        # Only one card — the ADB version wins
        assert len(cards) == 1
        assert cards[0]["kind"] == "relational"
        assert cards[0]["_source"] == "adb"

    def test_skill_store_failure_falls_back_to_yaml(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "seed_kb")

        mock_store = MagicMock()
        mock_store.list_persona_builder_kbs.side_effect = RuntimeError("ADB down")

        # Should not raise
        shim = ShimKb(tmp_path, skill_store=mock_store)
        cards = shim.all_cards()

        # Falls back to YAML seed only
        assert len(cards) == 1
        assert cards[0]["name"] == "seed_kb"

    def test_list_persona_builder_kbs_called_with_production_status(self, tmp_path):
        mock_store = _make_skill_store_with_kbs([])
        ShimKb(tmp_path, skill_store=mock_store)

        mock_store.list_persona_builder_kbs.assert_called_once_with(status="production")

    def test_invalid_content_yaml_skipped(self, tmp_path):
        mock_store = _make_skill_store_with_kbs([{
            "persona": "tpm",
            "kb_name": "bad_entry",
            "content_yaml": ":::invalid yaml:::{{{",
            "status": "production",
            "updated_at": "2026-01-01",
        }])

        # Should not raise; bad entry is skipped
        shim = ShimKb(tmp_path, skill_store=mock_store)
        # No cards from the bad ADB entry
        assert shim.find_kb("bad_entry") is None


# ---------------------------------------------------------------------------
# ShimKb.reload()
# ---------------------------------------------------------------------------


class TestShimKbReload:
    def test_reload_calls_list_persona_builder_kbs_again(self, tmp_path):
        _write_persona_yaml(tmp_path, "tpm", "seed_kb")

        # First call returns empty; second call returns a new promoted KB
        adb_entry_yaml = yaml.safe_dump({
            "name": "newly_promoted",
            "kind": "vector",
            "retrieval_tools": [],
            "provides_fields": [],
            "kb_card": {},
        })
        mock_store = MagicMock()
        mock_store.list_persona_builder_kbs.side_effect = [
            [],  # first load() call at __init__
            [{   # reload() call
                "persona": "tpm",
                "kb_name": "newly_promoted",
                "content_yaml": adb_entry_yaml,
                "status": "production",
                "updated_at": "2026-05-12",
            }],
        ]

        shim = ShimKb(tmp_path, skill_store=mock_store)
        assert shim.find_kb("newly_promoted") is None  # not yet visible

        shim.reload()
        assert shim.find_kb("newly_promoted") is not None  # now visible

    def test_reload_without_skill_store_reruns_yaml_load(self, tmp_path):
        shim = ShimKb(tmp_path)
        assert shim.all_cards() == []

        # Add a new YAML file, then reload
        _write_persona_yaml(tmp_path, "tpm", "new_seed_kb")
        shim.reload()

        assert len(shim.all_cards()) == 1
        assert shim.all_cards()[0]["name"] == "new_seed_kb"
