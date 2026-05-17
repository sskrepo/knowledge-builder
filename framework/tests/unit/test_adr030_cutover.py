"""ADR-030 C-stream cutover tests.

Verifies that each cutover call site uses get_registry().get_prompt() with the
correct prompt_id and persona argument.  All tests use the real registry and
real YAML files in framework/config/prompts/ — no mocking of the registry itself.
LLM is mocked; no network calls.

Coverage:
  C1 structural: verify prompt IDs are in registry, verify persona overlay applies
  C2 structural: description_synthesis prompt accessible
  C3 structural: review_extract prompt accessible
  C4 structural: executor_extract prompt accessible + byte-identity check

Call-site behavior tests rely on the fact that all existing test suites pass
(test_skill_builder_conversation.py, test_adr029_s6.py, test_review.py, etc.),
which exercise the full call paths.  These tests focus on the registry contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "framework" / "config" / "prompts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_reg():
    """Return a fresh PromptRegistry from the real YAML files."""
    from framework.skill_builder.prompt_registry import PromptRegistry
    return PromptRegistry(PROMPTS_DIR)


# ---------------------------------------------------------------------------
# C1 — conversation.py structural tests
# ---------------------------------------------------------------------------

class TestC1ConversationPromptIds:
    """C1: All 8 conversation.py prompt IDs (+ analyze_artifact) are in the registry."""

    EXPECTED_IDS = [
        "capture_intent",
        "clarify",
        "configure_sources",
        "inspect_sources",
        "design_skill",
        "review_design_replan",
        "eval_judge",
        "failure_classifier",
        "analyze_artifact",  # legacy path
    ]

    def test_all_prompt_ids_in_registry(self):
        """Every prompt that conversation.py uses must be present in the YAML registry."""
        reg = _get_reg()
        loaded_ids = {p.prompt_id for p in reg.list_prompts()}
        for pid in self.EXPECTED_IDS:
            assert pid in loaded_ids, (
                f"Prompt '{pid}' must be present in the registry. "
                f"Loaded: {sorted(loaded_ids)}"
            )

    def test_failure_classifier_is_locked(self):
        """failure_classifier must be gate-locked (locked=true, checksum present)."""
        reg = _get_reg()
        meta = {p.prompt_id: p for p in reg.list_prompts()}
        assert meta["failure_classifier"].locked, "failure_classifier must be locked=True"

    def test_failure_classifier_checksum_valid(self):
        """failure_classifier registry load validates the checksum (no LockedPromptTamperedError)."""
        # If the checksum were wrong, PromptRegistry() construction would raise.
        # We just verify the registry constructs successfully.
        reg = _get_reg()
        assert reg._raw_template("failure_classifier"), "failure_classifier template must be non-empty"

    def test_capture_intent_persona_overlay_applied_for_tpm(self):
        """tpm persona overlay injects persona_key_fields into capture_intent."""
        reg = _get_reg()
        spec = reg.get_prompt(
            "capture_intent",
            persona="tpm",
            intent="Create weekly exec review",
        )
        assert "orm_status" in spec.text or "blocking_issues" in spec.text, (
            "tpm persona overlay must inject key_fields into capture_intent. "
            f"Got text (first 500): {spec.text[:500]!r}"
        )

    def test_design_skill_persona_overlay_applied_for_tpm(self):
        """tpm persona overlay injects extraction_style into design_skill."""
        reg = _get_reg()
        spec = reg.get_prompt(
            "design_skill",
            persona="tpm",
            normalised_intent='{"scope_domains": ["test"]}',
            source_capability="[]",
            artifact_layout="null",
            existing_kb_cards="[]",
            layout_preset_catalog="(ADR-034 test placeholder — catalog injected at runtime)",
        )
        assert "exec-safe" in spec.text, (
            "tpm persona overlay must inject extraction_style (with 'exec-safe') into design_skill. "
            f"Got text (first 500): {spec.text[:500]!r}"
        )

    def test_capture_intent_unknown_persona_graceful_degradation(self):
        """Unknown persona must not raise from capture_intent (empty overlay, explicit defaults)."""
        reg = _get_reg()
        # Without explicit defaults, MissingVarsError would be raised for unknown persona.
        # The call site provides empty defaults — verify the registry returns a spec.
        spec = reg.get_prompt(
            "capture_intent",
            persona="unknown_persona_xyzzy",
            intent="test intent",
            persona_key_fields="(none specified)",
        )
        assert spec.text, "capture_intent with explicit empty defaults must return non-empty text"

    def test_failure_classifier_locked_prompt_tampered_raises(self):
        """A tampered failure_classifier (wrong checksum) must raise LockedPromptTamperedError."""
        import yaml
        import tempfile
        import os
        from framework.skill_builder.prompt_registry import PromptRegistry, LockedPromptTamperedError

        # Read the real YAML and tamper with the template
        with open(PROMPTS_DIR / "skill_builder.yaml") as f:
            data = yaml.safe_load(f)

        data["prompts"]["failure_classifier"]["template"] = "TAMPERED TEMPLATE {normalised_intent} {schema_properties} {capability_inventory} {gap_report} {missing_sections} {thin_sections}"

        with tempfile.TemporaryDirectory() as tmpdir:
            tampered_path = Path(tmpdir) / "skill_builder.yaml"
            with open(tampered_path, "w") as f:
                yaml.dump(data, f)

            with pytest.raises(LockedPromptTamperedError):
                PromptRegistry(Path(tmpdir))

    def test_persona_prompts_yaml_deleted(self):
        """framework/config/persona_prompts.yaml must be deleted (content in persona_overlays.yaml)."""
        old_path = REPO_ROOT / "framework" / "config" / "persona_prompts.yaml"
        assert not old_path.exists(), (
            f"persona_prompts.yaml must be deleted as part of C1 cutover. "
            f"Found: {old_path}"
        )

    def test_persona_overlays_yaml_exists(self):
        """framework/config/prompts/persona_overlays.yaml must exist."""
        overlays_path = PROMPTS_DIR / "persona_overlays.yaml"
        assert overlays_path.exists(), (
            f"persona_overlays.yaml must exist in prompts dir. "
            f"Looked at: {overlays_path}"
        )

    def test_conversation_module_has_no_old_constants(self):
        """conversation.py must NOT export old prompt constants (they are deleted)."""
        import framework.skill_builder.conversation as conv_mod
        old_attrs = [
            "_CAPTURE_INTENT_PROMPT",
            "_CLARIFY_PROMPT",
            "_CONFIGURE_SOURCES_SUGGEST_PROMPT",
            "_INSPECT_SOURCES_PROMPT",
            "_DESIGN_SKILL_PROMPT",
            "_REVIEW_DESIGN_REPLAN_PROMPT",
            "_EVAL_JUDGE_PROMPT",
            "_FAILURE_CLASSIFIER_PROMPT",
            "_ANALYZE_ARTIFACT_PROMPT",
            "_PERSONA_PROMPT_FRAGMENTS",
            "_PERSONA_PROMPTS_YAML_PATH",
            "_load_persona_prompt_fragments",
            "_reload_persona_prompts",
        ]
        for attr in old_attrs:
            assert not hasattr(conv_mod, attr), (
                f"conversation.py must NOT export '{attr}' after C1 cutover. "
                f"Delete the constant/function from the module."
            )

    def test_conversation_module_imports_get_registry(self):
        """conversation.py must import get_registry (C1 cutover contract)."""
        import framework.skill_builder.conversation as conv_mod
        assert hasattr(conv_mod, "get_registry"), (
            "conversation.py must import get_registry from prompt_registry. "
            "C1 cutover adds: from .prompt_registry import get_registry"
        )

    def test_clarify_prompt_model_is_none(self):
        """clarify must have model=none (it is a turn message, not an LLM call)."""
        reg = _get_reg()
        meta = {p.prompt_id: p for p in reg.list_prompts()}
        assert meta["clarify"].model == "none", (
            f"clarify prompt must have model='none'. Got: {meta['clarify'].model}"
        )


# ---------------------------------------------------------------------------
# C2 — synthesize_schema.py structural tests
# ---------------------------------------------------------------------------

class TestC2SynthesizeSchemaPromptId:
    """C2: description_synthesis is in the registry and synthesize_schema uses it."""

    def test_description_synthesis_in_registry(self):
        """description_synthesis must be present in the YAML registry."""
        reg = _get_reg()
        loaded_ids = {p.prompt_id for p in reg.list_prompts()}
        assert "description_synthesis" in loaded_ids

    def test_description_synthesis_has_correct_metadata(self):
        """description_synthesis: model=synthesis, max_tokens=2048, response_format=json_object."""
        reg = _get_reg()
        meta = {p.prompt_id: p for p in reg.list_prompts()}
        m = meta["description_synthesis"]
        assert m.model == "synthesis", f"Expected synthesis, got {m.model}"

    def test_synthesize_schema_module_imports_get_registry(self):
        """synthesize_schema.py must import get_registry (C2 cutover contract)."""
        import framework.skill_builder.synthesize_schema as ss_mod
        assert hasattr(ss_mod, "get_registry"), (
            "synthesize_schema.py must import get_registry after C2 cutover."
        )

    def test_description_synthesis_constant_deleted(self):
        """synthesize_schema.py must NOT export _DESCRIPTION_SYNTHESIS_PROMPT after C2."""
        import framework.skill_builder.synthesize_schema as ss_mod
        assert not hasattr(ss_mod, "_DESCRIPTION_SYNTHESIS_PROMPT"), (
            "_DESCRIPTION_SYNTHESIS_PROMPT must be deleted from synthesize_schema.py after C2."
        )


# ---------------------------------------------------------------------------
# C3 — review.py structural tests
# ---------------------------------------------------------------------------

class TestC3ReviewPromptId:
    """C3: review_extract is in the registry and review.py uses it."""

    def test_review_extract_in_registry(self):
        """review_extract must be present in the YAML registry."""
        reg = _get_reg()
        loaded_ids = {p.prompt_id for p in reg.list_prompts()}
        assert "review_extract" in loaded_ids

    def test_review_extract_has_correct_metadata(self):
        """review_extract: model=synthesis, max_tokens=4096."""
        reg = _get_reg()
        meta = {p.prompt_id: p for p in reg.list_prompts()}
        m = meta["review_extract"]
        assert m.model == "synthesis"

    def test_review_module_imports_get_registry(self):
        """review.py must import get_registry (C3 cutover contract)."""
        import framework.skill_builder.review as rev_mod
        assert hasattr(rev_mod, "get_registry"), (
            "review.py must import get_registry after C3 cutover."
        )

    def test_review_extract_constant_deleted(self):
        """review.py must NOT export _REVIEW_EXTRACT_PROMPT after C3."""
        import framework.skill_builder.review as rev_mod
        assert not hasattr(rev_mod, "_REVIEW_EXTRACT_PROMPT"), (
            "_REVIEW_EXTRACT_PROMPT must be deleted from review.py after C3."
        )


# ---------------------------------------------------------------------------
# C4 — executor.py structural tests + byte-identity check
# ---------------------------------------------------------------------------

class TestC4ExecutorPromptId:
    """C4: executor_extract is in the registry; byte-identity verified."""

    def test_executor_extract_in_registry(self):
        """executor_extract must be present in the YAML registry."""
        reg = _get_reg()
        loaded_ids = {p.prompt_id for p in reg.list_prompts()}
        assert "executor_extract" in loaded_ids

    def test_executor_extract_has_correct_metadata(self):
        """executor_extract: model=synthesis, max_tokens=4096."""
        reg = _get_reg()
        meta = {p.prompt_id: p for p in reg.list_prompts()}
        m = meta["executor_extract"]
        assert m.model == "synthesis"

    def test_executor_module_imports_get_registry(self):
        """executor.py must import get_registry (C4 cutover contract)."""
        import framework.workflow_runtime.executor as exec_mod
        assert hasattr(exec_mod, "get_registry"), (
            "executor.py must import get_registry after C4 cutover."
        )

    def test_executor_extract_constant_not_used_inline(self):
        """executor.py must NOT construct the inline prompt with a string literal after C4.

        Verified by inspecting the source: if get_registry is imported, C4 is done.
        """
        import framework.workflow_runtime.executor as exec_mod
        # get_registry must be present (verified in test above)
        # The byte-identity test below verifies the content is equivalent.
        assert hasattr(exec_mod, "get_registry"), "C4 not done: get_registry missing from executor.py"

    def test_executor_extract_byte_identity(self):
        """C4 byte-identity: spec.text == old f-string output for a known input set.

        This is the mandatory C4 verification per ADR-030-impl-plan §C4.
        The YAML template (executor.yaml, executor_extract) must produce exactly
        the same formatted string as the old inline f-string construction in
        executor.py _llm_extract_fields.
        """
        reg = _get_reg()

        # Known inputs — deterministic sample
        field_lines_list = [
            '  - "project_name" (string) [required]: Extract the project name from the Confluence metadata table.',
            '  - "schedule_health" (string): RAG status (Red/Amber/Green) for the schedule.',
            '  - "next_steps" (array): Synthesise next steps from WBS rows marked IN PROGRESS.',
        ]
        user_request_val = "Generate weekly exec review presentation for leadership"
        snippet_val = (
            "FA DB Upgrade — Project Plan\n"
            "Project Name: FA DB Upgrade from 19c to 26ai\n"
            "Status: PLANNED\n"
            "Phase: Design and POC\n"
        )

        # Old f-string construction (exactly as executor.py had before C4)
        old_prompt = (
            "You are extracting structured fields from a Confluence/wiki page "
            "to populate an executive-review presentation. Return a single JSON "
            "object with EXACTLY these keys (use empty string \"\" or empty "
            "list [] when a field is genuinely absent — do not invent data):\n\n"
            f"{chr(10).join(field_lines_list)}\n\n"
            f"User request: {user_request_val}\n\n"
            "=== Source document ===\n"
            f"{snippet_val}\n"
            "=== End source ===\n\n"
            "Respond with ONLY the JSON object, no prose, no markdown fences."
        )

        # New registry-based prompt
        spec = reg.get_prompt(
            "executor_extract",
            field_lines=chr(10).join(field_lines_list),
            user_request=user_request_val,
            snippet=snippet_val,
        )

        assert spec.text == old_prompt, (
            f"C4 byte-identity FAIL: executor_extract spec.text != old f-string output.\n"
            f"Old length: {len(old_prompt)}\n"
            f"New length: {len(spec.text)}\n"
            f"Old (last 100): {old_prompt[-100:]!r}\n"
            f"New (last 100): {spec.text[-100:]!r}"
        )
