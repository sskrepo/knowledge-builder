"""LLM Parser — turns RawItems into ContentItems via OpenAI gpt-4o
constrained by a JSON-Schema extraction document.

Per ADR-003 §6.2 and ADR-004 (per-persona schemas).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.content import ContentItem, ContentMetadata, Edge
from ..core.ids import content_item_id, source_sha
from ..core.interfaces import Parser, RawItem, ParseContext
from ..core.llm import LLMClient

log = logging.getLogger(__name__)

PARSER_VERSION = "llm_parser-v1"


class LLMParser:
    """Schema-injected LLM extractor.

    On parse(raw, ctx):
      1. Loads the JSON-Schema at parsers/schemas/{ctx.schema_id}/v1.json
      2. Builds a system prompt that includes field descriptions
      3. Calls gpt-4o with response_format=json_object
      4. Validates output against the schema
      5. Builds a ContentItem with extracted fields in body and raw fields in metadata
    """
    name = "llm_parser"
    input_kinds = {"jira_issue", "confluence_page", "git_file"}

    def __init__(
        self,
        llm: LLMClient,
        schemas_dir: Path,
        persona: str,
        primary_axis_kind: str,
        metadata_defaults: dict,
        model: str = "gpt-4o",
    ):
        self.llm = llm
        self.schemas_dir = Path(schemas_dir)
        self.persona = persona
        self.primary_axis_kind = primary_axis_kind
        self.metadata_defaults = metadata_defaults
        self.model = model
        self._schema_cache: dict[str, dict] = {}

    def _load_schema(self, schema_id: str) -> dict:
        """schema_id like 'incidents/v1' → schemas_dir/incidents/v1.json"""
        if schema_id in self._schema_cache:
            return self._schema_cache[schema_id]
        path = self.schemas_dir / f"{schema_id}.json"
        with open(path) as f:
            schema = json.load(f)
        self._schema_cache[schema_id] = schema
        return schema

    def parse(self, raw: RawItem, ctx: ParseContext) -> ContentItem:
        schema = self._load_schema(ctx.schema_id)
        prompt = self._build_prompt(raw, schema)
        response = self.llm.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt["system"]},
                {"role": "user", "content": prompt["user"]},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=2000,
        )
        extracted = json.loads(response["text"])
        return self._build_content_item(raw, ctx, schema, extracted)

    def _build_prompt(self, raw: RawItem, schema: dict) -> dict:
        # Build a description of each schema field for the LLM
        fields_desc = self._render_field_descriptions(schema)
        required = schema.get("required", [])
        system = f"""You extract structured data from a {raw.source} {raw.kind}.

Output a JSON object matching this schema:

REQUIRED FIELDS: {required}

FIELD DESCRIPTIONS:
{fields_desc}

