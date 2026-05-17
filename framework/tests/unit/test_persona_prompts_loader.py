"""Persona overlay tests — ADR-030 C1 cutover version.

Originally tested framework/config/persona_prompts.yaml and
_load_persona_prompt_fragments (ADR-028 S4).  Both were deleted by ADR-030 C1:

  - persona_prompts.yaml -> content migrated to
    framework/config/prompts/persona_overlays.yaml
  - _load_persona_prompt_fragments() -> replaced by PromptRegistry overlay
    mechanism (get_registry().get_prompt(prompt_id, persona=..., ...))

This file now validates the equivalent guarantees via the registry:

  (A) Overlay-content assertions — verify persona_overlays.yaml has all 9
      personas, each with non-empty overlay_vars.
  (B) Registry-integration tests — verify that get_prompt() injects overlay
      vars correctly for known personas, and degrades gracefully for unknown.

Blueprint reference: ADR-030 C1 cutover spec.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from framework.skill_builder.prompt_registry import get_registry, MissingVarsError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSONA_OVERLAYS_YAML = REPO_ROOT / "framework" / "config" / "prompts" / "persona_overlays.yaml"

EXPECTED_PERSONAS = [
    "tpm",
    "pm",
    "architect",
    "eng_mgr",
    "developer",
    "ops_eng",
    "ops_mgr",
    "service_owner",
    "kbf_ops",
]

REQUIRED_OVERLAY_VARS = [
    "persona_key_fields",
    "persona_extraction_style",
    "persona_few_shot_example",
]

# Prompts that accept persona overlays
OVERLAY_PROMPT_IDS = ["capture_intent", "design_skill"]

# Minimal kwargs so overlay-using prompts can be rendered without MissingVarsError
# (the overlay provides persona_key_fields / persona_extraction_style / persona_few_shot_example;
# the caller must supply the non-overlay required_vars)
_CAPTURE_INTENT_BASE = dict(intent="weekly exec review")
_DESIGN_SKILL_BASE = dict(
    normalised_intent='{"output_kind": "pptx"}',
    source_capability="[]",
    artifact_layout="null",
    existing_kb_cards="[]",
    # ADR-034: layout_preset_catalog is now a required_var in design_skill
    layout_preset_catalog="(test placeholder — no presets)",
)


# ---------------------------------------------------------------------------
# (A) Overlay YAML content assertions
# ---------------------------------------------------------------------------


class TestPersonaOverlaysYamlContent:
    """Assert that persona_overlays.yaml is structurally complete.

    These tests load the YAML directly, without importing conversation.py.
    They MUST PASS because persona_overlays.yaml is committed.
    Any red result indicates a content defect in the YAML itself.
    """

    @pytest.fixture(scope="class")
    def yaml_data(self):
        assert PERSONA_OVERLAYS_YAML.exists(), (
            f"persona_overlays.yaml not found at {PERSONA_OVERLAYS_YAML}. "
            "This file must be committed — it absorbed persona_prompts.yaml in ADR-030 C1."
        )
        raw = PERSONA_OVERLAYS_YAML.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        assert isinstance(data, dict), "persona_overlays.yaml must be a YAML mapping at the top level"
        # The file has a top-level 'personas' key
        personas = data.get("personas", {})
        assert isinstance(personas, dict), "'personas' key must be a mapping"
        return personas

    def test_yaml_file_exists(self):
        assert PERSONA_OVERLAYS_YAML.exists(), (
            f"persona_overlays.yaml missing at expected path: {PERSONA_OVERLAYS_YAML}"
        )

    def test_old_yaml_deleted(self):
        old_yaml = REPO_ROOT / "framework" / "config" / "persona_prompts.yaml"
        assert not old_yaml.exists(), (
            f"persona_prompts.yaml still exists at {old_yaml}. "
            "ADR-030 C1 requires it to be deleted (content migrated to persona_overlays.yaml)."
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_persona_stanza_present(self, yaml_data, persona):
        """Every expected persona must have a stanza in persona_overlays.yaml."""
        assert persona in yaml_data, (
            f"Persona '{persona}' is missing from persona_overlays.yaml. "
            f"Present keys: {list(yaml_data.keys())}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_applies_to_present(self, yaml_data, persona):
        """Each stanza must have an applies_to list."""
        stanza = yaml_data.get(persona, {})
        assert "applies_to" in stanza, (
            f"Persona '{persona}' missing 'applies_to' in persona_overlays.yaml"
        )
        assert isinstance(stanza["applies_to"], list) and len(stanza["applies_to"]) > 0, (
            f"Persona '{persona}': applies_to must be a non-empty list"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_overlay_vars_present(self, yaml_data, persona):
        """Each stanza must have overlay_vars."""
        stanza = yaml_data.get(persona, {})
        assert "overlay_vars" in stanza, (
            f"Persona '{persona}' missing 'overlay_vars' in persona_overlays.yaml"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    @pytest.mark.parametrize("var_key", REQUIRED_OVERLAY_VARS)
    def test_required_overlay_var_present(self, yaml_data, persona, var_key):
        """Each persona overlay_vars must contain all three required vars."""
        overlay_vars = yaml_data.get(persona, {}).get("overlay_vars", {})
        assert var_key in overlay_vars, (
            f"Persona '{persona}' overlay_vars missing '{var_key}' in persona_overlays.yaml. "
            f"Present vars: {list(overlay_vars.keys())}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_persona_key_fields_nonempty(self, yaml_data, persona):
        """persona_key_fields must be a non-empty string."""
        val = yaml_data[persona]["overlay_vars"].get("persona_key_fields", "")
        assert isinstance(val, str) and val.strip(), (
            f"Persona '{persona}': persona_key_fields must be non-empty string"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_persona_extraction_style_nonempty(self, yaml_data, persona):
        """persona_extraction_style must be a non-empty string."""
        val = yaml_data[persona]["overlay_vars"].get("persona_extraction_style", "")
        assert isinstance(val, str) and val.strip(), (
            f"Persona '{persona}': persona_extraction_style must be non-empty string"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_persona_few_shot_example_nonempty(self, yaml_data, persona):
        """persona_few_shot_example must be a non-empty string."""
        val = yaml_data[persona]["overlay_vars"].get("persona_few_shot_example", "")
        assert isinstance(val, str) and val.strip(), (
            f"Persona '{persona}': persona_few_shot_example must be non-empty string"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_persona_key_fields_has_at_least_three_comma_separated(self, yaml_data, persona):
        """persona_key_fields should list at least 3 fields (comma-separated or newline-separated)."""
        val = yaml_data[persona]["overlay_vars"].get("persona_key_fields", "")
        # Count commas as field separators
        count = len([f.strip() for f in val.split(",") if f.strip()])
        assert count >= 3, (
            f"Persona '{persona}': persona_key_fields has only {count} entries; "
            "expected at least 3 for meaningful persona guidance."
        )

    def test_exactly_nine_personas_defined(self, yaml_data):
        """The overlay YAML must define exactly the 9 expected personas."""
        yaml_personas = set(yaml_data.keys())
        expected_set = set(EXPECTED_PERSONAS)
        missing = expected_set - yaml_personas
        extra = yaml_personas - expected_set
        assert not missing, f"Missing personas in overlay YAML: {sorted(missing)}"
        assert not extra, (
            f"Unexpected personas in overlay YAML: {sorted(extra)}. "
            "Add them to EXPECTED_PERSONAS in this test file if intentional."
        )

    def test_tpm_extraction_style_contains_exec_safe(self, yaml_data):
        """Spot-check: tpm extraction_style must mention exec-safe language."""
        style = yaml_data["tpm"]["overlay_vars"].get("persona_extraction_style", "")
        assert "exec-safe" in style.lower() or "exec safe" in style.lower(), (
            f"tpm persona_extraction_style must mention 'exec-safe' language, got: {style[:200]!r}"
        )


# ---------------------------------------------------------------------------
# (B) Registry integration tests
# ---------------------------------------------------------------------------


class TestRegistryOverlayIntegration:
    """Verify get_prompt() injects overlay vars for known personas.

    These tests use the real registry against the committed YAML store.
    They replace the old TestPersonaPromptFragmentsLoaderContract tests.
    """

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_capture_intent_renders_without_error_for_known_persona(self, persona):
        """get_prompt('capture_intent', persona=X, ...) must not raise for known personas."""
        spec = get_registry().get_prompt("capture_intent", persona=persona, **_CAPTURE_INTENT_BASE)
        assert spec.text, f"Rendered capture_intent text is empty for persona '{persona}'"

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_design_skill_renders_without_error_for_known_persona(self, persona):
        """get_prompt('design_skill', persona=X, ...) must not raise for known personas."""
        spec = get_registry().get_prompt("design_skill", persona=persona, **_DESIGN_SKILL_BASE)
        assert spec.text, f"Rendered design_skill text is empty for persona '{persona}'"

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_overlay_vars_injected_into_capture_intent(self, persona):
        """The persona overlay vars must actually appear in the rendered prompt (no raw placeholders)."""
        spec = get_registry().get_prompt("capture_intent", persona=persona, **_CAPTURE_INTENT_BASE)
        assert "{persona_key_fields}" not in spec.text, (
            f"persona_key_fields placeholder not substituted in capture_intent for persona '{persona}'"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_overlay_vars_injected_into_design_skill(self, persona):
        """design_skill placeholders must all be substituted."""
        spec = get_registry().get_prompt("design_skill", persona=persona, **_DESIGN_SKILL_BASE)
        for placeholder in ("{persona_key_fields}", "{persona_extraction_style}", "{persona_few_shot_example}"):
            assert placeholder not in spec.text, (
                f"{placeholder} not substituted in design_skill for persona '{persona}'"
            )

    def test_tpm_key_fields_appear_in_capture_intent(self):
        """tpm overlay must inject orm_status (first key field) into capture_intent text."""
        spec = get_registry().get_prompt("capture_intent", persona="tpm", **_CAPTURE_INTENT_BASE)
        assert "orm_status" in spec.text, (
            "tpm persona_key_fields (including 'orm_status') not injected into capture_intent"
        )

    def test_tpm_extraction_style_appears_in_design_skill(self):
        """tpm extraction_style ('exec-safe') must appear in design_skill rendered text."""
        spec = get_registry().get_prompt("design_skill", persona="tpm", **_DESIGN_SKILL_BASE)
        assert "exec-safe" in spec.text.lower() or "exec" in spec.text.lower(), (
            "tpm extraction_style (exec-safe language) not injected into design_skill"
        )

    def test_unknown_persona_raises_missing_vars_error(self):
        """Unknown persona has no overlay; registry raises MissingVarsError for required_vars."""
        with pytest.raises(MissingVarsError):
            get_registry().get_prompt("capture_intent", persona="unknown_persona_xyz",
                                      **_CAPTURE_INTENT_BASE)

    def test_unknown_persona_can_be_handled_with_explicit_defaults(self):
        """Callers can supply empty-string defaults to avoid MissingVarsError for unknown persona."""
        spec = get_registry().get_prompt(
            "capture_intent",
            persona="unknown_persona_xyz",
            intent="weekly review",
            persona_key_fields="(none specified)",
            persona_extraction_style="",
            persona_few_shot_example="",
        )
        assert spec.text, "Should render with explicit empty defaults"
        assert "(none specified)" in spec.text

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_overlay_is_idempotent_across_calls(self, persona):
        """Two get_prompt calls with the same args must return identical text."""
        spec_a = get_registry().get_prompt("capture_intent", persona=persona, **_CAPTURE_INTENT_BASE)
        spec_b = get_registry().get_prompt("capture_intent", persona=persona, **_CAPTURE_INTENT_BASE)
        assert spec_a.text == spec_b.text, (
            f"get_prompt('capture_intent', persona='{persona}') returned different text on two calls"
        )

    def test_caller_supplied_vars_override_overlay(self):
        """Explicit caller vars win over overlay vars (registry merge rule)."""
        custom_fields = "my_custom_field_only"
        spec = get_registry().get_prompt(
            "capture_intent",
            persona="tpm",
            intent="test",
            persona_key_fields=custom_fields,
        )
        assert custom_fields in spec.text, (
            "Caller-supplied persona_key_fields did not override tpm overlay"
        )
        assert "orm_status" not in spec.text, (
            "tpm overlay persona_key_fields took precedence over caller-supplied value"
        )
