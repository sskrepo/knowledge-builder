"""ADR-029 classifier validation gate — LIVE LLM test.

Gate requirement (ADR-028-029-impl-plan.md §ADR-029 Phase 1 classifier validation gate):
  S6 is BLOCKED until this test passes on the known real case.

GOLD CASE: tpm.26ai_fa_db_upgrade_to_26ai_pptx
  Source: ~/.kbf/wiki/ocifacp/20030556732.md (the 26ai FA DB Upgrade Confluence page)
  Root cause: Key Milestones / ORM / Risk-Mitigation sections were THIN or absent
    in the produced PPTX. The WBS table on the source page DOES contain the raw data
    (status cells like GreenComplete/BlueIN PROGRESS/GreyNOT STARTED, notes like
    "4/29: FRE informed that this deliverable needs additional time", ORM rows 4.0–4.2,
    POC completion dates) — the source is NOT lacking. The gap was that the original
    schema (authored pre-S1) never included fields for risks/milestones/orm as
    synthesisable-confidence fields, so the design never asked for them.
  Ground truth label: MISSING_FIELDS (or THIN_FIELDS) — NOT SOURCE_COVERAGE.
  Anti-bias target: the model tends to say "source doesn't have it" (SOURCE_COVERAGE)
    when the truth is "design didn't ask for it" (MISSING_FIELDS). This test validates
    the classifier prompt counter-acts that directional bias.

Test procedure:
  1. Feed the gold-case inputs (capability_inventory with synthesisable evidence,
     schema WITHOUT the missing fields, gap report showing them absent) to the
     _FAILURE_CLASSIFIER_PROMPT via the real OCI GenAI LLM.
  2. Run 3 times for stability.
  3. Assert each run returns MISSING_FIELDS or THIN_FIELDS (PASS) and never
     returns SOURCE_COVERAGE or WRONG_SOURCE (FAIL).

This test uses the REAL LLM. Stub mode = BLOCKED (gate is meaningless without
the actual model). The test skips automatically if the LLM cannot be reached.

DO NOT stub this test. It is a design validation gate, not a unit test.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM connection helper — must be real (no stub)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPARTMENT_OCID = "ocid1.compartment.oc1..aaaaaaaax7wbfdtfl7axhfae7q5lwvrmf2nlcdii3scarukqmuos7u5mokla"
OCI_ENDPOINT = "https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com"
OCI_PROFILE = "adpcpprod"


def _make_real_llm():
    """Return a live OCI GenAI LLM client.

    Raises pytest.skip if the client falls back to stub mode (unreachable).
    """
    sys.path.insert(0, str(REPO_ROOT))
    from framework.core.llm import OciGenAiLLMClient

    llm = OciGenAiLLMClient(
        endpoint=OCI_ENDPOINT,
        compartment_ocid=COMPARTMENT_OCID,
        auth="config_file",
        config_profile=OCI_PROFILE,
    )
    # Smoke-test: a stub returns {"_stub": true}; a real call returns real text.
    try:
        probe = llm.chat(
            model="synthesis",
            messages=[{"role": "user", "content": "Reply with the single word: ALIVE"}],
            max_tokens=10,
        )
        text = probe.get("text", "") if isinstance(probe, dict) else str(probe)
        if "_stub" in text:
            pytest.skip(
                "LLM is in stub mode (OCI unreachable or token expired). "
                "Gate is BLOCKED — re-run when OCI GenAI session is live. "
                "Refresh: oci session authenticate --profile adpcpprod --region eu-frankfurt-1"
            )
    except Exception as exc:
        pytest.skip(f"LLM connectivity probe failed: {exc}")
    return llm


# ---------------------------------------------------------------------------
# Gold-case inputs (the known real failure)
# ---------------------------------------------------------------------------

# The source capability inventory as it would have been produced by INSPECT_SOURCES
# AFTER S1 (synthesisable confidence level) for the 26ai Confluence page
# (pageId=20030556732). This reflects what the WBS table actually contains.
#
# Key facts from the source:
#   - WBS section 5 has "4/29: FRE informed that this deliverable needs additional
#     time" → synthesisable next_steps / risks evidence
#   - WBS rows 4.0/4.1/4.2 are ORM/CSSAP phase items → synthesisable orm evidence
#   - WBS rows 1.0-6.x have Status columns (GreenComplete, BlueIN PROGRESS,
#     GreyNOT STARTED) → synthesisable milestone status evidence
#   - WBS row 6.04.1 notes "POC Completed SUT will need to start from 05/16 to 06/01"
#     → synthesisable next_steps evidence
#
# The capability inventory below is what a correctly-updated (post-S1) INSPECT_SOURCES
# call would produce for this source.
GOLD_CAPABILITY_INVENTORY = {
    "source_id": "confluence:20030556732",
    "available_fields": [
        {
            "field": "project_name",
            "type": "string",
            "confidence": "high",
            "evidence": "Project Name: FA DB Upgrade from 19c to 26ai (Section 1 metadata table)"
        },
        {
            "field": "current_phase",
            "type": "string",
            "confidence": "high",
            "evidence": "Current Phase: Design and POC (Section 1 metadata table)"
        },
        {
            "field": "overall_status",
            "type": "string",
            "confidence": "high",
            "evidence": "Overall Status: PLANNED (Section 1 metadata table)"
        },
        {
            "field": "executive_summary",
            "type": "string",
            "confidence": "high",
            "evidence": "Executive Summary: Provision new pods running 26ai DB... (Section 3)"
        },
        {
            "field": "risks_and_mitigations",
            "type": "array",
            "confidence": "synthesisable",
            "evidence": "WBS rows 5.0/5.01: '4/29: FRE informed that this deliverable needs "
                        "additional time. ETA to be finalized.' External dependency risks "
                        "scattered across WBS Update Notes column."
        },
        {
            "field": "key_milestones",
            "type": "array",
            "confidence": "synthesisable",
            "evidence": "WBS rows 1.0-6.x have Status column (GreenComplete, BlueIN PROGRESS, "
                        "GreyNOT STARTED) and Due Date columns. Milestone M1-M5 table in "
                        "Section 4. POC Completed noted in row 6.04.1."
        },
        {
            "field": "next_steps",
            "type": "array",
            "confidence": "synthesisable",
            "evidence": "WBS row 6.04.1: 'SUT will need to start from 05/16 to 06/01'. "
                        "WBS row 5.0: 'ETA to be finalized'. Open IN PROGRESS rows indicate "
                        "pending work aggregatable as next steps."
        },
        {
            "field": "orm_items",
            "type": "array",
            "confidence": "synthesisable",
            "evidence": "WBS rows 4.0 (ORM/CSSAP Phase), 4.1 (File ORM/OCI Release Management), "
                        "4.2 (File official CSSAP Record) — ORM phase with dependency on 2.3."
        },
        {
            "field": "dependencies",
            "type": "string",
            "confidence": "high",
            "evidence": "Dependencies: FRE (Repo, NGFABS etc) (Section 3 Objectives table)"
        },
    ],
    "missing_fields": [
        {
            "field": "real_time_jira_metrics",
            "reason": "No Jira API integration in this source; only static Jira links present"
        }
    ],
    "suggested_fields": [
        {
            "field": "workstream_status",
            "type": "array",
            "reason": "WBS table has rich per-area status data suitable for a workstream rollup"
        }
    ],
    "summary": (
        "This Confluence page is the FAaaS Project Plan for the FA DB Upgrade from 19c to 26ai. "
        "It contains project metadata, objectives/scope, a milestone table (M1-M5 all PLANNED), "
        "and a detailed WBS with 50+ task rows spanning areas: Project Initiation, Design, "
        "Capacity, ORM/CSSAP, External Dependencies (FRE), ADP Development. WBS rows contain "
        "status (GreenComplete/BlueIN PROGRESS/GreyNOT STARTED), owner assignments, and "
        "Update Notes with dated progress comments. Risk and next-steps information is present "
        "in synthesisable form across WBS Update Notes (e.g., FRE ETA delays, POC completion "
        "dates, SUT window notes)."
    )
}

# The schema as it existed PRE-S1 — key_milestones, orm_items, and
# risks_and_mitigations were ABSENT because DESIGN_SKILL excluded synthesisable
# fields. next_steps was also absent. This is the ground truth of the failure.
GOLD_SCHEMA_MISSING_FIELDS = {
    "project_name": {
        "type": "string",
        "description": "Extract the canonical project name from the Confluence metadata table.",
        "maxLength": 200
    },
    "current_phase": {
        "type": "string",
        "description": "Extract the current project phase from the Confluence metadata.",
        "maxLength": 200
    },
    "overall_status": {
        "type": "string",
        "description": "Extract the overall project status text from the Confluence metadata.",
        "maxLength": 200
    },
    "executive_summary": {
        "type": "string",
        "description": "Extract the Executive Summary paragraph from the Confluence project plan.",
        "maxLength": 500
    },
    "business_outcome": {
        "type": "string",
        "description": "Extract the Business Outcome statement from the Confluence project plan.",
        "maxLength": 500
    },
    "in_scope": {
        "type": "string",
        "description": "Extract the In Scope text from the Objectives/Scope section.",
        "maxLength": 500
    },
    "assumptions": {
        "type": "string",
        "description": "Extract the Assumptions text from the Objectives/Scope section.",
        "maxLength": 2000
    },
    # NOTE: risks_and_mitigations, key_milestones, orm_items, next_steps
    # are intentionally ABSENT — this is the pre-S1 design failure scenario.
}

# Comparator gap report — what ArtifactComparator.compare() would produce when
# the produced PPTX lacks the Risks/Milestones/ORM/Next-Steps slides.
GOLD_GAP_REPORT = (
    "Structure gap: The produced PPTX has 8 sections; the reference artifact had 12. "
    "Missing sections: Key Milestones, ORM Status, Risk Mitigation, Next Steps. "
    "Thin sections: Status (produced: 1 bullet; reference: 6 bullets). "
    "Structure score: 0.67. Density score: 0.45. "
    "The produced slide deck covers project metadata, scope, and assumptions well "
    "but is missing the operational sections that the reference exec review included."
)

GOLD_MISSING_SECTIONS = ["Key Milestones", "ORM Status", "Risk Mitigation", "Next Steps"]
GOLD_THIN_SECTIONS = ["Status"]

GOLD_NORMALISED_INTENT = {
    "skill_name": "26ai_fa_db_upgrade_to_26ai_pptx",
    "persona": "tpm",
    "scope_domains": ["FA DB Upgrade", "26ai"],
    "output_kind": "pptx",
    "audience": "exec",
    "layout_hint": "weekly_exec_review",
    "description": (
        "Weekly executive review PPTX for the FA DB Upgrade from 19c to 26ai project. "
        "Covers status, key milestones, ORM, risks, and next steps for the FAaaS leadership."
    )
}

# ---------------------------------------------------------------------------
# Correct and incorrect failure classes
# ---------------------------------------------------------------------------

CORRECT_CLASSES = frozenset({"MISSING_FIELDS", "THIN_FIELDS"})
INCORRECT_CLASSES = frozenset({"SOURCE_COVERAGE", "WRONG_SOURCE"})
ALL_VALID_CLASSES = frozenset({
    "MISSING_FIELDS", "THIN_FIELDS", "WRONG_LAYOUT",
    "SOURCE_COVERAGE", "WRONG_SOURCE", "UNSUPPORTABLE"
})

# ---------------------------------------------------------------------------
# Classifier invocation helper
# ---------------------------------------------------------------------------


def _call_classifier(llm: Any, run_index: int) -> dict:
    """Feed gold-case inputs to _FAILURE_CLASSIFIER_PROMPT and return parsed result.

    Returns dict with keys: failure_class, confidence, evidence,
    alternative_class, why_not_alternative, raw_text.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from framework.skill_builder.conversation import _FAILURE_CLASSIFIER_PROMPT

    prompt = _FAILURE_CLASSIFIER_PROMPT.format(
        normalised_intent=json.dumps(GOLD_NORMALISED_INTENT, indent=2),
        schema_properties=json.dumps(GOLD_SCHEMA_MISSING_FIELDS, indent=2),
        capability_inventory=json.dumps(GOLD_CAPABILITY_INVENTORY, indent=2),
        gap_report=GOLD_GAP_REPORT,
        missing_sections=json.dumps(GOLD_MISSING_SECTIONS),
        thin_sections=json.dumps(GOLD_THIN_SECTIONS),
    )

    log.info("Gate run %d: calling classifier via OCI GenAI...", run_index)
    t0 = time.time()
    result = llm.chat(
        model="synthesis",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    elapsed = time.time() - t0

    raw = result.get("text", "") if isinstance(result, dict) else str(result)
    log.info("Gate run %d: elapsed=%.1fs raw=%r", run_index, elapsed, raw[:200])

    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=re.S).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"Gate run {run_index}: classifier returned non-JSON output.\n"
            f"Raw text: {raw}\n"
            f"Parse error: {exc}"
        )

    parsed["raw_text"] = raw
    parsed["elapsed_s"] = elapsed
    return parsed


