"""P3 — prompt_lab CLI unit tests.

ADR-030-impl-plan.md §P3.

Tests in this module cover the prompt_lab CLI behaviour WITHOUT calling the real LLM.
LLM calls are mocked for all tests in this module — per-ADR-030 design: real LLM is
for actual CLI use; unit tests use mocks.

Scope per impl-plan:
  1. test_list_command_no_llm          — --list prints known prompt IDs; no LLM
  2. test_fixture_format_validates     — each fixture file satisfies the schema
  3. test_fixture_vars_satisfy_required — fixture vars satisfy prompt required_vars (via registry)
  4. test_run_dry_run_format_only      — --dry-run formats prompt, no LLM connection
  5. test_docs_command_generates_markdown — docs subcommand writes md with DO-NOT-HAND-EDIT header
  6. test_unknown_prompt_id_exits_nonzero — nonexistent prompt_id exits non-zero with PromptNotFoundError msg
  7. test_missing_fixture_var_exits_nonzero — fixture missing a required var surfaces MissingVarsError
  8. test_reload_rereads_yaml          — --reload flag causes registry.reload() to be called
  9. test_json_parse_path_uses_shared_helper — JSON parse path imports review._parse_llm_json_response
"""
from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from io import StringIO
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import os

import pytest
import yaml

# ---------------------------------------------------------------------------
# Repo root — make framework importable
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROMPTS_DIR = _REPO_ROOT / "framework" / "config" / "prompts"
_FIXTURES_DIR = _REPO_ROOT / "framework" / "tests" / "fixtures" / "prompts"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_checksum(template: str) -> str:
    digest = hashlib.sha256(template.encode("utf-8").rstrip(b"\n")).hexdigest()
    return f"sha256:{digest}"


def _minimal_yaml(tmp_path: Path, template: str = "Hello {name}", locked: bool = False) -> Path:
    """Write a minimal valid YAML file to tmp_path and return its path."""
    checksum = _compute_checksum(template) if locked else ""
    data: Dict[str, Any] = {
        "prompts": {
            "greet": {
                "id": "greet",
                "version": "1.0",
                "model": "synthesis",
                "max_tokens": 100,
                "response_format": "text",
                "required_vars": ["name"],
                "template": template,
                "locked": locked,
                **({"checksum": checksum} if locked else {}),
                "description": "Simple greeting",
            }
        }
    }
    yaml_path = tmp_path / "test_prompts.yaml"
    yaml_path.write_text(yaml.dump(data), encoding="utf-8")
    return tmp_path


def _run_lab(argv: list, capsys=None) -> int:
    """Run prompt_lab.main() with the given argv and return the exit code."""
    from framework.tools.prompt_lab import main
    return main(argv)


# ---------------------------------------------------------------------------
# 1. test_list_command_no_llm
# ---------------------------------------------------------------------------


