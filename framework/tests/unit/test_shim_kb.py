"""Unit tests for ShimKb — including *.yaml.new_kb loading (authored-but-not-promoted KBs).

Coverage:
  - Full *.yaml persona builders loaded correctly
  - *.yaml.new_kb raw KB entry loaded and included in cards
  - new_kb card inherits owning persona's visibility defaults
  - new_kb card uses vector_search as default retrieval tool
  - new_kb card visible via cards_visible_to(persona)
  - new_kb card visible via render_for_persona_prompt(persona)
  - find_kb("persona.kb_name") finds new_kb entry
  - Malformed new_kb file skipped without crash
  - new_kb without 'name' key skipped
"""
from __future__ import annotations

import yaml
import pytest
from pathlib import Path

from framework.orchestrator.shim_kb import ShimKb


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data))


def _make_full_pb(tmp_path: Path, persona: str, kb_names: list[str]) -> Path:
    """Write a minimal full persona builder YAML for `persona`."""
    path = tmp_path / f"{persona}.yaml"
    _write_yaml(path, {
        "persona": persona,
        "status": "production",
        "metadata_defaults": {
            "persona_visibility": [persona, "exec", "pm"],
        },
        "knowledge_bases": [
            {
                "name": kb,
                "kind": "wiki",
                "retrieval_tools": ["search_wiki", "vector_search"],
                "provides_fields": ["field_a", "field_b"],
                "kb_card": {
                    "summary": f"{kb} summary",
                    "use_when": f"Questions about {kb}",
                },
            }
            for kb in kb_names
        ],
    })
    return path


def _make_new_kb(tmp_path: Path, persona: str, kb_name: str) -> Path:
    """Write a *.yaml.new_kb raw KB entry (as COMMIT produces)."""
    path = tmp_path / f"{persona}.yaml.new_kb"
    _write_yaml(path, {
        "name": kb_name,
        "kind": "vector",
        "provides_fields": ["project_name", "overall_rag", "executive_summary"],
        "sources": [{"kind": "confluence", "space": "OCIFACP"}],
        "retrieval_tools": ["vector_search"],
        "kb_card": {
            "summary": "26ai weekly exec review data.",
            "use_when": "Questions about 26ai project status or exec review.",
        },
    })
    return path


# ---------------------------------------------------------------------------
# Tests — full persona builder loading (sanity)
# ---------------------------------------------------------------------------

class TestShimKbFullYaml:
    def test_loads_full_yaml(self, tmp_path):
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        kb = ShimKb(tmp_path)
        assert len(kb.all_cards()) == 1
        assert kb.all_cards()[0]["name"] == "tpm_weekly_ops"

    def test_skips_underscore_files(self, tmp_path):
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        (tmp_path / "_template.yaml").write_text("persona: _template\nknowledge_bases: []")
        kb = ShimKb(tmp_path)
        assert all(c["persona"] != "_template" for c in kb.all_cards())

    def test_cards_visible_to_respects_visibility(self, tmp_path):
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        kb = ShimKb(tmp_path)
        assert any(c["name"] == "tpm_weekly_ops" for c in kb.cards_visible_to("tpm"))
        assert any(c["name"] == "tpm_weekly_ops" for c in kb.cards_visible_to("exec"))
        assert not any(c["name"] == "tpm_weekly_ops" for c in kb.cards_visible_to("unknown_persona"))


# ---------------------------------------------------------------------------
# Tests — *.yaml.new_kb loading
# ---------------------------------------------------------------------------

