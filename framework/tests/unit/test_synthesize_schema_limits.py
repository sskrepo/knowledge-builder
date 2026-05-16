"""Unit tests for ADR-031: synthesize_schema must not emit arbitrary maxLength.

Group B ship test — asserts:
 1. summary/text/description/body/_narrative fields have NO maxLength.
 2. _id fields still have maxLength=64 (genuinely source-bounded).
 3. _status fields still have maxLength=50 (genuinely source-bounded).
 4. Catch-all string fields have NO maxLength.
"""
from __future__ import annotations

import pytest

from framework.skill_builder.synthesize_schema import (
    _infer_field_spec,
    synthesize_extraction_schema,
)


class TestNoArbitraryMaxLength:
    """ADR-031: synthesized schemas must not carry arbitrary maxLength caps."""

    # --- summary / text / description / body / narrative fields ---

    def test_summary_field_has_no_max_length(self):
        spec = _infer_field_spec("executive_summary")
        assert "maxLength" not in spec, (
            f"executive_summary should have no maxLength, got {spec}"
        )

    def test_description_field_has_no_max_length(self):
        spec = _infer_field_spec("project_description")
        assert "maxLength" not in spec, (
            f"project_description should have no maxLength, got {spec}"
        )

    def test_text_field_has_no_max_length(self):
        spec = _infer_field_spec("body_text")
        assert "maxLength" not in spec, (
            f"body_text should have no maxLength, got {spec}"
        )

    def test_narrative_field_has_no_max_length(self):
        spec = _infer_field_spec("risk_narrative")
        assert "maxLength" not in spec, (
            f"risk_narrative should have no maxLength, got {spec}"
        )

    def test_body_field_has_no_max_length(self):
        spec = _infer_field_spec("executive_body")
        assert "maxLength" not in spec, (
            f"executive_body should have no maxLength, got {spec}"
        )

    # --- catch-all string fields ---

    def test_catchall_string_field_has_no_max_length(self):
        spec = _infer_field_spec("project_name")
        assert "maxLength" not in spec, (
            f"project_name (catch-all string) should have no maxLength, got {spec}"
        )

    def test_generic_field_has_no_max_length(self):
        spec = _infer_field_spec("some_generic_field")
        assert "maxLength" not in spec, (
            f"some_generic_field (catch-all) should have no maxLength, got {spec}"
        )

    # --- _id fields MUST retain maxLength=64 ---

    def test_id_field_retains_max_length_64(self):
        spec = _infer_field_spec("project_id")
        assert spec.get("maxLength") == 64, (
            f"project_id should retain maxLength=64, got {spec}"
        )

    def test_bare_id_field_retains_max_length_64(self):
        spec = _infer_field_spec("id")
        assert spec.get("maxLength") == 64, (
            f"id should retain maxLength=64, got {spec}"
        )

    # --- _status fields MUST retain maxLength=50 ---

    def test_status_field_retains_max_length_50(self):
        spec = _infer_field_spec("project_status")
        assert spec.get("maxLength") == 50, (
            f"project_status should retain maxLength=50, got {spec}"
        )

    def test_bare_status_field_retains_max_length_50(self):
        spec = _infer_field_spec("status")
        assert spec.get("maxLength") == 50, (
            f"status should retain maxLength=50, got {spec}"
        )


class TestSynthesizeExtractionSchemaLimits:
    """Full schema synthesis must not emit maxLength on summary/text fields."""

    def test_summary_field_in_full_schema_has_no_max_length(self):
        fields = ["executive_summary", "project_id", "project_status", "project_name"]
        schema = synthesize_extraction_schema(fields, persona="tpm", kb_name="test_kb")
        props = schema["properties"]

        # summary — no maxLength
        assert "maxLength" not in props["executive_summary"], (
            f"executive_summary in synthesized schema must have no maxLength: {props['executive_summary']}"
        )
        # id — must have maxLength=64
        assert props["project_id"].get("maxLength") == 64
        # status — must have maxLength=50
        assert props["project_status"].get("maxLength") == 50
        # catch-all — no maxLength
        assert "maxLength" not in props["project_name"], (
            f"project_name (catch-all) in synthesized schema must have no maxLength: {props['project_name']}"
        )