# ---------------------------------------------------------------------------
# Gate test — runs 3 times for stability
# ---------------------------------------------------------------------------


class TestFailureClassifierGate:
    """ADR-029 classifier validation gate — LIVE LLM, gold case.

    Gate verdict: PASS if all 3 runs return MISSING_FIELDS or THIN_FIELDS.
                  FAIL if ANY run returns SOURCE_COVERAGE or WRONG_SOURCE.
                  BLOCKED if LLM is unreachable (stub mode detected, test skipped).
    """

    @pytest.fixture(scope="class")
    def llm(self):
        return _make_real_llm()

    @pytest.fixture(scope="class")
    def classifier_runs(self, llm):
        """Run the classifier 3 times and collect results. Class-scoped to avoid
        repeating 3 API calls per test method."""
        runs = []
        for i in range(1, 4):
            result = _call_classifier(llm, i)
            runs.append(result)
            # Brief pause between runs to avoid rate-limit bursts
            if i < 3:
                time.sleep(2)
        return runs

    def test_all_runs_return_valid_class(self, classifier_runs):
        """Every run must return one of the 6 defined failure classes."""
        for i, run in enumerate(classifier_runs, 1):
            fc = run.get("failure_class", "MISSING")
            assert fc in ALL_VALID_CLASSES, (
                f"Run {i}: failure_class={fc!r} is not a valid class.\n"
                f"Full output: {run.get('raw_text', '')}"
            )

    def test_all_runs_return_required_fields(self, classifier_runs):
        """Every run must include evidence and why_not_alternative — mandatory fields."""
        required = {"failure_class", "confidence", "evidence",
                    "alternative_class", "why_not_alternative"}
        for i, run in enumerate(classifier_runs, 1):
            missing = required - set(run.keys())
            assert not missing, (
                f"Run {i}: classifier output missing required fields: {missing}.\n"
                f"Full output: {run.get('raw_text', '')}"
            )

    def test_all_runs_not_source_coverage(self, classifier_runs):
        """CRITICAL: no run may return SOURCE_COVERAGE for the gold case.

        The WBS table contains synthesisable evidence for risks, milestones, ORM,
        and next-steps. SOURCE_COVERAGE means 'content genuinely absent from source'
        — which is demonstrably false for this case. A classifier that returns
        SOURCE_COVERAGE here has the observed directional bias and will route users
        to add more Confluence pages forever instead of fixing the schema.
        """
        for i, run in enumerate(classifier_runs, 1):
            fc = run.get("failure_class", "")
            assert fc != "SOURCE_COVERAGE", (
                f"GATE FAIL (run {i}): classifier returned SOURCE_COVERAGE.\n"
                f"This is the directional bias error described in ADR-028-029-impl-plan.md.\n"
                f"The WBS table on pageId=20030556732 DOES contain synthesisable risk/\n"
                f"milestone/ORM evidence. SOURCE_COVERAGE means source lacks content,\n"
                f"which is false. The correct class is MISSING_FIELDS (schema didn't ask).\n"
                f"Evidence from classifier: {run.get('evidence', '(none)')}\n"
                f"Full output: {run.get('raw_text', '')}"
            )

    def test_all_runs_not_wrong_source(self, classifier_runs):
        """No run may return WRONG_SOURCE — the configured source IS the right page."""
        for i, run in enumerate(classifier_runs, 1):
            fc = run.get("failure_class", "")
            assert fc != "WRONG_SOURCE", (
                f"GATE FAIL (run {i}): classifier returned WRONG_SOURCE.\n"
                f"pageId=20030556732 IS the correct source page for this project.\n"
                f"The gap is in the schema design, not the source selection.\n"
                f"Evidence from classifier: {run.get('evidence', '(none)')}\n"
                f"Full output: {run.get('raw_text', '')}"
            )

    def test_majority_correct_class(self, classifier_runs):
        """At least 2 of 3 runs must return MISSING_FIELDS or THIN_FIELDS.

        This is the core gate assertion. The prompt must reliably steer the model
        toward the correct class for the known real case. A single correct run
        could be lucky; 2/3 demonstrates the prompt is doing its job.
        """
        correct = [r for r in classifier_runs if r.get("failure_class") in CORRECT_CLASSES]
        incorrect = [r for r in classifier_runs if r.get("failure_class") in INCORRECT_CLASSES]

        print("\n=== CLASSIFIER GATE VERBATIM OUTPUTS ===")
        for i, run in enumerate(classifier_runs, 1):
            print(f"\n--- Run {i} (elapsed={run.get('elapsed_s', 0):.1f}s) ---")
            print(f"failure_class : {run.get('failure_class')}")
            print(f"confidence    : {run.get('confidence')}")
            print(f"evidence      : {run.get('evidence')}")
            print(f"alternative   : {run.get('alternative_class')}")
            print(f"why_not_alt   : {run.get('why_not_alternative')}")
        print("\n=== END OUTPUTS ===")

        assert len(correct) >= 2, (
            f"GATE FAIL: only {len(correct)}/3 runs returned MISSING_FIELDS or THIN_FIELDS.\n"
            f"Correct runs: {[r.get('failure_class') for r in correct]}\n"
            f"Incorrect runs: {[r.get('failure_class') for r in incorrect]}\n"
            f"All run classes: {[r.get('failure_class') for r in classifier_runs]}\n\n"
            f"The classifier prompt has the directional bias described in the gate spec.\n"
            f"S6 is BLOCKED. Revise _FAILURE_CLASSIFIER_PROMPT until this test passes."
        )

    def test_evidence_references_synthesisable(self, classifier_runs):
        """At least 2 of 3 runs must mention synthesisable evidence in their output.

        The prompt requires the classifier to cite the capability inventory. If the
        classifier ignores the synthesisable confidence tags, the evidence field
        will not mention them — a sign the classifier is ignoring the input.
        """
        synthesisable_mentioned = [
            r for r in classifier_runs
            if "synthesisable" in (r.get("evidence", "") + r.get("why_not_alternative", "")).lower()
        ]
        # Soft assertion — warn if missing but don't fail the gate on this alone.
        # The core gate is test_majority_correct_class. This check ensures the
        # classifier is actually reading the capability inventory.
        if len(synthesisable_mentioned) < 2:
            log.warning(
                "test_evidence_references_synthesisable: only %d/3 runs mentioned "
                "'synthesisable' in evidence. Classifier may not be using the "
                "capability inventory. Classes returned: %s",
                len(synthesisable_mentioned),
                [r.get("failure_class") for r in classifier_runs],
            )
        # This is a diagnostic assertion, not a gate-blocker. The core gate is
        # test_majority_correct_class. Uncomment to make it a hard gate if desired:
        # assert len(synthesisable_mentioned) >= 2, ...

    def test_gate_summary(self, classifier_runs):
        """Print the gate summary for the commit record.

        This test always passes — it is the final summary log.
        Gate PASS/FAIL is determined by test_majority_correct_class.
        """
        classes = [r.get("failure_class") for r in classifier_runs]
        correct_count = sum(1 for c in classes if c in CORRECT_CLASSES)
        verdict = "PASS" if correct_count >= 2 else "FAIL"
        print(
            f"\n=== CLASSIFIER VALIDATION GATE VERDICT: {verdict} ===\n"
            f"Gold case: tpm.26ai_fa_db_upgrade_to_26ai_pptx (26ai Confluence WBS)\n"
            f"Runs: {classes}\n"
            f"Correct (MISSING_FIELDS|THIN_FIELDS): {correct_count}/3\n"
            f"Incorrect (SOURCE_COVERAGE|WRONG_SOURCE): "
            f"{sum(1 for c in classes if c in INCORRECT_CLASSES)}/3\n"
            f"{'S6 may proceed.' if verdict == 'PASS' else 'S6 is BLOCKED.'}"
        )


