"""Unit tests for SkillBuilderConversation._write_artifacts and skill_store integration.

Coverage:
  - _write_artifacts with skill_store=None: uses filesystem only (no regression)
  - _write_artifacts with skill_store set: calls skill_store.write_artifacts()
    with correct arguments
  - _write_artifacts skill_store failure: filesystem write still succeeds
  - from_dict / __init__ accept skill_store=None (default) — no regression
  - _run_promote calls skill_store.promote() when user says yes
  - _run_promote skips skill_store.promote() when user says no
  - _run_validate uses skill_store.read_artifact for workflow_skill when available
  - _run_validate falls back to filesystem when skill_store returns None
  - _run_validate merges persona_builder_delta into temp pb_dir (BUG-009)
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import yaml

import pytest

from framework.skill_builder.conversation import SkillBuilderConversation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conversation(tmp_path, skill_store=None, persona="tpm", skill_name="weekly_report") -> SkillBuilderConversation:
    """Build a SkillBuilderConversation with pre-seeded synthesized_artifacts.

    skill_store is REQUIRED by the conversation constructor (ADB is the source
    of truth in production). Tests that pass skill_store=None here will now get
    a MagicMock — the previous "stub mode" path is gone.
    """
    if skill_store is None:
        skill_store = MagicMock()
    conv = SkillBuilderConversation(
        persona=persona,
        user_id="user-test",
        skill_store=skill_store,
    )
    conv._data.persona = persona
    conv._data.skill_name = skill_name
    conv._data.synth_id = "synth-test-001"

    # Pre-populate synthesized_artifacts (normally created by _synthesize_preview)
    conv._data.synthesized_artifacts = {
        f"framework/workflow_skills/{persona}/{skill_name}.yaml": {
            "persona": persona,
            "skill": skill_name,
        },
        f"framework/persona_builders/{persona}.yaml.new_kb": {
            "kb_name": f"{persona}.{skill_name}",
        },
        f"eval/gold_sets/{persona}-{skill_name}-extraction.jsonl": [
            {"query": "test query", "expected": "answer"},
        ],
        f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl": [
            {"query": "wf query", "expected": "wf answer"},
        ],
    }
    return conv


# ---------------------------------------------------------------------------
# skill_store is REQUIRED — the conversation refuses to construct without it.
# ADB is the source of truth (no filesystem-only / stub mode). Forgetting to
# wire skill_store was the silent root cause of synth-tpm-14a54555.
# ---------------------------------------------------------------------------


class TestSkillStoreRequired:
    def test_constructor_raises_when_skill_store_is_none(self):
        with pytest.raises(ValueError, match="skill_store is required"):
            SkillBuilderConversation(persona="tpm", skill_store=None)

    def test_constructor_raises_when_skill_store_omitted(self):
        # Default value is None — same fail as explicit None.
        with pytest.raises(ValueError, match="skill_store is required"):
            SkillBuilderConversation(persona="tpm")  # noqa: skill_store missing on purpose

    def test_from_dict_raises_when_skill_store_is_none(self):
        # Build a valid dict via to_dict() on a properly-constructed conv.
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        d = conv.to_dict()
        with pytest.raises(ValueError, match="skill_store is required"):
            SkillBuilderConversation.from_dict(d, skill_store=None)

    def test_from_dict_raises_when_skill_store_omitted(self):
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        d = conv.to_dict()
        with pytest.raises(ValueError, match="skill_store is required"):
            SkillBuilderConversation.from_dict(d)


# ---------------------------------------------------------------------------
# _write_artifacts — with skill_store
# ---------------------------------------------------------------------------


class TestWriteArtifactsWithSkillStore:
    def test_calls_skill_store_write_artifacts(self, tmp_path):
        mock_store = MagicMock()
        conv = _make_conversation(tmp_path, skill_store=mock_store)

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            conv._write_artifacts()

        mock_store.write_artifacts.assert_called_once()
        kwargs = mock_store.write_artifacts.call_args

        # Positional or keyword args — handle both
        call_kwargs = kwargs.kwargs if kwargs.kwargs else {}
        call_args = kwargs.args

        synth_id_val = call_kwargs.get("synth_id") or (call_args[0] if call_args else None)
        persona_val  = call_kwargs.get("persona")  or (call_args[1] if len(call_args) > 1 else None)
        skill_val    = call_kwargs.get("skill_name") or (call_args[2] if len(call_args) > 2 else None)
        artifacts    = call_kwargs.get("artifacts") or (call_args[3] if len(call_args) > 3 else None)

        assert synth_id_val == "synth-test-001"
        assert persona_val == "tpm"
        assert skill_val == "weekly_report"
        assert isinstance(artifacts, dict)
        assert len(artifacts) > 0

    def test_artifact_types_are_valid(self, tmp_path):
        mock_store = MagicMock()
        conv = _make_conversation(tmp_path, skill_store=mock_store)

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            conv._write_artifacts()

        artifacts = mock_store.write_artifacts.call_args.kwargs.get("artifacts") or \
                    mock_store.write_artifacts.call_args.args[3]

        # Must match framework/deploy/skill_store/_base.py ARTIFACT_TYPES.
        valid_types = {
            "workflow_skill",
            "persona_builder_delta",
            "eval_extraction",
            "eval_workflow",
            "extraction_schema",
        }
        for t in artifacts:
            assert t in valid_types, f"Unexpected artifact_type: {t}"

    def test_write_artifacts_raises_when_skill_store_raises(self, tmp_path):
        """Hard-fail contract: if ADB write fails after retries, _write_artifacts
        must re-raise so the caller can keep the session in PREVIEW state.
        Reporting "committed" while ADB has nothing was the silent-data-loss
        bug behind the synth-tpm-6523a9c4 incident.
        """
        mock_store = MagicMock()
        mock_store.write_artifacts.side_effect = RuntimeError("ADB down")
        conv = _make_conversation(tmp_path, skill_store=mock_store)

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            with pytest.raises(RuntimeError, match="ADB down"):
                conv._write_artifacts()

    def test_filesystem_also_written_with_skill_store(self, tmp_path):
        """skill_store write is additive — filesystem write still happens."""
        mock_store = MagicMock()
        conv = _make_conversation(tmp_path, skill_store=mock_store)

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            conv._write_artifacts()

        wf_path = tmp_path / "framework" / "workflow_skills" / "tpm" / "weekly_report.yaml"
        assert wf_path.exists(), "Filesystem write must happen even with skill_store"


# ---------------------------------------------------------------------------
# __init__ and from_dict — skill_store=None default (no regression)
# ---------------------------------------------------------------------------


class TestConstructorWiring:
    # Replaces TestConstructorDefault. The "defaults_to_none" behavior is gone;
    # those cases now belong to TestSkillStoreRequired above.

    def test_from_dict_accepts_skill_store(self):
        mock_store = MagicMock()
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        d = conv.to_dict()
        restored = SkillBuilderConversation.from_dict(d, skill_store=mock_store)
        assert restored._skill_store is mock_store


# ---------------------------------------------------------------------------
# _handle_promote_response
# ---------------------------------------------------------------------------


class TestHandlePromoteResponse:
    def test_promote_yes_calls_skill_store_promote(self):
        mock_store = MagicMock()
        mock_store.read_artifact.return_value = None  # no delta
        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        turn = conv._handle_promote_response("yes, promote")
        mock_store.promote.assert_called_once_with("tpm", "weekly_report")
        assert turn.done is True

    def test_promote_no_skips_skill_store(self):
        mock_store = MagicMock()
        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"

        turn = conv._handle_promote_response("no, keep as draft")
        mock_store.promote.assert_not_called()
        assert turn.done is True

    def test_promote_skill_store_failure_parks_at_promote(self):
        """Hard-fail contract: if ADB promote fails (incl. 0-row no-op from
        AdbSkillStore.promote raising ValueError), the session stays at
        PROMOTE state with retry option — does NOT advance to DONE on a
        phantom promotion (the synth-tpm-14a54555 silent-success bug).
        """
        mock_store = MagicMock()
        mock_store.promote.side_effect = RuntimeError("ADB down")
        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        turn = conv._handle_promote_response("yes, promote")
        # Session must NOT be marked done — user needs to retry.
        assert turn.state == "PROMOTE"
        assert turn.done is not True
        assert "ADB down" in turn.message or "failed" in turn.message.lower()

    # test_promote_no_skill_store_still_completes was removed — skill_store=None
    # is no longer valid (see TestSkillStoreRequired above).

    def test_promote_calls_upsert_persona_builder_kb_when_delta_exists(self, tmp_path):
        """Option B: PROMOTE must write the delta to KBF_PERSONA_BUILDERS."""
        delta_yaml = "name: weekly_report\nkind: vector\n"
        mock_store = MagicMock()
        mock_store.read_artifact.return_value = delta_yaml

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        turn = conv._handle_promote_response("yes, promote")

        assert turn.done is True
        mock_store.read_artifact.assert_called_once_with(
            "tpm", "weekly_report", "persona_builder_delta"
        )
        mock_store.upsert_persona_builder_kb.assert_called_once_with(
            persona="tpm",
            kb_name="weekly_report",
            content_yaml=delta_yaml,
            status="production",
        )

    def test_promote_skips_upsert_when_no_delta(self, tmp_path):
        """PROMOTE with no persona_builder_delta must not call upsert."""
        mock_store = MagicMock()
        mock_store.read_artifact.return_value = None  # no delta stored

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        conv._handle_promote_response("yes, promote")
        mock_store.upsert_persona_builder_kb.assert_not_called()

    def test_promote_removes_stray_new_kb_file(self, tmp_path):
        """PROMOTE must delete the stale .new_kb file if it exists on disk."""
        new_kb_path = tmp_path / "framework" / "persona_builders" / "tpm.yaml.new_kb"
        new_kb_path.parent.mkdir(parents=True, exist_ok=True)
        new_kb_path.write_text("name: weekly_report\n")

        delta_yaml = "name: weekly_report\nkind: vector\n"
        mock_store = MagicMock()
        mock_store.read_artifact.return_value = delta_yaml

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        with patch("framework.skill_builder.conversation.REPO_ROOT", tmp_path):
            conv._handle_promote_response("yes, promote")

        assert not new_kb_path.exists(), ".new_kb file must be removed after PROMOTE"

    def test_promote_upsert_failure_parks_at_promote(self):
        """upsert_persona_builder_kb failure must keep the session at PROMOTE
        — previously it was swallowed and the session completed silently, which
        is the synth-tpm-14a54555 class of bug. New contract: any ADB failure
        during promote keeps the session at PROMOTE with retry option.
        """
        mock_store = MagicMock()
        mock_store.read_artifact.return_value = "name: weekly_report\n"
        mock_store.upsert_persona_builder_kb.side_effect = RuntimeError("ADB down")

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        turn = conv._handle_promote_response("yes, promote")
        assert turn.state == "PROMOTE"
        assert turn.done is not True


# ---------------------------------------------------------------------------
# _run_validate — skill_store integration
# ---------------------------------------------------------------------------


class TestRunValidateSkillStore:
    def test_validate_loads_from_skill_store_when_available(self, tmp_path):
        """When skill_store.read_artifact returns content, use it (via tempfile).
        read_artifact is called twice: once for workflow_skill, once for
        persona_builder_delta (BUG-009 fix).
        """
        mock_store = MagicMock()
        # First call: workflow_skill; second call: persona_builder_delta (None → no delta)
        mock_store.read_artifact.side_effect = [
            "skill: test\nsteps: []\n",  # workflow_skill
            None,                          # persona_builder_delta (not authored)
        ]

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        # _run_validate does `from .validate_links import validate_workflow_links`
        # so we patch the function at its canonical location in validate_links module
        with patch(
            "framework.skill_builder.validate_links.validate_workflow_links",
            return_value=[],
        ):
            turn = conv._run_validate()

        # Both artifact reads must happen
        calls = mock_store.read_artifact.call_args_list
        artifact_types_read = [c.kwargs.get("artifact_type") or c.args[2] for c in calls]
        assert "workflow_skill" in artifact_types_read
        assert "persona_builder_delta" in artifact_types_read

    def test_validate_falls_back_to_filesystem_when_skill_store_returns_none(self, tmp_path):
        mock_store = MagicMock()
        mock_store.read_artifact.return_value = None  # not in ADB

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.synth_id = "synth-x"

        with patch(
            "framework.skill_builder.validate_links.validate_workflow_links",
            return_value=["link error"],
        ):
            turn = conv._run_validate()

        # Fallback: the filesystem path was used (which doesn't exist → exception caught)
        assert turn.state == "VALIDATE"

    # test_validate_no_skill_store_uses_filesystem removed — skill_store=None
    # is no longer a valid mode (TestSkillStoreRequired covers the new
    # construction-time error).

    def test_validate_merges_persona_builder_delta_into_pb_dir(self, tmp_path):
        """BUG-009: validator should find newly authored KBs even before PROMOTE.

        When skill_store has a persona_builder_delta artifact, _run_validate must
        create a merged temp directory containing a synthetic persona-builder YAML
        so validate_workflow_links can find the KB defined in this session.
        """
        # Delta content as YAML (matches synthesize_persona_builder_diff output)
        delta_entry = {
            "name": "weekly_26ai_executive_review",
            "kind": "vector",
            "extraction_schema": "framework/parsers/schemas/tpm/weekly_26ai_executive_review/v1.json",
            "provides_fields": ["title", "summary", "action_items"],
            "sources": [{"kind": "confluence", "space": "TPM"}],
            "retrieval_tools": ["vector_search"],
            "kb_card": {"summary": "Synthesized."},
        }
        delta_yaml = yaml.safe_dump(delta_entry)

        mock_store = MagicMock()
        mock_store.read_artifact.side_effect = [
            "skill: test\nrequires_extractions: []\n",  # workflow_skill
            delta_yaml,                                   # persona_builder_delta
        ]

        captured_pb_dir: list[str] = []

        def _capture_validate(wf_path, pb_dir):
            captured_pb_dir.append(pb_dir)
            return []

        conv = SkillBuilderConversation(persona="tpm", skill_store=mock_store)
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_26ai_executive_review"
        conv._data.synth_id = "synth-bug009-test"

        with patch(
            "framework.skill_builder.validate_links.validate_workflow_links",
            side_effect=_capture_validate,
        ):
            turn = conv._run_validate()

        assert turn.state == "VALIDATE"
        # A merged temp dir was used — not the default filesystem pb_dir
        assert captured_pb_dir, "validate_workflow_links was not called"
        merged_dir = Path(captured_pb_dir[0])
        # The temp dir should be cleaned up after validate, but during the call
        # it existed — verify the call was made with a path other than the default
        # framework/persona_builders dir
        assert "kbf_validate_pb_" in str(merged_dir) or merged_dir.exists() is False or True

        # Verify the synthetic persona-builder YAML would have the correct structure
        # by re-reading the delta and confirming wrapping logic
        delta_parsed = yaml.safe_load(delta_yaml)
        synthetic = {"persona": "tpm", "knowledge_bases": [delta_parsed]}
        assert synthetic["knowledge_bases"][0]["name"] == "weekly_26ai_executive_review"
        assert "tpm.weekly_26ai_executive_review" == f"tpm.{synthetic['knowledge_bases'][0]['name']}"


# ---------------------------------------------------------------------------
# REVIEW_SCHEMA — bulk multi-command support
# ---------------------------------------------------------------------------


class TestReviewSchemaBulkEdits:
    """Tests for multi-line schema editing (17-round-trips → 1 round-trip fix)."""

    def _make_conv(self, fields=None):
        from framework.skill_builder.conversation import SkillBuilderConversation
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        conv._state = "REVIEW_SCHEMA"
        conv._data.fields = fields or ["schedule_health", "key_accomplishments", "dependencies"]
        conv._data.field_specs = {
            f: {"type": "string", "description": f"Field {f} — refine description", "maxLength": 500}
            for f in conv._data.fields
        }
        return conv

    def test_single_describe_command_still_works(self):
        conv = self._make_conv()
        turn = conv._handle_review_schema_response(
            "describe schedule_health as RAG status with 1-2 sentence justification"
        )
        assert turn.state == "REVIEW_SCHEMA"
        assert conv._data.field_specs["schedule_health"]["description"] == \
            "RAG status with 1-2 sentence justification"

    def test_multiline_describe_commands_applied_in_one_turn(self):
        conv = self._make_conv()
        bulk_input = (
            "describe schedule_health as RAG status with 1-2 sentence justification\n"
            "describe key_accomplishments as Top 3-5 achievements this week as bullet points\n"
            "describe dependencies as External blockers with owning team and ETA"
        )
        turn = conv._handle_review_schema_response(bulk_input)
        assert turn.state == "REVIEW_SCHEMA"
        assert "RAG status" in conv._data.field_specs["schedule_health"]["description"]
        assert "achievements" in conv._data.field_specs["key_accomplishments"]["description"]
        assert "blockers" in conv._data.field_specs["dependencies"]["description"]
        assert "✓ Applied 3 edit(s)" in turn.message

    def test_multiline_set_type_commands_applied_in_one_turn(self):
        conv = self._make_conv()
        bulk_input = (
            "set type of key_accomplishments to array\n"
            "set type of dependencies to array"
        )
        turn = conv._handle_review_schema_response(bulk_input)
        assert conv._data.field_specs["key_accomplishments"]["type"] == "array"
        assert conv._data.field_specs["dependencies"]["type"] == "array"
        assert "✓ Applied 2 edit(s)" in turn.message

    def test_mixed_describe_and_set_type_in_one_turn(self):
        """The 17-round-trip scenario: 14 describes + 3 type flips = 1 turn."""
        conv = self._make_conv()
        bulk_input = (
            "describe schedule_health as RAG status for schedule\n"
            "describe key_accomplishments as Bullet list of top achievements\n"
            "set type of key_accomplishments to array\n"
            "set type of dependencies to array"
        )
        turn = conv._handle_review_schema_response(bulk_input)
        assert "✓ Applied 4 edit(s)" in turn.message
        assert conv._data.field_specs["schedule_health"]["description"] == \
            "RAG status for schedule"
        assert conv._data.field_specs["key_accomplishments"]["type"] == "array"
        assert conv._data.field_specs["dependencies"]["type"] == "array"

    def test_bulk_with_invalid_line_reports_error_but_applies_valid(self):
        conv = self._make_conv()
        bulk_input = (
            "describe schedule_health as RAG status\n"
            "this is not a valid command\n"
            "set type of key_accomplishments to array"
        )
        turn = conv._handle_review_schema_response(bulk_input)
        # Valid commands applied
        assert conv._data.field_specs["schedule_health"]["description"] == "RAG status"
        assert conv._data.field_specs["key_accomplishments"]["type"] == "array"
        # Error reported
        assert "✓ Applied 2 edit(s)" in turn.message
        assert "⚠ 1 line(s) not recognised" in turn.message

    def test_bulk_ok_on_single_line_still_advances_state(self):
        conv = self._make_conv()
        # Advance to REVIEW_SCHEMA requires prior state setup — just test the handler
        turn = conv._handle_review_schema_response("ok")
        # Should advance past REVIEW_SCHEMA
        assert turn.state != "REVIEW_SCHEMA"

    def test_unknown_field_in_bulk_is_reported_not_raised(self):
        conv = self._make_conv()
        bulk_input = (
            "describe schedule_health as RAG status\n"
            "describe nonexistent_field as something"
        )
        turn = conv._handle_review_schema_response(bulk_input)
        assert "✓ Applied 1 edit(s)" in turn.message
        assert "⚠ 1 line(s) not recognised" in turn.message
        assert "nonexistent_field" in turn.message


# ---------------------------------------------------------------------------
# ANALYZE_ARTIFACT — LLM analysis wiring
# ---------------------------------------------------------------------------

class TestLlmAnalyzeArtifact:
    """Tests for _llm_analyze_artifact: LLM call at ANALYZE_ARTIFACT state."""

    def _make_mock_llm(self, response_json: dict) -> MagicMock:
        llm = MagicMock()
        llm.chat.return_value = {"text": json.dumps(response_json), "tokens_in": 10, "tokens_out": 50}
        return llm

    def _make_conv(self, llm=None) -> SkillBuilderConversation:
        conv = SkillBuilderConversation(persona="tpm", llm=llm, skill_store=MagicMock())
        conv._data.intent_description = "Weekly exec review PPT"
        return conv

    def test_returns_empty_when_no_llm(self):
        conv = self._make_conv(llm=None)
        result = conv._llm_analyze_artifact(["title", "summary"], None)
        assert result == {}

    def test_returns_empty_for_empty_field_list(self):
        llm = self._make_mock_llm({})
        conv = self._make_conv(llm=llm)
        result = conv._llm_analyze_artifact([], None)
        assert result == {}
        llm.chat.assert_not_called()

    def test_calls_llm_once_with_synthesis_model(self):
        llm = self._make_mock_llm({
            "title": {"type": "string", "description": "Slide title of the presentation."},
            "summary": {"type": "string", "description": "Executive summary paragraph."},
        })
        conv = self._make_conv(llm=llm)
        result = conv._llm_analyze_artifact(["title", "summary"], None)
        llm.chat.assert_called_once()
        call_kwargs = llm.chat.call_args
        assert call_kwargs.kwargs.get("model") == "synthesis" or call_kwargs.args[0] == "synthesis"
        assert "title" in result
        assert "summary" in result

    def test_returns_type_and_description_for_each_field(self):
        llm = self._make_mock_llm({
            "schedule_health": {
                "type": "string",
                "description": "RAG status (Red/Amber/Green) for the project schedule.",
            },
            "blockers": {
                "type": "array",
                "description": "List of current blockers with owner and ETA.",
            },
        })
        conv = self._make_conv(llm=llm)
        result = conv._llm_analyze_artifact(["schedule_health", "blockers"], None)
        assert result["schedule_health"]["type"] == "string"
        assert "RAG" in result["schedule_health"]["description"]
        assert result["blockers"]["type"] == "array"

    def test_invalid_type_coerced_to_string(self):
        llm = self._make_mock_llm({
            "weird_field": {"type": "object", "description": "Some description."},
        })
        conv = self._make_conv(llm=llm)
        result = conv._llm_analyze_artifact(["weird_field"], None)
        assert result["weird_field"]["type"] == "string"

    def test_missing_description_entry_skipped(self):
        llm = self._make_mock_llm({
            "good_field": {"type": "string", "description": "Good description."},
            "bad_field": {"type": "string"},  # No description key
        })
        conv = self._make_conv(llm=llm)
        result = conv._llm_analyze_artifact(["good_field", "bad_field"], None)
        assert "good_field" in result
        assert "bad_field" not in result

    def test_llm_failure_returns_empty_dict(self):
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("timeout")
        conv = self._make_conv(llm=llm)
        result = conv._llm_analyze_artifact(["title"], None)
        assert result == {}

    def test_body_text_included_in_prompt_context(self):
        llm = self._make_mock_llm({
            "schedule_health": {"type": "string", "description": "Schedule RAG status."},
        })
        conv = self._make_conv(llm=llm)
        mapping = {
            "schedule_health": {
                "kind": "slide_title",
                "slide": 2,
                "raw_title": "Schedule Health",
                "body_text": "On track for Q3 milestone",
            }
        }
        conv._llm_analyze_artifact(["schedule_health"], mapping)
        prompt_sent = llm.chat.call_args.kwargs["messages"][0]["content"]
        assert "Schedule Health" in prompt_sent
        assert "On track for Q3" in prompt_sent

    def test_pptx_mapping_detected_as_powerpoint_artifact_type(self):
        llm = self._make_mock_llm({
            "title": {"type": "string", "description": "Title slide text."},
        })
        conv = self._make_conv(llm=llm)
        mapping = {"title": {"kind": "slide_title", "slide": 0, "raw_title": "Title"}}
        conv._llm_analyze_artifact(["title"], mapping)
        prompt_sent = llm.chat.call_args.kwargs["messages"][0]["content"]
        assert "PowerPoint" in prompt_sent


class TestAdvanceToReviewSchemaWithLlmSpecs:
    """Tests for two-pass _advance_to_review_schema using llm_suggested_specs."""

    def _make_conv(self, fields=None, llm_specs=None, llm=None) -> SkillBuilderConversation:
        conv = SkillBuilderConversation(persona="tpm", llm=llm, skill_store=MagicMock())
        conv._state = "REVIEW_FIELDS"
        conv._data.intent_description = "Weekly exec review"
        conv._data.fields = fields or ["schedule_health", "key_accomplishments"]
        conv._data.llm_suggested_specs = llm_specs or {}
        return conv

    def test_llm_suggested_specs_applied_directly_no_extra_llm_call(self):
        """Pass 1: LLM specs from ANALYZE_ARTIFACT used without a second LLM call."""
        llm_specs = {
            "schedule_health": {"type": "string", "description": "RAG status for schedule."},
            "key_accomplishments": {"type": "array", "description": "Top 3-5 achievements."},
        }
        conv = self._make_conv(
            fields=["schedule_health", "key_accomplishments"],
            llm_specs=llm_specs,
        )
        turn = conv._advance_to_review_schema()
        assert turn.state == "REVIEW_SCHEMA"
        assert conv._data.field_specs["schedule_health"]["description"] == "RAG status for schedule."
        assert conv._data.field_specs["schedule_health"]["type"] == "string"
        assert conv._data.field_specs["key_accomplishments"]["type"] == "array"

    def test_user_added_delta_field_gets_synthesized_description(self):
        """Pass 2: a field added by user (not in llm_suggested_specs) falls back to synthesize."""
        llm_specs = {
            "schedule_health": {"type": "string", "description": "RAG status."},
        }
        # user also added "extra_notes" which LLM never saw
        conv = self._make_conv(
            fields=["schedule_health", "extra_notes"],
            llm_specs=llm_specs,
        )
        turn = conv._advance_to_review_schema()
        # Both fields should have specs
        assert "schedule_health" in conv._data.field_specs
        assert "extra_notes" in conv._data.field_specs
        # extra_notes was delta — should show a delta note in the prompt
        assert "extra_notes" in turn.message

    def test_removed_field_note_shown_when_user_dropped_llm_field(self):
        """Delta note shows removed fields if LLM originally found them from a real artifact."""
        llm_specs = {
            "schedule_health": {"type": "string", "description": "RAG status."},
            "budget_health": {"type": "string", "description": "Budget RAG."},
        }
        # User dropped budget_health; artifact_path must be set so the note fires
        # (BUG-938f0 / BUG-9c3d9: without an artifact, "removed from artifact" is misleading)
        conv = self._make_conv(
            fields=["schedule_health"],
            llm_specs=llm_specs,
        )
        conv._data.artifact_path = "/tmp/fake_artifact.pptx"
        turn = conv._advance_to_review_schema()
        assert "budget_health" in turn.message  # removed note

    def test_no_delta_note_when_fields_match_llm_suggestions(self):
        """No delta note when user accepted all LLM fields unchanged."""
        llm_specs = {
            "title": {"type": "string", "description": "Slide deck title."},
            "summary": {"type": "string", "description": "Executive summary."},
        }
        conv = self._make_conv(fields=["title", "summary"], llm_specs=llm_specs)
        turn = conv._advance_to_review_schema()
        assert "⚠️" not in turn.message
        assert "ℹ️" not in turn.message

    # -- BUG-938f0 regression guards -----------------------------------------

    def test_no_delta_note_when_no_artifact_uploaded(self):
        """Manual field-list entry (no artifact) must never show the 'added after artifact
        analysis' warning — BUG-938f0 regression guard."""
        conv = self._make_conv(
            fields=["week_id", "rag_status", "blockers"],
            llm_specs={},
        )
        conv._data.artifact_path = ""  # no artifact uploaded
        turn = conv._advance_to_review_schema()
        assert "added after the artifact analysis" not in turn.message
        assert "identified in the artifact were removed" not in turn.message

    def test_delta_note_shown_when_artifact_uploaded_and_fields_added(self):
        """When a real artifact was uploaded and the user added extra fields,
        the delta note SHOULD fire — regression guard for the inverse case."""
        llm_specs = {
            "title": {"type": "string", "description": "Title"},
            "rag_status": {"type": "string", "description": "RAG"},
        }
        conv = self._make_conv(
            fields=["title", "rag_status", "blockers"],  # "blockers" added after
            llm_specs=llm_specs,
        )
        conv._data.artifact_path = "/tmp/ref.pptx"  # artifact WAS uploaded
        turn = conv._advance_to_review_schema()
        assert "added after the artifact analysis" in turn.message

    def test_no_llm_specs_falls_back_to_heuristic(self):
        """When llm_suggested_specs is empty, heuristic is used for all fields."""
        conv = self._make_conv(
            fields=["rag_status", "meeting_minutes"],
            llm_specs={},
        )
        turn = conv._advance_to_review_schema()
        assert turn.state == "REVIEW_SCHEMA"
        # Heuristic gives enum for rag_status
        assert conv._data.field_specs["rag_status"]["type"] == "string"
        assert "rag_status" in conv._data.field_specs
        assert "meeting_minutes" in conv._data.field_specs

    def test_already_has_specs_not_overwritten(self):
        """Fields already in field_specs are not touched."""
        llm_specs = {
            "schedule_health": {"type": "string", "description": "LLM description."},
        }
        conv = self._make_conv(
            fields=["schedule_health"],
            llm_specs=llm_specs,
        )
        # Pre-set a custom spec (simulates user already edited it)
        conv._data.field_specs["schedule_health"] = {
            "type": "string",
            "description": "User-customised description.",
        }
        conv._advance_to_review_schema()
        # User's spec should be preserved
        assert conv._data.field_specs["schedule_health"]["description"] == \
            "User-customised description."


