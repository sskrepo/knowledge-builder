"""Workflow executor — runs a workflow skill end-to-end.

Per ADR-016. Steps: source discovery → extract (or read cached) → retrieve →
synthesize → render → deliver → cost telemetry → eval recording.

Laptop-mode friendly: uses FilestoreContentStore + filesystem deliverer when no
ADB/Vault/OCI configured.
"""
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
_TELEMETRY_DIR = Path.home() / ".kbf" / "telemetry"


class WorkflowExecutor:
    def __init__(self, store=None, llm=None):
        self.store = store
        self.llm = llm

    def execute(self, skill_yaml_path: Path, inputs: dict) -> dict:
        t_start = time.monotonic()
        cfg = yaml.safe_load(Path(skill_yaml_path).read_text())
        skill_name = cfg.get("workflow_skill")
        persona = cfg.get("persona")
        log.info("executing workflow skill %s for persona %s with inputs=%s",
                 skill_name, persona, inputs)

        # 1. Resolve source set (procedural discovery or static)
        sources = self._resolve_sources(cfg, inputs)
        log.info("resolved %d sources", len(sources))

        # 2. Retrieve relevant ContentItems for the inputs
        t_retrieve_start = time.monotonic()
        passages = self._retrieve_for_inputs(cfg, inputs, sources)
        retrieve_ms = int((time.monotonic() - t_retrieve_start) * 1000)

        # 3. Synthesize structured data per slide_mapping
        rendered_data = self._synthesize(cfg, inputs, passages)

        # 4. Render to artifact
        t_render_start = time.monotonic()
        artifact_bytes = self._render(cfg, rendered_data)
        render_ms = int((time.monotonic() - t_render_start) * 1000)

        # 5. Deliver
        t_deliver_start = time.monotonic()
        delivery_result = self._deliver(cfg, artifact_bytes, inputs)
        deliver_ms = int((time.monotonic() - t_deliver_start) * 1000)

        total_ms = int((time.monotonic() - t_start) * 1000)

        output_path = (
            delivery_result.get("url")
            or delivery_result.get("path")
            or delivery_result.get("archive")
            or ""
        )

        # 9. Cost telemetry
        self._record_cost(skill_name, persona, {
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_calls": 0,
            "latency_ms": total_ms,
            "render_ms": render_ms,
            "deliver_ms": deliver_ms,
        })

        # 10. Eval gold-set recording
        self._record_eval_entry(skill_name, inputs, output_path)

        return {
            "skill": skill_name,
            "persona": persona,
            "inputs": inputs,
            "rendered_data": rendered_data,
            "delivery": delivery_result,
            "executed_at": datetime.utcnow().isoformat() + "Z",
            "metrics": {
                "latency_ms": total_ms,
                "render_ms": render_ms,
                "deliver_ms": deliver_ms,
            },
        }

    # ------------------------------------------------------------------
    def _record_cost(self, skill_name: str, persona: str, metrics: dict) -> None:
        """Write cost telemetry for this workflow execution.
        metrics: {tokens_in, tokens_out, llm_calls, latency_ms, render_ms, deliver_ms}
        """
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "operation_kind": "workflow_execute",
            "skill_name": skill_name,
            "persona": persona,
            **metrics,
        }
        costs_file = _TELEMETRY_DIR / "workflow_costs.jsonl"
        with costs_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        log.debug("cost telemetry written: skill=%s latency_ms=%d", skill_name, metrics.get("latency_ms", 0))

    def _record_eval_entry(self, skill_name: str, inputs: dict, output_path: str) -> None:
        """Record this execution as a potential gold-set entry for eval."""
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "skill_name": skill_name,
            "inputs": inputs,
            "output_path": output_path,
            "candidate": True,
        }
        eval_file = _TELEMETRY_DIR / "workflow_eval_candidates.jsonl"
        with eval_file.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        log.debug("eval candidate recorded: skill=%s", skill_name)

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
            passages = self._load_fixture_passages(inputs, cfg=cfg)

        return passages

    def _load_fixture_passages(self, inputs: dict, cfg: dict | None = None) -> list[dict]:
        fixtures_dir = REPO_ROOT / "framework" / "_dev_fixtures"
        if not fixtures_dir.exists():
            return []

        input_values = {str(v) for v in inputs.values() if v is not None}

        # 1. Try exact id-based match across all fixture dirs
        for kind_dir in sorted(fixtures_dir.iterdir()):
            if not kind_dir.is_dir():
                continue
            for fpath in sorted(kind_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text())
                except Exception:
                    continue
                id_candidates = {
                    str(data.get("id", "")),
                    str(data.get("source_id", "")),
                    str(data.get("release_id", "")),
                }
                id_candidates.discard("")
                if input_values & id_candidates:
                    return [{
                        "text": json.dumps(data, indent=2),
                        "citation": f"fixture://{fpath.name}",
                        "metadata": data,
                    }]

        # 2. Match against fixture field values (e.g., project="all" matches data.project)
        for kind_dir in sorted(fixtures_dir.iterdir()):
            if not kind_dir.is_dir():
                continue
            for fpath in sorted(kind_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text())
                except Exception:
                    continue
                if any(str(data.get(k)) == str(v) for k, v in inputs.items()
                       if v is not None and k != "project" or str(v) != "all"):
                    return [{
                        "text": json.dumps(data, indent=2),
                        "citation": f"fixture://{fpath.name}",
                        "metadata": data,
                    }]

        # 3. KB-name-based dir match — if skill requires a KB and a matching fixture dir exists
        if cfg:
            for req in cfg.get("requires_extractions", []):
                kb_name = (req.get("kb") or "").split(".")[-1].replace("_", "-")
                for kind_dir in fixtures_dir.iterdir():
                    if not kind_dir.is_dir():
                        continue
                    dir_name = kind_dir.name.replace("_", "-")
                    if dir_name in kb_name or kb_name in dir_name:
                        passages = []
                        for fpath in sorted(kind_dir.glob("*.json")):
                            try:
                                data = json.loads(fpath.read_text())
                                passages.append({
                                    "text": json.dumps(data, indent=2),
                                    "citation": f"fixture://{fpath.name}",
                                    "metadata": data,
                                })
                            except Exception:
                                continue
                        if passages:
                            return passages

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
