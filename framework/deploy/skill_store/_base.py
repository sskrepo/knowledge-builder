"""SkillStore ABC — durable storage for synthesized skill artifacts.

Implements DECISION-006 Option A: Oracle ADB as the primary artifact store
for skill authoring sessions, with a filesystem fallback for laptop mode.

Artifact types (5 per skill):
  workflow_skill          — framework/workflow_skills/{persona}/{skill_name}.yaml
  persona_builder_delta   — framework/persona_builders/{persona}.yaml.new_kb entry
  eval_extraction         — eval/gold_sets/{persona}-{skill_name}-extraction.jsonl
  eval_workflow           — eval/gold_sets/{persona}-{skill_name}-workflow.jsonl
  extraction_schema       — framework/parsers/schemas/{persona}/{skill_name}/v1.json

artifact_id (primary key):
  "{persona}.{skill_name}.{artifact_type}"
"""
from __future__ import annotations

from abc import ABC, abstractmethod

ARTIFACT_TYPES = frozenset({
    "workflow_skill",
    "persona_builder_delta",
    "eval_extraction",
    "eval_workflow",
    "extraction_schema",      # framework/parsers/schemas/{persona}/{skill_name}/v1.json
})


def make_artifact_id(persona: str, skill_name: str, artifact_type: str) -> str:
    """Construct the canonical artifact_id primary key."""
    return f"{persona}.{skill_name}.{artifact_type}"


class SkillStore(ABC):
    """Abstract base class for skill artifact storage.

    Implementations:
      FilestoreSkillStore — writes 4 files under REPO_ROOT (laptop fallback)
      AdbSkillStore       — MERGE INTO KBF_SKILL_ARTIFACTS (staging / production)

    All methods are synchronous (blocking I/O).
    """

    @abstractmethod
    def write_artifacts(
        self,
        synth_id: str,
        persona: str,
        skill_name: str,
        artifacts: dict[str, str],
    ) -> None:
        """Persist all 4 synthesized artifacts for a skill authoring session.

        Args:
            synth_id:   Session ID (for the synth_id FK column).
            persona:    Persona slug (e.g. "ops_eng").
            skill_name: Skill slug (e.g. "weekly_incident_summary").
            artifacts:  Mapping of {artifact_type: text_content}.
                        Keys must be a subset of ARTIFACT_TYPES.
                        The rel_path is derived from the artifact_type.

        Raises:
            ValueError: if an artifact_type is not in ARTIFACT_TYPES.
        """

    @abstractmethod
    def read_artifact(
        self,
        persona: str,
        skill_name: str,
        artifact_type: str,
    ) -> str | None:
        """Return text content for a single artifact, or None if not found.

        Args:
            persona:       Persona slug.
            skill_name:    Skill slug.
            artifact_type: One of the 4 ARTIFACT_TYPES.

        Returns:
            Content string, or None if the artifact does not exist.
        """

    @abstractmethod
    def promote(self, persona: str, skill_name: str) -> None:
        """Set status='promoted' for all artifacts of this skill.

        Args:
            persona:    Persona slug.
            skill_name: Skill slug.
        """

    @abstractmethod
    def list_skills(self, persona: str | None = None) -> list[dict]:
        """Return a summary list of stored skills.

        Args:
            persona: Optional filter by persona.

        Returns:
            List of dicts, each with keys:
              persona, skill_name, status, artifact_count, updated_at
        """

    @abstractmethod
    def delete(self, persona: str, skill_name: str) -> list[str]:
        """Hard-delete all stored artifacts for a skill.

        This is a destructive, irreversible operation. The caller is responsible
        for confirming the deletion password before invoking this method.

        Args:
            persona:    Persona slug (e.g. "ops_eng").
            skill_name: Skill slug (e.g. "weekly_incident_summary").

        Returns:
            List of artifact_type strings that were deleted (may be empty if skill
            was not found or had no artifacts).
        """