Rules:
- Output ONLY the JSON object — no preface, no markdown.
- Required fields MUST be present; optional fields MAY be omitted if not present in the source.
- Stay grounded in the source text; do not invent values.
- For string max-lengths, summarize concisely; do not exceed the cap.
"""
        user_payload = self._payload_for_user_message(raw)
        return {"system": system, "user": user_payload}

    def _render_field_descriptions(self, schema: dict, indent: int = 0) -> str:
        lines: list[str] = []
        for field, spec in schema.get("properties", {}).items():
            t = spec.get("type", "string")
            desc = spec.get("description", "")
            constraint_bits: list[str] = []
            if "maxLength" in spec:
                constraint_bits.append(f"max {spec['maxLength']} chars")
            if "enum" in spec:
                constraint_bits.append(f"one of {spec['enum']}")
            if "items" in spec and isinstance(spec["items"], dict):
                if "type" in spec["items"]:
                    constraint_bits.append(f"items: {spec['items']['type']}")
            constraints = f" ({', '.join(constraint_bits)})" if constraint_bits else ""
            lines.append(f"{'  '*indent}- {field} [{t}]{constraints}: {desc}")
        return "\n".join(lines)

    def _payload_for_user_message(self, raw: RawItem) -> str:
        # Truncate large payloads — extraction works on summaries
        payload_str = json.dumps(raw.payload, default=str)
        if len(payload_str) > 30000:
            payload_str = payload_str[:30000] + "\n...[truncated]"
        return f"Source: {raw.source} {raw.source_id}\n\nContent:\n{payload_str}"

    def _build_content_item(
        self,
        raw: RawItem,
        ctx: ParseContext,
        schema: dict,
        extracted: dict,
    ) -> ContentItem:
        now = datetime.utcnow()
        title = extracted.get("title") or extracted.get("feature_name") or extracted.get("decision_title") or extracted.get("runbook_title") or extracted.get("incident_id") or raw.source_id
        body = json.dumps(extracted, indent=2, default=str)

        # Multi-axis dimensions — best-effort extraction from extracted fields
        functional_area_all = self._coerce_list(extracted.get("functional_area"))
        resources = self._coerce_list(extracted.get("resources_affected") or extracted.get("resources"))
        services = self._coerce_list(extracted.get("services_touched") or extracted.get("services") or extracted.get("service"))
        kind = extracted.get("kind") or self._infer_kind_from_schema_id(ctx.schema_id)
        primary_axis_value = self._extract_primary_axis_value(extracted)

        metadata = ContentMetadata(
            persona_visibility=list(self.metadata_defaults.get("persona_visibility", [])),
            owner=self.metadata_defaults.get("owner", self.persona),
            classification=self.metadata_defaults.get("classification", "internal"),
            source_sha=source_sha(json.dumps(raw.payload, default=str, sort_keys=True)),
            parser_version=PARSER_VERSION,
            schema_version=int(ctx.parser_version.rsplit("v", 1)[-1].split(".")[0]) if "v" in ctx.parser_version else 1,
            created_at=now,
            updated_at=now,
            extracted_by=raw.metadata.get("extracted_by", raw.source),
            extraction_schema=ctx.schema_id,
            extra={"raw_metadata": raw.metadata, "extracted": extracted},
        )

        # Build edges from extracted relationships
        edges = self._build_edges(raw, extracted)

        cid = content_item_id(raw.source, raw.source_id, metadata.schema_version)

        return ContentItem(
            id=cid,
            source=raw.source,
            source_id=raw.source_id,
            path=f"{raw.source}://{raw.source_id}",
            title=title[:500],
            body=body,
            persona=self.persona,
            primary_axis_kind=self.primary_axis_kind,
            primary_axis_value=primary_axis_value,
            functional_area_all=functional_area_all,
            resources=resources,
            services=services,
            kind=kind,
            metadata=metadata,
            edges=edges,
        )

    def _coerce_list(self, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            return [v]
        return [str(v)]

    def _extract_primary_axis_value(self, extracted: dict) -> str:
        if self.primary_axis_kind == "functional_area":
            v = extracted.get("functional_area")
            if isinstance(v, list) and v:
                return str(v[0])
            if isinstance(v, str):
                return v
            return ""
        if self.primary_axis_kind == "service_id":
            return str(extracted.get("service_id") or extracted.get("service") or "")
        if self.primary_axis_kind == "feature_or_release":
            return str(extracted.get("target_release") or extracted.get("release_id") or "")
        if self.primary_axis_kind == "program":
            return str(extracted.get("week_id") or extracted.get("program") or extracted.get("initiative") or "")
        return ""

    def _infer_kind_from_schema_id(self, schema_id: str) -> str:
        """schema_id like 'incidents/v1' → kind = 'incident_history'"""
        kind_map = {
            "incidents": "incident_history",
            "postmortems": "postmortem",
            "runbooks": "runbook",
            "designs": "design",
            "adrs": "decision",
            "decisions": "decision",
            "known-issues": "known_issue",
            "briefs": "feature_brief",
            "release-plans": "release_plan",
            "research": "concept",
            "weekly-ops": "weekly_summary",
            "ecars": "ecar",
            "dependencies": "concept",
            "slas": "sla",
            "escalation": "runbook",
            "compliance": "concept",
            "catalog": "catalog_entry",
            "openapi": "concept",
            "system-maps": "design",
        }
        # schema_id: "incidents/v1" or "ops-mgr/slas/v1"
        parts = schema_id.replace("\\", "/").split("/")
        # find the part right before the version
        for i, p in enumerate(parts):
            if p.startswith("v") and p[1:].isdigit() and i > 0:
                return kind_map.get(parts[i - 1], "concept")
        return "concept"

    def _build_edges(self, raw: RawItem, extracted: dict) -> list[Edge]:
        from ..core import urns
        edges: list[Edge] = []
        # incident → service edges
        for svc in self._coerce_list(extracted.get("services_touched") or extracted.get("services")):
            edges.append(Edge(
                src=urns.content("incidents", raw.source_id),
                dst=urns.service(svc),
                rel="impacts",
            ))
        # incident → resource edges
        for res in self._coerce_list(extracted.get("resources_affected") or extracted.get("resources")):
            edges.append(Edge(
                src=urns.content("incidents", raw.source_id),
                dst=urns.resource(res),
                rel="references",
            ))
        # incident → tenant edges
        for t in self._coerce_list(extracted.get("tenant_ids") or extracted.get("tenants_affected")):
            edges.append(Edge(
                src=urns.content("incidents", raw.source_id),
                dst=urns.tenant(t),
                rel="impacts",
            ))
        # incident → owner team edge
        owner = extracted.get("service_owner")
        if owner:
            edges.append(Edge(
                src=urns.content("incidents", raw.source_id),
                dst=f"urn:faaas:team:{owner}",
                rel="owned_by",
            ))
        return edges
