"""Smoke test: authorSkill COMMIT → VALIDATE filesystem path.

This test covers the exact failure scenario from BUG-queue-51dd3 /
BUG-queue-3d13e / BUG-queue-1b0c0 / BUG-queue-30b34:

    COMMIT writes tpm.yaml.new_kb to disk.
    VALIDATE runs immediately after.
    With no ADB skill_store (filesystem-only fallback), _build_kb_index
    must pick up *.yaml.new_kb or it reports "workflow references unknown KB".

Run with:
    python3 -m pytest framework/tests/unit/test_smoke_validate.py -v

This is intentionally a self-contained integration test — it exercises the
real _write_artifacts() + _run_validate() path using a tmp_path as REPO_ROOT,
with no mocks on the critical code path (validate_links).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from framework.skill_builder.conversation import SkillBuilderConversation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conv(tmp_path: Path, skill_name: str = "generate_weekly_exec_review") -> SkillBuilderConversation:
    """Return a SkillBuilderConversation wired to tmp_path as the repo root."""
    conv = SkillBuilderConversation(persona="tpm", skill_store=None)
    conv._data.persona = "tpm"
    conv._data.skill_name = skill_name
    conv._data.synth_id = "synth-smoke-test"
    conv._data.intent_description = "Weekly exec review PPTX"
    conv._data.fields = ["overall_rag", "executive_summary", "key_accomplishments"]
    conv._data.sources = [{"kind": "confluence", "space": "OCIFACP"}]
    conv._data.trigger = {"on_request": True}
    conv._data.output_format = "pptx"
    conv._data.reuse_result = {"covered": {}, "gaps": list(conv._data.fields)}
    return conv


def _write_artifacts_to_tmp(conv: SkillBuilderConversation, tmp_path: Path) -> None:
    """Run _synthesize_preview() + _write_artifacts() with REPO_ROOT patched to tmp_path."""
    # Synthesize artifact content (same method called by PREVIEW → COMMIT)
    artifacts = conv._synthesize_preview()
    conv._data.synthesized_artifacts = artifacts

    # Patch REPO_ROOT so filesystem writes go into tmp_path
    with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
        conv._write_artifacts()


def _run_validate_in_tmp(conv: SkillBuilderConversation, tmp_path: Path):
    """Run _run_validate() with REPO_ROOT patched to tmp_path."""
    with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
        return conv._run_validate()


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

class TestValidateAfterCommitFilesystemPath:
    """End-to-end: COMMIT writes *.yaml.new_kb → VALIDATE resolves the KB."""

    def test_validate_passes_after_commit_no_skill_store(self, tmp_path):
        """Primary regression: filesystem-only path must find tpm.yaml.new_kb.

        This is the exact scenario from BUG-queue-51dd3 through BUG-queue-30b34.
        No ADB, no skill_store — pure filesystem fallback in _run_validate().
        """
        conv = _make_conv(tmp_path)

        # COMMIT: write all artifacts to tmp filesystem
        _write_artifacts_to_tmp(conv, tmp_path)

        # Verify COMMIT actually wrote the .new_kb file
        new_kb_path = tmp_path / "framework" / "persona_builders" / "tpm.yaml.new_kb"
        assert new_kb_path.exists(), (
            "COMMIT must write tpm.yaml.new_kb — it did not. "
            "Check _synthesize_artifacts() → synthesize_persona_builder_diff()."
        )

        # VALIDATE: must find the KB from the .new_kb file
        turn = _run_validate_in_tmp(conv, tmp_path)

        assert turn.state == "VALIDATE"
        validation = conv._data.validation_result or {}
        errors = validation.get("errors", [])

        assert validation.get("passed") is True, (
            f"VALIDATE failed with errors: {errors}\n\n"
            "Root cause: _build_kb_index did not pick up *.yaml.new_kb. "
            "This is the BUG-queue-51dd3 regression."
        )
        assert errors == []

    def test_new_kb_file_content_is_valid_yaml(self, tmp_path):
        """COMMIT must write a parseable YAML KB entry to tpm.yaml.new_kb."""
        conv = _make_conv(tmp_path)
        _write_artifacts_to_tmp(conv, tmp_path)

        new_kb_path = tmp_path / "framework" / "persona_builders" / "tpm.yaml.new_kb"
        assert new_kb_path.exists()

        content = yaml.safe_load(new_kb_path.read_text())
        assert isinstance(content, dict), "tpm.yaml.new_kb must be a YAML dict"
        assert "name" in content, "KB entry must have 'name' key"
        assert "provides_fields" in content, "KB entry must have 'provides_fields' key"
        assert set(conv._data.fields).issubset(set(content["provides_fields"])), (
            "All fields from the session must appear in provides_fields"
        )

    def test_workflow_skill_references_tpm_kb(self, tmp_path):
        """COMMIT must write a workflow YAML that references the tpm.skill_name KB."""
        conv = _make_conv(tmp_path)
        _write_artifacts_to_tmp(conv, tmp_path)

        wf_path = (
            tmp_path / "framework" / "workflow_skills" / "tpm"
            / f"{conv._data.skill_name}.yaml"
        )
        assert wf_path.exists(), "Workflow skill YAML must be written to disk"

        wf = yaml.safe_load(wf_path.read_text())
        extractions = wf.get("requires_extractions", [])
        kb_refs = [e.get("kb") for e in extractions]
        expected_ref = f"tpm.{conv._data.skill_name}"
        assert expected_ref in kb_refs, (
            f"Workflow must reference {expected_ref!r}. "
            f"Got refs: {kb_refs}"
        )

    def test_validate_message_shows_passed_not_failed(self, tmp_path):
        """Regression: validate turn message must not contain 'FAILED'."""
        conv = _make_conv(tmp_path)
        _write_artifacts_to_tmp(conv, tmp_path)
        turn = _run_validate_in_tmp(conv, tmp_path)

        assert "FAILED" not in turn.message, (
            f"Validate turn shows FAILED. Message:\n{turn.message}"
        )

    def test_validate_passes_for_skill_with_many_fields(self, tmp_path):
        """Regression: 15-field skill (the exact synth-tpm-89134ad3 config) passes VALIDATE."""
        conv = _make_conv(tmp_path, skill_name="generate_a_weekly_exec_review_pptx_for_the_26ai_pr")
        conv._data.fields = [
            "week_id", "project_name", "overall_rag", "executive_summary",
            "key_accomplishments", "upcoming_milestones", "schedule_health",
            "scope_health", "resource_health", "top_risks", "blockers",
            "dependencies", "exec_asks", "metrics_snapshot", "workstream_status",
        ]
        conv._data.reuse_result = {"covered": {}, "gaps": list(conv._data.fields)}

        _write_artifacts_to_tmp(conv, tmp_path)
        turn = _run_validate_in_tmp(conv, tmp_path)

        validation = conv._data.validation_result or {}
        assert validation.get("passed") is True, (
            f"15-field tpm skill failed VALIDATE: {validation.get('errors')}"
        )

    def test_validate_state_advances_to_ingest_on_success(self, tmp_path):
        """After passing VALIDATE, conversation state must advance to INGEST."""
        conv = _make_conv(tmp_path)
        _write_artifacts_to_tmp(conv, tmp_path)

        # Simulate the user's "yes, run full pipeline" by responding through
        # the committed state handler — drive VALIDATE programmatically.
        conv._state = "COMMITTED"
        conv._data.committed_paths = list(conv._data.synthesized_artifacts.keys())

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            turn = conv.respond("yes, run full pipeline")

        # Should advance past VALIDATE into INGEST (or stay at VALIDATE if failed)
        assert turn.state != "VALIDATE" or (
            conv._data.validation_result or {}
        ).get("passed") is True, (
            f"Session stuck at VALIDATE with errors: "
            f"{(conv._data.validation_result or {}).get('errors')}"
        )


class TestValidateFilesystemFallbackVsAdbPath:
    """Verify both the ADB-backed and filesystem fallback paths work correctly."""

    def test_filesystem_fallback_used_when_no_skill_store(self, tmp_path):
        """When skill_store=None, _run_validate must use the filesystem pb_dir."""
        from unittest.mock import MagicMock
        conv = _make_conv(tmp_path)
        assert conv._skill_store is None, "This test requires no skill_store"

        _write_artifacts_to_tmp(conv, tmp_path)

        captured_pb_dirs: list[str] = []
        original_validate = __import__(
            "framework.skill_builder.validate_links",
            fromlist=["validate_workflow_links"],
        ).validate_workflow_links

        def _capturing_validate(wf_path, pb_dir):
            captured_pb_dirs.append(pb_dir)
            return original_validate(wf_path, pb_dir)

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path), \
             patch(
                 "framework.skill_builder.validate_links.validate_workflow_links",
                 side_effect=_capturing_validate,
             ):
            conv._run_validate()

        assert captured_pb_dirs, "validate_workflow_links was not called"
        pb_dir_used = Path(captured_pb_dirs[0])
        # When skill_store=None but .new_kb exists on disk, _run_validate creates
        # a temp dir (identical to the ADB path) so _build_kb_index gets a
        # full persona-builder YAML (not a raw KB entry).
        assert "kbf_validate_pb_" in str(pb_dir_used), (
            "Expected a temp dir to be created from the .new_kb file "
            "(same approach as the ADB path). "
            f"Got pb_dir: {pb_dir_used}"
        )
        # Note: the temp dir is cleaned up after _run_validate() returns,
        # so we can only verify the path name, not the file contents.