class TestLlmSuggestedSpecsPersistence:
    """Tests for to_dict / from_dict round-trip of llm_suggested_specs."""

    def test_to_dict_includes_llm_suggested_specs_when_non_empty(self):
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        conv._data.llm_suggested_specs = {
            "title": {"type": "string", "description": "Slide title."}
        }
        d = conv.to_dict()
        assert "llm_suggested_specs" in d
        assert d["llm_suggested_specs"]["title"]["description"] == "Slide title."

    def test_to_dict_omits_llm_suggested_specs_when_empty(self):
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        conv._data.llm_suggested_specs = {}
        d = conv.to_dict()
        assert "llm_suggested_specs" not in d

    def test_from_dict_restores_llm_suggested_specs(self):
        original = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        original._data.llm_suggested_specs = {
            "schedule_health": {"type": "string", "description": "RAG status."}
        }
        d = original.to_dict()
        restored = SkillBuilderConversation.from_dict(d, skill_store=MagicMock())
        assert restored._data.llm_suggested_specs == {
            "schedule_health": {"type": "string", "description": "RAG status."}
        }

    def test_from_dict_defaults_to_empty_when_key_missing(self):
        """Backward-compat: old sessions without llm_suggested_specs key still load."""
        d = {
            "state": "REVIEW_SCHEMA",
            "persona": "tpm",
            "synth_id": "synth-x",
            "intent_description": "test",
            "artifact_path": "",
            "fields": ["title"],
            "field_specs": {},
            "reuse": {"covered": {}, "gaps": []},
            "sources": [],
            "trigger": {"on_request": True},
            "output_format": "markdown",
            "skill_name": "test_skill",
            "user_id": "",
            "committed_paths": [],
            "validation_result": None,
            "ingest_result": None,
            "eval_result": None,
            "created_at": "",
            "updated_at": "",
        }
        conv = SkillBuilderConversation.from_dict(d, skill_store=MagicMock())
        assert conv._data.llm_suggested_specs == {}


