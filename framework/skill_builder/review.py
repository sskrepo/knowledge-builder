"""review — run extraction on samples and format results for human review.

Per ADR-015 §REVIEW step and ADR-026 §Fix 3.

ADR-026: _extract_stub is replaced by a real LLM extraction call when an LLM
client is wired.  When llm is None the function raises RuntimeError — no silent
stub fallback (no-stub-mode policy).

_extract_stub is retained for tests that pass stub_mode=True explicitly.

BUG-queue-573e3 (2026-05-15): OCI JSON_OBJECT mode does NOT guarantee that
control characters inside JSON string values are escaped.  Multi-line content
from wide Confluence table cells produces bare \\n / \\r / \\t inside string
values, causing json.loads() to raise JSONDecodeError("Unterminated string").
The fix is _escape_bare_control_chars() — a state-machine that escapes those
characters ONLY while inside a double-quoted JSON string, leaving structural
whitespace between keys untouched.  See _llm_extract for usage.

BUG-queue-44364 (2026-05-15): Schemas with many fields (e.g. 32+ fields) cause
the OCI model to emit exactly _EXTRACT_MAX_TOKENS output tokens — the hard
ceiling — so the JSON is TRUNCATED mid-string.  All three parse-recovery
attempts fail because the trailing fields are structurally absent, not merely
containing unescaped control chars (that is BUG-queue-573e3, a distinct issue).
The fix: raise _EXTRACT_MAX_TOKENS from 2048 to 4096 (matching
WorkflowExecutor._llm_extract_fields — the production path this preview
mirrors), and add explicit post-call truncation detection.  If all parse
attempts fail AND tokens_out >= max_tokens the error names the truncation root
cause and instructs the operator to increase max_tokens or reduce schema size.
See _llm_extract for the detection logic.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Maximum tokens requested from the LLM for extraction.  Must match
# WorkflowExecutor._llm_extract_fields (executor.py ~line 495) so the eval
# preview path and the production runtime path cannot drift.
# Raised from 2048 → 4096 to fix BUG-queue-44364 (truncation on 32-field schemas).
_EXTRACT_MAX_TOKENS = 4096


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
# JSON sanitisation helper — BUG-queue-573e3
# ---------------------------------------------------------------------------

def _escape_bare_control_chars(s: str) -> str:
    """Escape bare control characters (\\n, \\r, \\t) that appear inside
    JSON double-quoted string values but are NOT already backslash-escaped.

    Background (BUG-queue-573e3): OCI GenAI JSON_OBJECT mode does not
    guarantee that control characters inside string values are escaped.
    Multi-line Confluence table-cell content produces raw newlines inside
    string values, which breaks json.loads() with "Unterminated string".

    Strategy: walk the string character by character, tracking whether we
    are inside a double-quoted JSON string (respecting \\-escape sequences
    and the JSON 'true'/'false'/'null'/number literal contexts where no
    quoting occurs).  Escape raw \\n/\\r/\\t only when inside a string.

    Structural whitespace between keys/values is OUTSIDE any string and is
    left untouched, so the resulting text is still valid JSON layout.

    Already-escaped sequences (e.g. the two-character sequence backslash-n
    that the model did emit correctly) are left intact because the
    backslash advances the state machine past the next character.
    """
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_string:
            if ch == "\\":
                # Consume the escape sequence as-is (both backslash and the
                # next character are passed through unchanged).
                out.append(ch)
                i += 1
                if i < len(s):
                    out.append(s[i])
                    i += 1
                continue
            elif ch == '"':
                # End of string
                in_string = False
                out.append(ch)
                i += 1
                continue
            elif ch == "\n":
                out.append("\\n")
                i += 1
                continue
            elif ch == "\r":
                out.append("\\r")
                i += 1
                continue
            elif ch == "\t":
                out.append("\\t")
                i += 1
                continue
        else:
            if ch == '"':
                in_string = True
        out.append(ch)
        i += 1
    return "".join(out)


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

    n_fields = len(properties)
    max_tokens = _EXTRACT_MAX_TOKENS

    try:
        result = llm.chat(
            model="synthesis",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
        )
        # Capture tokens_out for truncation detection (BUG-queue-44364).
        # llm_oci.py returns {"text": ..., "tokens_in": ..., "tokens_out": ..., ...}.
        tokens_out = result.get("tokens_out") if isinstance(result, dict) else None
        raw = result.get("text", "") if isinstance(result, dict) else str(result)
        cleaned = re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=re.S).strip()

        # --- Attempt 1: try the cleaned output as-is (fast path for well-formed JSON)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # --- Attempt 2: sanitize bare control chars inside string values
        # (BUG-queue-573e3: OCI JSON_OBJECT mode does not escape \\n/\\r/\\t
        # inside string values from multi-line Confluence table-cell content)
        sanitized = _escape_bare_control_chars(cleaned)
        try:
            return json.loads(sanitized)
        except json.JSONDecodeError:
            pass

        # --- Attempt 3: extract the outermost {...} slice and sanitize it
        m = re.search(r"\{.*\}", sanitized, re.S)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass

        # --- All parse attempts failed.  Determine the most likely root cause
        # so the error is actionable.  Do NOT return {} — that is silent data
        # loss (no-stub-mode policy).

        # BUG-queue-44364 detection: if the response hit the token ceiling the
        # JSON is structurally truncated — missing trailing fields and closing
        # braces.  This is DISTINCT from BUG-queue-573e3 (bare control chars).
        if tokens_out is not None and tokens_out >= max_tokens:
            log.error(
                "_llm_extract: LLM response hit token ceiling "
                "(tokens_out=%d >= max_tokens=%d, schema_fields=%d). "
                "JSON is structurally truncated — this is BUG-queue-44364, "
                "NOT a control-char issue. First 500 chars: %s",
                tokens_out, max_tokens, n_fields, sanitized[:500],
            )
            raise ValueError(
                f"_llm_extract: LLM response truncated at max_tokens={max_tokens} "
                f"(tokens_out={tokens_out}); schema has {n_fields} fields. "
                f"Increase max_tokens or reduce schema size. "
                f"This is NOT BUG-queue-573e3 (control chars) — it is structural "
                f"truncation (BUG-queue-44364). "
                f"First 500 chars: {sanitized[:500]!r}"
            )

        # Non-ceiling failure: could be BUG-queue-573e3 (bare control chars),
        # other model formatting issues, or a combination of both.  Both bug IDs
        # are named so the operator can investigate either path.
        log.error(
            "_llm_extract: all JSON parse attempts failed "
            "(tokens_out=%s, max_tokens=%d, schema_fields=%d). "
            "Possible causes: unescaped control chars (BUG-queue-573e3) or "
            "other malformed output. First 500 chars of sanitized payload: %s",
            tokens_out, max_tokens, n_fields, sanitized[:500],
        )
        raise ValueError(
            f"_llm_extract: could not parse LLM JSON response after sanitization. "
            f"Possible causes: (1) unescaped control characters in OCI JSON_OBJECT "
            f"output (see BUG-queue-573e3); (2) other model formatting error. "
            f"tokens_out={tokens_out}, max_tokens={max_tokens}, "
            f"schema_fields={n_fields}. "
            f"First 500 chars: {sanitized[:500]!r} "
            f"(see BUG-queue-573e3, BUG-queue-44364)"
        )
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
