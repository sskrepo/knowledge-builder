"""synthesize_schema — JSON-Schema generation from a field list.

Per ADR-015 §Schema synthesis from samples. In stub LLM mode, builds a
reasonable JSON-Schema 2020-12 document from field names + heuristic typing.
When an LLM client is provided alongside a slide/section mapping, uses the
LLM to synthesize extraction instructions from the actual artifact content.
"""
from __future__ import annotations
import json
import logging

log = logging.getLogger(__name__)

_DESCRIPTION_SYNTHESIS_PROMPT = """\
You are a Knowledge Builder Framework schema engineer. Your job is to write
precise extraction instructions for each field in an extraction schema.

The schema will be used by an LLM parser to extract structured data from
{artifact_type} documents for persona "{persona}".

User intent: "{intent}"

For each field below, write a single concise extraction instruction (1-2 sentences)
that tells the LLM parser exactly what content to look for and how to format it.
Be specific — reference the section/slide title and describe the expected format.

Fields (with source location in the artifact):
{field_contexts}

Return ONLY a JSON object mapping field_name → extraction_instruction string.
Example: {{"schedule_health": "RAG status (Red/Amber/Green) for the schedule, \
with a 1-2 sentence justification citing specific milestone dates or blockers."}}
"""


def synthesize_field_descriptions(
    fields: list[str],
    mapping: dict | None,
    intent: str,
    persona: str,
    llm=None,
) -> dict[str, str]:
    """Return a {field_name: description} dict for use in field_specs.

    When llm is provided and mapping has body_text / raw_title context:
      - Calls the LLM once with all fields to synthesize extraction instructions.
      - Falls back to heuristic per field on LLM failure.

    When llm is None (stub mode):
      - Uses raw_title / raw_heading from mapping as a hint if available.
      - Otherwise falls back to _infer_field_spec heuristic.
    """
    if llm is not None and mapping:
        try:
            return _llm_synthesize_descriptions(fields, mapping, intent, persona, llm)
        except Exception as exc:
            log.warning("synthesize_field_descriptions: LLM call failed (%s) — using heuristic", exc)

    # Heuristic path: use raw_title/raw_heading as a hint when available
    result: dict[str, str] = {}
    for field in fields:
        hint = None
        if mapping and field in mapping:
            hint = mapping[field].get("raw_title") or mapping[field].get("raw_heading")
        if hint and hint.lower().replace(" ", "_").replace("-", "_") != field:
            # The raw label has more information than the snake_case field name
            result[field] = f"Extract the '{hint}' section — refine this description."
        else:
            result[field] = _infer_field_spec(field).get("description", f"Field {field} — refine description")
    return result


def _llm_synthesize_descriptions(
    fields: list[str],
    mapping: dict,
    intent: str,
    persona: str,
    llm,
) -> dict[str, str]:
    """Call the LLM to synthesize extraction instructions for all fields."""
    # Determine artifact type from mapping entries
    kinds = {v.get("kind", "") for v in mapping.values()}
    if "slide_title" in kinds:
        artifact_type = "PowerPoint presentation"
    elif "heading" in kinds:
        artifact_type = "document"
    else:
        artifact_type = "document"

    # Build field context lines
    context_lines: list[str] = []
    for field in fields:
        m = mapping.get(field, {})
        raw_label = m.get("raw_title") or m.get("raw_heading") or field
        body = m.get("body_text", "").strip()
        location = ""
        if "slide" in m:
            location = f"slide {m['slide'] + 1}"
        elif "line_number" in m:
            location = f"line {m['line_number']}"
        ctx = f"- {field}: section/slide titled '{raw_label}'"
        if location:
            ctx += f" ({location})"
        if body:
            ctx += f"\n  Sample content: {body[:200]}"
        context_lines.append(ctx)

    prompt = _DESCRIPTION_SYNTHESIS_PROMPT.format(
        artifact_type=artifact_type,
        persona=persona,
        intent=intent,
        field_contexts="\n".join(context_lines),
    )

    result = llm.chat(
        model="synthesis",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=2048,
    )
    raw = result["text"] if isinstance(result, dict) else str(result)

    import re as _re
    raw_clean = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=_re.S).strip()
    data = json.loads(raw_clean)

    # Return only valid field entries; fill missing with heuristic
    descriptions: dict[str, str] = {}
    for field in fields:
        desc = data.get(field, "")
        descriptions[field] = str(desc).strip() if desc else _infer_field_spec(field).get(
            "description", f"Field {field} — refine description"
        )
    return descriptions


def synthesize_extraction_schema(
    fields: list[str],
    persona: str,
    kb_name: str,
) -> dict:
    """Generate a JSON-Schema 2020-12 object for the given extraction fields.

    In stub mode (no LLM), heuristic types are inferred from field name patterns.
    A real LLM provider would call out to get bottom-up + top-down refinement
    (per ADR-015 §Schema synthesis from samples), but this implementation is
    fully deterministic and works without external services.
    """
    properties: dict[str, dict] = {}
    for field in fields:
        properties[field] = _infer_field_spec(field)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"framework/parsers/schemas/{persona}/{kb_name}/v1.json",
        "title": f"{persona} — {kb_name} — v1",
        "description": f"Synthesized by skill_builder for persona={persona}, kb={kb_name}. Refine before promoting.",
        "type": "object",
        "required": list(fields),
        "properties": properties,
        "additionalProperties": False,
    }


def _infer_field_spec(field: str) -> dict:
    """Infer a JSON-Schema property descriptor from a field name.

    Heuristics (ordered by specificity):
    - Plural noun → array of strings
    - *_id suffix → short string
    - *_at / *_date → date-time string
    - *_count / *_minutes / *_score → number
    - *_status / *_rag / rag_* → enum or short string
    - *_summary / *_description / *_text / *_body → long string
    - Everything else → generic string
    """
    f = field.lower()

    if f.endswith("s") and any(
        f.endswith(suffix)
        for suffix in (
            "items", "steps", "risks", "links", "owners", "factors",
            "affected", "asks", "tags", "ids", "codes", "milestones",
            "blockers", "findings", "implications", "gaps", "contacts",
        )
    ):
        return {
            "type": "array",
            "items": {"type": "string"},
            "description": f"Multi-valued list — {field}",
        }

    if f.endswith("_id") or f == "id":
        return {"type": "string", "description": f"Identifier — {field}", "maxLength": 64}

    if f.endswith("_at") or "date" in f or f.endswith("_time"):
        return {
            "type": "string",
            "format": "date-time",
            "description": f"Timestamp — {field}",
        }

    if any(kw in f for kw in ("_count", "_minutes", "_score", "_size", "_num")):
        return {"type": "integer", "minimum": 0, "description": f"Numeric — {field}"}

    if "rag" in f or f == "rag_status":
        return {
            "type": "string",
            "enum": ["red", "amber", "green"],
            "description": f"RAG status — {field}",
        }

    if f.endswith("_status") or f == "status":
        return {
            "type": "string",
            "description": f"Status indicator — {field}",
            "maxLength": 50,
        }

    if any(kw in f for kw in ("_summary", "_description", "_text", "_body", "_narrative")):
        return {
            "type": "string",
            "description": f"Free-text summary — {field}",
            "maxLength": 1000,
        }

    if "bool" in f or f.startswith("is_") or f.startswith("has_") or f.startswith("enabled"):
        return {"type": "boolean", "description": f"Boolean flag — {field}"}

    return {"type": "string", "description": f"Field {field} — refine description", "maxLength": 500}