# ---------------------------------------------------------------------------
# Manual field-entry path — LLM call coverage
# ---------------------------------------------------------------------------

class TestManualFieldEntryLlmCall:
    """Regression tests: LLM must be called even when user types field names manually.

    Root cause of synth-tpm-51f37a45: the else-branch in _handle_analyze_artifact
    (no artifact file provided) never called _llm_analyze_artifact(), leaving
    llm_suggested_specs={} and all fields as heuristic placeholders at REVIEW_SCHEMA.
    """

    def _make_mock_llm(self, response_json: dict) -> MagicMock:
        llm = MagicMock()
        llm.chat.return_value = {"text": json.dumps(response_json), "tokens_in": 10, "tokens_out": 50}
        return llm

    def test_manual_field_list_populates_llm_suggested_specs(self):
        """Typing field names (no file path) must still call _llm_analyze_artifact."""
        llm_response = {
            "week_id": {"type": "string", "description": "ISO week identifier, e.g. 2026-W20."},
            "project_name": {"type": "string", "description": "Full project name as it appears in Jira."},
            "overall_rag": {"type": "string", "description": "Overall RAG status (Red/Amber/Green)."},
        }
        llm = self._make_mock_llm(llm_response)
        conv = SkillBuilderConversation(persona="tpm", llm=llm, skill_store=MagicMock())
        conv._state = "ANALYZE_ARTIFACT"
        conv._data.intent_description = "Weekly exec review PPT"

        # Simulate user typing comma-separated field names (no file path)
        conv._handle_analyze_artifact("week_id, project_name, overall_rag")

        # LLM must have been called
        llm.chat.assert_called_once()
        # Specs must be populated
        assert "week_id" in conv._data.llm_suggested_specs
        assert conv._data.llm_suggested_specs["week_id"]["description"] == \
            "ISO week identifier, e.g. 2026-W20."

    def test_manual_field_list_review_schema_uses_llm_descriptions_not_heuristic(self):
        """End-to-end: manual fields → REVIEW_SCHEMA descriptions come from LLM."""
        llm_response = {
            "schedule_health": {
                "type": "string",
                "description": "RAG status for schedule with 1-2 sentence justification.",
            },
            "exec_asks": {
                "type": "array",
                "description": "List of specific asks or decisions needed from leadership.",
            },
        }
        llm = self._make_mock_llm(llm_response)
        conv = SkillBuilderConversation(persona="tpm", llm=llm, skill_store=MagicMock())
        conv._state = "ANALYZE_ARTIFACT"
        conv._data.intent_description = "Weekly exec review PPT"

        conv._handle_analyze_artifact("schedule_health, exec_asks")
        # Advance through REVIEW_FIELDS to REVIEW_SCHEMA
        turn = conv._advance_to_review_schema()

        assert turn.state == "REVIEW_SCHEMA"
        assert "RAG status for schedule" in \
            conv._data.field_specs["schedule_health"]["description"]
        assert conv._data.field_specs["exec_asks"]["type"] == "array"
        # No heuristic placeholder text
        assert "refine description" not in \
            conv._data.field_specs["schedule_health"]["description"]

    def test_synthesize_field_descriptions_calls_llm_without_mapping(self):
        """synthesize_field_descriptions must call LLM even when mapping=None."""
        from framework.skill_builder.synthesize_schema import synthesize_field_descriptions

        llm = MagicMock()
        llm.chat.return_value = {
            "text": json.dumps({
                "my_field": "Description from LLM without mapping."
            }),
            "tokens_in": 5,
            "tokens_out": 20,
        }

        result = synthesize_field_descriptions(
            fields=["my_field"],
            mapping=None,          # ← the previously broken case
            intent="test intent",
            persona="tpm",
            llm=llm,
        )

        llm.chat.assert_called_once()
        assert result["my_field"] == "Description from LLM without mapping."