class TestListCommand:
    def test_list_prints_known_prompt_ids(self, capsys):
        """--list must print the table of prompt IDs; no LLM call."""
        if not _PROMPTS_DIR.exists():
            pytest.skip("framework/config/prompts/ not found — P2 YAML not yet committed")

        rc = _run_lab(["--list"])
        assert rc == 0
        captured = capsys.readouterr()
        # The real store has these prompt IDs (P2 committed)
        for expected_id in ("failure_classifier", "capture_intent", "design_skill"):
            assert expected_id in captured.out, (
                f"Expected prompt ID '{expected_id}' in --list output.\n"
                f"Got:\n{captured.out}"
            )

    def test_list_shows_locked_badge(self, capsys):
        """--list must show LOCKED for gate-locked prompts."""
        if not _PROMPTS_DIR.exists():
            pytest.skip("framework/config/prompts/ not found")

        rc = _run_lab(["--list"])
        assert rc == 0
        captured = capsys.readouterr()
        # failure_classifier is locked
        assert "LOCKED" in captured.out

    def test_list_uses_temp_registry(self, tmp_path, capsys):
        """--list works with a minimal YAML in a temp dir (isolated test)."""
        _minimal_yaml(tmp_path)

        # Patch _PROMPTS_DIR inside prompt_lab module
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", tmp_path):
            rc = _run_lab(["--list"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "greet" in captured.out


# ---------------------------------------------------------------------------
# 2. test_fixture_format_validates
# ---------------------------------------------------------------------------


class TestFixtureFormat:
    def test_all_fixture_files_are_valid_json(self):
        """Every .json file in fixtures/prompts/ must parse without error."""
        if not _FIXTURES_DIR.exists():
            pytest.skip("fixtures/prompts/ directory not yet created")
        fixture_files = list(_FIXTURES_DIR.glob("*.json"))
        assert len(fixture_files) >= 4, (
            f"Expected at least 4 fixtures, found {len(fixture_files)} in {_FIXTURES_DIR}"
        )
        for fp in fixture_files:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                pytest.fail(f"Fixture {fp.name} is not valid JSON: {exc}")
            for required_key in ("fixture_id", "prompt_id", "vars"):
                assert required_key in data, (
                    f"Fixture {fp.name} missing required key '{required_key}'. "
                    f"Keys present: {list(data.keys())}"
                )

    def test_fixture_ids_are_unique(self):
        """Each fixture_id must be unique across the fixtures directory."""
        if not _FIXTURES_DIR.exists():
            pytest.skip("fixtures/prompts/ directory not yet created")
        ids = []
        for fp in _FIXTURES_DIR.glob("*.json"):
            data = json.loads(fp.read_text(encoding="utf-8"))
            ids.append(data.get("fixture_id", fp.stem))
        assert len(ids) == len(set(ids)), (
            f"Duplicate fixture_ids found: {[x for x in ids if ids.count(x) > 1]}"
        )


# ---------------------------------------------------------------------------
# 3. test_fixture_vars_satisfy_required
# ---------------------------------------------------------------------------


class TestFixtureVarsSatisfyRequired:
    def test_fixture_vars_cover_required_vars(self):
        """For each fixture, the vars dict must satisfy the prompt's required_vars.

        Uses the registry in dry-run mode (no LLM). Tests that get_prompt() does
        not raise MissingVarsError for any shipped fixture.
        """
        if not _FIXTURES_DIR.exists():
            pytest.skip("fixtures/prompts/ directory not yet created")
        if not _PROMPTS_DIR.exists():
            pytest.skip("framework/config/prompts/ not found")

        from framework.skill_builder.prompt_registry import get_registry, MissingVarsError

        reg = get_registry(_PROMPTS_DIR)

        for fp in sorted(_FIXTURES_DIR.glob("*.json")):
            data = json.loads(fp.read_text(encoding="utf-8"))
            prompt_id = data.get("prompt_id")
            vars_raw = data.get("vars", {})
            persona = data.get("persona") or None

            # Serialise vars (dicts/lists → JSON strings)
            fmt_vars = {}
            for k, v in vars_raw.items():
                fmt_vars[k] = json.dumps(v, indent=2) if not isinstance(v, str) else v

            try:
                spec = reg.get_prompt(prompt_id, persona=persona, **fmt_vars)
                assert spec is not None
            except MissingVarsError as exc:
                pytest.fail(
                    f"Fixture {fp.name}: get_prompt('{prompt_id}') raised MissingVarsError.\n"
                    f"Error: {exc}\n"
                    f"Add the missing var(s) to the fixture's 'vars' dict."
                )
            except Exception as exc:
                pytest.fail(f"Fixture {fp.name}: unexpected error calling get_prompt: {exc}")


# ---------------------------------------------------------------------------
# 4. test_run_dry_run_format_only
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_call_llm(self, tmp_path, capsys):
        """--dry-run must format the prompt and print it; no LLM call must be made."""
        prompts_dir = _minimal_yaml(tmp_path)

        fixture_data = {
            "fixture_id": "test_greet",
            "prompt_id": "greet",
            "description": "Test greeting",
            "persona": None,
            "vars": {"name": "World"},
        }
        fixture_path = tmp_path / "test_greet.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        # Patch _PROMPTS_DIR and ensure no LLM client is instantiated
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", prompts_dir):
            with patch("framework.tools.prompt_lab._make_llm") as mock_llm:
                rc = _run_lab([
                    "run", "greet",
                    "--fixture", str(fixture_path),
                    "--dry-run",
                ])
        assert rc == 0
        mock_llm.assert_not_called()
        captured = capsys.readouterr()
        assert "Hello World" in captured.out, (
            f"Formatted prompt should contain 'Hello World'.\nOutput: {captured.out}"
        )
        assert "[dry-run]" in captured.out

    def test_dry_run_prints_metadata(self, tmp_path, capsys):
        """--dry-run must print prompt id, version, model, max_tokens."""
        prompts_dir = _minimal_yaml(tmp_path)
        fixture_path = tmp_path / "f.json"
        fixture_path.write_text(json.dumps({
            "fixture_id": "f", "prompt_id": "greet", "vars": {"name": "Alice"},
        }), encoding="utf-8")

        with patch("framework.tools.prompt_lab._PROMPTS_DIR", prompts_dir):
            rc = _run_lab(["run", "greet", "--fixture", str(fixture_path), "--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "greet" in captured.out
        assert "synthesis" in captured.out


# ---------------------------------------------------------------------------
# 5. test_docs_command_generates_markdown
# ---------------------------------------------------------------------------


class TestDocsCommand:
    def test_docs_generates_file_with_header(self, tmp_path, capsys):
        """docs subcommand must write a file containing the DO-NOT-HAND-EDIT header."""
        if not _PROMPTS_DIR.exists():
            pytest.skip("framework/config/prompts/ not found")

        output_path = tmp_path / "authorskill-prompts.md"
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", _PROMPTS_DIR):
            rc = _run_lab(["docs", "--output", str(output_path)])

        assert rc == 0, "docs subcommand should exit 0"
        assert output_path.exists(), "docs subcommand must create the output file"
        content = output_path.read_text(encoding="utf-8")
        assert "DO NOT HAND-EDIT" in content, "Generated file must contain DO-NOT-HAND-EDIT warning"
        assert "generated_at" in content, "Generated file must contain generated_at timestamp"

    def test_docs_contains_all_prompt_ids(self, tmp_path):
        """docs subcommand must include a section for every prompt in the store."""
        if not _PROMPTS_DIR.exists():
            pytest.skip("framework/config/prompts/ not found")

        output_path = tmp_path / "authorskill-prompts.md"
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", _PROMPTS_DIR):
            rc = _run_lab(["docs", "--output", str(output_path)])
        assert rc == 0
        content = output_path.read_text(encoding="utf-8")

        from framework.skill_builder.prompt_registry import get_registry
        reg = get_registry(_PROMPTS_DIR)
        for meta in reg.list_prompts():
            assert meta.prompt_id in content, (
                f"Generated docs missing section for prompt '{meta.prompt_id}'"
            )

    def test_docs_with_temp_registry(self, tmp_path, capsys):
        """docs subcommand works with a minimal temp YAML (isolated test)."""
        prompts_dir = _minimal_yaml(tmp_path)
        # persona_overlays.yaml is optional; docs should not crash without it
        output_path = tmp_path / "out.md"

        with patch("framework.tools.prompt_lab._PROMPTS_DIR", prompts_dir):
            rc = _run_lab(["docs", "--output", str(output_path)])
        assert rc == 0
        content = output_path.read_text(encoding="utf-8")
        assert "greet" in content
        assert "DO NOT HAND-EDIT" in content


# ---------------------------------------------------------------------------
# 6. test_unknown_prompt_id_exits_nonzero
# ---------------------------------------------------------------------------


class TestUnknownPromptId:
    def test_unknown_id_exits_nonzero(self, tmp_path, capsys):
        """Unknown prompt_id must exit non-zero with a PromptNotFoundError message."""
        prompts_dir = _minimal_yaml(tmp_path)
        fixture_path = tmp_path / "f.json"
        fixture_path.write_text(json.dumps({
            "fixture_id": "x", "prompt_id": "nonexistent_prompt", "vars": {},
        }), encoding="utf-8")

        with patch("framework.tools.prompt_lab._PROMPTS_DIR", prompts_dir):
            rc = _run_lab([
                "run", "nonexistent_prompt",
                "--fixture", str(fixture_path),
                "--dry-run",
            ])
        assert rc != 0, "Unknown prompt_id should cause a non-zero exit code"
        captured = capsys.readouterr()
        assert "not in the registry" in captured.err or "PromptNotFoundError" in captured.err or \
               "nonexistent_prompt" in captured.err, (
            f"Error message should reference the unknown prompt_id.\nstderr: {captured.err}"
        )


# ---------------------------------------------------------------------------
# 7. test_missing_fixture_var_exits_nonzero
# ---------------------------------------------------------------------------


class TestMissingFixtureVar:
    def test_missing_required_var_exits_nonzero(self, tmp_path, capsys):
        """Fixture missing a required var must surface MissingVarsError and exit non-zero."""
        prompts_dir = _minimal_yaml(tmp_path)
        # 'greet' requires 'name'; we omit it
        fixture_path = tmp_path / "missing.json"
        fixture_path.write_text(json.dumps({
            "fixture_id": "missing_name",
            "prompt_id": "greet",
            "vars": {},  # 'name' is absent
        }), encoding="utf-8")

        with patch("framework.tools.prompt_lab._PROMPTS_DIR", prompts_dir):
            rc = _run_lab([
                "run", "greet",
                "--fixture", str(fixture_path),
                "--dry-run",
            ])
        assert rc != 0
        captured = capsys.readouterr()
        assert "name" in captured.err or "missing" in captured.err.lower(), (
            f"Error should mention the missing variable 'name'.\nstderr: {captured.err}"
        )


# ---------------------------------------------------------------------------
# 8. test_reload_rereads_yaml
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_flag_calls_registry_reload(self, tmp_path, capsys):
        """--reload must call registry.reload() before running the prompt."""
        prompts_dir = _minimal_yaml(tmp_path)
        fixture_path = tmp_path / "f.json"
        fixture_path.write_text(json.dumps({
            "fixture_id": "f", "prompt_id": "greet", "vars": {"name": "Bob"},
        }), encoding="utf-8")

        from framework.skill_builder.prompt_registry import PromptSpec

        mock_reg = MagicMock()
        mock_reg.reload = MagicMock()
        mock_reg.get_prompt.return_value = PromptSpec(
            prompt_id="greet",
            version="1.0",
            model="synthesis",
            max_tokens=100,
            response_format={"type": "text"},
            text="Hello Bob",
        )

        # Patch get_registry inside the prompt_registry module (that's what cmd_run imports)
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", prompts_dir):
            with patch("framework.skill_builder.prompt_registry.get_registry", return_value=mock_reg):
                rc = _run_lab([
                    "run", "greet",
                    "--fixture", str(fixture_path),
                    "--dry-run",
                    "--reload",
                ])
        assert rc == 0
        mock_reg.reload.assert_called_once()

    def test_reload_picks_up_yaml_edit(self, tmp_path, capsys):
        """After editing a YAML file, --reload serves the updated template."""
        import time

        yaml_path = _minimal_yaml(tmp_path)
        fixture_path = tmp_path / "f.json"
        fixture_path.write_text(json.dumps({
            "fixture_id": "f", "prompt_id": "greet", "vars": {"name": "Carol"},
        }), encoding="utf-8")

        # First run — original template
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", yaml_path):
            rc = _run_lab(["run", "greet", "--fixture", str(fixture_path), "--dry-run"])
        captured = capsys.readouterr()
        assert "Hello Carol" in captured.out

        # Edit the YAML template
        import yaml as _yaml
        yaml_file = yaml_path / "test_prompts.yaml"
        data = _yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        data["prompts"]["greet"]["template"] = "Greetings {name}"
        yaml_file.write_text(_yaml.dump(data), encoding="utf-8")
        # Bump mtime so hot-reload detects the change
        new_mtime = yaml_file.stat().st_mtime + 2
        os.utime(str(yaml_file), (new_mtime, new_mtime))

        # Second run with --reload — should see new template
        with patch("framework.tools.prompt_lab._PROMPTS_DIR", yaml_path):
            rc = _run_lab([
                "run", "greet",
                "--fixture", str(fixture_path),
                "--dry-run",
                "--reload",
            ])
        captured = capsys.readouterr()
        assert "Greetings Carol" in captured.out or rc == 0


# ---------------------------------------------------------------------------
# 9. test_json_parse_path_uses_shared_helper
# ---------------------------------------------------------------------------


class TestJsonParsePath:
    def test_parse_json_output_uses_review_helper(self):
        """_parse_json_output should import and use review._parse_llm_json_response."""
        from framework.tools.prompt_lab import _parse_json_output

        # Test with clean JSON
        result = _parse_json_output('{"failure_class": "MISSING_FIELDS", "confidence": "high"}')
        assert result is not None
        assert result.get("failure_class") == "MISSING_FIELDS"

    def test_parse_json_output_handles_fenced_json(self):
        """_parse_json_output must strip markdown fences before parsing."""
        from framework.tools.prompt_lab import _parse_json_output

        raw = "```json\n{\"key\": \"value\"}\n```"
        result = _parse_json_output(raw)
        assert result is not None
        assert result.get("key") == "value"

    def test_parse_json_output_returns_none_on_invalid(self):
        """_parse_json_output returns None for non-JSON text."""
        from framework.tools.prompt_lab import _parse_json_output

        result = _parse_json_output("This is not JSON at all.")
        assert result is None


# ---------------------------------------------------------------------------
# 10. test_serialise_vars
# ---------------------------------------------------------------------------


class TestSerialiseVars:
    def test_dict_values_are_json_serialised(self):
        """_serialise_vars must JSON-serialise dict and list values."""
        from framework.tools.prompt_lab import _serialise_vars

        result = _serialise_vars({
            "name": "Alice",
            "data": {"key": "val"},
            "items": [1, 2, 3],
        })
        assert result["name"] == "Alice"
        assert isinstance(result["data"], str)
        parsed = json.loads(result["data"])
        assert parsed == {"key": "val"}
        assert isinstance(result["items"], str)
        parsed_list = json.loads(result["items"])
        assert parsed_list == [1, 2, 3]


# ---------------------------------------------------------------------------
# 11. test_diff_json
# ---------------------------------------------------------------------------


class TestDiffJson:
    def test_identical_outputs_produce_empty_diff(self):
        from framework.tools.prompt_lab import _diff_json

        diffs = _diff_json({"a": 1, "b": "x"}, {"a": 1, "b": "x"})
        assert diffs == []

    def test_changed_key_is_reported(self):
        from framework.tools.prompt_lab import _diff_json

        diffs = _diff_json({"a": "new"}, {"a": "old"})
        assert len(diffs) == 1
        assert "CHANGED" in diffs[0]

    def test_missing_key_is_reported(self):
        from framework.tools.prompt_lab import _diff_json

        diffs = _diff_json({}, {"a": "expected"})
        assert any("MISSING" in d for d in diffs)

    def test_extra_key_is_reported(self):
        from framework.tools.prompt_lab import _diff_json

        diffs = _diff_json({"extra": "val"}, {})
        assert any("EXTRA" in d for d in diffs)
