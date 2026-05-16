"""Regression test — BUG-queue-44364 comprehensive fix.

Verifies:
 1. Real 26ai_fa_db_upgrade schema (~32 fields) + rich synthetic source:
    mock LLM returning complete 32-field JSON (tokens_out=4500, max_tokens=16384)
    → no ValueError, all 32 keys present, executive_summary NOT clipped at 500 chars.

 2. Mock LLM returning tokens_out==16384 (hits ceiling) → ValueError raised
    mentioning BUG-queue-44364 (detection still works at the raised ceiling).

 3. synthesize_schema no longer emits maxLength for summary/text fields.

All LLM calls are mocked — no live calls.

Follows patterns from test_review.py and test_prompt_registry.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.skill_builder.review import _llm_extract, _parse_llm_json_response
from framework.skill_builder.synthesize_schema import (
    _infer_field_spec,
    synthesize_extraction_schema,
)

# ---------------------------------------------------------------------------
# Load the real 26ai_fa_db_upgrade schema
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_real_schema() -> dict:
    """Load the real 26ai_fa_db_upgrade_to_26ai_pptx/v1.json schema."""
    candidates = [
        _REPO_ROOT / "framework" / "parsers" / "schemas" / "tpm"
        / "26ai_fa_db_upgrade_to_26ai_pptx" / "v1.json",
        _REPO_ROOT / "framework" / "parsers" / "schemas" / "tpm"
        / "26ai_fa_db_upgrade_pptx" / "v1.json",
    ]
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text())
    # Fallback: synthesize a 32-field schema matching the production shape
    fields = [
        "project_name", "current_phase", "overall_status", "executive_summary",
        "business_outcome", "in_scope", "out_of_scope", "dependencies_text",
        "assumptions", "primary_faaaspmo_jira_key", "primary_faaaspmo_jira_url",
        "primary_faaasinges_jira_key", "primary_faaasinges_jira_url",
        "jira_bug_report_title", "jira_bug_report_url", "project_repository_url",
        "slack_channels", "target_ga_date", "milestone_columns",
        "milestone_rows_partial", "plan_items", "restriction_notes",
        "downtime_estimate", "cluster_counts", "need_by_dates",
        "workstreams_discovered", "workstream_status_next_steps",
        "weekly_changes_trailing_7_days_pt", "overall_status_rag",
        "key_milestones_final", "orm_items", "risks_and_mitigations",
    ]
    return synthesize_extraction_schema(fields, persona="tpm", kb_name="26ai_fa_db_upgrade")


def _make_rich_source_text(schema: dict) -> str:
    """Build a synthetic rich source document that covers all schema fields."""
    props = schema.get("properties", {})
    lines = [
        "26ai FA DB Upgrade — Project Status Page",
        "Project Name: 26ai FA DB Upgrade",
        "Current Phase: Design and POC",
        "Overall Status: PLANNED",
        "Executive Summary: " + ("This project upgrades the FA database to the 26ai release, "
                                  "enabling faster analytics and improved SLA compliance for all "
                                  "FAAAS tenants. The project is currently in the Design and POC "
                                  "phase with no blockers identified." * 3),
        "Business Outcome: Reduce FA query latency by 40% and eliminate legacy DB constraints "
                          "preventing 26ai feature delivery. " * 2,
        "In Scope: FA DB upgrade to 26ai schema; data migration; SLA testing.",
        "Out of Scope: UI redesign; unrelated service migrations.",
        "Dependencies: Requires OCI GenAI endpoint upgrade (FAAASPMO-1190). "
                     "Coordination with FAAASINGES team on schema compatibility.",
        "Assumptions: Non-prod upgrade completes by Q2 2026. Prod upgrade Q3 2026.",
        "Primary FAAASPMO Jira Key: FAAASPMO-1190",
        "Primary FAAASPMO Jira URL: https://jira.oci.oraclecorp.com/browse/FAAASPMO-1190",
        "Primary FAAASINGES Jira Key: FAAASINGES-2526",
        "Primary FAAASINGES Jira URL: https://jira.oci.oraclecorp.com/browse/FAAASINGES-2526",
        "Jira Bug Report: FAAASPMO-1190 — FA DB 26ai upgrade tracking",
        "Project Repository: https://sharepoint.example.com/sites/26ai-fa-db",
        "Slack Channels: #26ai-fa-db-upgrade, #faaas-oncall",
        "Target GA Date: TBD",
        "Milestones: Design | 2026-03-31 | team_lead | Complete | ...",
        "Plan Items: SL#1 - DB schema analysis, PoC ETA 2026-04-15, Status: In Progress",
        "Restriction Notes: Production upgrade restricted to maintenance windows only.",
        "Downtime Estimate: 6 to 8 Hours per cluster",
        "Cluster Counts: 12 Non-Prod, 8 Prod clusters",
        "Need By Dates: 2026-06-30 (SRE sign-off), new date: 2026-07-15",
        "Workstreams Discovered: Hub page 20030556732, Schema WS 20030556733",
        "Workstream Status/Next Steps: Schema WS — In Progress — Next: finalize migration scripts",
        "Weekly Changes: 2026-05-10 — Updated milestone dates; 2026-05-09 — Added cluster count",
        "Overall Status RAG: Amber",
        "Key Milestones Final: Design | 2026-03-31 | done; PoC | 2026-04-30 | in-progress",
        "ORM Items: ORM review completed 2026-03-20; approval pending",
        "Risks and Mitigations: Risk: downtime > 8h. Mitigation: pre-stage scripts.",
    ]
    # Add extra filler to produce a realistic long document
    filler = "\n\nWBS Table:\n" + "\n".join(
        f"Row {i}: Task-{i} | In Progress | Owner-{i} | Due 2026-0{(i % 6) + 1}-15"
        for i in range(1, 50)
    )
    return "\n".join(lines) + filler


def _make_rich_llm_response(schema: dict) -> dict:
    """Build a complete 32-field extracted JSON for the real schema."""
    props = schema.get("properties", {})
    extracted: dict = {}
    for field, prop in props.items():
        field_type = prop.get("type", "string")
        if field_type == "array":
            extracted[field] = [f"item-{field}-1", f"item-{field}-2"]
        elif field_type == "integer":
            extracted[field] = 42
        elif field_type == "boolean":
            extracted[field] = True
        else:
            # Realistic long values for text/summary fields — must be > 500 chars
            # to verify no maxLength:500 cap is applied anywhere in the pipeline.
            if any(k in field for k in ("summary", "description", "text", "narrative", "body")):
                # Deliberately > 500 chars to catch any residual maxLength:500 clipping.
                base = (
                    f"Full extracted content for {field}. "
                    "This value is intentionally long — far exceeding the old arbitrary "
                    "500-character maxLength cap that was removed by ADR-031 Group B. "
                    "ADB CLOB is the backing store for all skill content, so there is no "
                    "storage reason to clip values at 500 or 1000 chars. "
                    "The FA DB upgrade project is currently in Design and POC phase. "
                    "No blockers identified as of 2026-05-16. "
                    "Key risks: downtime > 8h, mitigation: pre-staged scripts. "
                    "ORM approval pending. SRE sign-off target: 2026-06-30."
                )
                # Pad to ensure > 600 chars regardless of field name length
                extracted[field] = base + " " * max(0, 601 - len(base))
            elif field.endswith("_status") or field == "status":
                extracted[field] = "PLANNED"
            elif field.endswith("_id") or field == "id":
                extracted[field] = "FAAASPMO-1190"
            elif "rag" in field:
                extracted[field] = "Amber"
            else:
                extracted[field] = f"Extracted value for {field}"
    return extracted


def _make_mock_llm(response_text: str, tokens_out: int | None = None) -> MagicMock:
    mock = MagicMock()
    result: dict = {"text": response_text}
    if tokens_out is not None:
        result["tokens_out"] = tokens_out
    mock.chat.return_value = result
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractionNoTruncation:
    """Regression: BUG-queue-44364 comprehensive fix — no arbitrary content caps."""

    def setup_method(self):
        self.schema = _load_real_schema()
        self.props = self.schema.get("properties", {})
        self.source_text = _make_rich_source_text(self.schema)
        assert len(self.props) >= 30, (
            f"Expected ≥30 fields in schema, got {len(self.props)}"
        )

    def test_complete_32_field_response_parses_all_keys(self):
        """Complete 32-field LLM response (tokens_out=4500, max_tokens=16384)
        parses without error; all schema keys present; no ValueError raised.
        """
        extracted_dict = _make_rich_llm_response(self.schema)
        response_json = json.dumps(extracted_dict)

        # Verify the JSON is non-trivial
        assert len(response_json) > 2000, (
            "Response JSON is unexpectedly short — check _make_rich_llm_response"
        )

        # tokens_out=4500 well under new ceiling of 16384 → should succeed
        mock_llm = _make_mock_llm(response_json, tokens_out=4500)
        sample = {
            "content": self.source_text,
            "source_citation": "https://confluence.example.com/pages/20030556732",
        }

        result = _llm_extract(sample, self.schema, mock_llm)

        assert isinstance(result, dict), "Expected dict result"
        # All schema fields must be present
        for field in self.props:
            assert field in result, (
                f"Field '{field}' missing from extraction result. "
                "This indicates the LLM response was silently clipped or ignored."
            )

    def test_executive_summary_not_clipped_at_500_chars(self):
        """executive_summary value longer than 500 chars must survive extraction.

        Before ADR-031 Group B fix, synthesize_schema emitted maxLength:500 on
        summary fields. After the fix, no maxLength cap exists, so rich values
        flow through unchanged.
        """
        extracted_dict = _make_rich_llm_response(self.schema)
        summary_val = extracted_dict.get("executive_summary", "")
        assert len(summary_val) > 500, (
            "Test setup: executive_summary fixture value should be > 500 chars"
        )

        response_json = json.dumps(extracted_dict)
        mock_llm = _make_mock_llm(response_json, tokens_out=4500)
        sample = {
            "content": self.source_text,
            "source_citation": "https://confluence.example.com/pages/20030556732",
        }

        result = _llm_extract(sample, self.schema, mock_llm)
        assert "executive_summary" in result
        # The value must NOT be clipped — the full text must survive
        assert len(result["executive_summary"]) > 500, (
            f"executive_summary was clipped: only {len(result['executive_summary'])} chars. "
            "This suggests a maxLength cap is still active somewhere."
        )

    def test_tokens_out_at_new_ceiling_raises_bug44364_error(self):
        """tokens_out==16384 (new ceiling) must raise ValueError mentioning BUG-queue-44364.

        This verifies truncation detection still works at the raised ceiling.
        """
        # Build a truncated response
        partial = {f"field_{i}": f"value {i}" for i in range(10)}
        truncated_json = json.dumps(partial, indent=2)[:-5]  # cut off the closing brace
        truncated_json += '  "incomplete'  # mid-key

        # tokens_out == 16384 = hits the new ceiling exactly
        mock_llm = _make_mock_llm(truncated_json, tokens_out=16384)
        sample = {
            "content": self.source_text,
            "source_citation": "https://confluence.example.com/pages/20030556732",
        }

        with pytest.raises(ValueError) as exc_info:
            _llm_extract(sample, self.schema, mock_llm)

        msg = str(exc_info.value)
        assert "BUG-queue-44364" in msg, (
            f"Expected 'BUG-queue-44364' in error message, got: {msg}"
        )
        # Must describe truncation, not generic failure
        assert "truncated" in msg.lower() or "max_tokens" in msg.lower(), (
            f"Expected truncation language in error: {msg}"
        )


class TestSynthesizeSchemaNoMaxLengthRegression:
    """Regression: synthesize_schema must not emit maxLength for summary/text fields."""

    def test_synthesize_schema_summary_field_no_max_length(self):
        """synthesize_extraction_schema must not emit maxLength on summary fields."""
        schema = synthesize_extraction_schema(
            ["executive_summary", "project_id", "project_status"],
            persona="tpm",
            kb_name="test",
        )
        props = schema["properties"]
        assert "maxLength" not in props["executive_summary"], (
            f"executive_summary must not have maxLength in synthesized schema: "
            f"{props['executive_summary']}"
        )
        # _id still bounded
        assert props["project_id"].get("maxLength") == 64
        # _status still bounded
        assert props["project_status"].get("maxLength") == 50

    def test_infer_field_spec_catch_all_no_max_length(self):
        """_infer_field_spec for a catch-all string must not include maxLength."""
        spec = _infer_field_spec("project_owner")
        assert "maxLength" not in spec, (
            f"Catch-all field 'project_owner' must not have maxLength: {spec}"
        )

    def test_parse_llm_json_response_truncation_at_16384(self):
        """_parse_llm_json_response detects truncation when tokens_out==max_tokens==16384."""
        truncated = '{"a": "incomplete'
        with pytest.raises(ValueError) as exc_info:
            _parse_llm_json_response(truncated, tokens_out=16384, max_tokens=16384)
        assert "BUG-queue-44364" in str(exc_info.value)
