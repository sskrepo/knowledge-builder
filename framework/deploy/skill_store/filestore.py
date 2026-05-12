"""FilestoreSkillStore — filesystem-backed implementation for tests and CI.

NOTE: Production and laptop both use AdbSkillStore. This class exists only
for unit tests that need a concrete SkillStore without a real DB connection.
Do NOT use build_skill_store() with pool=None — it raises ValueError.
Instantiate FilestoreSkillStore directly in tests.

Layout under REPO_ROOT:
  framework/workflow_skills/{persona}/{skill_name}.yaml            (workflow_skill)
  framework/persona_builders/{persona}.yaml.new_kb                 (persona_builder_delta)
  eval/gold_sets/{persona}-{skill_name}-extraction.jsonl           (eval_extraction)
  eval/gold_sets/{persona}-{skill_name}-workflow.jsonl             (eval_workflow)
  framework/parsers/schemas/{persona}/{skill_name}/v1.json         (extraction_schema)

read_artifact loads the file back from disk.
promote is a no-op in filestore mode (no status column).
list_skills scans REPO_ROOT for workflow_skills/*.yaml.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from ._base import ARTIFACT_TYPES, SkillStore, make_artifact_id

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

# Maps artifact_type → relative path template (filled at runtime).
_REL_PATH_TEMPLATES: dict[str, str] = {
    "workflow_skill":         "framework/workflow_skills/{persona}/{skill_name}.yaml",
    "persona_builder_delta":  "framework/persona_builders/{persona}.yaml.new_kb",
    "eval_extraction":        "eval/gold_sets/{persona}-{skill_name}-extraction.jsonl",
    "eval_workflow":          "eval/gold_sets/{persona}-{skill_name}-workflow.jsonl",
    "extraction_schema":      "framework/parsers/schemas/{persona}/{skill_name}/v1.json",
}


def _rel_path(persona: str, skill_name: str, artifact_type: str) -> str:
    return _REL_PATH_TEMPLATES[artifact_type].format(
        persona=persona, skill_name=skill_name
    )


class FilestoreSkillStore(SkillStore):
    """Filesystem-backed skill store — laptop / CI fallback."""

    def __init__(self, repo_root: Path | str | None = None) -> None:
        self._root = Path(repo_root) if repo_root else REPO_ROOT

    # ------------------------------------------------------------------
    # SkillStore interface
    # ------------------------------------------------------------------

    def write_artifacts(
        self,
        synth_id: str,
        persona: str,
        skill_name: str,
        artifacts: dict[str, str],
    ) -> None:
        for artifact_type, content in artifacts.items():
            if artifact_type not in ARTIFACT_TYPES:
                raise ValueError(
                    f"Unknown artifact_type {artifact_type!r}; expected one of {sorted(ARTIFACT_TYPES)}"
                )
            rel = _rel_path(persona, skill_name, artifact_type)
            full = self._root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            log.info(
                "FilestoreSkillStore.write: synth_id=%s artifact=%s path=%s",
                synth_id, artifact_type, full,
            )

    def read_artifact(
        self,
        persona: str,
        skill_name: str,
        artifact_type: str,
    ) -> str | None:
        if artifact_type not in ARTIFACT_TYPES:
            return None
        rel = _rel_path(persona, skill_name, artifact_type)
        full = self._root / rel
        if not full.exists():
            log.debug(
                "FilestoreSkillStore.read: not found — %s", full
            )
            return None
        try:
            return full.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("FilestoreSkillStore.read: error reading %s: %s", full, exc)
            return None

    def promote(self, persona: str, skill_name: str) -> None:
        # Filesystem has no status column; promote is a no-op here.
        log.info(
            "FilestoreSkillStore.promote: no-op for %s.%s (filesystem mode)",
            persona, skill_name,
        )

    def list_skills(self, persona: str | None = None) -> list[dict]:
        skills_dir = self._root / "framework" / "workflow_skills"
        results: list[dict] = []
        if not skills_dir.exists():
            return results

        search_dirs = (
            [skills_dir / persona] if persona else list(skills_dir.iterdir())
        )

        for persona_dir in search_dirs:
            if not persona_dir.is_dir():
                continue
            p_name = persona_dir.name
            for skill_file in sorted(persona_dir.glob("*.yaml")):
                if skill_file.name.startswith("_"):
                    continue
                skill_n = skill_file.stem
                # Count how many of the 4 artifact files actually exist
                count = sum(
                    1
                    for at in ARTIFACT_TYPES
                    if (self._root / _rel_path(p_name, skill_n, at)).exists()
                )
                try:
                    mtime = skill_file.stat().st_mtime
                    from datetime import datetime, timezone
                    updated = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                except OSError:
                    updated = ""

                results.append({
                    "persona":        p_name,
                    "skill_name":     skill_n,
                    "status":         "draft",   # filesystem has no status
                    "artifact_count": count,
                    "updated_at":     updated,
                })

        return results

    def delete(self, persona: str, skill_name: str) -> list[str]:
        deleted_types: list[str] = []
        for artifact_type in ARTIFACT_TYPES:
            rel = _rel_path(persona, skill_name, artifact_type)
            full = self._root / rel
            if full.exists():
                try:
                    full.unlink()
                    deleted_types.append(artifact_type)
                    log.info(
                        "FilestoreSkillStore.delete: removed %s", full
                    )
                except OSError as exc:
                    log.warning(
                        "FilestoreSkillStore.delete: could not remove %s: %s", full, exc
                    )
        return deleted_types
