"""synthesize_schema — JSON-Schema generation from a field list.

Per ADR-015 §Schema synthesis from samples. In stub LLM mode, builds a
reasonable JSON-Schema 2020-12 document from field names + heuristic typing.
"""
from __future__ import annotations


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
