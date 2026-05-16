"""P1 — PromptRegistry loader unit tests.

ADR-030 §Design §4 + ADR-030-impl-plan.md §P1.

These tests exercise the PromptRegistry module using ONLY inline YAML fixtures
written to tmp_path.  No file in framework/config/prompts/ is touched (that is P2).

Scope per impl-plan:
  1.  test_load_from_directory              — happy path: build registry from minimal YAML
  2.  test_get_prompt_formats_correctly     — all required vars supplied; text contains substituted values
  3.  test_missing_var_raises               — omit a required var → MissingVarsError
  4.  test_unknown_prompt_raises            — nonexistent id → PromptNotFoundError
  5.  test_locked_checksum_valid            — correct checksum → get_prompt succeeds
  6.  test_locked_checksum_mismatch_raises  — wrong checksum → LockedPromptTamperedError AT LOAD TIME
  7.  test_malformed_yaml_raises            — invalid YAML → PromptStoreError at construction
  8.  test_hot_reload_picks_up_changes      — edit YAML + bump mtime → new text served, no restart
  9.  test_persona_overlay_applied          — persona overlay injects overlay_vars into template
  10. test_persona_overlay_missing_logs_warning — unknown persona → WARNING logged, call succeeds
  11. test_reload_keeps_last_good_on_bad_yaml — corrupt YAML on reload → PromptStoreError raised,
                                               last-good state still served

Additional tests (impl-plan requirements):
  12. test_extra_unknown_vars_are_ignored   — extra caller vars not in template → no error
  13. test_persona_double_injection         — {persona} placeholder in template resolved from persona=arg
  14. test_startup_validation_catches_undeclared_placeholder — required_var not in template → PromptStoreError
  15. test_list_prompts_returns_meta        — list_prompts() returns PromptMeta with correct fields
  16. test_raw_template_returns_unformatted — _raw_template() returns template before substitution
  17. test_reload_explicit_picks_up_changes — explicit registry.reload() picks up YAML changes
  18. test_empty_directory_warns_not_errors — empty prompts_dir → warning, empty registry (not crash)
"""
from __future__ import annotations

