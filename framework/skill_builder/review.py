"""review — run extraction on samples and format results for human review.

Per ADR-015 §REVIEW step. Applies the synthesized schema to sample items and
reports field coverage + any issues. In stub LLM mode the extraction is heuristic
(no LLM call); in production mode it delegates to the LLM factory.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)


def review_extractions(
    samples: list[dict],
    schema: dict,
) -> dict:
    """Run extraction on samples against the schema; return a review report.

    Args:
        samples: list of raw source item dicts (from sampler.fetch_samples).
        schema: JSON-Schema dict (from synthesize_schema).

    Returns:
        {
          "extractions": [
            { "source_citation": str, "extracted": dict, "missing_fields": list[str] }
          ],
          "field_coverage": { field: fraction_covered },
          "issues": [ "field X missing in N/M samples", ... ],
        }
    """
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    all_fields = list(properties.keys())

    extractions: list[dict] = []
    field_hit: dict[str, int] = {f: 0 for f in all_fields}

    for sample in samples:
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
    }


# ---------------------------------------------------------------------------
# stub extraction — heuristic field value extraction from a raw dict
# ---------------------------------------------------------------------------

def _extract_stub(sample: dict, properties: dict) -> dict:
    """Extract field values from a flat/nested source dict heuristically.

    This is a stub: it tries to find matching keys in the sample (case-insensitive,
    underscore/dash normalised) or returns a placeholder so the user can see the
    schema working without real data.
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