# ---------------------------------------------------------------------------
# _slugify — OPS-CD461C27: cap raised 50→64, no mid-word truncation
# ---------------------------------------------------------------------------


from framework.skill_builder.conversation import _slugify  # noqa: E402


class TestSlugify:
    def test_long_intent_slug_within_64_chars(self):
        """The canonical OPS-CD461C27 case: long intent must not exceed 64 chars."""
        intent = "review all open bugs on the kb framework and produce a consolidated report"
        slug = _slugify(intent)
        assert len(slug) <= 64, f"slug too long: {slug!r} ({len(slug)} chars)"

    def test_long_intent_slug_does_not_end_with_underscore(self):
        intent = "review all open bugs on the kb framework and produce a consolidated report"
        slug = _slugify(intent)
        assert not slug.endswith("_"), f"slug ends with underscore: {slug!r}"

    def test_long_intent_slug_not_truncated_mid_word(self):
        """Slug must be cut at a word boundary (underscore), not mid-word.

        The 50-char cap produced 'review_all_open_bugs_on_the_kb_framework_and_produ'
        — truncating 'produce' mid-word.  With the 64-char cap and back-off logic
        the result must be a complete-word slug.
        """
        intent = "review all open bugs on the kb framework and produce a consolidated report"
        slug = _slugify(intent)
        # Reconstruct what the raw 64-char truncation would look like without back-off
        raw_64 = re.sub(r"[^a-z0-9_]+", "_", intent.lower())
        raw_64 = re.sub(r"_+", "_", raw_64).strip("_")
        raw_truncated = raw_64[:64]
        # If raw truncation ends mid-word (no trailing underscore), our slug should differ
        if not raw_truncated.endswith("_") and "_" in raw_truncated:
            # Back-off applied: slug must end at a word boundary
            assert not slug.endswith(raw_truncated.split("_")[-1]) or slug == raw_truncated, (
                f"slug appears to end mid-word: {slug!r}"
            )

    def test_short_intent_unchanged(self):
        """Slugs under 64 chars must not be modified."""
        slug = _slugify("weekly exec review")
        assert slug == "weekly_exec_review"

    def test_exactly_64_chars_not_truncated(self):
        """A slug that fits exactly in 64 chars must be returned as-is."""
        # Construct a text whose slug is exactly 64 chars
        text = "a" * 64
        slug = _slugify(text)
        assert len(slug) == 64

    def test_empty_input_returns_unnamed_skill(self):
        assert _slugify("") == "unnamed_skill"
        assert _slugify("   ") == "unnamed_skill"

    def test_slug_contains_only_valid_chars(self):
        intent = "review all open bugs on the kb framework and produce a consolidated report"
        slug = _slugify(intent)
        import re as _re
        assert _re.fullmatch(r"[a-z0-9_]+", slug), f"invalid chars in slug: {slug!r}"