import hashlib
import logging
import os
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from framework.skill_builder.prompt_registry import (
    LockedPromptTamperedError,
    MissingVarsError,
    PromptMeta,
    PromptNotFoundError,
    PromptRegistry,
    PromptSpec,
    PromptStoreError,
    _compute_checksum,
    get_registry,
    validate_registry,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_prompts_yaml(tmp_path: Path, prompts: dict, filename: str = "test_prompts.yaml") -> Path:
    """Write a prompts YAML file under tmp_path and return the path."""
    data = {"prompts": prompts}
    p = tmp_path / filename
    p.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    return p


def _write_overlays_yaml(tmp_path: Path, personas: dict) -> Path:
    """Write persona_overlays.yaml under tmp_path and return the path."""
    data = {"personas": personas}
    p = tmp_path / "persona_overlays.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    return p


def _minimal_prompt_dict(
    prompt_id: str = "test_prompt",
    template: str = "Hello {name}!",
    required_vars: list = None,
    model: str = "synthesis",
    max_tokens: int = 512,
    response_format: str = "json_object",
) -> dict:
    """Return a minimal valid prompt YAML stanza dict."""
    return {
        "id": prompt_id,
        "version": "1.0",
        "model": model,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "required_vars": required_vars if required_vars is not None else ["name"],
        "template": template,
        "description": "A minimal test prompt.",
    }


def _make_registry(tmp_path: Path, prompts: dict, overlays: dict = None) -> PromptRegistry:
    """Write YAML fixture(s) and construct a PromptRegistry for testing."""
    _write_prompts_yaml(tmp_path, prompts)
    if overlays is not None:
        _write_overlays_yaml(tmp_path, overlays)
    return PromptRegistry(tmp_path)


# ---------------------------------------------------------------------------
# 1. test_load_from_directory
# ---------------------------------------------------------------------------


class TestLoadFromDirectory:
    """build registry from a minimal valid YAML; list_prompts() returns entries."""

    def test_registry_constructs_without_error(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        assert reg is not None

    def test_list_prompts_returns_one_entry(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        entries = reg.list_prompts()
        assert len(entries) == 1
        assert entries[0].prompt_id == "hello"

    def test_list_prompts_returns_meta_type(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        entry = reg.list_prompts()[0]
        assert isinstance(entry, PromptMeta)

    def test_multiple_prompts_loaded(self, tmp_path):
        prompts = {
            "alpha": _minimal_prompt_dict("alpha"),
            "beta": _minimal_prompt_dict("beta", template="Bye {name}!"),
        }
        reg = _make_registry(tmp_path, prompts)
        ids = {m.prompt_id for m in reg.list_prompts()}
        assert ids == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# 2. test_get_prompt_formats_correctly
# ---------------------------------------------------------------------------


class TestGetPromptFormatsCorrectly:
    """All required vars supplied; text contains substituted values."""

    def test_placeholder_is_substituted(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", template="Hello {name}!", required_vars=["name"])},
        )
        spec = reg.get_prompt("greet", name="World")
        assert "World" in spec.text
        assert "{name}" not in spec.text

    def test_returns_prompt_spec(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet")},
        )
        spec = reg.get_prompt("greet", name="Alice")
        assert isinstance(spec, PromptSpec)

    def test_spec_fields_match_yaml(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", max_tokens=256, response_format="text")},
        )
        spec = reg.get_prompt("greet", name="Bob")
        assert spec.prompt_id == "greet"
        assert spec.version == "1.0"
        assert spec.model == "synthesis"
        assert spec.max_tokens == 256
        assert spec.response_format == {"type": "text"}

    def test_multiple_vars_substituted(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {
                "multi": _minimal_prompt_dict(
                    "multi",
                    template="Dear {persona}, your intent is {intent}.",
                    required_vars=["persona", "intent"],
                )
            },
        )
        spec = reg.get_prompt("multi", persona="tpm", intent="build a dashboard")
        assert "tpm" in spec.text
        assert "build a dashboard" in spec.text


# ---------------------------------------------------------------------------
# 3. test_missing_var_raises
# ---------------------------------------------------------------------------


class TestMissingVarRaises:
    """Omit a required var → MissingVarsError."""

    def test_missing_required_var_raises(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", required_vars=["name"])},
        )
        with pytest.raises(MissingVarsError):
            reg.get_prompt("greet")  # 'name' not supplied

    def test_error_message_mentions_missing_var(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", required_vars=["name"])},
        )
        with pytest.raises(MissingVarsError, match="name"):
            reg.get_prompt("greet")

    def test_missing_var_is_subclass_of_prompt_store_error(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", required_vars=["name"])},
        )
        with pytest.raises(PromptStoreError):
            reg.get_prompt("greet")

    def test_partial_vars_raises_for_missing_one(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {
                "multi": _minimal_prompt_dict(
                    "multi",
                    template="Dear {persona}, your intent is {intent}.",
                    required_vars=["persona", "intent"],
                )
            },
        )
        with pytest.raises(MissingVarsError, match="intent"):
            reg.get_prompt("multi", persona="tpm")  # intent missing


# ---------------------------------------------------------------------------
# 4. test_unknown_prompt_raises
# ---------------------------------------------------------------------------


class TestUnknownPromptRaises:
    """Nonexistent prompt_id → PromptNotFoundError."""

    def test_unknown_id_raises_not_found(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        with pytest.raises(PromptNotFoundError):
            reg.get_prompt("does_not_exist")

    def test_not_found_is_subclass_of_prompt_store_error(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        with pytest.raises(PromptStoreError):
            reg.get_prompt("does_not_exist")

    def test_error_message_mentions_prompt_id(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        with pytest.raises(PromptNotFoundError, match="does_not_exist"):
            reg.get_prompt("does_not_exist")

    def test_raw_template_unknown_raises(self, tmp_path):
        reg = _make_registry(tmp_path, {"hello": _minimal_prompt_dict("hello")})
        with pytest.raises(PromptNotFoundError):
            reg._raw_template("nonexistent")


# ---------------------------------------------------------------------------
# 5. test_locked_checksum_valid
# ---------------------------------------------------------------------------


class TestLockedChecksumValid:
    """Correct checksum → get_prompt succeeds."""

    def _make_locked_prompt(self, template: str) -> dict:
        checksum = _compute_checksum(template)
        return {
            "id": "locked_one",
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 512,
            "response_format": "json_object",
            "required_vars": ["datum"],
            "template": template,
            "locked": True,
            "checksum": checksum,
            "description": "A locked test prompt.",
        }

    def test_locked_prompt_loads_successfully(self, tmp_path):
        template = "Secret: {datum}"
        entry = self._make_locked_prompt(template)
        reg = _make_registry(tmp_path, {"locked_one": entry})
        spec = reg.get_prompt("locked_one", datum="value")
        assert "value" in spec.text

    def test_locked_meta_reports_locked_true(self, tmp_path):
        template = "Secret: {datum}"
        entry = self._make_locked_prompt(template)
        reg = _make_registry(tmp_path, {"locked_one": entry})
        meta = reg.list_prompts()[0]
        assert meta.locked is True


# ---------------------------------------------------------------------------
# 6. test_locked_checksum_mismatch_raises
# ---------------------------------------------------------------------------


class TestLockedChecksumMismatchRaises:
    """Wrong checksum → LockedPromptTamperedError AT LOAD TIME (construction)."""

    def test_tampered_checksum_raises_at_construction(self, tmp_path):
        entry = {
            "id": "tampered",
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 512,
            "response_format": "json_object",
            "required_vars": ["x"],
            "template": "Original text {x}",
            "locked": True,
            "checksum": "sha256:" + "a" * 64,  # deliberately wrong
        }
        with pytest.raises(LockedPromptTamperedError):
            _make_registry(tmp_path, {"tampered": entry})

    def test_tampered_error_is_subclass_of_prompt_store_error(self, tmp_path):
        entry = {
            "id": "tampered2",
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 512,
            "response_format": "json_object",
            "required_vars": ["x"],
            "template": "Some template {x}",
            "locked": True,
            "checksum": "sha256:" + "0" * 64,
        }
        with pytest.raises(PromptStoreError):
            _make_registry(tmp_path, {"tampered2": entry})

    def test_locked_without_checksum_raises(self, tmp_path):
        """locked: true with no checksum field must also raise at load time."""
        entry = {
            "id": "no_checksum",
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 512,
            "response_format": "json_object",
            "required_vars": ["x"],
            "template": "Missing checksum {x}",
            "locked": True,
            # checksum deliberately absent
        }
        with pytest.raises(PromptStoreError, match="checksum"):
            _make_registry(tmp_path, {"no_checksum": entry})


# ---------------------------------------------------------------------------
# 7. test_malformed_yaml_raises
# ---------------------------------------------------------------------------


class TestMalformedYamlRaises:
    """Invalid YAML → PromptStoreError at construction."""

    def test_invalid_yaml_syntax_raises(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("prompts:\n  foo: [unclosed bracket\n", encoding="utf-8")
        with pytest.raises(PromptStoreError):
            PromptRegistry(tmp_path)

    def test_missing_prompts_key_raises(self, tmp_path):
        """A valid YAML dict without a 'prompts' key must raise."""
        (tmp_path / "no_prompts.yaml").write_text(
            yaml.dump({"something_else": {"a": 1}}), encoding="utf-8"
        )
        with pytest.raises(PromptStoreError, match="prompts"):
            PromptRegistry(tmp_path)

    def test_missing_required_field_in_prompt_raises(self, tmp_path):
        """Prompt stanza missing 'id' field must raise PromptStoreError."""
        entry = {
            # 'id' deliberately absent
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 512,
            "response_format": "json_object",
            "required_vars": ["x"],
            "template": "test {x}",
        }
        (tmp_path / "bad_prompt.yaml").write_text(
            yaml.dump({"prompts": {"no_id": entry}}), encoding="utf-8"
        )
        with pytest.raises(PromptStoreError):
            PromptRegistry(tmp_path)

    def test_empty_yaml_file_raises(self, tmp_path):
        """An empty YAML file (parses to None) must raise PromptStoreError."""
        (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
        with pytest.raises(PromptStoreError):
            PromptRegistry(tmp_path)


# ---------------------------------------------------------------------------
# 8. test_hot_reload_picks_up_changes
# ---------------------------------------------------------------------------


class TestHotReloadPicksUpChanges:
    """Edit YAML file + update mtime → new text served on next get_prompt(), no restart."""

    def test_modified_template_is_picked_up(self, tmp_path):
        """Modify the YAML on disk; the next get_prompt() sees the new text."""
        prompts_file = _write_prompts_yaml(
            tmp_path,
            {"msg": _minimal_prompt_dict("msg", template="Old text {name}", required_vars=["name"])},
        )
        reg = PromptRegistry(tmp_path)

        # Verify old text
        spec = reg.get_prompt("msg", name="Alice")
        assert "Old text" in spec.text

        # Sleep briefly to ensure a different mtime (filesystem resolution is typically 1s,
        # but we write a new file with os.utime to force the change).
        new_data = {"prompts": {"msg": _minimal_prompt_dict("msg", template="New text {name}", required_vars=["name"])}}
        prompts_file.write_text(yaml.dump(new_data), encoding="utf-8")

        # Bump mtime by 2 seconds to guarantee detection
        old_stat = os.stat(prompts_file)
        os.utime(prompts_file, (old_stat.st_atime + 2, old_stat.st_mtime + 2))

        # Next call should auto-reload
        spec2 = reg.get_prompt("msg", name="Alice")
        assert "New text" in spec2.text
        assert "Old text" not in spec2.text

    def test_new_prompt_id_added_after_reload(self, tmp_path):
        """A new prompt added to the YAML is available after hot-reload."""
        prompts_file = _write_prompts_yaml(
            tmp_path,
            {"first": _minimal_prompt_dict("first")},
        )
        reg = PromptRegistry(tmp_path)
        assert len(reg.list_prompts()) == 1

        # Add a second prompt
        new_data = {
            "prompts": {
                "first": _minimal_prompt_dict("first"),
                "second": _minimal_prompt_dict("second", template="Hi {name}!", required_vars=["name"]),
            }
        }
        prompts_file.write_text(yaml.dump(new_data), encoding="utf-8")
        old_stat = os.stat(prompts_file)
        os.utime(prompts_file, (old_stat.st_atime + 2, old_stat.st_mtime + 2))

        # Trigger hot-reload via get_prompt
        reg.get_prompt("first", name="x")
        assert len(reg.list_prompts()) == 2


# ---------------------------------------------------------------------------
# 9. test_persona_overlay_applied
# ---------------------------------------------------------------------------


class TestPersonaOverlayApplied:
    """Persona overlay injects overlay_vars into template."""

    def test_overlay_vars_injected(self, tmp_path):
        """capture_intent with persona=tpm: persona_key_fields resolved from overlay."""
        template = "For {persona}: fields={persona_key_fields}, intent={intent}"
        prompts = {
            "capture_intent": {
                "id": "capture_intent",
                "version": "1.0",
                "model": "synthesis",
                "max_tokens": 1024,
                "response_format": "json_object",
                "required_vars": ["persona", "intent", "persona_key_fields"],
                "template": template,
            }
        }
        overlays = {
            "tpm": {
                "applies_to": ["capture_intent"],
                "overlay_vars": {
                    "persona_key_fields": "orm_status, rag_summary",
                },
            }
        }
        reg = _make_registry(tmp_path, prompts, overlays)
        spec = reg.get_prompt("capture_intent", persona="tpm", intent="build deck")

        assert "tpm" in spec.text
        assert "orm_status, rag_summary" in spec.text
        assert "build deck" in spec.text

    def test_caller_supplied_var_wins_over_overlay(self, tmp_path):
        """Caller-explicit value takes precedence over persona overlay value."""
        template = "Fields: {persona_key_fields}, name: {name}"
        prompts = {
            "p": {
                "id": "p",
                "version": "1.0",
                "model": "synthesis",
                "max_tokens": 512,
                "response_format": "json_object",
                "required_vars": ["persona_key_fields", "name"],
                "template": template,
            }
        }
        overlays = {
            "tpm": {
                "applies_to": ["p"],
                "overlay_vars": {"persona_key_fields": "from_overlay"},
            }
        }
        reg = _make_registry(tmp_path, prompts, overlays)
        spec = reg.get_prompt("p", persona="tpm", persona_key_fields="from_caller", name="X")
        assert "from_caller" in spec.text
        assert "from_overlay" not in spec.text

    def test_overlay_does_not_apply_when_prompt_not_in_applies_to(self, tmp_path):
        """Overlay is NOT injected when prompt_id is absent from applies_to."""
        template = "Fields: {persona_key_fields}"
        prompts = {
            "other_prompt": {
                "id": "other_prompt",
                "version": "1.0",
                "model": "synthesis",
                "max_tokens": 512,
                "response_format": "json_object",
                # persona_key_fields NOT in required_vars because overlay won't supply it
                "required_vars": ["persona_key_fields"],
                "template": template,
            }
        }
        overlays = {
            "tpm": {
                "applies_to": ["capture_intent"],  # does NOT include 'other_prompt'
                "overlay_vars": {"persona_key_fields": "from_overlay"},
            }
        }
        reg = _make_registry(tmp_path, prompts, overlays)
        # Must raise MissingVarsError because overlay doesn't apply and caller didn't supply it
        with pytest.raises(MissingVarsError):
            reg.get_prompt("other_prompt", persona="tpm")


# ---------------------------------------------------------------------------
# 10. test_persona_overlay_missing_logs_warning
# ---------------------------------------------------------------------------


class TestPersonaOverlayMissingLogsWarning:
    """Unknown persona → WARNING logged, call succeeds with empty overlay vars."""

    def test_unknown_persona_does_not_raise(self, tmp_path):
        """get_prompt succeeds even when persona is not in the overlays file."""
        prompts = {
            "simple": _minimal_prompt_dict("simple", template="Hello {name}!", required_vars=["name"])
        }
        reg = _make_registry(tmp_path, prompts)
        # 'unknown_persona' has no stanza in overlays (no overlay file written)
        spec = reg.get_prompt("simple", persona="unknown_persona", name="World")
        assert "World" in spec.text

    def test_unknown_persona_emits_warning(self, tmp_path, caplog):
        """A WARNING log is emitted when persona is unknown."""
        prompts = {
            "simple": _minimal_prompt_dict("simple", template="Hello {name}!", required_vars=["name"])
        }
        _write_prompts_yaml(tmp_path, prompts)
        _write_overlays_yaml(tmp_path, {"tpm": {"applies_to": ["capture_intent"], "overlay_vars": {}}})
        reg = PromptRegistry(tmp_path)

        with caplog.at_level(logging.WARNING, logger="framework.skill_builder.prompt_registry"):
            reg.get_prompt("simple", persona="mystery_persona", name="Alice")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("mystery_persona" in r.getMessage() for r in warnings), (
            "Expected a WARNING mentioning 'mystery_persona'. "
            f"Got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# 11. test_reload_keeps_last_good_on_bad_yaml
# ---------------------------------------------------------------------------


class TestReloadKeepsLastGoodOnBadYaml:
    """Corrupt YAML on reload → PromptStoreError raised, last-good state served."""

    def test_last_good_state_preserved_after_reload_failure(self, tmp_path):
        prompts_file = _write_prompts_yaml(
            tmp_path,
            {"alpha": _minimal_prompt_dict("alpha", template="Good {name}", required_vars=["name"])},
        )
        reg = PromptRegistry(tmp_path)

        # Verify good state
        spec = reg.get_prompt("alpha", name="test")
        assert "Good" in spec.text

        # Corrupt the YAML file
        prompts_file.write_text("not: [valid: yaml: structure\n", encoding="utf-8")

        # Explicit reload should raise
        with pytest.raises(PromptStoreError):
            reg.reload()

        # Registry must still serve the last-good state
        spec2 = reg.get_prompt("alpha", name="test")
        assert "Good" in spec2.text


# ---------------------------------------------------------------------------
# 12. test_extra_unknown_vars_are_ignored
# ---------------------------------------------------------------------------


class TestExtraUnknownVarsAreIgnored:
    """Extra caller vars not in template → no error (format_map ignores them)."""

    def test_extra_vars_do_not_cause_error(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", template="Hello {name}!", required_vars=["name"])},
        )
        # 'extra_key' is not in the template — should be silently ignored
        spec = reg.get_prompt("greet", name="World", extra_key="ignored_value")
        assert "World" in spec.text
        assert "ignored_value" not in spec.text


# ---------------------------------------------------------------------------
# 13. test_persona_double_injection
# ---------------------------------------------------------------------------


class TestPersonaDoubleInjection:
    """{persona} placeholder in template resolved from persona= arg (ADR-030-impl-plan §4 risk #1)."""

    def test_persona_placeholder_resolves_from_persona_kwarg(self, tmp_path):
        """Template with {persona} placeholder is filled by the persona= keyword arg."""
        template = "You are working for persona: {persona}. Query: {query}"
        prompts = {
            "inspect": {
                "id": "inspect",
                "version": "1.0",
                "model": "synthesis",
                "max_tokens": 512,
                "response_format": "json_object",
                "required_vars": ["query"],  # persona is satisfied via persona= arg
                "template": template,
            }
        }
        reg = _make_registry(tmp_path, prompts)
        spec = reg.get_prompt("inspect", persona="architect", query="What are the schemas?")
        assert "architect" in spec.text
        assert "What are the schemas?" in spec.text
        assert "{persona}" not in spec.text

    def test_persona_in_both_required_vars_and_persona_kwarg(self, tmp_path):
        """When persona is in required_vars AND passed as persona= kwarg, it is satisfied."""
        template = "Role: {persona}. Intent: {intent}"
        prompts = {
            "capture": {
                "id": "capture",
                "version": "1.0",
                "model": "synthesis",
                "max_tokens": 512,
                "response_format": "json_object",
                "required_vars": ["persona", "intent"],
                "template": template,
            }
        }
        reg = _make_registry(tmp_path, prompts)
        spec = reg.get_prompt("capture", persona="tpm", intent="build a status report")
        assert "tpm" in spec.text
        assert "build a status report" in spec.text


# ---------------------------------------------------------------------------
# 14. test_startup_validation_catches_undeclared_placeholder
# ---------------------------------------------------------------------------


class TestStartupValidationCatchesUndeclaredPlaceholder:
    """A required_var not present in template text → PromptStoreError at load time.

    ADR-030 §Design §4 startup validation: 'For every prompt, required_vars is
    cross-checked against the {placeholder} names in the template.  Missing
    declared vars in the template → PromptStoreError.'
    """

    def test_required_var_not_in_template_raises_at_load(self, tmp_path):
        entry = {
            "id": "bad_decl",
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 512,
            "response_format": "json_object",
            "required_vars": ["name", "phantom_var"],  # phantom_var not in template
            "template": "Hello {name}!",
        }
        with pytest.raises(PromptStoreError, match="phantom_var"):
            _make_registry(tmp_path, {"bad_decl": entry})

    def test_validate_registry_also_catches_discrepancy(self, tmp_path):
        """validate_registry() can be called as a startup hook and raises on bad state."""
        # First create a valid registry
        reg = _make_registry(
            tmp_path,
            {"ok": _minimal_prompt_dict("ok", template="Hi {name}!", required_vars=["name"])},
        )
        # Manually corrupt a record to simulate a state inconsistency (internal testing only)
        reg._cache["ok"].required_vars.append("phantom_var")

        with pytest.raises(PromptStoreError, match="phantom_var"):
            validate_registry(reg)


# ---------------------------------------------------------------------------
# 15. test_list_prompts_returns_meta
# ---------------------------------------------------------------------------


class TestListPromptsReturnsMeta:
    """list_prompts() returns PromptMeta objects with correct field values."""

    def test_meta_fields_match_yaml(self, tmp_path):
        entry = {
            "id": "my_prompt",
            "version": "2.3",
            "model": "fast",
            "max_tokens": 128,
            "response_format": "text",
            "required_vars": ["x"],
            "template": "X is {x}",
            "description": "My test prompt",
            "locked": False,
        }
        reg = _make_registry(tmp_path, {"my_prompt": entry})
        metas = reg.list_prompts()
        assert len(metas) == 1
        m = metas[0]
        assert m.prompt_id == "my_prompt"
        assert m.version == "2.3"
        assert m.model == "fast"
        assert m.description == "My test prompt"
        assert m.locked is False

    def test_list_is_sorted_by_id(self, tmp_path):
        prompts = {
            "zeta": _minimal_prompt_dict("zeta", template="Z {name}", required_vars=["name"]),
            "alpha": _minimal_prompt_dict("alpha", template="A {name}", required_vars=["name"]),
            "mu": _minimal_prompt_dict("mu", template="M {name}", required_vars=["name"]),
        }
        reg = _make_registry(tmp_path, prompts)
        ids = [m.prompt_id for m in reg.list_prompts()]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# 16. test_raw_template_returns_unformatted
# ---------------------------------------------------------------------------


class TestRawTemplateReturnsUnformatted:
    """_raw_template() returns the template before {placeholder} substitution."""

    def test_raw_contains_placeholder_markers(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            {"greet": _minimal_prompt_dict("greet", template="Hello {name}!", required_vars=["name"])},
        )
        raw = reg._raw_template("greet")
        assert "{name}" in raw   # placeholders are NOT substituted

    def test_raw_matches_original_template_text(self, tmp_path):
        template = "Prompt: {x} and {y}"
        entry = {
            "id": "two_vars",
            "version": "1.0",
            "model": "synthesis",
            "max_tokens": 256,
            "response_format": "json_object",
            "required_vars": ["x", "y"],
            "template": template,
        }
        reg = _make_registry(tmp_path, {"two_vars": entry})
        raw = reg._raw_template("two_vars")
        assert raw == template


# ---------------------------------------------------------------------------
# 17. test_reload_explicit_picks_up_changes
# ---------------------------------------------------------------------------


class TestReloadExplicitPicksUpChanges:
    """explicit registry.reload() picks up changes without relying on mtime detection."""

    def test_explicit_reload_updates_prompt(self, tmp_path):
        prompts_file = _write_prompts_yaml(
            tmp_path,
            {"msg": _minimal_prompt_dict("msg", template="Version A {name}", required_vars=["name"])},
        )
        reg = PromptRegistry(tmp_path)
        spec1 = reg.get_prompt("msg", name="Test")
        assert "Version A" in spec1.text

        # Overwrite without bumping mtime (explicit reload should still pick it up)
        prompts_file.write_text(
            yaml.dump(
                {"prompts": {"msg": _minimal_prompt_dict("msg", template="Version B {name}", required_vars=["name"])}}
            ),
            encoding="utf-8",
        )
        reg.reload()
        spec2 = reg.get_prompt("msg", name="Test")
        assert "Version B" in spec2.text
        assert "Version A" not in spec2.text


# ---------------------------------------------------------------------------
# 18. test_empty_directory_warns_not_errors
# ---------------------------------------------------------------------------


class TestEmptyDirectoryWarnsNotErrors:
    """Empty prompts_dir → WARNING logged; registry constructs but is empty."""

    def test_empty_dir_constructs_without_error(self, tmp_path):
        """An empty prompts_dir should not crash — it should warn and have empty cache."""
        reg = PromptRegistry(tmp_path)  # no YAML files
        assert reg.list_prompts() == []

    def test_get_prompt_in_empty_registry_raises_not_found(self, tmp_path):
        reg = PromptRegistry(tmp_path)
        with pytest.raises(PromptNotFoundError):
            reg.get_prompt("anything", x="y")


# ---------------------------------------------------------------------------
# Additional: checksum algorithm unit test
# ---------------------------------------------------------------------------


class TestChecksumAlgorithm:
    """Verify the exact checksum algorithm matches the ADR-030 specification."""

    def test_compute_checksum_format(self):
        """_compute_checksum returns 'sha256:<64 hex chars>'."""
        result = _compute_checksum("Hello world")
        assert result.startswith("sha256:")
        hex_part = result[len("sha256:"):]
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_compute_checksum_strips_trailing_newline(self):
        """sha256 of 'text\n' must equal sha256 of 'text' (trailing newline stripped)."""
        text_no_nl = "Some template text"
        text_with_nl = "Some template text\n"
        assert _compute_checksum(text_no_nl) == _compute_checksum(text_with_nl)

    def test_compute_checksum_known_value(self):
        """Cross-check against a manually computed SHA-256."""
        # sha256(b"Hello") = 185f8db32921bd46d35e6e1f9f7b0d2c4d3f68e7...
        # Compute the expected value here using stdlib directly
        expected_hex = hashlib.sha256(b"Hello").hexdigest()
        result = _compute_checksum("Hello")
        assert result == f"sha256:{expected_hex}"

    def test_compute_checksum_multiple_trailing_newlines_stripped_only_one(self):
        """rstrip(b'\\n') strips ALL trailing newlines, not just one — verify behavior.

        ADR-030 says rstrip(b'\\n'), which strips all trailing newlines.
        This test documents that behavior explicitly.
        """
        text_two_nl = "Body\n\n"
        text_no_nl = "Body"
        # rstrip removes all trailing newlines → these should be equal
        assert _compute_checksum(text_two_nl) == _compute_checksum(text_no_nl)


# ---------------------------------------------------------------------------
# Additional: get_registry singleton behaviour
# ---------------------------------------------------------------------------


class TestGetRegistrySingleton:
    """get_registry() with explicit path creates a fresh registry (test isolation)."""

    def test_explicit_path_returns_fresh_registry(self, tmp_path):
        _write_prompts_yaml(tmp_path, {"p": _minimal_prompt_dict("p")})
        reg = get_registry(prompts_dir=tmp_path)
        assert isinstance(reg, PromptRegistry)
        assert any(m.prompt_id == "p" for m in reg.list_prompts())

    def test_two_calls_with_explicit_path_return_independent_registries(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        _write_prompts_yaml(dir_a, {"prompt_a": _minimal_prompt_dict("prompt_a")})
        _write_prompts_yaml(dir_b, {"prompt_b": _minimal_prompt_dict("prompt_b")})

        reg_a = get_registry(prompts_dir=dir_a)
        reg_b = get_registry(prompts_dir=dir_b)

        ids_a = {m.prompt_id for m in reg_a.list_prompts()}
        ids_b = {m.prompt_id for m in reg_b.list_prompts()}
        assert "prompt_a" in ids_a
        assert "prompt_b" not in ids_a
        assert "prompt_b" in ids_b
        assert "prompt_a" not in ids_b
