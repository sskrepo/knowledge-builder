"""KbfOpsReviewEngine — LLM-powered quality review of authorSkill sessions.

Takes a SessionBundle and returns a QualityReport that scores 7 dimensions
of skill authoring quality and lists bugs to file.

depth="structural": deterministic pre-checks only (no LLM, llm=None is fine).
depth="semantic" | "full": structural checks + LLM critique.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

# Repo root: review_engine.py lives at framework/deploy/ops/review_engine.py
# → parents[0]=ops, parents[1]=deploy, parents[2]=framework, parents[3]=repo-root
_REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DimensionResult:
    score: float
    findings: list[str] = field(default_factory=list)


@dataclass
class BugToFile:
    check_name: str
    severity: str  # critical | major | minor
    detail: str
    suggested_fix: str


@dataclass
class QualityReport:
    """Full output of a KbfOps review run."""

    synth_id: str
    review_id: str
    persona: str
    skill_names: list[str]
    status: str
    overall_score: float
    recommendation: str  # promote | promote_with_fixes | do_not_promote
    dimensions: dict[str, DimensionResult]
    bugs_to_file: list[BugToFile]


# ---------------------------------------------------------------------------
# Review prompt template
# ---------------------------------------------------------------------------

_REVIEW_PROMPT = """\
You are a senior Knowledge Builder Framework engineer performing a quality review of a skill
authoring session. You have access to everything the server produced during this session.
Your job is to identify all quality gaps — bugs the server made, not the user.

SESSION CONTEXT
==============
Persona: {persona}
Skill name(s): {skill_names}
Session status: {status}
Intent stated by user: {intent_description}

CONVERSATION HISTORY (abridged to key turns)
============================================
{conversation_summary}

UPLOADED EXAMPLE ARTIFACT
=========================
Filename: {uploaded_filename}
Content:
{uploaded_content}

COMMITTED ARTIFACTS
===================
Workflow skill YAML:
{workflow_yaml}

Persona builder delta:
{persona_builder_delta}

Extraction gold set:
{eval_extraction}

Workflow gold set:
{eval_workflow}

ERRORS DURING SESSION
=====================
{errors_summary}

REVIEW DIMENSIONS
=================
Evaluate each of the following dimensions. For each: give a score 0-10, list specific findings,
and for each finding: state the severity (critical/major/minor), what specifically is wrong,
and the suggested fix. Be concrete — reference field names, line content, specific values.

1. INTENT FIDELITY
   Did the server correctly understand what the user wanted to automate?
   Do the committed artifacts faithfully reflect the user's stated intent and uploaded example?

2. SCHEMA COMPLETENESS
   Count the fields the user described (in conversation + uploaded artifact).
   Count the fields in the committed schema.
   Are all described fields present? Are any extra fields present that were not described?
   Note specific missing/extra field names.

3. KB WIRING
   For each `requires_extractions` entry:
   - Does the referenced KB name exist in the persona builder delta?
   - Do the `required_fields` match the KB's `provides_fields`?
   - Is the KB semantically appropriate (does it make sense that this KB would provide these fields)?
   Flag any hallucinated KB references (KBs that don't exist or don't belong to this domain).

4. ROUTING DESCRIPTORS
   Examine `skill_card.use_when` and `example_invocations`.
   Are there >= 3 distinct, natural-language phrasings?
   Would a user saying something like "{{natural query}}" hit this skill above 0.85 cosine similarity?
   Simulate 3 realistic user queries and predict whether they would route to this skill at Tier 1.

5. EVAL QUALITY
   Are `expected_extraction` values non-null and realistic?
   Are `expected_output_includes` fields scoped to this skill's own requires_extractions (not another skill's)?
   Is the gold set populated from the uploaded artifact content where applicable?

6. ARTIFACT CONSISTENCY
   Do all 4 artifacts tell the same story?
   - Field names consistent across workflow YAML, schema, and persona builder?
   - Skill name consistent across all artifacts?
   - KB names in workflow YAML match those in persona builder delta?

7. ASK-KB ROUTING SIMULATION
   If a user asks `askKnowledgeBase` with a natural query matching this skill's domain,
   which tier would they land in? Predict the routing path and explain why.
   Would they get the workflow skill output (Tier 1) or fall through to plain KB retrieval (Tier 2+)?

OUTPUT FORMAT
=============
Respond with valid JSON matching this exact schema. No prose outside the JSON.

