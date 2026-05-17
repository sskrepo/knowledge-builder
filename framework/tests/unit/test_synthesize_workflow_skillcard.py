"""BUG-queue-2ad9a FIX 2 — synthesize_workflow skill card output_format token.

Verifies that _build_skill_card (and by extension synthesize_workflow_skill)
produces example_invocations that:
  1. Are longer than 150 characters (so enough context is present for the
     Tier-1 LLM router to distinguish skills).
  2. Contain the output_format token (e.g. 'eml', 'pptx') so the router
     can distinguish a .eml workflow from a .pptx workflow even when task
     descriptions are similar.

Before the fix: example_invocations[0] = task[:100] which (a) was truncated
too early and (b) never mentioned the output format, causing silent wrong-output
routing in the Tier-1 classifier (BUG-queue-2ad9a root cause).
"""
from __future__ import annotations

import pytest

from framework.skill_builder.synthesize_workflow import (
    synthesize_workflow_skill,
    _build_skill_card,
    derive_space_allow_list,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LONG_TASK = (
    "Create a weekly executive review deck for the 26AI project that pulls "
    "status from Confluence pages including ORM, risk register, and key "
    "milestone tracker. Produce a slide deck with a per-project RAG summary, "
    "top 3 risks, and owner actions."
)  # 290+ chars


# ---------------------------------------------------------------------------
# _build_skill_card unit tests
# ---------------------------------------------------------------------------

class TestBuildSkillCard:
    def test_example_invocation_longer_than_150_chars(self):
        """example_invocations[0] must be > 150 chars (was capped at 100 before fix)."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        ex = card["example_invocations"][0]
        assert len(ex) > 150, (
            f"example_invocations[0] length={len(ex)} must be > 150 "
            f"so the Tier-1 router has enough context. Got: {ex!r}"
        )

    def test_example_invocation_contains_output_format_pptx(self):
        """example_invocations[0] must contain 'pptx' for pptx skills."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        ex = card["example_invocations"][0]
        assert "pptx" in ex.lower(), (
            f"'pptx' must appear in example_invocations[0] so the router "
            f"can distinguish it from .eml skills. Got: {ex!r}"
        )

    def test_example_invocation_contains_output_format_eml(self):
        """example_invocations[0] must contain 'eml' for email skills."""
        card = _build_skill_card(
            "Send a project tracking stakeholder status email every Monday morning",
            "stakeholder_status_email",
            "eml",
        )
        ex = card["example_invocations"][0]
        assert "eml" in ex.lower(), (
            f"'eml' must appear in example_invocations[0]. Got: {ex!r}"
        )

    def test_use_when_contains_output_format(self):
        """use_when should also mention the output format."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        assert "pptx" in card["use_when"].lower(), (
            f"use_when should reference 'pptx' output type. Got: {card['use_when']!r}"
        )

    def test_summary_unchanged(self):
        """summary should still be task[:200]."""
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        assert card["summary"] == _LONG_TASK[:200]

    def test_do_not_use_for_present(self):
        card = _build_skill_card(_LONG_TASK, "weekly_exec_review", "pptx")
        assert "do_not_use_for" in card
        assert len(card["do_not_use_for"]) > 0

    def test_default_output_format_markdown(self):
        """Default output_format is 'markdown' if not provided."""
        card = _build_skill_card("some task", "some_skill")
        ex = card["example_invocations"][0]
        assert "markdown" in ex.lower(), (
            f"Default output_format 'markdown' must appear in example. Got: {ex!r}"
        )


# ---------------------------------------------------------------------------
# synthesize_workflow_skill integration (output_format flows through)
# ---------------------------------------------------------------------------

class TestSynthesizeWorkflowSkill:
    def _make_intent(self, output_format: str = "pptx") -> dict:
        return {
            "task_description": _LONG_TASK,
            "output_format": output_format,
            "trigger": {"on_request": True},
            "delivery": {"kind": "filesystem", "path": f"~/.kbf/outputs/test.{output_format}"},
        }

    def test_pptx_skill_card_contains_pptx_token(self):
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_exec_review",
            intent=self._make_intent("pptx"),
            fields=["rag_status", "top_milestones"],
        )
        ex = result["skill_card"]["example_invocations"][0]
        assert "pptx" in ex.lower(), (
            f"synthesize_workflow_skill: pptx skill card must include 'pptx' token. Got: {ex!r}"
        )
        assert len(ex) > 150, (
            f"example_invocations[0] length={len(ex)} must be > 150 chars."
        )

    def test_eml_skill_card_contains_eml_token(self):
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="stakeholder_status_email",
            intent=self._make_intent("eml"),
            fields=["project_name", "rag_status"],
        )
        ex = result["skill_card"]["example_invocations"][0]
        assert "eml" in ex.lower(), (
            f"synthesize_workflow_skill: eml skill card must include 'eml' token. Got: {ex!r}"
        )

    def test_pptx_and_eml_example_invocations_are_different(self):
        """pptx and eml skills with the same task must produce different
        example_invocations so the Tier-1 classifier can tell them apart."""
        pptx_result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_pptx",
            intent=self._make_intent("pptx"),
            fields=["rag_status"],
        )
        eml_result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_eml",
            intent=self._make_intent("eml"),
            fields=["rag_status"],
        )
        pptx_ex = pptx_result["skill_card"]["example_invocations"][0]
        eml_ex = eml_result["skill_card"]["example_invocations"][0]
        assert pptx_ex != eml_ex, (
            "pptx and eml skill example_invocations must differ "
            "(output_format token differentiates them for the Tier-1 router)."
        )
        assert "pptx" in pptx_ex.lower()
        assert "eml" in eml_ex.lower()


# ---------------------------------------------------------------------------
# ADR-032: ask_parameterized source_binding emission
# ---------------------------------------------------------------------------

class TestAskParameterizedSourceBinding:
    """ADR-032 synthesizer wiring gap fix.

    Newly authored ask_parameterized skills must emit a complete source_binding
    block and a typed trigger input.  author_fixed mode must produce no
    source_binding block (byte-identical to pre-ADR-032 output).
    """

    _TASK = (
        "Accept a Confluence project tracking page and draft a weekly "
        "stakeholder status email based on its content."
    )
    _FIELDS = ["project_name", "rag_status", "next_steps", "email_subject"]

    def _make_intent(self, output_format: str = "email") -> dict:
        return {
            "task_description": self._TASK,
            "output_format": output_format,
            "trigger": {"on_request": True},
            "delivery": {"kind": "filesystem", "path": f"~/.kbf/outputs/test.{output_format}"},
        }

    # -- ask_parameterized mode -----------------------------------------------

    def test_ask_parameterized_emits_source_binding_block(self):
        """ask_parameterized mode must emit a source_binding block."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent("email"),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        assert "source_binding" in result, (
            "ask_parameterized skill must have a source_binding block"
        )

    def test_ask_parameterized_source_binding_mode_field(self):
        """source_binding.mode must be 'ask_parameterized'."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        sb = result["source_binding"]
        assert sb["mode"] == "ask_parameterized", (
            f"source_binding.mode must be 'ask_parameterized', got {sb['mode']!r}"
        )

    def test_ask_parameterized_input_param_field(self):
        """source_binding.input_param must be present and non-empty."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        sb = result["source_binding"]
        assert sb.get("input_param"), (
            "source_binding.input_param must be present and non-empty"
        )
        assert sb["input_param"] == "page_id", (
            f"Default input_param must be 'page_id', got {sb['input_param']!r}"
        )

    def test_ask_parameterized_ingest_on_demand_true(self):
        """source_binding.ingest_on_demand must be True."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        assert result["source_binding"]["ingest_on_demand"] is True

    def test_ask_parameterized_source_type_field(self):
        """source_binding.source_type must be present."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        assert result["source_binding"].get("source_type"), (
            "source_binding.source_type must be present"
        )

    def test_ask_parameterized_space_allow_list_non_empty(self):
        """source_binding.space_allow_list must be the passed list (non-empty)."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        sal = result["source_binding"].get("space_allow_list")
        assert sal and isinstance(sal, list) and len(sal) > 0, (
            f"space_allow_list must be a non-empty list, got {sal!r}"
        )
        assert "OCIFACP" in sal, (
            f"OCIFACP must be in space_allow_list, got {sal!r}"
        )

    def test_ask_parameterized_ephemeral_ttl_seconds_present(self):
        """source_binding.ephemeral_ttl_seconds must be present."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        assert "ephemeral_ttl_seconds" in result["source_binding"], (
            "source_binding.ephemeral_ttl_seconds must be present"
        )
        assert result["source_binding"]["ephemeral_ttl_seconds"] == 300

    def test_ask_parameterized_trigger_input_is_typed_page_ref(self):
        """ask_parameterized mode must replace generic input with typed page_id input."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        inputs = result["trigger"]["on_request"]["inputs"]
        assert inputs, "trigger.on_request.inputs must not be empty"
        first = inputs[0]
        assert first["name"] == "page_id", (
            f"First trigger input name must be 'page_id' for ask_parameterized, got {first['name']!r}"
        )
        assert first["type"] == "confluence_page_ref", (
            f"First trigger input type must be 'confluence_page_ref', got {first['type']!r}"
        )
        assert first.get("required") is True, (
            "First trigger input must have required=true"
        )

    def test_ask_parameterized_input_param_matches_trigger_input(self):
        """source_binding.input_param must match a declared trigger.on_request.inputs name.

        This is the core P1-D contract check: if input_param != any declared trigger
        input name, VALIDATE will fail.  We verify the synthesizer satisfies it.
        """
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        input_param = result["source_binding"]["input_param"]
        declared_names = [
            inp["name"]
            for inp in result["trigger"]["on_request"]["inputs"]
            if inp.get("name")
        ]
        assert input_param in declared_names, (
            f"source_binding.input_param={input_param!r} must match a declared "
            f"trigger input name. Declared: {declared_names}"
        )

    def test_ask_parameterized_all_six_required_fields_present(self):
        """The source_binding block must contain all 6 required fields per ADR-032 §D.1."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP"],
        )
        sb = result["source_binding"]
        required = {"mode", "input_param", "ingest_on_demand", "source_type",
                    "space_allow_list", "ephemeral_ttl_seconds"}
        missing = required - set(sb.keys())
        assert not missing, (
            f"source_binding is missing required fields: {missing}"
        )

    def test_ask_parameterized_custom_input_param(self):
        """Custom input_param is respected in both source_binding and trigger input."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["FA"],
            input_param="confluence_page",
        )
        assert result["source_binding"]["input_param"] == "confluence_page"
        declared_names = [
            inp["name"] for inp in result["trigger"]["on_request"]["inputs"]
        ]
        assert "confluence_page" in declared_names, (
            f"Custom input_param 'confluence_page' must appear in trigger inputs. "
            f"Got: {declared_names}"
        )

    def test_ask_parameterized_multiple_spaces(self):
        """Multiple space keys are all preserved in space_allow_list."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_ask_skill",
            intent=self._make_intent(),
            fields=self._FIELDS,
            source_binding_mode="ask_parameterized",
            space_allow_list=["OCIFACP", "FA", "PROJ"],
        )
        sal = result["source_binding"]["space_allow_list"]
        assert set(sal) == {"OCIFACP", "FA", "PROJ"}, (
            f"All space keys must be in space_allow_list, got {sal!r}"
        )

    # -- author_fixed mode (no source_binding emitted) -------------------------

    def test_author_fixed_no_source_binding_block(self):
        """author_fixed mode must NOT emit a source_binding block."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_author_fixed_skill",
            intent=self._make_intent("pptx"),
            fields=["rag_status", "top_risks"],
            source_binding_mode="author_fixed",
        )
        assert "source_binding" not in result, (
            "author_fixed mode must not emit a source_binding block "
            f"(found: {result.get('source_binding')})"
        )

    def test_author_fixed_trigger_input_is_generic(self):
        """author_fixed mode must keep the generic string trigger input."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_author_fixed_skill",
            intent=self._make_intent("pptx"),
            fields=["rag_status"],
            source_binding_mode="author_fixed",
        )
        inputs = result["trigger"]["on_request"]["inputs"]
        # Must NOT have a confluence_page_ref typed input
        for inp in inputs:
            assert inp.get("type") != "confluence_page_ref", (
                f"author_fixed mode must not have a confluence_page_ref input: {inp!r}"
            )

    def test_author_fixed_default_mode(self):
        """Default mode (no source_binding_mode arg) is author_fixed: no source_binding block."""
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_default_skill",
            intent=self._make_intent("pptx"),
            fields=["rag_status"],
            # source_binding_mode intentionally omitted — must default to author_fixed
        )
        assert "source_binding" not in result, (
            "Default synthesize_workflow_skill() must not emit source_binding "
            "(pre-ADR-032 byte-identical behavior)"
        )

    def test_author_fixed_output_unchanged_by_new_params(self):
        """Calling with explicit author_fixed must produce identical output to the default call."""
        intent = self._make_intent("pptx")
        fields = ["rag_status", "top_risks"]
        default_result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_parity_skill",
            intent=intent,
            fields=fields,
        )
        explicit_result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="test_parity_skill",
            intent=intent,
            fields=fields,
            source_binding_mode="author_fixed",
        )
        assert default_result == explicit_result, (
            "Explicit author_fixed must produce byte-identical output to default call"
        )


