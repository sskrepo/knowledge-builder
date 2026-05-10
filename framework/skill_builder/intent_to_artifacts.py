"""intent_to_artifacts — synthesize extraction + workflow skills from an intent file.

Per ADR-015. Non-interactive mode: takes a YAML intent file describing the task,
sources, and example outcome → produces all framework artifacts (persona-builder
KB entry, JSON-Schema, workflow skill YAML, synthesis mapping, gold-set seeds).

Conversational mode (kb-cli skill-builder without --intent-file) is Phase 3 polish.

Intent file format (intent_file.yaml):
  persona: tpm
  task_description: "..."
  sources:
    - kind: confluence
      space: PRODUCT
      labels: [weekly-status]
  example_outcome:
    kind: pptx | docx | markdown | structured
    path: framework/_dev_fixtures/example_weekly_review.pptx     # optional
    fields:                                                       # optional, when artifact missing
      week_id: 2026-W17
      rag_status: amber
      top_milestones: [...]
  trigger:
    on_request: true
    on_schedule: "0 16 * * 5"
  output_format: pptx
  delivery:
    kind: filesystem
    path: ~/.kbf/outputs/weekly-exec-review-{week_id}.pptx
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


class SkillBuilder:
    def __init__(self, llm=None):
        self.llm = llm

    def synthesize(self, intent: dict, dry_run: bool = False) -> dict:
        """Synthesize all artifacts; return a dict of file_path → content.
        If dry_run=True, doesn't write to disk."""
        persona = intent["persona"]
        task = intent["task_description"]
        sources = intent.get("sources", [])
        example = intent.get("example_outcome", {})
        trigger = intent.get("trigger", {})
        output_format = intent.get("output_format", "markdown")
        delivery = intent.get("delivery", {"kind": "sync_return"})

        # 1. Infer required fields from example outcome
        required_fields = self._infer_fields_from_example(example, task)

        # 2. Reuse detection: which fields already exist in shim_kb?
        from ..orchestrator.shim_kb import ShimKb
        shim_kb = ShimKb(REPO_ROOT / "framework" / "persona_builders")
        coverage = self._detect_reuse(required_fields, persona, shim_kb)

        # 3. Synthesize artifacts
        artifacts: dict[str, str] = {}
        skill_name = self._slugify_skill_name(task)
        kb_name = f"{persona}.{skill_name}_data"

        # Extraction skill (only if there are gap fields)
        if coverage["gaps"]:
            schema_path = f"framework/parsers/schemas/{persona}/{skill_name}/v1.json"
            schema = self._synthesize_schema(coverage["gaps"], task, sources, example)
            artifacts[schema_path] = json.dumps(schema, indent=2)

            # Persona-builder KB entry
            pb_path = f"framework/persona_builders/{persona}.yaml"
            pb_diff = self._synthesize_persona_builder_diff(
                persona, kb_name, schema_path, sources, coverage["gaps"]
            )
            artifacts[pb_path + ".diff"] = pb_diff

            # Extraction gold set
            ext_gold_path = f"eval/gold_sets/{persona}-{skill_name}-extraction.jsonl"
            artifacts[ext_gold_path] = self._gold_seed_extraction(example, sources)

        # Workflow skill (always synthesized for tasks with output)
        wf_path = f"framework/workflow_skills/{persona}/{skill_name}.yaml"
        wf_yaml = self._synthesize_workflow_skill(
            persona, skill_name, task, kb_name if coverage["gaps"] else None,
            list(coverage["covered"].values()), required_fields,
            trigger, output_format, delivery,
        )
        artifacts[wf_path] = wf_yaml

        # Workflow gold set
        wf_gold_path = f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl"
        artifacts[wf_gold_path] = self._gold_seed_workflow(skill_name, example, task)

        # Synthesis mapping
        if output_format in ("pptx", "docx", "markdown", "email"):
            sm_path = f"framework/synthesis/mappings/{skill_name}.yaml"
            artifacts[sm_path] = self._synthesize_mapping(skill_name, required_fields, output_format)

        # 4. Write to disk if not dry-run
        if not dry_run:
            for rel_path, content in artifacts.items():
                full = REPO_ROOT / rel_path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content)
                log.info("wrote %s", full)

        return {
            "skill_name": skill_name,
            "kb_name": kb_name,
            "persona": persona,
            "required_fields": required_fields,
            "reuse": coverage,
            "artifacts": list(artifacts.keys()),
        }

    # ------------------------------------------------------------------
    # Field inference
    # ------------------------------------------------------------------
    def _infer_fields_from_example(self, example: dict, task: str) -> list[str]:
        """Infer field names from example outcome.

        Strategy:
        - If example has 'fields' explicitly → use those keys
        - If example has a path to a PPT/DOCX → analyze_artifact (Phase 3 polish; for now stub)
        - Else → derive from task description via LLM (or simple heuristic)
        """
        if "fields" in example and isinstance(example["fields"], dict):
            return list(example["fields"].keys())
        if "path" in example:
            from .analyze_artifact import analyze_artifact
            return analyze_artifact(example["path"])
        # Fallback: heuristic field set based on common task patterns
        if "weekly" in task.lower() and "status" in task.lower():
            return ["week_id", "rag_status", "top_milestones", "blockers", "exec_asks"]
        if "incident" in task.lower():
            return ["incident_id", "summary", "severity", "service", "resolution"]
        if "release" in task.lower():
            return ["release_id", "target_date", "scope_items", "gating_risks", "owners"]
        return ["title", "summary", "details", "source_url"]

    # ------------------------------------------------------------------
    # Reuse detection (ACL-aware per ADR-015)
    # ------------------------------------------------------------------
    def _detect_reuse(self, required_fields: list[str], persona: str, shim_kb) -> dict:
        visible = shim_kb.cards_visible_to(persona)
        covered: dict[str, str] = {}
        gaps: list[str] = []
        for field in required_fields:
            match = next(
                (kb for kb in visible if field in kb.get("provides_fields", [])),
                None,
            )
            if match:
                covered[field] = f"{match['persona']}.{match['name']}"
            else:
                gaps.append(field)
        return {"covered": covered, "gaps": gaps}

    # ------------------------------------------------------------------
    # Schema synthesis
    # ------------------------------------------------------------------
    def _synthesize_schema(self, fields: list[str], task: str, sources: list, example: dict) -> dict:
        """Build a JSON-Schema from the inferred fields. Real impl uses LLM;
        this is the heuristic-driven fallback that runs without an LLM."""
        properties: dict[str, dict] = {}
        for f in fields:
            properties[f] = self._infer_field_spec(f, example)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": f"Extraction schema for: {task}",
            "type": "object",
            "required": list(fields),
            "properties": properties,
            "additionalProperties": True,
        }

    def _infer_field_spec(self, field: str, example: dict) -> dict:
        # Heuristic typing based on field name + example value
        ex_value = (example.get("fields") or {}).get(field)
        if isinstance(ex_value, list):
            inner = "string" if not ex_value else type(ex_value[0]).__name__
            return {"type": "array", "items": {"type": "string"}, "description": f"Multi-valued {field}"}
        if isinstance(ex_value, (int, float)):
            return {"type": "number", "description": f"Numeric {field}"}
        if isinstance(ex_value, dict):
            return {"type": "object", "description": f"Structured {field}"}
        # String defaults with hints
        if "id" in field or "_id" in field:
            return {"type": "string", "description": f"Identifier — {field}", "maxLength": 64}
        if "date" in field or "_at" in field:
            return {"type": "string", "format": "date-time", "description": f"Timestamp — {field}"}
        if "summary" in field or "description" in field:
            return {"type": "string", "description": f"Summary text — {field}", "maxLength": 1000}
        if "status" in field or "rag" in field:
            return {"type": "string", "description": f"Status — {field}",
                    "enum": ["red", "amber", "green"] if "rag" in field else None}
        return {"type": "string", "description": f"Field {field} — refine description"}

    # ------------------------------------------------------------------
    # Persona-builder diff
    # ------------------------------------------------------------------
    def _synthesize_persona_builder_diff(self, persona: str, kb_name: str,
                                          schema_path: str, sources: list, fields: list) -> str:
        kb_short = kb_name.split(".", 1)[1] if "." in kb_name else kb_name
        return f"""# Diff for framework/persona_builders/{persona}.yaml — APPLY MANUALLY OR via kb-cli
# Append the following knowledge_bases entry:

knowledge_bases:
  - name: {kb_short}
    kind: vector
    extraction_schema: {schema_path}
    provides_fields:
{chr(10).join(f'      - {f}' for f in fields)}
    sources:
{chr(10).join('      - ' + json.dumps(s) for s in sources)}
    retrieval_tools: [vector_search]
    kb_card:
      summary: "Synthesized by skill builder; refine after first dry-run."
      use_when: "Queries about {', '.join(fields[:3])}, etc."
      input_shape: "Natural-language question with optional filters."
      output_shape: "Cited passages with structured metadata."
"""

    # ------------------------------------------------------------------
    # Workflow skill synthesis
    # ------------------------------------------------------------------
    def _synthesize_workflow_skill(self, persona: str, skill_name: str, task: str,
                                     new_kb: str | None, reuse_kbs: list[str],
                                     required_fields: list[str], trigger: dict,
                                     output_format: str, delivery: dict) -> str:
        all_kbs = ([new_kb] if new_kb else []) + list(set(reuse_kbs))
        on_request_block = ""
        if trigger.get("on_request"):
            inputs = trigger.get("inputs", [{"name": "input", "type": "string"}])
            on_request_block = f"""  on_request:
    enabled: true
    inputs:
{chr(10).join('      - ' + json.dumps(i) for i in inputs)}
    output_format: {output_format}
    response_mode: artifact_url"""
        on_schedule_block = ""
        if trigger.get("on_schedule"):
            on_schedule_block = f"""  on_schedule:
    cron: "{trigger['on_schedule']}"
    delivery:
      kind: {delivery.get('kind', 'filesystem')}
      path: "{delivery.get('path', f'~/.kbf/outputs/{skill_name}.' + output_format)}"  """

        requires = ""
        for kb in all_kbs:
            requires += f"""  - kb: {kb}
    required_fields:
{chr(10).join(f'      - {f}' for f in required_fields)}
"""

        return f"""# Synthesized by skill_builder. Refine after first dry-run.
workflow_skill: {skill_name}
persona: {persona}
status: draft

trigger:
{on_request_block}
{on_schedule_block}

skill_card:
  summary: "{task[:200]}"
  use_when: "User asks for: {task[:200]}"
  example_invocations:
    - "{task[:100]}"
  do_not_use_for: "Single-fact lookups (use vector_search). Live operational data (use query_fleet)."

requires_extractions:
{requires}

synthesis:
  output_format: {output_format}
  template: synthesis/templates/{skill_name}.{output_format}
  slide_mapping: synthesis/mappings/{skill_name}.yaml

delivery:
  kind: {delivery.get('kind', 'filesystem')}
  path: "{delivery.get('path', f'~/.kbf/outputs/{skill_name}.' + output_format)}"

eval:
  gold_set: eval/gold_sets/{persona}-{skill_name}-workflow.jsonl
  exit_criteria:
    field_accuracy: 0.85
    delivery_success_rate: 0.99
"""

    # ------------------------------------------------------------------
    # Synthesis mapping (field → section)
    # ------------------------------------------------------------------
    def _synthesize_mapping(self, skill_name: str, fields: list[str], output_format: str) -> str:
        sections = "\n".join(f"  {f}: {{ section: '{f.replace('_', ' ').title()}', source_field: {f} }}" for f in fields)
        return f"""# Synthesis mapping for {skill_name}
# Field → section/slide mapping. Refine after first dry-run.
title: "{skill_name.replace('_', ' ').title()}"
sections:
{sections}
"""

    # ------------------------------------------------------------------
    # Gold set seeds
    # ------------------------------------------------------------------
    def _gold_seed_extraction(self, example: dict, sources: list) -> str:
        # Format: jsonl, one object per source-target pair
        if "fields" in example:
            return json.dumps({
                "id": "ext-001",
                "input_description": "Example outcome provided by persona team",
                "expected_extraction": example["fields"],
                "must_match_fields": list(example["fields"].keys())[:3],
                "notes": "STARTER — replace with real (source, expected_extraction) pairs",
            }) + "\n"
        return json.dumps({
            "id": "ext-001",
            "input_description": "REPLACE with real source",
            "expected_extraction": {},
            "notes": "STARTER",
        }) + "\n"

    def _gold_seed_workflow(self, skill_name: str, example: dict, task: str) -> str:
        return json.dumps({
            "id": "wf-001",
            "question": task,
            "expected_skill": skill_name,
            "expected_output_includes": list((example.get("fields") or {}).keys())[:3],
            "min_intent_match_score": 0.85,
            "min_field_accuracy": 0.85,
        }) + "\n"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify_skill_name(task: str) -> str:
        import re
        s = re.sub(r"[^a-z0-9_]+", "_", task.lower())
        s = re.sub(r"_+", "_", s).strip("_")
        return s[:50] or "unnamed_skill"
