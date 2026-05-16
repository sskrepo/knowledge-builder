"""P1 — persona_prompts.yaml loader unit tests.

Stream C / QA Engineer — parallel stream for ADR-028/ADR-029 impl-plan P1.

PURPOSE
-------
These tests specify the contract for the ``_load_persona_prompt_fragments``
loader that Stream A will implement in S4 (framework/skill_builder/conversation.py).

TWO CATEGORIES of tests live here:

  (A) YAML-content assertions — these can pass RIGHT NOW because
      framework/config/persona_prompts.yaml is already committed.  They assert
      that every required persona stanza is present and non-empty.  These
      MUST be green before S4 work begins.

  (B) Loader-function contract tests — these target the
      ``_load_persona_prompt_fragments`` helper that S4 will implement.  They
      WILL FAIL until S4 lands; that is expected and correct — they are the
      executable contract that S4 must satisfy.  Tests are NOT xfail/skip'd;
      they fail hard so the developer gets an immediate red signal.

Blueprint reference: ADR-028-029-impl-plan.md §P1 and §S4.

PERSONAS UNDER TEST
-------------------
The nine personas defined in framework/config/persona_prompts.yaml:

  tpm, pm, architect, eng_mgr, developer, ops_eng, ops_mgr,
  service_owner, kbf_ops
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSONA_PROMPTS_YAML = REPO_ROOT / "framework" / "config" / "persona_prompts.yaml"

EXPECTED_PERSONAS = [
    "tpm",
    "pm",
    "architect",
    "eng_mgr",
    "developer",
    "ops_eng",
    "ops_mgr",
    "service_owner",
    "kbf_ops",
]

REQUIRED_KEYS = ["key_fields", "extraction_style", "few_shot_example"]


# ---------------------------------------------------------------------------
# (A) YAML-content assertions — MUST BE GREEN NOW
#     persona_prompts.yaml is already committed; these validate its content.
# ---------------------------------------------------------------------------


class TestPersonaPromptsYamlContent:
    """Assert that the committed YAML is structurally complete.

    These tests do NOT import conversation.py. They load the YAML directly.
    They MUST PASS immediately because the file is already committed.
    Any red result here indicates a content defect in persona_prompts.yaml itself.
    """

    @pytest.fixture(scope="class")
    def yaml_data(self):
        assert PERSONA_PROMPTS_YAML.exists(), (
            f"persona_prompts.yaml not found at {PERSONA_PROMPTS_YAML}. "
            "This file must be committed before tests run."
        )
        raw = PERSONA_PROMPTS_YAML.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        assert isinstance(data, dict), "persona_prompts.yaml must be a YAML mapping at the top level"
        return data

    def test_yaml_file_exists(self):
        assert PERSONA_PROMPTS_YAML.exists(), (
            f"persona_prompts.yaml missing at expected path: {PERSONA_PROMPTS_YAML}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_persona_stanza_present(self, yaml_data, persona):
        """Every expected persona must have a top-level key in the YAML."""
        assert persona in yaml_data, (
            f"Persona '{persona}' is missing from persona_prompts.yaml. "
            f"Present keys: {list(yaml_data.keys())}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    @pytest.mark.parametrize("required_key", REQUIRED_KEYS)
    def test_required_key_present_for_persona(self, yaml_data, persona, required_key):
        """Each persona stanza must contain key_fields, extraction_style, and few_shot_example."""
        stanza = yaml_data.get(persona, {})
        assert required_key in stanza, (
            f"Persona '{persona}' is missing required key '{required_key}' "
            f"in persona_prompts.yaml. Present keys: {list(stanza.keys())}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_key_fields_is_non_empty_list(self, yaml_data, persona):
        """key_fields must be a non-empty list of strings."""
        key_fields = yaml_data[persona]["key_fields"]
        assert isinstance(key_fields, list), (
            f"Persona '{persona}': key_fields must be a list, got {type(key_fields).__name__}"
        )
        assert len(key_fields) > 0, (
            f"Persona '{persona}': key_fields must be non-empty"
        )
        for item in key_fields:
            assert isinstance(item, str), (
                f"Persona '{persona}': each key_fields entry must be a string, got {type(item).__name__}: {item!r}"
            )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_extraction_style_is_non_empty_string(self, yaml_data, persona):
        """extraction_style must be a non-empty string."""
        style = yaml_data[persona]["extraction_style"]
        # YAML block scalars (|, >) parse to str; strip to catch whitespace-only values
        assert isinstance(style, str), (
            f"Persona '{persona}': extraction_style must be a string, got {type(style).__name__}"
        )
        assert style.strip(), (
            f"Persona '{persona}': extraction_style must not be empty or whitespace-only"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_few_shot_example_is_non_empty_string(self, yaml_data, persona):
        """few_shot_example must be a non-empty string."""
        example = yaml_data[persona]["few_shot_example"]
        assert isinstance(example, str), (
            f"Persona '{persona}': few_shot_example must be a string, got {type(example).__name__}"
        )
        assert example.strip(), (
            f"Persona '{persona}': few_shot_example must not be empty or whitespace-only"
        )

    def test_exactly_nine_personas_defined(self, yaml_data):
        """The YAML must define exactly the 9 expected personas.

        No extra personas, no missing ones.  Extra stanzas indicate drift
        between the YAML and the expected set.
        """
        yaml_personas = set(yaml_data.keys())
        expected_set = set(EXPECTED_PERSONAS)
        missing = expected_set - yaml_personas
        extra = yaml_personas - expected_set
        assert not missing, f"Missing personas in YAML: {sorted(missing)}"
        assert not extra, (
            f"Unexpected personas in YAML (not in EXPECTED_PERSONAS): {sorted(extra)}. "
            "Add them to EXPECTED_PERSONAS in this test file if they are intentional."
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_key_fields_has_at_least_three_entries(self, yaml_data, persona):
        """Each persona should declare at least 3 key fields (minimum useful guidance)."""
        key_fields = yaml_data[persona]["key_fields"]
        assert len(key_fields) >= 3, (
            f"Persona '{persona}': key_fields has only {len(key_fields)} entries; "
            "expected at least 3 for meaningful persona guidance."
        )


# ---------------------------------------------------------------------------
# (B) Loader-function contract tests — WILL FAIL until Stream A S4 lands.
#
#     These tests target:
#       framework.skill_builder.conversation._load_persona_prompt_fragments
#
#     The loader is specified in ADR-028-029-impl-plan.md §S4 as:
#       def _load_persona_prompt_fragments(persona: str) -> dict
#     returning a dict with keys: key_fields, extraction_style, few_shot_example.
#     Unknown persona => empty-string defaults + logged warning (graceful degradation).
#
#     Status: AWAITING STREAM A S4
# ---------------------------------------------------------------------------


def _import_loader():
    """Attempt to import the loader from its blueprint-specified location.

    Returns the callable, or raises ImportError / AttributeError if S4 has
    not landed yet.  Test functions call this so the failure is an
    AttributeError or ImportError with a clear message, not a mysterious crash.
    """
    mod = importlib.import_module("framework.skill_builder.conversation")
    loader = getattr(mod, "_load_persona_prompt_fragments", None)
    if loader is None:
        raise AttributeError(
            "_load_persona_prompt_fragments is not yet defined in "
            "framework.skill_builder.conversation — awaiting Stream A S4."
        )
    return loader


class TestPersonaPromptFragmentsLoaderContract:
    """Contract tests for _load_persona_prompt_fragments (Stream A S4).

    ALL tests in this class will FAIL until S4 lands.  That is expected and
    correct — they are the executable specification.

    Blueprint contract (ADR-028-029-impl-plan.md §S4):
      - Reads framework/config/persona_prompts.yaml
      - Returns dict with keys: key_fields, extraction_style, few_shot_example
      - Unknown persona => returns dict with empty-string defaults, logs a warning
      - Must NOT raise for unknown personas
    """

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_known_persona_returns_non_empty_key_fields(self, persona):
        """Known persona: key_fields must be a non-empty list. AWAITING STREAM A S4."""
        loader = _import_loader()
        result = loader(persona)
        assert isinstance(result, dict), (
            f"loader('{persona}') must return a dict, got {type(result).__name__}"
        )
        key_fields = result.get("key_fields")
        assert isinstance(key_fields, list) and len(key_fields) > 0, (
            f"loader('{persona}')['key_fields'] must be a non-empty list, got: {key_fields!r}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_known_persona_returns_non_empty_extraction_style(self, persona):
        """Known persona: extraction_style must be a non-empty string. AWAITING STREAM A S4."""
        loader = _import_loader()
        result = loader(persona)
        style = result.get("extraction_style")
        assert isinstance(style, str) and style.strip(), (
            f"loader('{persona}')['extraction_style'] must be a non-empty string, got: {style!r}"
        )

    @pytest.mark.parametrize("persona", EXPECTED_PERSONAS)
    def test_known_persona_returns_non_empty_few_shot_example(self, persona):
        """Known persona: few_shot_example must be a non-empty string. AWAITING STREAM A S4."""
        loader = _import_loader()
        result = loader(persona)
        example = result.get("few_shot_example")
        assert isinstance(example, str) and example.strip(), (
            f"loader('{persona}')['few_shot_example'] must be a non-empty string, got: {example!r}"
        )

    def test_unknown_persona_does_not_raise(self):
        """Unknown persona must NOT raise any exception. AWAITING STREAM A S4.

        Graceful-but-LOUD degradation: the loader returns empty-string defaults
        and logs a warning.  It must never raise KeyError, ValueError, etc.
        """
        loader = _import_loader()
        # Must not raise
        result = loader("unknown_persona_xyzzy_12345")
        assert isinstance(result, dict), (
            "loader('unknown_persona_xyzzy_12345') must return a dict, not raise"
        )

    def test_unknown_persona_returns_empty_string_defaults(self):
        """Unknown persona returns empty-string defaults for all three keys. AWAITING STREAM A S4.

        The ADR specifies: 'the kwargs default to empty strings and a warning is logged
        (the prompt degrades gracefully to the current static template).'
        """
        loader = _import_loader()
        result = loader("unknown_persona_xyzzy_12345")
        # All three keys must exist with falsy values (empty string or empty list)
        for key in REQUIRED_KEYS:
            assert key in result, (
                f"Unknown persona result must contain key '{key}'; got keys: {list(result.keys())}"
            )
        # extraction_style and few_shot_example must be empty strings (or falsy)
        assert not result.get("extraction_style", ""), (
            f"Unknown persona: extraction_style must be empty string, got: {result.get('extraction_style')!r}"
        )
        assert not result.get("few_shot_example", ""), (
            f"Unknown persona: few_shot_example must be empty string, got: {result.get('few_shot_example')!r}"
        )
        # key_fields must be empty list (or falsy)
        kf = result.get("key_fields", [])
        assert not kf, (
            f"Unknown persona: key_fields must be empty list, got: {kf!r}"
        )

    def test_unknown_persona_logs_warning(self, caplog):
        """Unknown persona must emit a logged warning. AWAITING STREAM A S4.

        The ADR specifies: 'logs a warning' — the warning must be at WARNING
        level or above and must mention the unknown persona name.
        """
        loader = _import_loader()
        unknown = "unknown_persona_xyzzy_12345"
        with caplog.at_level(logging.WARNING, logger="framework.skill_builder.conversation"):
            loader(unknown)

        # At least one warning record must mention the unknown persona
        matching = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and unknown in r.getMessage()
        ]
        assert matching, (
            f"Expected a WARNING-level log mentioning '{unknown}' when loading an "
            f"unknown persona, but no matching log record was found. "
            f"Log records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    def test_loader_returns_dict_with_all_three_required_keys(self):
        """Return value must always have exactly (or at least) the three spec keys. AWAITING STREAM A S4."""
        loader = _import_loader()
        for persona in EXPECTED_PERSONAS:
            result = loader(persona)
            for key in REQUIRED_KEYS:
                assert key in result, (
                    f"loader('{persona}') result is missing required key '{key}'. "
                    f"Returned keys: {list(result.keys())}"
                )

    def test_loader_is_idempotent_across_calls(self):
        """Calling the loader twice for the same persona returns identical content. AWAITING STREAM A S4.

        The loader may cache internally (module-level dict) or re-read each time;
        either is acceptable, but the results must be identical.
        """
        loader = _import_loader()
        result_a = loader("tpm")
        result_b = loader("tpm")
        assert result_a == result_b, (
            "loader('tpm') returned different results on two consecutive calls — "
            "must be deterministic (idempotent)."
        )

    def test_loader_tpm_extraction_style_contains_exec_safe(self):
        """Spot-check: tpm extraction_style must mention exec-safe language. AWAITING STREAM A S4.

        This is grounded in the actual YAML content (already committed).
        The loader must faithfully return what the YAML says.
        """
        loader = _import_loader()
        result = loader("tpm")
        style = result.get("extraction_style", "")
        assert "exec-safe" in style.lower() or "exec safe" in style.lower(), (
            f"tpm extraction_style must mention 'exec-safe' language "
            f"(as defined in persona_prompts.yaml), got: {style[:200]!r}"
        )
