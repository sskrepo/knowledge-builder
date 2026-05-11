"""Skill registry — scans framework/workflow_skills/ and exposes on_request skills as MCP tools.

Per ADR-016: register_workflow_skills_as_mcp_tools() returns a keyed dict of
WorkflowMCPTool objects for use by the MCP server at startup.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class WorkflowMCPTool:
    name: str
    persona: str
    skill_name: str
    description: str
    input_schema: dict
    skill_config: dict
    _path: str = field(repr=False, default="")

    def to_mcp_tool_definition(self) -> dict:
        """Return the MCP tool definition dict for registration in the tool registry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.input_schema.get("properties", {}),
                "required": self.input_schema.get("required", []),
            },
            "persona": self.persona,
            "skill_name": self.skill_name,
            "skill_path": self._path,
        }


def _build_input_schema(inputs: list[dict]) -> dict:
    """Convert skill YAML inputs list to JSON-Schema properties dict."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for inp in inputs:
        name = inp.get("name", "")
        if not name:
            continue
        prop: dict[str, Any] = {"type": inp.get("type", "string")}
        if "description" in inp:
            prop["description"] = inp["description"]
        if "default" in inp:
            prop["default"] = inp["default"]
        else:
            required.append(name)
        properties[name] = prop
    return {"properties": properties, "required": required}


def _tool_name(persona: str, skill_name: str) -> str:
    """Canonical MCP tool name: '{persona}__{skill_name}'."""
    return f"{persona}__{skill_name}"


def discover_workflow_skills(workflow_skills_dir: Path) -> list[WorkflowMCPTool]:
    """Scan workflow_skills/ dir and return all skills as WorkflowMCPTool objects.

    Skills with names starting '_' are ignored (templates).
    """
    out: list[WorkflowMCPTool] = []
    if not workflow_skills_dir.exists():
        return out
    for path in sorted(workflow_skills_dir.rglob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            cfg = yaml.safe_load(path.read_text()) or {}
        except Exception as e:
            log.warning("failed to parse %s: %s", path, e)
            continue

        persona = cfg.get("persona", "")
        skill_name = cfg.get("workflow_skill", "")
        if not persona or not skill_name:
            log.warning("skill at %s missing persona or workflow_skill key; skipping", path)
            continue

        on_request_cfg = (cfg.get("trigger") or {}).get("on_request") or {}
        inputs_list = on_request_cfg.get("inputs") or []
        input_schema = _build_input_schema(inputs_list)

        description = (
            (cfg.get("skill_card") or {}).get("use_when")
            or (cfg.get("skill_card") or {}).get("summary")
            or f"Workflow skill {skill_name} for persona {persona}"
        ).strip()

        tool = WorkflowMCPTool(
            name=_tool_name(persona, skill_name),
            persona=persona,
            skill_name=skill_name,
            description=description,
            input_schema=input_schema,
            skill_config=cfg,
            _path=str(path),
        )
        out.append(tool)
    return out


def register_workflow_skills_as_mcp_tools(
    skills_dir: str | Path = "framework/workflow_skills",
) -> dict[str, WorkflowMCPTool]:
    """Return keyed registry {tool_name: WorkflowMCPTool} for on_request skills.

    Only skills with trigger.on_request.enabled=true are included.
    Called at MCP server startup per ADR-016.
    """
    workflow_skills_dir = Path(skills_dir)
    all_skills = discover_workflow_skills(workflow_skills_dir)
    registry: dict[str, WorkflowMCPTool] = {}
    for tool in all_skills:
        on_request = (tool.skill_config.get("trigger") or {}).get("on_request") or {}
        if on_request.get("enabled"):
            registry[tool.name] = tool
            log.info("registered workflow MCP tool: %s", tool.name)
        else:
            log.debug("skill %s has no on_request trigger; not registered as MCP tool", tool.skill_name)
    return registry
