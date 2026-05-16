"""validate_links — promote-time validation per ADR-017.

Checks that a workflow skill's required_fields are all covered by the linked
KBs' provides_fields, AND that the workflow's owning persona is in each linked
KB's persona_visibility ACL.

Returns a list of error strings (empty = valid). Called by kb-cli promote.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def validate_workflow_links(
    workflow_skill_path: str,
    persona_builders_dir: str,
) -> list[str]:
    """Validate that a workflow skill's required_fields are covered by linked KBs.

    Checks per ADR-017:
    1. Workflow's required_fields ⊆ union of linked KBs' provides_fields.
    2. Workflow's owning persona is in each linked KB's persona_visibility.

    Args:
        workflow_skill_path: path to a workflow_skill YAML file.
        persona_builders_dir: path to the directory containing persona builder YAMLs.

    Returns:
        List of error strings. Empty list means valid.
    """
    wf_path = Path(workflow_skill_path)
    if not wf_path.exists():
        return [f"workflow skill file does not exist: {wf_path}"]

    try:
        cfg = yaml.safe_load(wf_path.read_text()) or {}
    except yaml.YAMLError as e:
        return [f"failed to parse workflow skill YAML {wf_path}: {e}"]

    workflow_persona = cfg.get("persona")
    if not workflow_persona:
        return [f"workflow skill {wf_path} is missing 'persona' field"]

    kb_index = _build_kb_index(Path(persona_builders_dir))
    errors: list[str] = []

    for req in cfg.get("requires_extractions", []):
        kb_ref = req.get("kb")
        if not kb_ref:
            errors.append("requires_extractions entry missing 'kb' key")
            continue

        kb_entry = _find_kb(kb_index, kb_ref)
        if kb_entry is None:
            errors.append(
                f"workflow references unknown KB: {kb_ref!r}. "
                f"Ensure the persona builder YAML exists and the KB name matches."
            )
            continue

        required = set(req.get("required_fields", []))
        provided = set(kb_entry.get("provides_fields", []))
        missing = required - provided

        if missing:
            errors.append(
                f"workflow requires fields not provided by {kb_ref!r}: {sorted(missing)}. "
                f"Either add them to the extraction schema's provides_fields or remove "
                f"them from required_fields."
            )

        visibility: list[str] = (
            kb_entry.get("_persona_visibility", [])
        )
        if visibility and workflow_persona not in visibility:
            owning = kb_entry.get("_owning_persona", kb_ref.split(".")[0])
            errors.append(
                f"workflow's persona {workflow_persona!r} is not in "
                f"{kb_ref!r}'s persona_visibility {visibility}. "
                f"Request access from {owning!r} or rescope the workflow."
            )

    return errors


# ---------------------------------------------------------------------------
# private helpers — build a KB index from persona builder YAMLs
# ---------------------------------------------------------------------------

def _build_kb_index(builders_dir: Path) -> dict[str, dict]:
    """Return a flat dict mapping 'persona.kb_name' → augmented KB entry dict.

    The entry dict has extra private keys:
    - _owning_persona: the persona that authors this KB
    - _persona_visibility: the read-ACL list
    """
    index: dict[str, dict] = {}
    # Also pick up *.yaml.new_kb files — these are in-session persona builder
    # candidates written by COMMIT before they are promoted to *.yaml.  The
    # *.yaml glob does NOT match *.yaml.new_kb (different suffix), so without
    # this line a newly authored KB is invisible to the validator when the ADB
    # skill_store fallback is unavailable.  (BUG-queue-51dd3 / BUG-queue-3d13e
    # / BUG-queue-1b0c0 — belt-and-suspenders fix.)
    candidate_paths = sorted(builders_dir.glob("*.yaml")) + sorted(
        builders_dir.glob("*.yaml.new_kb")
    )
    for yaml_path in candidate_paths:
        if yaml_path.name.startswith("_"):
            continue
        try:
            cfg = yaml.safe_load(yaml_path.read_text()) or {}
        except yaml.YAMLError as e:
            log.warning("failed to load %s: %s", yaml_path, e)
            continue

        # *.yaml.new_kb files are raw KB entry dicts written by COMMIT
        # (not full persona builders).  Wrap them so the index can parse them.
        # e.g. "tpm.yaml.new_kb" → persona="tpm", knowledge_bases=[<entry>]
        if yaml_path.name.endswith(".yaml.new_kb") and "name" in cfg and "persona" not in cfg:
            persona_name = yaml_path.name.split(".")[0]
            cfg = {"persona": persona_name, "knowledge_bases": [cfg]}

        persona = cfg.get("persona")
        if not persona:
            continue

        visibility: list[str] = (
            (cfg.get("metadata_defaults") or {}).get("persona_visibility") or [persona]
        )

        for kb in cfg.get("knowledge_bases", []):
            kb_name = kb.get("name")
            if not kb_name:
                continue
            qualified = f"{persona}.{kb_name}"
            entry = dict(kb)
            entry["_owning_persona"] = persona
            entry["_persona_visibility"] = visibility
            index[qualified] = entry

    return index


def _find_kb(index: dict[str, dict], kb_ref: str) -> dict | None:
    """Lookup by 'persona.kb_name' (e.g. 'tpm.weekly_project_status').

    Defensive fallback: if kb_ref is a bare name (no '.'), resolve it
    against any 'persona.<kb_ref>' key. DESIGN_SKILL reuse plans
    historically emitted bare KB names; the synthesizer now
    persona-qualifies them, but tolerate bare refs so older artifacts
    don't fail the link check spuriously.

    Returns None if not found.
    """
    hit = index.get(kb_ref)
    if hit is not None:
        return hit
    if "." not in kb_ref:
        for key, entry in index.items():
            if key.rsplit(".", 1)[-1] == kb_ref:
                return entry
    return None