class TestShimKbNewKb:
    def test_new_kb_included_in_all_cards(self, tmp_path):
        """A *.yaml.new_kb entry must appear in all_cards()."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        _make_new_kb(tmp_path, "tpm", "generate_a_weekly_exec_review_pptx_for_the_26ai_pr")
        kb = ShimKb(tmp_path)
        names = [c["name"] for c in kb.all_cards()]
        assert "generate_a_weekly_exec_review_pptx_for_the_26ai_pr" in names, (
            "newly authored KB from .new_kb must be visible in all_cards()"
        )

    def test_new_kb_inherits_persona_visibility(self, tmp_path):
        """new_kb entry must inherit the owning persona's visibility list."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        _make_new_kb(tmp_path, "tpm", "26ai_exec_review")
        kb = ShimKb(tmp_path)
        card = next(c for c in kb.all_cards() if c["name"] == "26ai_exec_review")
        assert "exec" in card["persona_visibility"], (
            "new_kb must inherit visibility=[tpm, exec, pm] from tpm.yaml"
        )

    def test_new_kb_visible_to_tpm(self, tmp_path):
        """cards_visible_to('tpm') must include the new_kb entry."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        _make_new_kb(tmp_path, "tpm", "generate_a_weekly_exec_review_pptx_for_the_26ai_pr")
        kb = ShimKb(tmp_path)
        visible_names = [c["name"] for c in kb.cards_visible_to("tpm")]
        assert "generate_a_weekly_exec_review_pptx_for_the_26ai_pr" in visible_names

    def test_new_kb_appears_in_persona_prompt(self, tmp_path):
        """render_for_persona_prompt must include the new_kb entry."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        _make_new_kb(tmp_path, "tpm", "generate_a_weekly_exec_review_pptx_for_the_26ai_pr")
        kb = ShimKb(tmp_path)
        prompt = kb.render_for_persona_prompt("tpm")
        assert "generate_a_weekly_exec_review_pptx_for_the_26ai_pr" in prompt, (
            "new_kb must appear in the persona prompt shown to the retrieval LLM"
        )

    def test_new_kb_find_by_qualified_name(self, tmp_path):
        """find_kb('tpm.generate_...') must resolve the new_kb entry."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        _make_new_kb(tmp_path, "tpm", "generate_a_weekly_exec_review_pptx_for_the_26ai_pr")
        kb = ShimKb(tmp_path)
        card = kb.find_kb("tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr")
        assert card is not None, (
            "find_kb by qualified name must find the new_kb entry"
        )
        assert card["persona"] == "tpm"

    def test_new_kb_uses_vector_search_tool(self, tmp_path):
        """new_kb card must carry vector_search as a retrieval tool."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        _make_new_kb(tmp_path, "tpm", "26ai_exec_review")
        kb = ShimKb(tmp_path)
        card = next(c for c in kb.all_cards() if c["name"] == "26ai_exec_review")
        assert "vector_search" in card["retrieval_tools"]

    def test_new_kb_without_name_skipped(self, tmp_path):
        """A new_kb file with no 'name' key must be silently skipped."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        # Write a nameless new_kb file
        (tmp_path / "tpm.yaml.new_kb").write_text(
            "kind: vector\nprovides_fields: [f1]\n"
        )
        kb = ShimKb(tmp_path)
        # Only the full YAML KB should be present
        assert len(kb.all_cards()) == 1

    def test_malformed_new_kb_skipped(self, tmp_path):
        """A malformed *.yaml.new_kb file must be skipped, not crash ShimKb."""
        _make_full_pb(tmp_path, "tpm", ["tpm_weekly_ops"])
        (tmp_path / "tpm.yaml.new_kb").write_text("{bad yaml{{{{")
        kb = ShimKb(tmp_path)  # Must not raise
        assert len(kb.all_cards()) == 1  # Only full YAML card

    def test_new_kb_no_existing_full_yaml_defaults_to_persona(self, tmp_path):
        """new_kb without a matching full *.yaml defaults visibility to [persona]."""
        # No tpm.yaml — only the new_kb file
        (tmp_path / "tpm.yaml.new_kb").write_text(yaml.dump({
            "name": "my_kb",
            "kind": "vector",
            "provides_fields": ["f1"],
            "retrieval_tools": ["vector_search"],
        }))
        kb = ShimKb(tmp_path)
        card = next((c for c in kb.all_cards() if c["name"] == "my_kb"), None)
        assert card is not None
        assert card["persona_visibility"] == ["tpm"]
