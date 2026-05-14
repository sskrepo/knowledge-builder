"""review — run extraction on samples and format results for human review.

Per ADR-015 §REVIEW step and ADR-026 §Fix 3.

ADR-026: _extract_stub is replaced by a real LLM extraction call when an LLM
client is wired.  When llm is None the function raises RuntimeError — no silent
stub fallback (no-stub-mode policy).

_extract_stub is retained for tests that pass stub_mode=True explicitly.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def review_extractions(
    samples: list[dict],
    schema: dict,
    llm=None,
    stub_mode: bool = False,
) -> dict:
    """Run extraction on samples against the schema; return a review report.

    Args:
        samples: list of raw source item dicts (from sampler.fetch_samples).
        schema: JSON-Schema dict (from synthesize_schema).
        llm: LLM client (framework LLMClient or compatible). REQUIRED unless
             stub_mode=True. Per ADR-026 no-stub-mode policy, passing llm=None
             without stub_mode=True raises RuntimeError.
        stub_mode: if True, use heuristic extraction (test/dev use only).

    Returns:
        {
          "extractions": [
            { "source_citation": str, "extracted": dict, "missing_fields": list[str] }
          ],
          "field_coverage": { field: fraction_covered },
          "issues": [ "field X missing in N/M samples", ... ],
          "extraction_mode": "llm" | "stub",
        }
    """
    if llm is None and not stub_mode:
        raise RuntimeError(
            "review_extractions: llm is required. Per ADR-026 no-stub-mode policy, "
            "heuristic extraction is not permitted in production. "
            "Pass an LLMClient or set stub_mode=True for tests."
        )

    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    all_fields = list(properties.keys())

    extractions: list[dict] = []
    field_hit: dict[str, int] = {f: 0 for f in all_fields}

    extraction_mode = "stub" if (llm is None or stub_mode) else "llm"

    for sample in samples:
        if extraction_mode == "llm":
            extracted = _llm_extract(sample, schema, llm)
        else:
            extracted = _extract_stub(sample, properties)

        missing = [f for f in required_fields if not extracted.get(f)]
        for f in all_fields:
            if extracted.get(f):
                field_hit[f] += 1
        extractions.append(
            {
                "source_citation": sample.get("source_citation", "unknown"),
                "extracted": extracted,
                "missing_fields": missing,
            }
        )

    total = max(len(samples), 1)
    field_coverage = {f: round(hit / total, 2) for f, hit in field_hit.items()}

    issues: list[str] = []
    for f in required_fields:
        cov = field_coverage.get(f, 0.0)
        if cov < 0.5:
            issues.append(
                f"required field '{f}' covered in only {int(cov * total)}/{total} samples. "
                "Consider making it optional or enriching your sources."
            )
    for sample_result in extractions:
        missing = sample_result["missing_fields"]
        if missing:
            cit = sample_result["source_citation"]
            issues.append(f"sample {cit!r} missing required fields: {missing}")

    return {
        "extractions": extractions,
        "field_coverage": field_coverage,
        "issues": issues,
        "extraction_mode": extraction_mode,
    }


# ---------------------------------------------------------------------------
# LLM extraction (ADR-026 Fix 3)
# ---------------------------------------------------------------------------

_REVIEW_EXTRACT_PROMPT = """\
You are extracting structured fields from a source document to preview what an
LLM parser would produce when this schema is applied at ingest time.

Return a single JSON object with EXACTLY these field keys
(use empty string "" or empty list [] when a field is genuinely absent —
do NOT invent data that is not in the source document):

{field_lines}

=== Source document ===
{text}
=== End source ===

Respond with ONLY the JSON object, no prose, no markdown fences.
"""


def _llm_extract(sample: dict, schema: dict, llm) -> dict[str, Any]:
    """Extract field values from a sample using the LLM.

    This is the authoritative extraction preview shown at REVIEW_SCHEMA.
    It uses the same prompt structure as WorkflowExecutor._llm_extract_fields
    so the authorSkill preview is consistent with what the skill does at
    query time.
    """
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    field_lines = []
    for name, prop in properties.items():
        type_hint = prop.get("type", "string")
        enum = prop.get("enum")
        desc = prop.get("description", "")
        extra = f" (one of: {enum})" if enum else ""
        req_tag = " [required]" if name in required else ""
        field_lines.append(f'  - "{name}" ({type_hint}{extra}){req_tag}: {desc}')

    text = sample.get("content") or sample.get("text") or sample.get("body") or ""
    if isinstance(text, dict):
        text = json.dumps(text, indent=2)
    text = str(text)[:12000]  # cap for prompt budget

    if not text.strip():
        log.warning("_llm_extract: sample has no extractable text, returning empty")
        return {}

    prompt = _REVIEW_EXTRACT_PROMPT.format(
        field_lines="\n".join(field_lines),
        text=text,
    )

    try:
        result = llm.chat(
            model="synthesis",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2048,
        )
        raw = result.get("text", "") if isinstance(result, dict) else str(result)
        cleaned = re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=re.S).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.S)
            if m:
                return json.loads(m.group())
            log.warning("_llm_extract: could not parse LLM JSON output: %s", raw[:200])
            return {}
    except Exception as exc:
        log.error("_llm_extract: LLM call failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# stub extraction — heuristic field value extraction from a raw dict
# (test / stub_mode=True only — ADR-026)
# ---------------------------------------------------------------------------

def _extract_stub(sample: dict, properties: dict) -> dict:
    """Extract field values from a flat/nested source dict heuristically.

    TEST/STUB USE ONLY (per ADR-026).  In production review_extractions is
    always called with a real LLM client.
    """
    extracted: dict[str, Any] = {}
    flat = _flatten(sample)

    for field, spec in properties.items():
        normalised_field = field.lower().replace("-", "_")
        matched_value: Any = None

        for key, val in flat.items():
            if key.lower().replace("-", "_") == normalised_field:
                matched_value = val
                break

        if matched_value is None and spec.get("type") == "string":
            text = str(sample.get("content", sample.get("text", sample.get("body", ""))))
            if len(text) > 20:
                matched_value = _heuristic_string_extract(field, text, spec)

        if matched_value is not None:
            extracted[field] = matched_value

    return extracted


def _flatten(obj: Any, prefix: str = "", sep: str = ".") -> dict:
    result: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{prefix}{sep}{k}" if prefix else k
            if isinstance(v, (dict, list)):
                result.update(_flatten(v, new_key, sep))
            else:
                result[new_key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            result.update(_flatten(v, f"{prefix}[{i}]", sep))
    else:
        result[prefix] = obj
    return result


def _heuristic_string_extract(field: str, text: str, spec: dict) -> str | None:
    if spec.get("maxLength", 10000) < 100:
        words = text.split()
        return " ".join(words[:5]) if words else None
    return text[:spec.get("maxLength", 500)]