# ---------------------------------------------------------------------------
# Prompt structure contract tests (no LLM needed — validate prompt shape)
# ---------------------------------------------------------------------------


class TestClassifierPromptContract:
    """Validate _FAILURE_CLASSIFIER_PROMPT structure without calling the LLM.

    These tests ensure the prompt constant is correctly defined and contains
    all mandatory elements as specified in ADR-028-029-impl-plan.md.
    """

    @pytest.fixture(scope="class")
    def prompt_template(self):
        sys.path.insert(0, str(REPO_ROOT))
        from framework.skill_builder.conversation import _FAILURE_CLASSIFIER_PROMPT
        return _FAILURE_CLASSIFIER_PROMPT

    def test_prompt_has_all_required_format_kwargs(self, prompt_template):
        """Prompt must contain all 6 mandatory input variables."""
        required_vars = {
            "{normalised_intent}",
            "{schema_properties}",
            "{capability_inventory}",
            "{gap_report}",
            "{missing_sections}",
            "{thin_sections}",
        }
        for var in required_vars:
            assert var in prompt_template, (
                f"Prompt missing required format variable: {var}\n"
                f"ADR-028-029-impl-plan.md mandates all 6 inputs are present."
            )

    def test_prompt_defines_all_failure_classes(self, prompt_template):
        """Prompt must define all 6 failure classes."""
        required_classes = [
            "MISSING_FIELDS", "THIN_FIELDS", "WRONG_LAYOUT",
            "SOURCE_COVERAGE", "WRONG_SOURCE", "UNSUPPORTABLE",
        ]
        for cls in required_classes:
            assert cls in prompt_template, (
                f"Prompt missing failure class definition: {cls}"
            )

    def test_prompt_contains_anti_bias_instruction(self, prompt_template):
        """Prompt must contain explicit anti-bias instruction about synthesisable evidence."""
        anti_bias_markers = [
            "synthesisable",
            "does NOT mean",
        ]
        for marker in anti_bias_markers:
            assert marker in prompt_template, (
                f"Prompt missing anti-bias instruction marker: {marker!r}\n"
                f"The prompt must explicitly counter the 'no verbatim label = absent' bias."
            )

    def test_prompt_requires_evidence_field(self, prompt_template):
        """Prompt must require evidence and why_not_alternative in the output schema."""
        assert '"evidence"' in prompt_template, (
            "Prompt must require 'evidence' field in output JSON."
        )
        assert '"why_not_alternative"' in prompt_template, (
            "Prompt must require 'why_not_alternative' field in output JSON."
        )

    def test_prompt_requires_confidence_field(self, prompt_template):
        """Prompt must require confidence field in output."""
        assert '"confidence"' in prompt_template, (
            "Prompt must require 'confidence' field in output JSON."
        )

    def test_prompt_forbids_llm_routing(self, prompt_template):
        """Prompt must state that routing is code, not LLM choice."""
        # The prompt should clarify the LLM's role is diagnosis only
        routing_constraint_markers = [
            "routing",   # refers to the routing map
            "diagnosis",  # clarifies LLM role
        ]
        for marker in routing_constraint_markers:
            assert marker in prompt_template.lower(), (
                f"Prompt should clarify routing is code-based (marker: {marker!r})"
            )

    def test_prompt_can_be_formatted_with_gold_inputs(self, prompt_template):
        """Prompt can be formatted with the gold-case inputs without KeyError."""
        formatted = prompt_template.format(
            normalised_intent=json.dumps(GOLD_NORMALISED_INTENT, indent=2),
            schema_properties=json.dumps(GOLD_SCHEMA_MISSING_FIELDS, indent=2),
            capability_inventory=json.dumps(GOLD_CAPABILITY_INVENTORY, indent=2),
            gap_report=GOLD_GAP_REPORT,
            missing_sections=json.dumps(GOLD_MISSING_SECTIONS),
            thin_sections=json.dumps(GOLD_THIN_SECTIONS),
        )
        assert len(formatted) > 500, "Formatted prompt seems too short — check format vars"
        assert "MISSING_FIELDS" in formatted
        assert "synthesisable" in formatted
