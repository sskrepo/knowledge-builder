"""synthesize_builder — produce a persona-builder YAML diff (new KB entry).

Per ADR-015 + ADR-017. The returned dict is suitable for merging into
the existing persona-builder YAML's `knowledge_bases` list.
"""
from __future__ import annotations

import json


def synthesize_persona_builder_diff(
    persona: str,
    kb_name: str,
    schema_path: str,
    sources: dict,
    fields: list[str] | None = None,
) -> dict:
    """Return a dict representing the new KB entry to be appended.

    Args:
        persona: persona id (e.g. "tpm").
        kb_name: full dotted name (e.g. "tpm.weekly_project_status") or just
                 the short name (e.g. "weekly_project_status").
        schema_path: relative path to the generated JSON-Schema.
        sources: dict describing sources — either a list of source descriptors
                 or a single descriptor. May also be a list of raw source dicts.
        fields: list of field names declared in the schema (used for
                provides_fields per ADR-017).

    Returns:
        A dict with keys: name, kind, extraction_schema, provides_fields,
        sources, retrieval_tools, kb_card.  Caller is responsible for
        inserting this into the YAML.
    """
    short_name = kb_name.split(".", 1)[1] if "." in kb_name else kb_name

    normalized_sources: list[dict] = _normalize_sources(sources)

    fields = fields or []

    # Infer KB `kind` + retrieval tools from source types. Hardcoding
    # `kind: vector` regardless of source caused live retrieval to fail for
    # the weekly_exec_review_26ai skill (session synth-tpm-bcbc739d): the
    # ingest path for confluence sources writes markdown + WikiMetadataStore
    # records, which the `search_wiki` retriever queries — NOT the
    # vector_search retriever's embedded chunks. Map per-kind:
    #   confluence       → wiki         → search_wiki
    #   git              → code_wiki    → find_symbol + read_code_page
    #   jira             → vector       → vector_search
    #   adb              → adb          → query_fleet
    #   mixed / unknown  → vector       → vector_search (safest default)
    source_kinds = {s.get("kind") for s in normalized_sources if isinstance(s, dict)}
    if source_kinds == {"confluence"}:
        kind = "wiki"
        retrieval_tools = ["search_wiki"]
    elif source_kinds == {"git"}:
        kind = "code_wiki"
        retrieval_tools = ["find_symbol", "read_code_page"]
    elif source_kinds == {"jira"}:
        kind = "vector"
        retrieval_tools = ["vector_search"]
    elif source_kinds == {"adb"}:
        kind = "adb"
        retrieval_tools = ["query_fleet"]
    else:
        kind = "vector"
        retrieval_tools = ["vector_search"]

    return {
        "name": short_name,
        "kind": kind,
        "extraction_schema": schema_path,
        "provides_fields": list(fields),
        "sources": normalized_sources,
        "retrieval_tools": retrieval_tools,
        "kb_card": {
            "summary": f"Synthesized by skill_builder. Refine after first dry-run.",
            "use_when": (
                f"Queries about {', '.join(fields[:3])}."
                if fields
                else f"Queries from the {short_name} knowledge base."
            ),
            "input_shape": "Natural-language question with optional filters.",
            "output_shape": "Cited passages with structured metadata.",
        },
    }


def _normalize_sources(sources: dict | list) -> list[dict]:
    if isinstance(sources, list):
        return [s if isinstance(s, dict) else {"kind": "unknown", "raw": str(s)} for s in sources]
    if isinstance(sources, dict):
        return [sources]
    return []


def render_diff_as_yaml_comment(persona: str, kb_entry: dict) -> str:
    """Produce a human-readable YAML snippet for the new KB entry.

    This is for display / git-diff purposes. Programmatic merge should use
    the dict returned by synthesize_persona_builder_diff() directly.
    """
    lines = [
        f"# Append the following to framework/persona_builders/{persona}.yaml",
        f"# under the 'knowledge_bases:' key:",
        "",
        f"  - name: {kb_entry['name']}",
        f"    kind: {kb_entry['kind']}",
        f"    extraction_schema: {kb_entry['extraction_schema']}",
        "    provides_fields:",
    ]
    for f in kb_entry.get("provides_fields", []):
        lines.append(f"      - {f}")
    lines.append("    sources:")
    for s in kb_entry.get("sources", []):
        lines.append(f"      - {json.dumps(s)}")
    lines.append(f"    retrieval_tools: {kb_entry.get('retrieval_tools', [])}")
    card = kb_entry.get("kb_card", {})
    lines.append("    kb_card:")
    for k, v in card.items():
        lines.append(f'      {k}: "{v}"')
    return "\n".join(lines)