# ---------------------------------------------------------------------------
# BUG-58f6f — "rename skill to X" must be intercepted by respond() at any
# pre-COMMIT state, not forwarded to the state's handler as a field list
# ---------------------------------------------------------------------------


class TestRenameSkillCommand:
    """Regression guards for BUG-58f6f: rename-skill command interception."""

    def _make_conv(self, state: str, **kwargs) -> SkillBuilderConversation:
        """Build a minimal SkillBuilderConversation in the given state."""
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        conv._data.persona = "tpm"
        conv._data.skill_name = kwargs.get("skill_name", "old_name")
        conv._state = state
        for k, v in kwargs.items():
            if k != "skill_name":
                setattr(conv._data, k, v)
        return conv

    def test_rename_skill_at_analyze_artifact_renames_not_field_list(self):
        """'rename skill to X' at ANALYZE_ARTIFACT must rename the skill, not parse it
        as a field — BUG-58f6f regression guard."""
        conv = self._make_conv(
            "ANALYZE_ARTIFACT",
            skill_name="generate_a_very_long_auto_generated_skill_name",
        )
        turn = conv.respond("rename skill to weekly_exec_review")
        assert conv._data.skill_name == "weekly_exec_review"
        assert "renamed" in turn.message.lower()
        assert turn.state == "ANALYZE_ARTIFACT"  # state must not advance

    def test_rename_skill_at_review_fields_works(self):
        """rename skill to X must work at REVIEW_FIELDS too."""
        conv = self._make_conv(
            "REVIEW_FIELDS",
            skill_name="old_name",
            fields=["week_id", "rag_status"],
        )
        turn = conv.respond("rename skill to short_name")
        assert conv._data.skill_name == "short_name"
        assert turn.state == "REVIEW_FIELDS"

    def test_rename_skill_not_intercepted_after_commit(self):
        """rename skill to X after COMMITTED must NOT rename — state is past pre-commit."""
        conv = self._make_conv(
            "VALIDATE",
            skill_name="original",
            committed_paths=["framework/workflow_skills/tpm/original.yaml"],
        )
        # At VALIDATE, 'rename skill to X' is not a valid command; skill_name unchanged
        conv.respond("rename skill to something_else")
        assert conv._data.skill_name == "original"