{output_schema}
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["synthId", "reviewId", "persona", "skillNames", "overallScore",
                 "recommendation", "dimensions", "bugsToFile"],
    "properties": {
        "synthId":  {"type": "string"},
        "reviewId": {"type": "string"},
        "persona":  {"type": "string"},
        "skillNames": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string"},
        "overallScore": {"type": "number", "minimum": 0, "maximum": 10},
        "recommendation": {"type": "string",
                           "enum": ["do_not_promote", "promote_with_fixes", "promote"]},
        "dimensions": {
            "type": "object",
            "properties": {
                k: {"type": "object",
                    "required": ["score", "findings"],
                    "properties": {
                        "score": {"type": "number"},
                        "findings": {"type": "array", "items": {"type": "string"}},
                    }}
                for k in [
                    "intentFidelity", "schemaCompleteness", "kbWiring",
                    "routingDescriptors", "evalQuality", "artifactConsistency",
                    "askKbRoutingSimulation",
                ]
            },
        },
        "bugsToFile": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["checkName", "severity", "detail", "suggestedFix"],
                "properties": {
                    "checkName":    {"type": "string"},
                    "severity":     {"type": "string",
                                     "enum": ["critical", "major", "minor"]},
                    "detail":       {"type": "string"},
                    "suggestedFix": {"type": "string"},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Structural pre-check helpers
# ---------------------------------------------------------------------------


def _count_artifacts(skill_artifacts: dict[str, str]) -> tuple[bool, list[str]]:
    """check_artifact_count: all 5 artifact types must be present per skill."""
    from ..skill_store._base import ARTIFACT_TYPES
    found = set(skill_artifacts.keys())
    expected = ARTIFACT_TYPES
    missing = expected - found
    extra = found - expected
    findings: list[str] = []
    ok = True
    if missing:
        ok = False
        findings.append(
            f"Missing artifact types: {sorted(missing)}. "
            "Each skill must have all 5 artifact types: "
            "workflow_skill, persona_builder_delta, eval_extraction, eval_workflow, extraction_schema."
        )
    if extra:
        ok = False
        findings.append(f"Unexpected artifact types: {sorted(extra)}")
    return ok, findings


def _check_gold_set_not_null(eval_extraction: str) -> tuple[bool, list[str]]:
    """check_gold_set_not_null: no all-null expected_extraction entries."""
    if not eval_extraction:
        return True, []
    findings: list[str] = []
    ok = True
    for lineno, line in enumerate(eval_extraction.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        expected = entry.get("expected_extraction") or entry.get("expected_output_includes")
        if expected is None:
            continue
        if isinstance(expected, dict) and all(v is None for v in expected.values()):
            ok = False
            findings.append(
                f"Line {lineno}: all values in expected_extraction are null. "
                "Gold set entries must have at least one non-null expected field value."
            )
        elif isinstance(expected, list) and all(v is None for v in expected):
            ok = False
            findings.append(
                f"Line {lineno}: all values in expected_extraction list are null."
            )
    return ok, findings


def _check_skill_name_not_truncated(
    session_skill_name: str, skill_artifacts: dict[str, str]
) -> tuple[bool, list[str]]:
    """check_skill_name_not_truncated: artifact skill_name matches session skill_name."""
    findings: list[str] = []
    ok = True
    workflow_yaml = skill_artifacts.get("workflow_skill", "")
    if not workflow_yaml or not session_skill_name:
        return ok, findings

    try:
        import yaml
        doc = yaml.safe_load(workflow_yaml)
        if isinstance(doc, dict):
            art_skill_name = (
                doc.get("workflow_skill")
                or doc.get("skill_name")
                or doc.get("name")
                or ""
            )
            if art_skill_name and art_skill_name != session_skill_name:
                ok = False
                findings.append(
                    f"Skill name mismatch: session has '{session_skill_name}' "
                    f"but workflow_skill artifact has '{art_skill_name}'. "
                    "The skill_name may have been truncated or renamed during synthesis."
                )
    except Exception:
        pass  # YAML parse error is a separate concern

    return ok, findings


def _check_kb_references_resolve(
    skill_artifacts: dict[str, str],
    persona: str = "",
    repo_root: Path | None = None,
) -> tuple[bool, list[str]]:
    """check_kb_references_resolve: each requires_extractions.kb exists in persona builder delta
    or in the on-disk persona builder YAML (for reused/pre-existing KBs).

    Args:
        skill_artifacts: artifact dict for one skill.
        persona: the session persona (e.g. "tpm"). Used to load the on-disk persona
            builder YAML so that reused KBs (which are NOT in the delta) can be resolved.
            If empty, falls back to reading the persona from the workflow_skill artifact's
            ``persona:`` field. Falls back further to inferring from kb_ref prefixes only
            as a last resort (logged as a warning).
        repo_root: override for the repo root path (used in tests). Defaults to _REPO_ROOT.
    """
    findings: list[str] = []
    ok = True

    workflow_yaml = skill_artifacts.get("workflow_skill", "")
    persona_builder_delta = skill_artifacts.get("persona_builder_delta", "")
    if not workflow_yaml or not persona_builder_delta:
        return ok, findings

    try:
        import yaml
        wf_doc = yaml.safe_load(workflow_yaml)
        pb_doc = yaml.safe_load(persona_builder_delta)
    except Exception:
        return ok, findings

    if not isinstance(wf_doc, dict) or not isinstance(pb_doc, dict):
        return ok, findings

    requires = wf_doc.get("requires_extractions") or []
    if not isinstance(requires, list):
        return ok, findings

    # ------------------------------------------------------------------
    # Resolve `persona` authoritatively, in priority order:
    #   1. `persona` parameter (set by caller from bundle.persona)
    #   2. workflow_skill YAML's `persona:` field
    #   3. Fallback: infer from the first kb_ref prefix (logged as warning)
    # ------------------------------------------------------------------
    resolved_persona = persona.strip()
    if not resolved_persona:
        resolved_persona = (wf_doc.get("persona") or "").strip()
    if not resolved_persona:
        # Last-resort: try to infer from the first kb_ref that contains a dot
        for entry in requires:
            if isinstance(entry, dict):
                kb_ref = entry.get("kb", "")
                if kb_ref and "." in kb_ref:
                    resolved_persona = kb_ref.split(".")[0]
                    log.warning(
                        "_check_kb_references_resolve: persona not available from bundle or "
                        "workflow_skill artifact; inferred persona=%r from kb_ref=%r — "
                        "this is a fallback only; wire persona through the call chain for accuracy",
                        resolved_persona,
                        kb_ref,
                    )
                    break

    # ------------------------------------------------------------------
    # A1 — Collect known KB names from the persona_builder_delta artifact.
    #
    # Production artifact shape (from synthesize_persona_builder_diff):
    #   {"name": "short_kb_name", "kind": ..., "extraction_schema": ...,
    #    "provides_fields": [...], "sources": [...], "retrieval_tools": [...],
    #    "kb_card": {...}}
    # → single-KB delta: pb_doc["name"] is the bare KB name.
    #
    # Legacy / test-fixture shape (qualified-name-as-top-level-key):
    #   {"tpm.weekly_ops": {"provides_fields": [...]}}
    # → top-level keys ARE the KB names.
    #
    # Structured shape (knowledge_bases / kbs list):
    #   {"knowledge_bases": [{"name": "foo", ...}]}
    # ------------------------------------------------------------------
    pb_kbs: set[str] = set()

    if "name" in pb_doc and "knowledge_bases" not in pb_doc and "kbs" not in pb_doc:
        # A1 — production artifact-dict shape: single-KB delta.
        bare_name = pb_doc["name"]
        pb_kbs.add(bare_name)
        if resolved_persona:
            pb_kbs.add(f"{resolved_persona}.{bare_name}")
        log.debug(
            "_check_kb_references_resolve: artifact-dict shape detected; "
            "delta KB name=%r, resolved_persona=%r",
            bare_name,
            resolved_persona,
        )
    else:
        # Legacy / structured shape.
        kbs_section = pb_doc.get("kbs") or pb_doc.get("knowledge_bases") or {}
        if isinstance(kbs_section, dict):
            for k in kbs_section:
                pb_kbs.add(k)
                if resolved_persona and not k.startswith(f"{resolved_persona}."):
                    pb_kbs.add(f"{resolved_persona}.{k}")
        elif isinstance(kbs_section, list):
            for item in kbs_section:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("kb_name")
                    if name:
                        pb_kbs.add(name)
                        if resolved_persona:
                            pb_kbs.add(f"{resolved_persona}.{name}")
        else:
            # Top-level keys as KB names (test-fixture / legacy shape).
            for k in pb_doc:
                pb_kbs.add(k)
                if resolved_persona and not k.startswith(f"{resolved_persona}."):
                    pb_kbs.add(f"{resolved_persona}.{k}")

    # ------------------------------------------------------------------
    # A2 — Resolve reused KBs from the on-disk persona builder YAML.
    #
    # Reused KBs (e.g. tpm.tpm_dependencies, tpm.tpm_weekly_ops) are NOT
    # included in the delta — they already exist in the persona builder.
    # Load the on-disk YAML and add every KB it declares.
    # ------------------------------------------------------------------
    if resolved_persona:
        _root = repo_root or _REPO_ROOT
        pb_yaml_path = _root / "framework" / "persona_builders" / f"{resolved_persona}.yaml"
        try:
            import yaml as _yaml
            with open(pb_yaml_path, encoding="utf-8") as fh:
                pb_on_disk = _yaml.safe_load(fh)
            if isinstance(pb_on_disk, dict):
                for kb_entry in (pb_on_disk.get("knowledge_bases") or []):
                    if isinstance(kb_entry, dict):
                        name = kb_entry.get("name") or kb_entry.get("kb_name")
                        if name:
                            pb_kbs.add(name)
                            pb_kbs.add(f"{resolved_persona}.{name}")
            log.debug(
                "_check_kb_references_resolve: loaded on-disk persona builder %s; "
                "pb_kbs now has %d entries",
                pb_yaml_path,
                len(pb_kbs),
            )
        except FileNotFoundError:
            # Not fatal — the delta alone must resolve refs; log clearly.
            log.warning(
                "_check_kb_references_resolve: on-disk persona builder not found at %s; "
                "reused KB resolution skipped — only delta KBs are known",
                pb_yaml_path,
            )
        except Exception as exc:
            log.warning(
                "_check_kb_references_resolve: failed to load on-disk persona builder %s: %s; "
                "reused KB resolution skipped",
                pb_yaml_path,
                exc,
            )
    else:
        log.warning(
            "_check_kb_references_resolve: persona unknown; "
            "on-disk persona builder not loaded — reused KBs may produce false positives"
        )

    # ------------------------------------------------------------------
    # A3 — Check each kb_ref. Accept if the exact ref OR its short form
    # (persona-stripped) appears in pb_kbs.
    # ------------------------------------------------------------------
    for entry in requires:
        if not isinstance(entry, dict):
            continue
        kb_ref = entry.get("kb", "")
        if not kb_ref:
            continue

        # Derive short form: strip leading "persona." prefix if present.
        short_ref = kb_ref
        if resolved_persona and kb_ref.startswith(f"{resolved_persona}."):
            short_ref = kb_ref[len(resolved_persona) + 1:]

        if kb_ref not in pb_kbs and short_ref not in pb_kbs:
            ok = False
            findings.append(
                f"KB reference '{kb_ref}' in requires_extractions does not appear in "
                f"persona_builder_delta or in the on-disk persona builder for "
                f"persona '{resolved_persona or '(unknown)'}'. "
                f"Known KBs: {sorted(pb_kbs) or '(none found)'}. "
                "This may be a hallucinated reference."
            )

    return ok, findings


def _check_gold_fields_scoped(
    skill_artifacts: dict[str, str],
) -> tuple[bool, list[str]]:
    """check_gold_fields_scoped: expected_output_includes fields must be subset of this skill's requires_extractions fields."""
    findings: list[str] = []
    ok = True

    workflow_yaml = skill_artifacts.get("workflow_skill", "")
    eval_workflow = skill_artifacts.get("eval_workflow", "")
    if not workflow_yaml or not eval_workflow:
        return ok, findings

    try:
        import yaml
        wf_doc = yaml.safe_load(workflow_yaml)
    except Exception:
        return ok, findings

    if not isinstance(wf_doc, dict):
        return ok, findings

    # Collect all fields declared in requires_extractions for this skill.
    declared_fields: set[str] = set()
    for entry in (wf_doc.get("requires_extractions") or []):
        if not isinstance(entry, dict):
            continue
        for f in (entry.get("required_fields") or []):
            declared_fields.add(f)
        for f in (entry.get("provides_fields") or []):
            declared_fields.add(f)

    if not declared_fields:
        return ok, findings

    # Check each gold set line.
    for lineno, line in enumerate(eval_workflow.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        expected = entry.get("expected_output_includes") or {}
        if not isinstance(expected, dict):
            continue
        for field_name in expected:
            if field_name not in declared_fields:
                ok = False
                findings.append(
                    f"Line {lineno}: field '{field_name}' in expected_output_includes "
                    f"is not in this skill's requires_extractions fields "
                    f"({sorted(declared_fields)}). This may reference another skill's fields."
                )

    return ok, findings


# ---------------------------------------------------------------------------
# Review engine
# ---------------------------------------------------------------------------


class KbfOpsReviewEngine:
    """LLM-powered quality review engine.

    Args:
        llm: LLMClient instance, or None.  When None, only depth="structural"
             is supported.
    """

    def __init__(self, llm) -> None:
        self._llm = llm

    def review(self, bundle: "SessionBundle", depth: str = "full") -> QualityReport:
        """Run a quality review of the session bundle.

        Args:
            bundle: Loaded session data.
            depth:  "structural" | "semantic" | "full".
                    "structural" runs only deterministic checks (no LLM).
                    "semantic" / "full" adds LLM critique after structural checks.

        Returns:
            A QualityReport dataclass.
        """
        review_id = f"rev-{uuid4().hex[:12]}"

        # Always run structural checks first.
        structural_bugs, structural_findings = self._run_structural_checks(bundle)

        if depth == "structural":
            return self._build_report_structural_only(
                bundle, review_id, structural_bugs, structural_findings
            )

        # Semantic / full: run LLM critique.
        if self._llm is None:
            log.warning(
                "KbfOpsReviewEngine: LLM not configured, depth=%s requested — "
                "falling back to structural-only review",
                depth,
            )
            return self._build_report_structural_only(
                bundle, review_id, structural_bugs, structural_findings
            )

        return self._run_llm_review(bundle, review_id, structural_bugs)

    # ------------------------------------------------------------------
    # Structural checks
    # ------------------------------------------------------------------

    def _run_structural_checks(
        self, bundle: "SessionBundle"
    ) -> tuple[list[BugToFile], dict[str, DimensionResult]]:
        """Run all deterministic pre-checks. Returns (bugs, dimension_map)."""
        bugs: list[BugToFile] = []
        per_dim_findings: dict[str, list[str]] = {
            "artifactConsistency": [],
            "schemaCompleteness":  [],
            "evalQuality":         [],
            "kbWiring":            [],
        }

        for skill_name, skill_artifacts in bundle.artifacts.items():
            # 1. check_artifact_count
            ok, findings = _count_artifacts(skill_artifacts)
            if not ok:
                for f in findings:
                    bugs.append(BugToFile(
                        check_name="check_artifact_count",
                        severity="critical",
                        detail=f"[{skill_name}] {f}",
                        suggested_fix=(
                            "Ensure all 5 artifact types are synthesized and persisted "
                            "before marking the session as committed."
                        ),
                    ))
                    per_dim_findings["artifactConsistency"].append(f)

            # 2. check_gold_set_not_null
            eval_extraction = skill_artifacts.get("eval_extraction", "")
            ok, findings = _check_gold_set_not_null(eval_extraction)
            if not ok:
                for f in findings:
                    bugs.append(BugToFile(
                        check_name="check_gold_set_not_null",
                        severity="major",
                        detail=f"[{skill_name}] {f}",
                        suggested_fix=(
                            "Populate expected_extraction with realistic values from the "
                            "uploaded example artifact before committing."
                        ),
                    ))
                    per_dim_findings["evalQuality"].append(f)

            # 3. check_skill_name_not_truncated
            ok, findings = _check_skill_name_not_truncated(skill_name, skill_artifacts)
            if not ok:
                for f in findings:
                    bugs.append(BugToFile(
                        check_name="check_skill_name_not_truncated",
                        severity="major",
                        detail=f"[{skill_name}] {f}",
                        suggested_fix=(
                            "Ensure the skill_name in the workflow YAML matches the "
                            "session skill_name exactly."
                        ),
                    ))
                    per_dim_findings["artifactConsistency"].append(f)

            # 4. check_kb_references_resolve
            # A4: source persona from bundle (authoritative), not guessed from kb_ref.
            ok, findings = _check_kb_references_resolve(
                skill_artifacts,
                persona=bundle.persona,
            )
            if not ok:
                for f in findings:
                    bugs.append(BugToFile(
                        check_name="check_kb_references_resolve",
                        severity="major",
                        detail=f"[{skill_name}] {f}",
                        suggested_fix=(
                            "Verify that KB names referenced in requires_extractions "
                            "match the kb_name entries in the persona builder delta."
                        ),
                    ))
                    per_dim_findings["kbWiring"].append(f)

            # 5. check_gold_fields_scoped
            ok, findings = _check_gold_fields_scoped(skill_artifacts)
            if not ok:
                for f in findings:
                    bugs.append(BugToFile(
                        check_name="check_gold_fields_scoped",
                        severity="minor",
                        detail=f"[{skill_name}] {f}",
                        suggested_fix=(
                            "Ensure expected_output_includes only references fields "
                            "declared in this skill's requires_extractions entries."
                        ),
                    ))
                    per_dim_findings["evalQuality"].append(f)

        # Build dimension results for structural checks.
        dim_results: dict[str, DimensionResult] = {}
        for dim_name, findings in per_dim_findings.items():
            score = max(0.0, 10.0 - len(findings) * 2.0)
            dim_results[dim_name] = DimensionResult(score=score, findings=findings)

        return bugs, dim_results

    def _build_report_structural_only(
        self,
        bundle: "SessionBundle",
        review_id: str,
        bugs: list[BugToFile],
        structural_findings: dict[str, DimensionResult],
    ) -> QualityReport:
        """Build a QualityReport from structural checks only."""
        # Pad missing dimensions with neutral scores.
        all_dims = [
            "intentFidelity", "schemaCompleteness", "kbWiring",
            "routingDescriptors", "evalQuality", "artifactConsistency",
            "askKbRoutingSimulation",
        ]
        for dim in all_dims:
            if dim not in structural_findings:
                structural_findings[dim] = DimensionResult(
                    score=10.0,
                    findings=["(not evaluated — structural mode only)"],
                )

        scores = [d.score for d in structural_findings.values()]
        overall = sum(scores) / len(scores) if scores else 0.0
        recommendation = _score_to_recommendation(overall)

        return QualityReport(
            synth_id=bundle.synth_id,
            review_id=review_id,
            persona=bundle.persona,
            skill_names=bundle.skill_names,
            status=bundle.status,
            overall_score=round(overall, 1),
            recommendation=recommendation,
            dimensions=structural_findings,
            bugs_to_file=bugs,
        )

    # ------------------------------------------------------------------
    # LLM review
    # ------------------------------------------------------------------

    def _run_llm_review(
        self,
        bundle: "SessionBundle",
        review_id: str,
        structural_bugs: list[BugToFile],
    ) -> QualityReport:
        """Run the LLM critique and merge with structural bugs."""
        prompt = self._build_prompt(bundle, review_id)

        try:
            result = self._llm.chat(
                model="eval_judge",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=4096,
            )
            raw_response = result["text"] if isinstance(result, dict) else str(result)
        except Exception as exc:
            log.warning("KbfOpsReviewEngine: LLM call failed: %s", exc)
            # Return structural-only report with an error note.
            structural_bugs_copy = list(structural_bugs) + [
                BugToFile(
                    check_name="llm_review_failed",
                    severity="minor",
                    detail=f"LLM review failed: {exc}",
                    suggested_fix="Retry with depth='structural' or check LLM configuration.",
                )
            ]
            _, structural_findings = self._run_structural_checks(bundle)
            return self._build_report_structural_only(
                bundle, review_id, structural_bugs_copy, structural_findings
            )

        try:
            report = self._parse_llm_response(raw_response, bundle, review_id)
        except Exception as exc:
            log.warning("KbfOpsReviewEngine: failed to parse LLM response: %s", exc)
            structural_bugs_copy = list(structural_bugs) + [
                BugToFile(
                    check_name="llm_response_parse_failed",
                    severity="minor",
                    detail=f"LLM returned malformed JSON: {exc}",
                    suggested_fix="Review LLM prompt and output schema alignment.",
                )
            ]
            _, structural_findings = self._run_structural_checks(bundle)
            return self._build_report_structural_only(
                bundle, review_id, structural_bugs_copy, structural_findings
            )

        # Merge structural bugs into the LLM report's bugs_to_file.
        report.bugs_to_file = structural_bugs + report.bugs_to_file
        return report

    def _build_prompt(self, bundle: "SessionBundle", review_id: str) -> str:
        """Build the review prompt string."""
        # Conversation summary: last 10 turns max.
        turns = bundle.conversation_history[-10:]
        conv_lines: list[str] = []
        for turn in turns:
            role = turn.get("role", "unknown")
            content = turn.get("content", "")
            if isinstance(content, list):
                # MCP message format
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            conv_lines.append(f"{role}: {content[:400]}")
        conversation_summary = "\n".join(conv_lines) or "(no conversation history)"

        # Uploaded file (first one if multiple).
        uploaded_filename = "(none)"
        uploaded_content = "(none)"
        if bundle.uploaded_files:
            uf = bundle.uploaded_files[0]
            uploaded_filename = uf.get("filename", "(unknown)")
            uploaded_content = uf.get("content", "")[:2000] or "(empty)"

        # Skill artifacts for the first skill.
        skill_name = bundle.skill_names[0] if bundle.skill_names else ""
        skill_arts = bundle.artifacts.get(skill_name, {})
        workflow_yaml = skill_arts.get("workflow_skill", "(not found)")[:3000]
        persona_builder_delta = skill_arts.get("persona_builder_delta", "(not found)")[:3000]
        eval_extraction = skill_arts.get("eval_extraction", "(not found)")[:2000]
        eval_workflow = skill_arts.get("eval_workflow", "(not found)")[:2000]

        # Errors summary.
        if bundle.errors:
            errors_lines = [
                f"  [{e.get('tool', '')}] {e.get('error_type', '')} — {e.get('message', '')[:200]}"
                for e in bundle.errors[:5]
            ]
            errors_summary = "\n".join(errors_lines)
        else:
            errors_summary = "(none)"

        return _REVIEW_PROMPT.format(
            persona=bundle.persona,
            skill_names=", ".join(bundle.skill_names) or "(unknown)",
            status=bundle.status,
            intent_description=bundle.intent_description or "(not specified)",
            conversation_summary=conversation_summary,
            uploaded_filename=uploaded_filename,
            uploaded_content=uploaded_content,
            workflow_yaml=workflow_yaml,
            persona_builder_delta=persona_builder_delta,
            eval_extraction=eval_extraction,
            eval_workflow=eval_workflow,
            errors_summary=errors_summary,
            natural_query=f"tell me about {bundle.intent_description[:80] if bundle.intent_description else 'this skill'}",
            output_schema=json.dumps(_OUTPUT_SCHEMA, indent=2),
        )

    def _parse_llm_response(
        self, raw: str, bundle: "SessionBundle", review_id: str
    ) -> QualityReport:
        """Parse the LLM's JSON response into a QualityReport.

        Raises ValueError on malformed JSON or missing required fields.
        """
        # Strip any markdown code fences.
        raw_clean = re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=re.S).strip()
        data = json.loads(raw_clean)

        if not isinstance(data, dict):
            raise ValueError("LLM response is not a JSON object")

        dims_raw = data.get("dimensions") or {}
        dimensions: dict[str, DimensionResult] = {}
        for dim_key, dim_val in dims_raw.items():
            if isinstance(dim_val, dict):
                dimensions[dim_key] = DimensionResult(
                    score=float(dim_val.get("score", 0)),
                    findings=dim_val.get("findings") or [],
                )

        # Compute overall_score as mean of dimension scores.
        scores = [d.score for d in dimensions.values()]
        overall = sum(scores) / len(scores) if scores else 0.0
        recommendation = _score_to_recommendation(overall)

        bugs: list[BugToFile] = []
        for b in (data.get("bugsToFile") or []):
            if not isinstance(b, dict):
                continue
            bugs.append(BugToFile(
                check_name=b.get("checkName", ""),
                severity=b.get("severity", "minor"),
                detail=b.get("detail", ""),
                suggested_fix=b.get("suggestedFix", ""),
            ))

        return QualityReport(
            synth_id=bundle.synth_id,
            review_id=review_id,
            persona=bundle.persona,
            skill_names=bundle.skill_names,
            status=bundle.status,
            overall_score=round(overall, 1),
            recommendation=recommendation,
            dimensions=dimensions,
            bugs_to_file=bugs,
        )


# ---------------------------------------------------------------------------
# Score → recommendation mapping
# ---------------------------------------------------------------------------


def _score_to_recommendation(score: float) -> str:
    if score >= 8.0:
        return "promote"
    if score >= 5.0:
        return "promote_with_fixes"
    return "do_not_promote"
