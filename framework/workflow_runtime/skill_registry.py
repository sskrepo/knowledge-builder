"""Skill registry — scans framework/workflow_skills/ and exposes on_request as MCP tools."""
from __future__ import annotations
import logging
from pathlib import Path
import yaml

log = logging.getLogger(__name__)


def discover_workflow_skills(workflow_skills_dir: Path) -> list[dict]:
    """Returns list of {name, persona, path, on_request, on_schedule, ...} for each skill."""
    out = []
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
        out.append({
            "name": cfg.get("workflow_skill"),
            "persona": cfg.get("persona"),
            "path": str(path),
            "on_request": bool((cfg.get("trigger") or {}).get("on_request", {}).get("enabled")),
            "on_schedule": bool((cfg.get("trigger") or {}).get("on_schedule", {}).get("cron")),
            "status": cfg.get("status", "draft"),
        })
    return out