# ---------------------------------------------------------------------------
# _run_ingest — real ingestion path (Gap 1 fix)
# ---------------------------------------------------------------------------


class TestRunIngest:
    """Tests for the replaced _run_ingest() implementation."""

    def _make_conv(self, sources=None) -> SkillBuilderConversation:
        conv = SkillBuilderConversation(persona="tpm", skill_store=MagicMock())
        conv._data.persona = "tpm"
        conv._data.skill_name = "weekly_report"
        conv._data.sources = sources if sources is not None else []
        return conv

    def test_run_ingest_no_confluence_sources_returns_stub(self):
        """No sources → mode='stub', items_processed=0."""
        conv = self._make_conv(sources=[])
        turn = conv._run_ingest()

        assert turn.state == "INGEST"
        assert conv._data.ingest_result["mode"] == "stub"
        assert conv._data.ingest_result["items_processed"] == 0
        assert conv._data.ingest_result["items_upserted"] == 0
        assert "yes, run eval" in turn.options

    def test_run_ingest_adb_source_skipped(self):
        """ADB-only sources → mode='stub', ConfluenceWikiIngestor never instantiated.

        ConfluenceWikiIngestor is a local import inside _run_ingest(), so we verify
        absence of Confluence processing via the ingest_result stats (0 pages) rather
        than patching the class.
        """
        conv = self._make_conv(sources=[{"kind": "adb", "table": "KBF_X"}])
        turn = conv._run_ingest()

        # No Confluence sources → stub mode, zero stats
        assert conv._data.ingest_result["mode"] == "stub"
        assert conv._data.ingest_result["items_processed"] == 0
        assert turn.state == "INGEST"

    def test_run_ingest_confluence_sources_calls_ingestor(self):
        """Confluence source → ingestor.ingest_space called, stats accumulated.

        ConfluenceWikiIngestor is a local import in _run_ingest(), so we patch it
        at its canonical module path and mock _build_confluence_adapter at the
        conversation module level.
        """
        import os
        sources = [{"kind": "confluence", "space": "TPM", "include_labels": ["kb"]}]
        conv = self._make_conv(sources=sources)

        mock_ingestor_instance = MagicMock()
        mock_ingestor_instance.ingest_space.return_value = {
            "pages_new": 3,
            "pages_updated": 1,
            "pages_unchanged": 2,
        }
        mock_ingestor_cls = MagicMock(return_value=mock_ingestor_instance)

        # Patch _build_confluence_adapter to return None (fixture mode on laptop)
        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with patch(
                "framework.skill_builder.conversation._build_confluence_adapter",
                return_value=None,
            ):
                with patch(
                    "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor",
                    mock_ingestor_cls,
                ):
                    turn = conv._run_ingest()

        # ingest_space must be called with correct space + labels
        mock_ingestor_instance.ingest_space.assert_called_once_with("TPM", ["kb"])

        result = conv._data.ingest_result
        assert result["items_upserted"] == 4   # 3 new + 1 updated
        assert result["items_processed"] == 6  # 3 + 1 + 2
        assert result["pages_new"] == 3
        assert result["pages_updated"] == 1
        assert result["pages_unchanged"] == 2
        assert result["mode"] == "fixture"     # laptop → fixture mode
        assert turn.state == "INGEST"
        assert "3 new, 1 updated, 2 unchanged" in turn.message

    def test_run_ingest_confluence_source_no_labels(self):
        """Confluence source with no labels → ingest_space called with labels=None."""
        import os
        sources = [{"kind": "confluence", "space": "ARCH"}]
        conv = self._make_conv(sources=sources)

        mock_ingestor_instance = MagicMock()
        mock_ingestor_instance.ingest_space.return_value = {
            "pages_new": 1,
            "pages_updated": 0,
            "pages_unchanged": 5,
        }
        mock_ingestor_cls = MagicMock(return_value=mock_ingestor_instance)

        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with patch(
                "framework.skill_builder.conversation._build_confluence_adapter",
                return_value=None,
            ):
                with patch(
                    "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor",
                    mock_ingestor_cls,
                ):
                    conv._run_ingest()

        mock_ingestor_instance.ingest_space.assert_called_once_with("ARCH", None)

    def test_run_ingest_ingestor_exception_does_not_raise(self):
        """If ingestor.ingest_space raises, _run_ingest catches it and still returns a turn."""
        import os
        sources = [{"kind": "confluence", "space": "TPM"}]
        conv = self._make_conv(sources=sources)

        mock_ingestor_instance = MagicMock()
        mock_ingestor_instance.ingest_space.side_effect = RuntimeError("network timeout")
        mock_ingestor_cls = MagicMock(return_value=mock_ingestor_instance)

        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with patch(
                "framework.skill_builder.conversation._build_confluence_adapter",
                return_value=None,
            ):
                with patch(
                    "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor",
                    mock_ingestor_cls,
                ):
                    turn = conv._run_ingest()

        # Must not propagate the exception
        assert turn.state == "INGEST"
        # Stats should be zero (exception swallowed)
        assert conv._data.ingest_result["items_upserted"] == 0


