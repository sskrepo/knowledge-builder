"""Unit tests for validate_links._build_kb_index and validate_workflow_links.

Coverage:
  - _build_kb_index reads *.yaml files (basic)
  - _build_kb_index ALSO reads *.yaml.new_kb files (BUG-queue-51dd3 / 3d13e / 1b0c0)
  - _build_kb_index skips files starting with '_'
  - validate_workflow_links returns empty list for a valid workflow
  - validate_workflow_links returns error for unknown KB ref
  - validate_workflow_links returns error when required fields not provided
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from framework.skill_builder.validate_links import _build_kb_index, validate_workflow_links


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_builder(tmp_path: Path, filename: str, persona: str, kbs: list[dict]) -> Path:
    """Write a persona builder YAML file and return the path."""
    p = tmp_path / filename
    p.write_text(
        yaml.safe_dump(
            {"persona": persona, "knowledge_bases": kbs},
            sort_keys=False,
            allow_unicode=True,
        )
    )
    return p


def _write_workflow(tmp_path: Path, persona: str, kb_ref: str, required_fields: list[str]) -> Path:
    p = tmp_path / "workflow.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "persona": persona,
                "skill_name": "test_skill",
                "requires_extractions": [
                    {"kb": kb_ref, "required_fields": required_fields}
                ],
            },
            sort_keys=False,
        )
    )
    return p


# ---------------------------------------------------------------------------
# _build_kb_index tests
# ---------------------------------------------------------------------------

class TestBuildKbIndex:

    def test_reads_yaml_files(self, tmp_path):
        _write_builder(tmp_path, "tpm.yaml", "tpm", [
            {"name": "weekly_status", "provides_fields": ["rag", "summary"]},
        ])
        index = _build_kb_index(tmp_path)
        assert "tpm.weekly_status" in index

    def test_reads_yaml_new_kb_files(self, tmp_path):
        """*.yaml.new_kb files (in-session candidates) must be visible to the validator.

        Regression test for BUG-queue-51dd3 / BUG-queue-3d13e / BUG-queue-1b0c0:
        COMMIT writes tpm.yaml.new_kb but _build_kb_index previously only globbed
        *.yaml — the .new_kb suffix caused the file to be silently ignored,
        producing 'workflow references unknown KB' on every VALIDATE step.
        """
        _write_builder(tmp_path, "tpm.yaml.new_kb", "tpm", [
            {"name": "generate_a_weekly_exec_review_pptx", "provides_fields": ["rag", "summary"]},
        ])
        index = _build_kb_index(tmp_path)
        assert "tpm.generate_a_weekly_exec_review_pptx" in index, (
            "*.yaml.new_kb file must be indexed — otherwise VALIDATE always fails "
            "for newly authored skills (BUG-queue-51dd3)"
        )

    def test_yaml_new_kb_and_yaml_both_indexed(self, tmp_path):
        """Both regular *.yaml and *.yaml.new_kb files contribute to the index."""
        _write_builder(tmp_path, "tpm.yaml", "tpm", [
            {"name": "existing_kb", "provides_fields": ["f1"]},
        ])
        _write_builder(tmp_path, "tpm.yaml.new_kb", "tpm", [
            {"name": "new_kb", "provides_fields": ["f2"]},
        ])
        index = _build_kb_index(tmp_path)
        assert "tpm.existing_kb" in index
        assert "tpm.new_kb" in index

    def test_skips_underscore_prefixed_files(self, tmp_path):
        _write_builder(tmp_path, "_private.yaml", "tpm", [
            {"name": "private_kb", "provides_fields": []},
        ])
        index = _build_kb_index(tmp_path)
        assert "tpm.private_kb" not in index

    def test_returns_empty_for_empty_dir(self, tmp_path):
        index = _build_kb_index(tmp_path)
        assert index == {}

    def test_skips_malformed_yaml(self, tmp_path):
        (tmp_path / "bad.yaml").write_text(": : : not yaml")
        index = _build_kb_index(tmp_path)
        assert index == {}

    def test_qualifies_kb_as_persona_dot_name(self, tmp_path):
        _write_builder(tmp_path, "architect.yaml", "architect", [
            {"name": "code_structure", "provides_fields": ["module_map"]},
        ])
        index = _build_kb_index(tmp_path)
        assert "architect.code_structure" in index
        assert index["architect.code_structure"]["_owning_persona"] == "architect"


# ---------------------------------------------------------------------------
# validate_workflow_links end-to-end tests
# ---------------------------------------------------------------------------

class TestValidateWorkflowLinks:

    def test_valid_workflow_returns_no_errors(self, tmp_path):
        _write_builder(tmp_path, "tpm.yaml", "tpm", [
            {"name": "weekly_status", "provides_fields": ["rag", "summary", "blockers"]},
        ])
        wf = _write_workflow(tmp_path, "tpm", "tpm.weekly_status", ["rag", "summary"])
        errors = validate_workflow_links(str(wf), str(tmp_path))
        assert errors == []

    def test_unknown_kb_reference_returns_error(self, tmp_path):
        _write_builder(tmp_path, "tpm.yaml", "tpm", [
            {"name": "weekly_status", "provides_fields": ["rag"]},
        ])
        wf = _write_workflow(tmp_path, "tpm", "tpm.nonexistent_kb", ["rag"])
        errors = validate_workflow_links(str(wf), str(tmp_path))
        assert any("unknown KB" in e for e in errors)
        assert any("tpm.nonexistent_kb" in e for e in errors)

    def test_new_kb_suffix_file_resolves_kb_reference(self, tmp_path):
        """The primary regression scenario: workflow references a KB that was
        committed as *.yaml.new_kb — must not produce 'unknown KB' error."""
        _write_builder(tmp_path, "tpm.yaml.new_kb", "tpm", [
            {
                "name": "generate_a_weekly_exec_review_pptx_for_the_26ai_pr",
                "provides_fields": ["week_id", "overall_rag", "executive_summary"],
            }
        ])
        wf = _write_workflow(
            tmp_path,
            "tpm",
            "tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr",
            ["week_id", "overall_rag"],
        )
        errors = validate_workflow_links(str(wf), str(tmp_path))
        assert errors == [], (
            f"Unexpected validation errors: {errors}\n"
            "This is the BUG-queue-51dd3 / 3d13e / 1b0c0 regression — "
            "*.yaml.new_kb must be visible to the validator."
        )

    def test_missing_required_fields_returns_error(self, tmp_path):
        _write_builder(tmp_path, "tpm.yaml", "tpm", [
            {"name": "weekly_status", "provides_fields": ["rag"]},
        ])
        wf = _write_workflow(tmp_path, "tpm", "tpm.weekly_status", ["rag", "budget_burn"])
        errors = validate_workflow_links(str(wf), str(tmp_path))
        assert any("budget_burn" in e for e in errors)

    def test_missing_workflow_file_returns_error(self, tmp_path):
        errors = validate_workflow_links(str(tmp_path / "nonexistent.yaml"), str(tmp_path))
        assert any("does not exist" in e for e in errors)