# ---------------------------------------------------------------------------
# ADR-032: _validate_source_binding_contract end-to-end regression
# ---------------------------------------------------------------------------

class TestAskParameterizedPassesValidateContract:
    """End-to-end regression: freshly synthesized ask_parameterized YAML must pass
    _validate_source_binding_contract with session mode 'ask_parameterized'.

    This is the core gap-closed proof: the synthesizer now emits a well-formed
    source_binding block that satisfies every predicate in _validate_source_binding_contract.
    """

    def _synthesize_ask_parameterized(self, space_allow_list=None) -> dict:
        return synthesize_workflow_skill(
            persona="tpm",
            skill_name="project_tracking_weekly_stakeholder_meeting_email",
            intent={
                "task_description": (
                    "Accept a Confluence project tracking page and draft a weekly "
                    "stakeholder status email."
                ),
                "output_format": "email",
                "trigger": {"on_request": True},
                "delivery": {"kind": "filesystem", "path": "~/.kbf/outputs/test.email"},
            },
            fields=["project_name", "rag_status", "next_steps"],
            source_binding_mode="ask_parameterized",
            space_allow_list=space_allow_list or ["OCIFACP"],
        )

    def test_synthesized_yaml_passes_validate_contract(self):
        """A freshly synthesized ask_parameterized YAML passes _validate_source_binding_contract."""
        from framework.skill_builder.conversation import _validate_source_binding_contract

        yaml_dict = self._synthesize_ask_parameterized(space_allow_list=["OCIFACP"])
        errors = _validate_source_binding_contract(yaml_dict, "ask_parameterized")
        assert errors == [], (
            f"Freshly synthesized ask_parameterized YAML must pass VALIDATE. "
            f"Errors: {errors}"
        )

    def test_synthesized_yaml_has_all_required_source_binding_fields(self):
        """All 6 required source_binding fields are present in the synthesized YAML."""
        from framework.skill_builder.conversation import _validate_source_binding_contract

        yaml_dict = self._synthesize_ask_parameterized()
        sb = yaml_dict.get("source_binding", {})
        required = {"mode", "input_param", "ingest_on_demand", "source_type",
                    "space_allow_list", "ephemeral_ttl_seconds"}
        assert required <= set(sb.keys()), (
            f"Missing required source_binding fields: {required - set(sb.keys())}"
        )

    def test_synthesized_author_fixed_passes_validate_contract(self):
        """author_fixed synthesized YAML passes _validate_source_binding_contract."""
        from framework.skill_builder.conversation import _validate_source_binding_contract

        yaml_dict = synthesize_workflow_skill(
            persona="tpm",
            skill_name="weekly_exec_review",
            intent={
                "task_description": "Draft a weekly exec review slide deck.",
                "output_format": "pptx",
                "trigger": {"on_request": True},
                "delivery": {"kind": "filesystem", "path": "~/.kbf/outputs/test.pptx"},
            },
            fields=["rag_status", "top_risks"],
            source_binding_mode="author_fixed",
        )
        errors = _validate_source_binding_contract(yaml_dict, "author_fixed")
        assert errors == [], (
            f"author_fixed synthesized YAML must pass VALIDATE: {errors}"
        )

    def test_validate_contract_catches_old_pre_adr032_yaml(self):
        """A pre-ADR-032 synthesized YAML (no source_binding) fails for ask_parameterized session."""
        from framework.skill_builder.conversation import _validate_source_binding_contract

        # Simulate the OLD synthesizer output (no source_binding block, generic input)
        old_yaml = synthesize_workflow_skill(
            persona="tpm",
            skill_name="old_skill",
            intent={
                "task_description": "Accept a page and draft email.",
                "output_format": "email",
                "trigger": {"on_request": True},
                "delivery": {"kind": "filesystem", "path": "~/.kbf/outputs/old.email"},
            },
            fields=["project_name"],
            # No source_binding_mode arg — old call pattern
        )
        # This old YAML has no source_binding block
        assert "source_binding" not in old_yaml, (
            "Old synthesizer output should not have source_binding (pre-ADR-032)"
        )
        # But a session with ask_parameterized mode would FAIL VALIDATE on this YAML
        errors = _validate_source_binding_contract(old_yaml, "ask_parameterized")
        assert errors, (
            "Pre-ADR-032 YAML (no source_binding) must fail VALIDATE when "
            "session mode is ask_parameterized"
        )
        assert any("mode" in e for e in errors), (
            f"Error should mention 'mode': {errors}"
        )