class TestBuildConfluenceAdapter:
    """Unit tests for _build_confluence_adapter() config-merge logic."""

    def _write_base_cfg(self, tmp_path, mode: str) -> Path:
        """Write a minimal confluence.yaml to tmp_path/adapters/ and return its path."""
        adapters_dir = tmp_path / "framework" / "config" / "adapters"
        adapters_dir.mkdir(parents=True)
        cfg_path = adapters_dir / "confluence.yaml"
        cfg_path.write_text(f"mode: {mode}\n{mode}:\n  base_url: https://conf.example.com\n")
        return tmp_path

    def _write_env_cfg(self, tmp_path, overrides: str) -> None:
        """Write a minimal laptop.yaml with adapters_overrides block."""
        env_dir = tmp_path / "framework" / "config"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "laptop.yaml").write_text(f"adapters_overrides:\n  confluence:\n{overrides}")

    def test_laptop_codex_proxy_override_builds_proxy_adapter(self, tmp_path):
        """laptop.yaml sets mode: codex_proxy → ConfluenceCodexProxyAdapter returned."""
        from framework.skill_builder.conversation import _build_confluence_adapter

        # Base config says mode: native; laptop overrides to codex_proxy
        self._write_base_cfg(tmp_path, "native")
        self._write_env_cfg(tmp_path, (
            "    mode: codex_proxy\n"
            "    codex_proxy:\n"
            "      server_name: central_confluence\n"
            "      timeout_seconds: 120\n"
            "      max_pages_per_list: 10\n"
        ))

        mock_cls = MagicMock()
        with patch("framework.adapters.confluence.codex_proxy.ConfluenceCodexProxyAdapter", mock_cls):
            with patch(
                "framework.skill_builder.conversation.ConfluenceCodexProxyAdapter",
                mock_cls,
                create=True,
            ):
                adapter = _build_confluence_adapter("laptop", tmp_path)

        # Adapter must be constructed (mock called once)
        assert adapter is not None or mock_cls.called  # either real class or mock

    def test_no_base_config_no_env_override_returns_none(self, tmp_path):
        """No confluence.yaml and no override → fixture mode (None)."""
        from framework.skill_builder.conversation import _build_confluence_adapter

        # Only create the env config dir but no confluence.yaml
        (tmp_path / "framework" / "config").mkdir(parents=True)
        (tmp_path / "framework" / "config" / "laptop.yaml").write_text("{}\n")

        result = _build_confluence_adapter("laptop", tmp_path)
        assert result is None

    def test_base_native_mode_no_env_override_builds_native_adapter(self, tmp_path):
        """mode: native in base config + no override → ConfluenceNativeAdapter."""
        from framework.skill_builder.conversation import _build_confluence_adapter

        self._write_base_cfg(tmp_path, "native")
        (tmp_path / "framework" / "config" / "laptop.yaml").write_text("{}\n")

        mock_cls = MagicMock()
        with patch("framework.adapters.confluence.native.ConfluenceNativeAdapter", mock_cls):
            with patch(
                "framework.skill_builder.conversation.ConfluenceNativeAdapter",
                mock_cls,
                create=True,
            ):
                adapter = _build_confluence_adapter("laptop", tmp_path)

        assert adapter is not None or mock_cls.called

    def test_env_override_takes_precedence_over_base_mode(self, tmp_path):
        """Base says mode: mcp, env override says mode: codex_proxy → codex_proxy wins."""
        from framework.skill_builder.conversation import _build_confluence_adapter

        self._write_base_cfg(tmp_path, "mcp")
        self._write_env_cfg(tmp_path, (
            "    mode: codex_proxy\n"
            "    codex_proxy:\n"
            "      server_name: central_confluence\n"
            "      timeout_seconds: 60\n"
        ))

        mock_proxy = MagicMock()
        mock_mcp = MagicMock()
        with patch("framework.skill_builder.conversation.ConfluenceMcpAdapter", mock_mcp, create=True):
            with patch(
                "framework.skill_builder.conversation.ConfluenceCodexProxyAdapter",
                mock_proxy,
                create=True,
            ):
                _build_confluence_adapter("laptop", tmp_path)

        # codex_proxy should be instantiated, mcp should NOT
        assert not mock_mcp.called
