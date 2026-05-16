"""
ADR-032 P1-E — source_binding YAML validation tests.

Verifies that:
- The 4 TPM project-tracking email skills have source_binding.mode == ask_parameterized,
  ingest_on_demand True, input_param that matches a declared trigger input name, and
  a non-empty space_allow_list.
- The 3 non-email TPM skills (26ai_confluence_pptx, 26ai_fa_db_upgrade_pptx,
  weekly_exec_review) have NO source_binding block (author_fixed default).
"""

import pathlib
import yaml
import pytest

SKILLS_DIR = pathlib.Path(__file__).parents[2] / "workflow_skills" / "tpm"

EMAIL_SKILLS = [
    "project_tracking_confluence_stakeholder_status_meeting_email.yaml",
    "project_tracking_stakeholder_status_email.yaml",
    "project_tracking_stakeholder_tracking_meeting_email.yaml",
    "project_tracking_weekly_stakeholder_status_email.yaml",
]

NON_EMAIL_SKILLS = [
    "26ai_confluence_pptx.yaml",
    "26ai_fa_db_upgrade_pptx.yaml",
    "weekly_exec_review.yaml",
]


def _load(filename: str) -> dict:
    path = SKILLS_DIR / filename
    assert path.exists(), f"Skill YAML not found: {path}"
    cfg = yaml.safe_load(path.read_text())
    assert isinstance(cfg, dict), f"YAML root is not a mapping in {filename}"
    return cfg


# ── Email skill assertions ────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_source_binding_mode(filename):
    """source_binding.mode must be ask_parameterized."""
    cfg = _load(filename)
    sb = cfg.get("source_binding")
    assert sb is not None, f"{filename}: missing source_binding block"
    assert sb.get("mode") == "ask_parameterized", (
        f"{filename}: expected mode=ask_parameterized, got {sb.get('mode')!r}"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_ingest_on_demand_true(filename):
    """source_binding.ingest_on_demand must be True."""
    cfg = _load(filename)
    sb = cfg["source_binding"]
    assert sb.get("ingest_on_demand") is True, (
        f"{filename}: ingest_on_demand must be True, got {sb.get('ingest_on_demand')!r}"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_input_param_matches_trigger_input(filename):
    """source_binding.input_param must match a declared trigger.on_request.inputs entry."""
    cfg = _load(filename)
    sb = cfg["source_binding"]
    input_param = sb.get("input_param")
    assert input_param, f"{filename}: source_binding.input_param is missing or empty"

    trigger_inputs = (
        cfg.get("trigger", {})
        .get("on_request", {})
        .get("inputs", [])
    )
    declared_names = [inp.get("name") for inp in trigger_inputs]
    assert input_param in declared_names, (
        f"{filename}: source_binding.input_param={input_param!r} not found in "
        f"trigger.on_request.inputs names {declared_names}"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_space_allow_list_non_empty(filename):
    """source_binding.space_allow_list must be a non-empty list."""
    cfg = _load(filename)
    sb = cfg["source_binding"]
    sal = sb.get("space_allow_list")
    assert sal and isinstance(sal, list) and len(sal) > 0, (
        f"{filename}: space_allow_list must be a non-empty list, got {sal!r}"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_source_type_confluence_page(filename):
    """source_binding.source_type must be confluence_page."""
    cfg = _load(filename)
    sb = cfg["source_binding"]
    assert sb.get("source_type") == "confluence_page", (
        f"{filename}: source_type must be confluence_page, got {sb.get('source_type')!r}"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_ephemeral_ttl_seconds(filename):
    """source_binding.ephemeral_ttl_seconds must be 300."""
    cfg = _load(filename)
    sb = cfg["source_binding"]
    assert sb.get("ephemeral_ttl_seconds") == 300, (
        f"{filename}: ephemeral_ttl_seconds must be 300, got {sb.get('ephemeral_ttl_seconds')!r}"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_trigger_input_is_page_id(filename):
    """The first trigger input must be named page_id with type confluence_page_ref."""
    cfg = _load(filename)
    inputs = (
        cfg.get("trigger", {})
        .get("on_request", {})
        .get("inputs", [])
    )
    assert inputs, f"{filename}: trigger.on_request.inputs is empty"
    first = inputs[0]
    assert first.get("name") == "page_id", (
        f"{filename}: first trigger input name must be page_id, got {first.get('name')!r}"
    )
    assert first.get("type") == "confluence_page_ref", (
        f"{filename}: first trigger input type must be confluence_page_ref, "
        f"got {first.get('type')!r}"
    )
    assert first.get("required") is True, (
        f"{filename}: first trigger input must have required=true"
    )


@pytest.mark.parametrize("filename", EMAIL_SKILLS)
def test_email_skill_old_generic_input_removed(filename):
    """The old generic {name:input, type:string} input must NOT be present."""
    cfg = _load(filename)
    inputs = (
        cfg.get("trigger", {})
        .get("on_request", {})
        .get("inputs", [])
    )
    for inp in inputs:
        if inp.get("name") == "input" and inp.get("type") == "string":
            pytest.fail(
                f"{filename}: old generic input {{name:input, type:string}} still present; "
                "must be replaced with {name:page_id, type:confluence_page_ref}"
            )


# ── Non-email skill assertions (author_fixed — no source_binding) ─────────────

@pytest.mark.parametrize("filename", NON_EMAIL_SKILLS)
def test_non_email_skill_has_no_source_binding(filename):
    """Non-email skills must NOT have a source_binding block (author_fixed default)."""
    cfg = _load(filename)
    sb = cfg.get("source_binding")
    assert sb is None, (
        f"{filename}: non-email skill must not have source_binding (author_fixed default), "
        f"but found: {sb!r}"
    )


# ── YAML smoke test for all tpm skills ───────────────────────────────────────

def test_all_tpm_skill_yamls_parse():
    """Every *.yaml in workflow_skills/tpm/ must parse without error."""
    yamls = list(SKILLS_DIR.glob("*.yaml"))
    assert yamls, f"No YAML files found in {SKILLS_DIR}"
    for path in yamls:
        try:
            cfg = yaml.safe_load(path.read_text())
            assert isinstance(cfg, dict), f"{path.name}: YAML root is not a mapping"
        except yaml.YAMLError as exc:
            pytest.fail(f"{path.name}: YAML parse error: {exc}")
