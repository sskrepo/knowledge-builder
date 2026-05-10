"""Workflow executor — runs a workflow skill end-to-end.

Per ADR-016. Steps: source discovery → extract (or read cached) → retrieve →
synthesize → render → deliver.

Laptop-mode friendly: uses FilestoreContentStore + filesystem deliverer when no
ADB/Vault/OCI configured.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


class WorkflowExecutor:
    def __init__(self, store=None, llm=None):
        self.store = store
        self.llm = llm

    def execute(self, skill_yaml_path: Path, inputs: dict) -> dict:
        cfg = yaml.safe_load(Path(skill_yaml_path).read_text())
        skill_name = cfg.get("workflow_skill")
        persona = cfg.get("persona")
        log.info("executing workflow skill %s for persona %s with inputs=%s",
                 skill_name, persona, inputs)

        # 1. Resolve source set (procedural discovery or static)
        sources = self._resolve_sources(cfg, inputs)
        log.info("resolved %d sources", len(sources))

        # 2. Retrieve relevant ContentItems for the inputs
        passages = self._retrieve_for_inputs(cfg, inputs, sources)

        # 3. Synthesize structured data per slide_mapping
        rendered_data = self._synthesize(cfg, inputs, passages)

        # 4. Render to artifact
        artifact_bytes = self._render(cfg, rendered_data)

        # 5. Deliver
        delivery_result = self._deliver(cfg, artifact_bytes, inputs)

        return {
            "skill": skill_name,
            "persona": persona,
            "inputs": inputs,
            "rendered_data": rendered_data,
            "delivery": delivery_result,
            "executed_at": datetime.utcnow().isoformat() + "Z",
        }

    # ------------------------------------------------------------------
    def _resolve_sources(self, cfg: dict, inputs: dict) -> list[dict]:
        # Phase 1: simple stub — return inputs as the source ref.
        # Phase 2/3: procedural discovery via Adapter.discover()
        if "sources" in cfg:
            return cfg["sources"]
        return []

    def _retrieve_for_inputs(self, cfg: dict, inputs: dict, sources: list[dict]) -> list[dict]:
        # If we have a real store, query it; else use fixture data
        passages = []
        if self.store:
            from ..core.interfaces import Query
            for req in cfg.get("requires_extractions", []):
                kb_name = req["kb"].split(".", 1)[-1]
                # Build query from inputs (simple pass-through: input as filter)
                if "incident_id" in inputs:
                    q = Query(kind="incident_summary", payload={"incident_id": inputs["incident_id"]})
                elif "release_id" in inputs:
                    q = Query(kind="filter", payload={"source_id": inputs["release_id"]})
                else:
                    q = Query(kind="vector_knn", payload={"query": " ".join(str(v) for v in inputs.values())})
                results = self.store.query(q)
                for r in results:
                    passages.append({
                        "text": r.text,
                        "citation": r.citation_url,
                        "metadata": r.metadata,
                    })

        # Fallback: load from fixture data
        if not passages:
            passages = self._load_fixture_passages(inputs)

        return passages

    def _load_fixture_passages(self, inputs: dict) -> list[dict]:
        fixtures_dir = REPO_ROOT / "framework" / "_dev_fixtures"
        if not fixtures_dir.exists():
            return []
        # Look up by id heuristics
        for kind_dir in fixtures_dir.iterdir():
            if not kind_dir.is_dir():
                continue
            for fpath in kind_dir.glob("*.json"):
                data = json.loads(fpath.read_text())
                # Match if any input value equals data['id'] or 'source_id' or 'release_id'
                if any(str(v) in (data.get("id"), data.get("source_id"), data.get("release_id"))
                       for v in inputs.values()):
                    return [{
                        "text": json.dumps(data, indent=2),
                        "citation": f"fixture://{fpath.name}",
                        "metadata": data,
                    }]
        return []

    def _synthesize(self, cfg: dict, inputs: dict, passages: list[dict]) -> dict:
        # Map passages → structured data per slide_mapping
        # Simple heuristic: if first passage has structured metadata, use it directly
        sections: dict[str, Any] = {}
        merged_meta: dict = {}
        for p in passages:
            if isinstance(p.get("metadata"), dict):
                merged_meta.update(p["metadata"])

        # Apply slide mapping if present
        mapping_path = cfg.get("synthesis", {}).get("slide_mapping")
        if mapping_path:
            mp = REPO_ROOT / mapping_path
            if mp.exists():
                mapping = yaml.safe_load(mp.read_text()) or {}
                for sec_name, sec_cfg in mapping.get("sections", {}).items():
                    src = sec_cfg.get("source_field") if isinstance(sec_cfg, dict) else sec_cfg
                    val = merged_meta.get(src)
                    if val is not None:
                        sections[sec_cfg.get("section", sec_name) if isinstance(sec_cfg, dict) else sec_name] = val

        # Fallback: dump everything from merged metadata
        if not sections:
            sections = {k.replace("_", " ").title(): v for k, v in merged_meta.items()}

        title = cfg.get("synthesis", {}).get("title") or merged_meta.get("title") or \
                cfg.get("workflow_skill", "Generated Output")
        return {
            "title": title,
            "subtitle": f"Generated by {cfg.get('workflow_skill')} for inputs={inputs}",
            "sections": sections,
            "citations": [p.get("citation") for p in passages],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

    def _render(self, cfg: dict, data: dict) -> bytes:
        from ..renderers.registry import get_renderer
        output_format = cfg.get("synthesis", {}).get("output_format") or \
                        cfg.get("trigger", {}).get("on_request", {}).get("output_format") or \
                        "markdown"
        renderer = get_renderer(output_format)
        template = cfg.get("synthesis", {}).get("template")
        return renderer.render(data, template)

    def _deliver(self, cfg: dict, artifact: bytes, inputs: dict) -> dict:
        from ..deliverers.registry import get_deliverer
        delivery = cfg.get("delivery", {"kind": "filesystem"})
        kind = delivery.get("kind", "filesystem")
        deliverer = get_deliverer(kind)
        # Substitute inputs into path template
        dest = dict(delivery)
        if "path" in dest:
            dest["path"] = dest["path"].format(**inputs)
        return deliverer.deliver(artifact, dest)