# ---------------------------------------------------------------------------
# ADR-032: derive_space_allow_list tests
# ---------------------------------------------------------------------------

class TestDeriveSpaceAllowList:
    """ADR-032 space_allow_list derivation rule.

    Derivation must use actual session data (not a hardcoded guess).
    Wrong default reproduces the P1-E OCIFACP bug where [FA, PROJ] was
    hardcoded and caused a hard allow-list failure at runtime.
    """

    def test_derives_from_source_samples_space_field(self):
        """Priority 1: space from source_samples (live-fetched metadata) is used."""
        source_samples = {
            "confluence:18625350641": [
                {"source_citation": "https://ocifacp.example.com/wiki/spaces/OCIFACP/pages/18625350641",
                 "space": "OCIFACP", "content": "...", "title": "PT page"},
            ]
        }
        result = derive_space_allow_list(sources=[], source_samples=source_samples)
        assert result == ["OCIFACP"], (
            f"Space must be derived from source_samples.space field, got {result!r}"
        )

    def test_ocifacp_session_derives_ocifacp(self):
        """The OCIFACP session (synth-tpm-5b3e690f class) must produce ['OCIFACP'], not ['FA','PROJ']."""
        source_samples = {
            "confluence:18625350641": [
                {"source_citation": "https://example.atlassian.net/wiki/spaces/OCIFACP/pages/18625350641",
                 "space": "OCIFACP", "content": "Project tracking weekly", "title": "Weekly PT"},
            ],
            "confluence:18625350642": [
                {"source_citation": "https://example.atlassian.net/wiki/spaces/OCIFACP/pages/18625350642",
                 "space": "OCIFACP", "content": "Another PT page", "title": "PT Q2"},
            ],
        }
        result = derive_space_allow_list(sources=[], source_samples=source_samples)
        assert result == ["OCIFACP"], (
            f"OCIFACP session must produce ['OCIFACP'], not {result!r} "
            "(this is the P1-E bug reproduced and fixed)"
        )

    def test_derives_multiple_spaces_from_source_samples(self):
        """Multiple spaces from source_samples are all returned (sorted)."""
        source_samples = {
            "confluence:111": [{"space": "FA", "content": "...", "title": "p1"}],
            "confluence:222": [{"space": "PROJ", "content": "...", "title": "p2"}],
        }
        result = derive_space_allow_list(sources=[], source_samples=source_samples)
        assert set(result) == {"FA", "PROJ"}, (
            f"All spaces from source_samples must be returned, got {result!r}"
        )

    def test_derives_from_url_in_sources_wiki_spaces_pattern(self):
        """Priority 2: space extracted from /wiki/spaces/{SPACE}/ URL in sources."""
        sources = [
            {
                "kind": "confluence",
                "page_url": "https://example.atlassian.net/wiki/spaces/OCIFACP/pages/18625350641",
            }
        ]
        result = derive_space_allow_list(sources=sources, source_samples={})
        assert result == ["OCIFACP"], (
            f"Space must be extracted from /wiki/spaces/SPACE/ URL, got {result!r}"
        )

    def test_derives_from_url_in_pages_list(self):
        """Space extracted from URL-form entries in sources[].pages list."""
        sources = [
            {
                "kind": "confluence",
                "pages": [
                    "https://confluence.example.com/display/MYSPACE/Project+Tracking"
                ],
            }
        ]
        result = derive_space_allow_list(sources=sources, source_samples={})
        assert result == ["MYSPACE"], (
            f"Space must be extracted from /display/SPACE/ URL in pages list, got {result!r}"
        )

    def test_derives_from_explicit_space_key_on_source(self):
        """Priority 3: explicit source.space key is used."""
        sources = [{"kind": "confluence", "space": "FA", "pages": ["12345678901"]}]
        result = derive_space_allow_list(sources=sources, source_samples={})
        assert result == ["FA"], (
            f"Space must be derived from source.space key, got {result!r}"
        )

    def test_source_samples_wins_over_url(self):
        """Priority 1 (source_samples) beats priority 2 (URL)."""
        sources = [
            {"kind": "confluence",
             "page_url": "https://example.com/wiki/spaces/FA/pages/1"}
        ]
        source_samples = {
            "confluence:1": [{"space": "OCIFACP", "content": "...", "title": "p"}]
        }
        # source_samples (OCIFACP) should win over URL (FA)
        result = derive_space_allow_list(sources=sources, source_samples=source_samples)
        assert "OCIFACP" in result, (
            f"source_samples space (OCIFACP) must win over URL-derived space (FA). Got {result!r}"
        )

    def test_returns_empty_for_bare_numeric_ids_no_samples(self):
        """Bare numeric page IDs with no source_samples and no space -> empty list (underivable)."""
        sources = [{"kind": "confluence", "pages": ["18625350641"]}]
        result = derive_space_allow_list(sources=sources, source_samples={})
        assert result == [], (
            f"Bare numeric IDs with no space context must return [] (underivable), got {result!r}"
        )

    def test_underivable_case_returns_empty_not_hardcoded_guess(self):
        """CRITICAL: underivable case must return [], not a hardcoded guess like ['FA','PROJ'].

        A wrong default is MORE dangerous than an empty list — the P1-E bug
        was caused by hardcoding [FA, PROJ] when the actual space was OCIFACP.
        """
        result = derive_space_allow_list(sources=[], source_samples={})
        assert result == [], (
            f"Underivable case must return [], got {result!r} "
            "(non-empty would mean we guessed a space, which reproduces the P1-E bug)"
        )

    def test_result_is_uppercase(self):
        """Space keys must be normalized to uppercase."""
        source_samples = {
            "confluence:123": [{"space": "ocifacp", "content": "...", "title": "p"}]
        }
        result = derive_space_allow_list(sources=[], source_samples=source_samples)
        assert all(s == s.upper() for s in result), (
            f"Space keys must be uppercase, got {result!r}"
        )

    def test_result_is_deduplicated(self):
        """Duplicate space entries are deduplicated."""
        source_samples = {
            "confluence:111": [{"space": "OCIFACP", "content": "...", "title": "p1"}],
            "confluence:222": [{"space": "OCIFACP", "content": "...", "title": "p2"}],
        }
        result = derive_space_allow_list(sources=[], source_samples=source_samples)
        assert result.count("OCIFACP") == 1, (
            f"Duplicate spaces must be deduplicated, got {result!r}"
        )
