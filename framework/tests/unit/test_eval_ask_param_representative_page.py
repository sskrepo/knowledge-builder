"""Tests for DECISION-020 §5: EVAL Path-A representative page injection for ask_parameterized skills.

FIX 1 coverage:
  T1. ask_parameterized skill with source_samples → exec_inputs[input_param] populated
  T2. ask_parameterized skill with only sources (no source_samples) → exec_inputs[input_param] populated from sources fallback
  T3. ask_parameterized skill with NO representative page → NoRepresentativePageError raised (typed, loud)
  T4. author_fixed skill → exec_inputs unchanged (no input_param injected)
  T5. ask_parameterized skill with source_samples containing URL ref → URL extracted correctly

FIX 2 coverage (routing precision — design_skill_card prompt):
  T6. Skill card with do_not_invoke_if_phrases → resolve_only hard-blocks the single-fact negative query
  T7. Skill card with do_not_invoke_if_phrases → genuine agenda-email positive STILL routes to the skill
  T8. design_skill_card prompt version updated to "1.1" (hot-reload signal)
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from framework.skill_builder.conversation import (
    NoRepresentativePageError,
    _resolve_representative_page,
    SkillBuilderConversation,
    _SessionData,
)
from framework.orchestrator.shim_workflows import ShimWorkflows


# ---------------------------------------------------------------------------
# FIX 1: _resolve_representative_page helper
# ---------------------------------------------------------------------------

class TestResolveRepresentativePage:
    """Unit tests for the _resolve_representative_page helper."""

    def test_returns_page_id_from_source_samples(self):
        """Priority 1: source_samples key 'confluence:{page_id}' → returns the page_id."""
        source_samples = {
            "confluence:18625350641": [{"content": "page body"}],
        }
        result = _resolve_representative_page(source_samples, sources=[])
        assert result == "18625350641"

    def test_returns_url_from_source_samples(self):
        """Priority 1: source_samples key 'confluence:{url}' → returns the URL."""
        url = "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=18625350641"
        source_samples = {
            f"confluence:{url}": [{"content": "page body"}],
        }
        result = _resolve_representative_page(source_samples, sources=[])
        assert result == url

    def test_prefers_source_samples_over_sources(self):
        """Priority 1 wins: source_samples takes precedence over sources fallback."""
        source_samples = {
            "confluence:111": [{"content": "sample from inspect"}],
        }
        sources = [
            {"kind": "confluence", "page_id": "999"},
        ]
        result = _resolve_representative_page(source_samples, sources)
        assert result == "111"

    def test_falls_back_to_sources_page_id(self):
        """Priority 2: when source_samples empty, returns page_id from sources."""
        sources = [
            {"kind": "confluence", "page_id": "18625350641"},
        ]
        result = _resolve_representative_page({}, sources)
        assert result == "18625350641"

    def test_falls_back_to_sources_page_url(self):
        """Priority 2: when source_samples empty and no page_id, returns page_url."""
        url = "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=9999"
        sources = [
            {"kind": "confluence", "page_url": url},
        ]
        result = _resolve_representative_page({}, sources)
        assert result == url

    def test_falls_back_to_sources_pages_list(self):
        """Priority 2: when source_samples empty and no page_id/url, returns first from pages list."""
        sources = [
            {"kind": "confluence", "pages": ["777", "888"]},
        ]
        result = _resolve_representative_page({}, sources)
        assert result == "777"

    def test_skips_non_confluence_sources(self):
        """Non-confluence sources are skipped in fallback."""
        sources = [
            {"kind": "jira", "page_id": "should-not-appear"},
            {"kind": "confluence", "page_id": "correct-id"},
        ]
        result = _resolve_representative_page({}, sources)
        assert result == "correct-id"

    def test_returns_none_when_both_empty(self):
        """Returns None when both source_samples and sources are empty."""
        result = _resolve_representative_page({}, [])
        assert result is None

    def test_skips_source_samples_key_without_confluence_prefix(self):
        """Keys not starting with 'confluence:' are ignored."""
        source_samples = {
            "jira:PROJECT-123": [{"content": "jira issue"}],
        }
        result = _resolve_representative_page(source_samples, sources=[])
        assert result is None


# ---------------------------------------------------------------------------
# FIX 1: _run_eval exec_inputs injection via SkillBuilderConversation
# ---------------------------------------------------------------------------

def _make_skill_store_with_wf_yaml(wf_yaml_text: str):
    """Return a mock skill_store whose read_artifact('workflow_skill') returns wf_yaml_text."""
    ss = MagicMock()
    ss.list_promoted_workflow_skills.return_value = set()

    def _read_artifact(persona, skill_name, artifact_type):
        if artifact_type == "workflow_skill":
            return wf_yaml_text
        return None

    ss.read_artifact.side_effect = _read_artifact
    return ss


def _ask_param_wf_yaml(input_param: str = "page_id") -> str:
    """Return a minimal ask_parameterized workflow_skill YAML."""
    return textwrap.dedent(f"""
        workflow_skill: test_skill
        persona: tpm
        status: draft
        trigger:
          on_request:
            enabled: true
            inputs:
              - name: {input_param}
                type: confluence_page_ref
                required: true
            output_format: email
        skill_card:
          summary: Test email skill
          use_when: Use when drafting a status email
          example_invocations:
            - Draft a status email from this page
        source_binding:
          mode: ask_parameterized
          input_param: {input_param}
          ingest_on_demand: true
          source_type: confluence_page
          space_allow_list:
            - FA
          ephemeral_ttl_seconds: 300
    """).strip()


def _author_fixed_wf_yaml() -> str:
    """Return a minimal author_fixed workflow_skill YAML."""
    return textwrap.dedent("""
        workflow_skill: fixed_skill
        persona: tpm
        status: draft
        trigger:
          on_request:
            enabled: true
            output_format: pptx
        skill_card:
          summary: Fixed skill
          use_when: Use when you need the weekly status deck
          example_invocations:
            - Give me the weekly status deck
        source_binding:
          mode: author_fixed
    """).strip()


class TestRunEvalExecInputsInjection:
    """Test that _run_eval injects the representative page into exec_inputs for ask_parameterized."""

    def _make_conv_with_samples(self, wf_yaml: str, source_samples: dict) -> SkillBuilderConversation:
        """Build a minimal SkillBuilderConversation patched for EVAL Path-A testing."""
        ss = _make_skill_store_with_wf_yaml(wf_yaml)
        # LLM mock that returns valid JSON for extraction (Step 3) so _run_eval
        # advances to Path-A without failing at the extraction step.
        llm = MagicMock()
        llm.chat.return_value = {
            "text": json.dumps({"rag_status": "GREEN", "blockers": []}),
            "tokens_out": 50,
        }
        conv = SkillBuilderConversation(
            persona="tpm",
            user_id="test-user",
            llm=llm,
            skill_store=ss,
        )
        conv._data.persona = "tpm"
        conv._data.skill_name = "test_skill"
        conv._data.normalised_intent = {"scope_domains": ["test_domain"]}
        conv._data.source_samples = source_samples
        conv._data.sources = []
        conv._data.fields = ["rag_status", "blockers"]
        conv._data.design_skill_card = {
            "summary": "Test skill",
            "use_when": "Use when drafting email",
            "routing_queries": {
                "positive": ["Draft a status email from this tracking page"],
                "negative": ["What is the current RAG status value?"],
            },
        }
        return conv

    def test_ask_parameterized_injects_page_id_from_source_samples(self):
        """T1: ask_parameterized with source_samples → exec_inputs[input_param] = page_ref from sample."""
        captured_exec_inputs: dict = {}

        def _fake_execute(wf_cfg, exec_inputs):
            captured_exec_inputs.update(exec_inputs)
            return {"artifact_url": None}

        source_samples = {"confluence:18625350641": [{"content": "page body"}]}
        conv = self._make_conv_with_samples(_ask_param_wf_yaml("page_id"), source_samples)

        with (
            patch("framework.skill_builder.conversation._build_confluence_adapter", return_value=None),
            patch("framework.workflow_runtime.executor.WorkflowExecutor") as mock_executor_cls,
        ):
            mock_executor_instance = MagicMock()
            mock_executor_cls.return_value = mock_executor_instance
            mock_executor_instance.execute_from_config.side_effect = _fake_execute

            # Patch out the parts we don't need (ShimWorkflows, gold writing)
            # ShimWorkflows is imported inside _run_eval, so patch via the module path
            with (
                patch("framework.orchestrator.shim_workflows.ShimWorkflows", side_effect=Exception("no shim")),
                patch.object(conv, "_skill_store") as mock_ss,
            ):
                mock_ss.read_artifact.side_effect = lambda persona, skill_name, artifact_type: (
                    _ask_param_wf_yaml("page_id") if artifact_type == "workflow_skill" else None
                )
                mock_ss.write_gold_row = MagicMock()

                # Run _run_eval — it will fail after Path-A (gold write etc.) but we just
                # need to confirm exec_inputs was populated before execute_from_config ran.
                try:
                    conv._run_eval()
                except Exception:
                    pass  # Expected — gold set write + Path-B will fail without full env

        # The critical assertion: page_id was injected
        assert "page_id" in captured_exec_inputs, (
            f"exec_inputs must contain 'page_id' for ask_parameterized skill. Got: {captured_exec_inputs}"
        )
        assert captured_exec_inputs["page_id"] == "18625350641", (
            f"Expected page_id='18625350641', got {captured_exec_inputs['page_id']!r}"
        )

    def test_ask_parameterized_no_representative_page_raises_typed_error(self):
        """T3: ask_parameterized with source_samples whose keys have NO confluence: prefix
        AND no sources → _resolve_representative_page returns None → NoRepresentativePageError.

        This exercises the DECISION-020 §4/§6 loud-failure path. The samples are present
        for extraction (so the early empty-samples guard doesn't fire) but the representative
        page resolution cannot extract a page ref from the non-standard keys.
        """
        # Source samples with non-standard key (no 'confluence:' prefix) → extraction works
        # but _resolve_representative_page cannot extract a page ref.
        non_standard_samples = {"jira:PROJECT-123": [{"content": "some content"}]}
        conv = self._make_conv_with_samples(_ask_param_wf_yaml("page_id"), non_standard_samples)
        conv._data.sources = []  # No fallback sources either

        # Need a working LLM mock for extraction to avoid earlier failure
        extract_response = json.dumps({"rag_status": "GREEN", "blockers": []})
        conv._llm.chat.return_value = {"text": extract_response, "tokens_out": 50}

        with (
            patch("framework.skill_builder.conversation._build_confluence_adapter", return_value=None),
            patch("framework.workflow_runtime.executor.WorkflowExecutor"),
            patch("framework.orchestrator.shim_workflows.ShimWorkflows", side_effect=Exception("no shim")),
            patch.object(conv, "_skill_store") as mock_ss,
        ):
            mock_ss.read_artifact.side_effect = lambda persona, skill_name, artifact_type: (
                _ask_param_wf_yaml("page_id") if artifact_type == "workflow_skill" else None
            )

            with pytest.raises(NoRepresentativePageError) as exc_info:
                conv._run_eval()

        err = exc_info.value
        assert "ask_parameterized" in str(err)
        assert "page_id" in str(err)
        assert "INSPECT_SOURCES" in str(err)
        assert err.input_param == "page_id"

    def test_author_fixed_skill_exec_inputs_unchanged(self):
        """T4: author_fixed skill → exec_inputs does NOT get a page_id injected."""
        captured_exec_inputs: dict = {}

        def _fake_execute(wf_cfg, exec_inputs):
            captured_exec_inputs.update(exec_inputs)
            return {"artifact_url": None}

        source_samples = {"confluence:99999": [{"content": "page body"}]}
        conv = self._make_conv_with_samples(_author_fixed_wf_yaml(), source_samples)
        conv._data.skill_name = "fixed_skill"

        with (
            patch("framework.skill_builder.conversation._build_confluence_adapter", return_value=None),
            patch("framework.workflow_runtime.executor.WorkflowExecutor") as mock_executor_cls,
            patch("framework.orchestrator.shim_workflows.ShimWorkflows", side_effect=Exception("no shim")),
            patch.object(conv, "_skill_store") as mock_ss,
        ):
            mock_ss.read_artifact.side_effect = lambda persona, skill_name, artifact_type: (
                _author_fixed_wf_yaml() if artifact_type == "workflow_skill" else None
            )
            mock_executor_instance = MagicMock()
            mock_executor_cls.return_value = mock_executor_instance
            mock_executor_instance.execute_from_config.side_effect = _fake_execute

            try:
                conv._run_eval()
            except Exception:
                pass

        # author_fixed: no page_id injection
        assert "page_id" not in captured_exec_inputs, (
            f"author_fixed skill must NOT inject page_id into exec_inputs. Got: {captured_exec_inputs}"
        )

    def test_ask_parameterized_custom_input_param_name(self):
        """T5: ask_parameterized skill with non-default input_param name → correct key injected."""
        captured_exec_inputs: dict = {}

        def _fake_execute(wf_cfg, exec_inputs):
            captured_exec_inputs.update(exec_inputs)
            return {"artifact_url": None}

        source_samples = {
            "confluence:https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=42": [
                {"content": "page body"}
            ],
        }
        conv = self._make_conv_with_samples(_ask_param_wf_yaml("confluence_page_ref"), source_samples)

        with (
            patch("framework.skill_builder.conversation._build_confluence_adapter", return_value=None),
            patch("framework.workflow_runtime.executor.WorkflowExecutor") as mock_executor_cls,
            patch("framework.orchestrator.shim_workflows.ShimWorkflows", side_effect=Exception("no shim")),
            patch.object(conv, "_skill_store") as mock_ss,
        ):
            mock_ss.read_artifact.side_effect = lambda persona, skill_name, artifact_type: (
                _ask_param_wf_yaml("confluence_page_ref") if artifact_type == "workflow_skill" else None
            )
            mock_executor_instance = MagicMock()
            mock_executor_cls.return_value = mock_executor_instance
            mock_executor_instance.execute_from_config.side_effect = _fake_execute

            try:
                conv._run_eval()
            except Exception:
                pass

        assert "confluence_page_ref" in captured_exec_inputs, (
            f"exec_inputs must use the declared input_param='confluence_page_ref'. Got: {captured_exec_inputs}"
        )
        # The URL is the full source_id extracted from the key
        assert "confluence.oraclecorp.com" in captured_exec_inputs["confluence_page_ref"]


# ---------------------------------------------------------------------------
# FIX 2: routing precision — do_not_invoke_if_phrases hard-blocks single-fact queries
# ---------------------------------------------------------------------------

def _make_skill_card_with_veto_phrases(
    skill_name: str = "project_tracking_stakeholder_status_email",
    persona: str = "tpm",
) -> dict:
    """Return a skill card as the NEW card-gen prompt would produce — with do_not_invoke_if_phrases."""
    return {
        "workflow_skill": skill_name,
        "persona": persona,
        "status": "draft",
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [{"name": "page_id", "type": "confluence_page_ref", "required": True}],
                "output_format": "email",
            }
        },
        "skill_card": {
            "summary": (
                "Generates a ready-to-send weekly stakeholder status meeting agenda email (.eml) "
                "by extracting key updates, health signals, blockers, and exec asks from a project tracking page."
            ),
            "use_when": (
                "Use when you need to draft a full weekly stakeholder meeting agenda email (.eml) "
                "with RAG summary, schedule health, blockers, next steps, and exec asks — "
                "not for single-value lookups."
            ),
            "example_invocations": [
                "Draft a weekly stakeholder agenda email (.eml) from this project tracking page",
                "Generate a stakeholder meeting agenda .eml for my weekly TPM tracking sync",
            ],
            # NEW: do_not_invoke_if_phrases generated by updated design_skill_card prompt
            "do_not_invoke_if_phrases": [
                "what is the current rag",
                "what is the current status",
                "what is the status of",
                "tell me the value",
            ],
            "do_not_use_for": (
                "Single-fact or value lookups (use vector_search). "
                "Slide decks (use pptx skill). "
                "Live operational data (use query_fleet)."
            ),
            "routing_queries": {
                "positive": [
                    "Create a weekly stakeholder status agenda email (.eml) based on this project tracking Confluence page",
                    "Turn this project tracking page into a publishable agenda email (.eml) for my weekly cross-team status meeting",
                    "I need a draft agenda email (.eml) for next week stakeholder tracking meeting",
                    "Generate the week-of stakeholder status agenda email (.eml) from our project tracker",
                    "Prepare an agenda email draft (.eml) for my weekly TPM tracking sync using this project tracking page",
                ],
                "negative": [
                    "What is the current RAG status for the project on this page?",
                    "What is the difference between RAG status and schedule health in TPM reporting?",
                    "Create a slide deck summarizing this project tracking page for the weekly exec review.",
                ],
            },
        },
        "source_binding": {
            "mode": "ask_parameterized",
            "input_param": "page_id",
            "ingest_on_demand": True,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
            "ephemeral_ttl_seconds": 300,
        },
    }


def _make_shim_with_one_card(card_cfg: dict) -> ShimWorkflows:
    """Return a ShimWorkflows instance backed by a single in-memory card."""
    wf_dir = Path("/tmp/shim_test_wf_dir")
    shim = ShimWorkflows.__new__(ShimWorkflows)
    shim._wf_dir = wf_dir
    shim._skill_store = None
    # Directly inject the card so we bypass disk I/O
    from framework.orchestrator.shim_workflows import _cfg_to_card
    card = _cfg_to_card(card_cfg, source="test", path="")
    shim._cards = [card]
    # Monkey-patch all_cards_including_draft to return our card
    shim.all_cards_including_draft = lambda: [card]
    shim.all_cards = lambda: [card]
    return shim


class TestRoutingPrecisionSingleFactVeto:
    """T6/T7: do_not_invoke_if_phrases hard-blocks single-fact negative, doesn't break positives."""

    def test_single_fact_rag_status_query_NOT_routed_to_email_skill(self):
        """T6: 'What is the current RAG status for the project on this page?' must NOT route to the email skill.

        This exercises REAL classifier logic (resolve_only token-overlap + hard phrase exclusion).
        The query contains do_not_invoke_if_phrases fragment 'what is the current' → hard-blocked.
        """
        card_cfg = _make_skill_card_with_veto_phrases()
        shim = _make_shim_with_one_card(card_cfg)

        # The problematic negative from the stuck session self-test
        negative_query = "What is the current RAG status for the project on this page?"
        result = shim.resolve_only(negative_query, scope="ingest_or_later")

        assert not result["matched"] or result["skill_id"] != "tpm.project_tracking_stakeholder_status_email", (
            f"Single-fact RAG status query must NOT route to email skill. "
            f"resolve_only returned: {result}"
        )

    def test_genuine_agenda_email_positive_still_routes_to_skill(self):
        """T7: Genuine email-agenda query STILL routes to the skill (no positive regression).

        Ensures the do_not_invoke_if_phrases veto doesn't block legitimate positives.
        """
        card_cfg = _make_skill_card_with_veto_phrases()
        shim = _make_shim_with_one_card(card_cfg)

        positive_queries = [
            "Create a weekly stakeholder status agenda email (.eml) based on this project tracking Confluence page",
            "Generate the week-of stakeholder status agenda email (.eml) from our project tracker",
            "Prepare an agenda email draft (.eml) for my weekly TPM tracking sync using this project tracking page",
        ]

        for q in positive_queries:
            result = shim.resolve_only(q, scope="ingest_or_later")
            assert result["matched"] and result["skill_id"] == "tpm.project_tracking_stakeholder_status_email", (
                f"Genuine positive query should route to email skill. "
                f"Query: {q!r}. Result: {result}"
            )

    def test_do_not_invoke_if_phrases_in_cfg_to_card(self):
        """T6 supporting: _cfg_to_card correctly extracts do_not_invoke_if_phrases from skill_card."""
        from framework.orchestrator.shim_workflows import _cfg_to_card
        card_cfg = _make_skill_card_with_veto_phrases()
        card = _cfg_to_card(card_cfg, source="test", path="")
        # do_not_invoke_if_phrases must be carried through
        assert card.get("do_not_invoke_if_phrases") is not None
        assert len(card["do_not_invoke_if_phrases"]) >= 2
        # Verify the specific phrase that vetoes our negative query
        phrases = [p.lower() for p in card["do_not_invoke_if_phrases"]]
        assert any("what is the current" in p for p in phrases), (
            f"Hard-veto phrase 'what is the current' must be in do_not_invoke_if_phrases. Got: {phrases}"
        )


class TestDesignSkillCardPromptVersion:
    """T8: design_skill_card prompt version is updated to 1.1 (hot-reload signal)."""

    def test_design_skill_card_prompt_version_is_1_1(self):
        """The design_skill_card prompt must be version '1.1' after the routing precision fix."""
        import pathlib
        import yaml as _yaml
        # framework/config/prompts/skill_builder.yaml
        # test file is at framework/tests/unit/ → parents[2] = framework/
        prompts_dir = pathlib.Path(__file__).parents[2] / "config" / "prompts"
        skill_builder_yaml = prompts_dir / "skill_builder.yaml"
        raw = _yaml.safe_load(skill_builder_yaml.read_text())
        version = raw.get("prompts", {}).get("design_skill_card", {}).get("version")
        assert version == "1.1", (
            f"design_skill_card prompt version must be '1.1' after routing precision fix. Got: {version!r}"
        )

    def test_design_skill_card_prompt_contains_do_not_invoke_if_phrases_instruction(self):
        """The design_skill_card prompt template must instruct the LLM to produce do_not_invoke_if_phrases."""
        import pathlib
        import yaml as _yaml
        prompts_dir = pathlib.Path(__file__).parents[2] / "config" / "prompts"
        raw = _yaml.safe_load((prompts_dir / "skill_builder.yaml").read_text())
        template = raw.get("prompts", {}).get("design_skill_card", {}).get("template", "")
        assert "do_not_invoke_if_phrases" in template, (
            "design_skill_card prompt template must include 'do_not_invoke_if_phrases' instruction"
        )
        assert "single-fact" in template.lower(), (
            "design_skill_card prompt must explicitly mention 'single-fact' in the do_not_invoke_if_phrases instruction"
        )
