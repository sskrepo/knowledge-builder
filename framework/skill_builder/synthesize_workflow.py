"""synthesize_workflow — generate a complete workflow skill YAML dict.

Per ADR-015 + ADR-016. Phase 3 addition. Takes intent + fields + optional
template path and produces a workflow skill YAML structure that matches
the ADR-016 schema.
"""
from __future__ import annotations

import re
from pathlib import Path


def synthesize_workflow_skill(
    persona: str,
    skill_name: str,
    intent: dict,
    fields: list[str],
    template_path: str | None = None,
) -> dict:
    """Generate a workflow skill YAML structure as a Python dict.

    Args:
        persona: persona id (e.g. "tpm").
        skill_name: snake_case skill name (e.g. "weekly_exec_review").
        intent: dict from the intent file (task_description, sources, trigger,
                output_format, delivery, requires_extractions etc.).
        fields: list of required field names for the synthesis mapping.
        template_path: optional path to an existing synthesis template; used
                       to populate synthesis.template.

    Returns:
        A dict that can be round-tripped through yaml.safe_dump to produce a
        valid workflow_skills/{persona}/{skill_name}.yaml file.
    """
    task = intent.get("task_description", skill_name.replace("_", " "))
    output_format = intent.get("output_format", "markdown")
    delivery = intent.get("delivery", {"kind": "filesystem",
                                        "path": f"~/.kbf/outputs/{skill_name}.{output_format}"})
    trigger_cfg = intent.get("trigger", {"on_request": True})
    requires_extractions = intent.get("requires_extractions", [])

    result: dict = {
        "workflow_skill": skill_name,
        "persona": persona,
        "status": "draft",
        "trigger": _build_trigger(trigger_cfg, output_format, skill_name),
        "skill_card": _build_skill_card(task, skill_name),
        "requires_extractions": _build_requires_extractions(
            requires_extractions, fields, persona, skill_name, intent
        ),
        "synthesis": _build_synthesis(skill_name, output_format, fields, template_path),
        "delivery": delivery,
        "eval": {
            "gold_set": f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl",
            "exit_criteria": {
                "field_accuracy": 0.85,
                "delivery_success_rate": 0.99,
            },
        },
    }
    return result


# ---------------------------------------------------------------------------
# private helpers
# ---------------------------------------------------------------------------

def _build_trigger(trigger_cfg: dict, output_format: str, skill_name: str) -> dict:
    trigger: dict = {}

    if trigger_cfg.get("on_request", False):
        inputs = trigger_cfg.get("inputs", [{"name": "input", "type": "string",
                                              "description": "Query or filter input"}])
        trigger["on_request"] = {
            "enabled": True,
            "inputs": inputs,
            "output_format": output_format,
            "response_mode": "artifact_url",
        }

    if trigger_cfg.get("on_schedule"):
        cron = trigger_cfg["on_schedule"]
        trigger["on_schedule"] = {
            "cron": cron,
            "delivery": trigger_cfg.get("delivery", {
                "kind": "filesystem",
                "path": f"~/.kbf/outputs/{skill_name}.{output_format}",
            }),
        }

    return trigger or {"on_request": {"enabled": True, "output_format": output_format,
                                       "response_mode": "artifact_url"}}


def _build_skill_card(task: str, skill_name: str) -> dict:
    summary = task[:200]
    return {
        "summary": summary,
        "use_when": f"User asks for: {summary}",
        "example_invocations": [task[:100]],
        "do_not_use_for": (
            "Single-fact lookups (use vector_search). "
            "Live operational data (use query_fleet)."
        ),
    }


def _build_requires_extractions(
    explicit_requires: list[dict],
    fields: list[str],
    persona: str,
    skill_name: str,
    intent: dict,
) -> list[dict]:
    if explicit_requires:
        return explicit_requires

    # Derive from intent: look for new_kb / reuse_kbs in intent
    reuse = intent.get("reuse", {})
    covered = reuse.get("covered", {})
    gaps = reuse.get("gaps", [])

    entries: list[dict] = []

    if gaps:
        kb_name = f"{persona}.{skill_name}_data"
        entries.append({
            "kb": kb_name,
            "required_fields": gaps,
        })

    seen_kbs: dict[str, list[str]] = {}
    for field, kb in covered.items():
        seen_kbs.setdefault(kb, []).append(field)
    for kb, kb_fields in seen_kbs.items():
        entries.append({
            "kb": kb,
            "required_fields": kb_fields,
        })

    if not entries and fields:
        entries.append({
            "kb": f"{persona}.{skill_name}_data",
            "required_fields": list(fields),
        })

    return entries


def _build_synthesis(
    skill_name: str,
    output_format: str,
    fields: list[str],
    template_path: str | None,
) -> dict:
    template = template_path or f"synthesis/templates/{skill_name}.{output_format}"
    slide_mapping_path = f"synthesis/mappings/{skill_name}.yaml"

    synthesis: dict = {
        "output_format": output_format,
        "template": template,
        "slide_mapping": slide_mapping_path,
    }

    if fields:
        synthesis["field_mapping"] = {
            f: {"section": f.replace("_", " ").title(), "source_field": f}
            for f in fields
        }

    return synthesis
