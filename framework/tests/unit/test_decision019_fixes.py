"""Tests for DECISION-019 fixes: RC1, RC2, and Finding-B.

RC1 — author_fixed skills now emit a source_binding block with pinned_ref
      derived from source_samples, and the executor resolves THAT specific page.

RC2 — design_skill output with a non-catalog-id layout is rejected loud at
      _run_design_skill; a valid internal_id passes; the prompt reasoning section
      contains NO internal_id while the output-schema enum does.

Finding-B — PptxRenderer.render() raises ValueError for unresolvable layout
             (no stub fallback); default/weekly_exec_review_v1 still renders.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_SOURCE_SAMPLES = {
    "confluence:https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project": [
        {
            "page_id": "12345678901",
            "space": "OCIFACP",
            "title": "FAaaS Kiwi Project",
            "text_len": 3987,
            "url": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
        }
    ]
}

SAMPLE_SOURCES = [
    {
        "source_id": "confluence:https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
        "kind": "confluence_page",
    }
]


# ===========================================================================
# RC1 Tests — author_fixed pinned source binding
# ===========================================================================


class TestRC1DerivePinnedSource:
    """derive_pinned_source() extracts pinned source from session state."""

    def test_derives_pinned_ref_from_source_samples_url_key(self):
        """Priority 1: pinned_ref comes from source_samples key's source_id (URL form)."""
        from framework.skill_builder.synthesize_workflow import derive_pinned_source
        result = derive_pinned_source(
            sources=SAMPLE_SOURCES,
            source_samples=SAMPLE_SOURCE_SAMPLES,
        )
        assert result is not None, "Must return a pinned_source dict, not None"
        assert result["pinned_ref"] == (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        assert result["source_type"] == "confluence_page"

    def test_space_allow_list_derived_from_source_samples(self):
        """Space key is derived from source_samples sample.space field."""
        from framework.skill_builder.synthesize_workflow import derive_pinned_source
        result = derive_pinned_source(
            sources=SAMPLE_SOURCES,
            source_samples=SAMPLE_SOURCE_SAMPLES,
        )
        assert result is not None
        assert "OCIFACP" in result["space_allow_list"]

    def test_no_external_source_returns_none(self):
        """When no source_samples and no URL sources, returns None (pure in-KB)."""
        from framework.skill_builder.synthesize_workflow import derive_pinned_source
        result = derive_pinned_source(sources=[], source_samples={})
        assert result is None, (
            "No external fixed source → None must be returned; "
            "no source_binding block should be emitted"
        )

    def test_none_source_samples_returns_none(self):
        """When source_samples is None, returns None."""
        from framework.skill_builder.synthesize_workflow import derive_pinned_source
        result = derive_pinned_source(sources=[], source_samples=None)
        assert result is None

    def test_url_form_source_fallback(self):
        """Priority 2: when source_samples is empty, derive from source URL in sources."""
        from framework.skill_builder.synthesize_workflow import derive_pinned_source
        sources_with_url = [
            {"page_url": "https://confluence.oraclecorp.com/display/OCIFACP/Some+Page"}
        ]
        result = derive_pinned_source(sources=sources_with_url, source_samples={})
        assert result is not None
        assert result["pinned_ref"] == "https://confluence.oraclecorp.com/display/OCIFACP/Some+Page"


class TestRC1SynthesizeWorkflowEmitsPinnedRef:
    """synthesize_workflow_skill emits source_binding block for author_fixed with pinned source."""

    def test_author_fixed_with_pinned_source_emits_source_binding(self):
        """When pinned_source is provided and mode is author_fixed, block is emitted."""
        from framework.skill_builder.synthesize_workflow import synthesize_workflow_skill
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="faaas_kiwi_project_pptx",
            intent={
                "task_description": "Weekly exec review for FAaaS Kiwi Project",
                "output_format": "pptx",
                "trigger": {"on_request": True},
            },
            fields=["slide_title", "rag_summary"],
            source_binding_mode="author_fixed",
            pinned_source={
                "pinned_ref": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
                "source_type": "confluence_page",
                "space_allow_list": ["OCIFACP"],
            },
        )
        assert "source_binding" in result, (
            "source_binding block must be emitted when author_fixed + pinned_source provided"
        )
        sb = result["source_binding"]
        assert sb["mode"] == "author_fixed"
        assert sb["pinned_ref"] == (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        assert sb["source_type"] == "confluence_page"
        assert "OCIFACP" in sb["space_allow_list"]
        assert sb["ingest_on_demand"] is False  # default: KB lookup, not live fetch

    def test_author_fixed_without_pinned_source_emits_no_block(self):
        """When pinned_source is None and mode is author_fixed, NO source_binding block."""
        from framework.skill_builder.synthesize_workflow import synthesize_workflow_skill
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="pure_in_kb_skill",
            intent={
                "task_description": "A skill with no external fixed source",
                "output_format": "markdown",
                "trigger": {"on_request": True},
            },
            fields=["summary"],
            source_binding_mode="author_fixed",
            pinned_source=None,
        )
        assert "source_binding" not in result, (
            "NO source_binding block must be emitted for author_fixed with no pinned_source "
            "(pure in-KB skill — unchanged pre-DECISION-019 behavior)"
        )

    def test_ask_parameterized_unaffected_by_pinned_source(self):
        """ask_parameterized skills still emit the standard 6-field block (RC1 does not change them)."""
        from framework.skill_builder.synthesize_workflow import synthesize_workflow_skill
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="param_skill",
            intent={
                "task_description": "Accept a page from the user",
                "output_format": "email",
                "trigger": {"on_request": True},
            },
            fields=["project_name"],
            source_binding_mode="ask_parameterized",
            space_allow_list=["FA"],
            pinned_source={
                "pinned_ref": "some_url",
                "source_type": "confluence_page",
                "space_allow_list": ["FA"],
            },
        )
        sb = result["source_binding"]
        assert sb["mode"] == "ask_parameterized"
        assert "input_param" in sb
        assert "ephemeral_ttl_seconds" in sb
        assert "pinned_ref" not in sb, (
            "ask_parameterized block must NOT contain pinned_ref"
        )


class TestRC1ExecutorResolvePinnedRef:
    """executor._retrieve_author_fixed_pinned resolves the pinned page, not generic KB."""

    def _make_executor(self, retrievers=None, shim_kb=None, confluence_adapter=None):
        from framework.workflow_runtime.executor import WorkflowExecutor
        return WorkflowExecutor(
            store=None,
            llm=None,
            retrievers=retrievers or {},
            shim_kb=shim_kb,
            confluence_adapter=confluence_adapter,
        )

    def _make_cfg(self, pinned_ref: str, ingest_on_demand: bool = False) -> dict:
        return {
            "workflow_skill": "tpm.faaas_kiwi_project_pptx",
            "persona": "tpm",
            "source_binding": {
                "mode": "author_fixed",
                "source_type": "confluence_page",
                "pinned_ref": pinned_ref,
                "space_allow_list": ["OCIFACP"],
                "ingest_on_demand": ingest_on_demand,
            },
            "requires_extractions": [{"kb": "tpm.faaas_kiwi_project_pptx"}],
        }

    def test_pinned_page_found_in_kb_returns_matching_passages(self):
        """When pinned page is in KB, executor returns only that page's passages.

        Uses a URL form that encodes the page ID so _resolve_page_id can extract
        the numeric ID used by _passage_matches_page_id.
        """
        from framework.workflow_runtime.executor import WorkflowExecutor, ConfluencePageNotInKBError
        pinned_page_id = "12345678901"
        # Use a URL that _resolve_page_id can extract the numeric ID from
        pinned_ref = f"https://confluence.oraclecorp.com/pages/{pinned_page_id}/?pageId={pinned_page_id}"

        # Mock retriever that returns one passage matching the pinned page
        matching_passage = MagicMock()
        matching_passage.text = "FAaaS Kiwi Project content"
        matching_passage.citation_url = f"https://confluence.oraclecorp.com/pages/{pinned_page_id}"
        matching_passage.metadata = {"page_id": pinned_page_id}

        mock_retriever = MagicMock(return_value=[matching_passage])
        mock_shim_kb = MagicMock()
        mock_shim_kb.all_cards.return_value = [
            {"name": "faaas_kiwi_project_pptx", "persona": "tpm", "retrieval_tools": ["search_wiki"]}
        ]

        executor = self._make_executor(
            retrievers={"search_wiki": mock_retriever},
            shim_kb=mock_shim_kb,
        )
        cfg = self._make_cfg(pinned_ref=pinned_ref)
        source_binding = cfg["source_binding"]
        passages = executor._retrieve_author_fixed_pinned(cfg, {"input": "kiwi project update"}, source_binding)

        assert len(passages) == 1
        assert passages[0]["text"] == "FAaaS Kiwi Project content"

    def test_pinned_page_not_in_kb_no_ingest_on_demand_raises(self):
        """When pinned page is NOT in KB and ingest_on_demand is False, hard-fail."""
        from framework.workflow_runtime.executor import ConfluencePageNotInKBError
        pinned_ref = "https://confluence.oraclecorp.com/display/OCIFACP/FAaaS+Kiwi+Project"

        # Retriever returns empty (page not in KB)
        mock_retriever = MagicMock(return_value=[])
        mock_shim_kb = MagicMock()
        mock_shim_kb.all_cards.return_value = [
            {"name": "faaas_kiwi_project_pptx", "persona": "tpm", "retrieval_tools": ["search_wiki"]}
        ]

        from framework.workflow_runtime.executor import WorkflowExecutor
        executor = WorkflowExecutor(
            store=None, llm=None,
            retrievers={"search_wiki": mock_retriever},
            shim_kb=mock_shim_kb,
            confluence_adapter=None,
        )
        cfg = self._make_cfg(pinned_ref=pinned_ref, ingest_on_demand=False)
        source_binding = cfg["source_binding"]

        with pytest.raises(ConfluencePageNotInKBError):
            executor._retrieve_author_fixed_pinned(cfg, {"input": "kiwi update"}, source_binding)

    def test_retrieve_for_inputs_dispatches_to_pinned_for_author_fixed_with_pinned_ref(self):
        """_retrieve_for_inputs dispatches to _retrieve_author_fixed_pinned when mode=author_fixed + pinned_ref."""
        from framework.workflow_runtime.executor import WorkflowExecutor
        cfg = self._make_cfg(
            pinned_ref="https://confluence.oraclecorp.com/display/OCIFACP/FAaaS+Kiwi+Project"
        )

        executor = self._make_executor()
        with patch.object(executor, "_retrieve_author_fixed_pinned") as mock_pinned:
            mock_pinned.return_value = [{"text": "pinned content", "citation": "https://...", "metadata": {}}]
            passages = executor._retrieve_for_inputs(cfg, {"input": "kiwi"}, [])

        mock_pinned.assert_called_once()
        assert passages[0]["text"] == "pinned content"

    def test_ask_parameterized_path_unaffected(self):
        """ask_parameterized skills still use _retrieve_ask_parameterized (RC1 does not regress them)."""
        from framework.workflow_runtime.executor import WorkflowExecutor, ConfluencePageNotInKBError
        cfg = {
            "workflow_skill": "tpm.param_skill",
            "persona": "tpm",
            "source_binding": {
                "mode": "ask_parameterized",
                "input_param": "page_id",
                "ingest_on_demand": False,
                "space_allow_list": ["FA"],
                "ephemeral_ttl_seconds": 300,
            },
            "requires_extractions": [{"kb": "tpm.param_skill"}],
        }
        executor = self._make_executor()
        with patch.object(executor, "_retrieve_ask_parameterized") as mock_ask:
            mock_ask.side_effect = ConfluencePageNotInKBError("12345", "tpm.param_skill")
            with pytest.raises(ConfluencePageNotInKBError):
                executor._retrieve_for_inputs(cfg, {"page_id": "12345"}, [])
        mock_ask.assert_called_once()

    def test_author_fixed_without_pinned_ref_uses_normal_path(self):
        """author_fixed with NO pinned_ref falls through to normal KB retrieval (pre-DECISION-019 path)."""
        from framework.workflow_runtime.executor import WorkflowExecutor
        cfg = {
            "workflow_skill": "tpm.legacy_skill",
            "persona": "tpm",
            # No source_binding block — pure author_fixed, no pinned_ref
            "requires_extractions": [{"kb": "tpm.legacy_skill"}],
        }
        executor = self._make_executor()
        with patch.object(executor, "_retrieve_author_fixed_pinned") as mock_pinned:
            with patch.object(executor, "_load_fixture_passages") as mock_fixture:
                mock_fixture.return_value = [{"text": "fixture content", "citation": "", "metadata": {}}]
                passages = executor._retrieve_for_inputs(cfg, {"input": "query"}, [])

        mock_pinned.assert_not_called()
        assert len(passages) == 1
        assert passages[0]["text"] == "fixture content"


# ===========================================================================
# RC2 Tests — DESIGN_SKILL layout constrained enum validation
# ===========================================================================


class TestRC2DesignSkillLayoutValidation:
    """_run_design_skill rejects non-catalog layout ids loud at design time."""

    def _make_conv_with_design(self, layout_value):
        """Build a conversation with a mock LLM that returns the given layout value."""
        from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData

        design_output = {
            "schema": {
                "properties": {
                    "slide_title": {"type": "string", "description": "Title of the slide"}
                },
                "required": ["slide_title"],
            },
            "source_bindings": {"slide_title": ["confluence:test"]},
            "workflow_shape": {
                "output_format": "pptx",
                "layout": layout_value,
                "layout_rationale": "Single-slide exec review format",
                "trigger": {"on_request": True},
            },
            "reuse_plan": {"covered": {}, "gaps": []},
            "blocking_questions": [],
        }

        mock_llm = MagicMock()
        card_output = {
            "summary": "Test skill card.",
            "use_when": "User asks for test output.",
            "example_invocations": ["Generate the test PPTX. Output: pptx."],
            "routing_queries": {"positive": ["test deck"], "negative": ["text summary"]},
        }
        mock_llm.chat.side_effect = [
            {"text": json.dumps(design_output), "tokens_out": 200},
            {"text": json.dumps(card_output), "tokens_out": 100},
        ]

        conv = object.__new__(SkillBuilderConversation)
        conv._state = "DESIGN_SKILL"
        conv._data = _SessionData(persona="tpm", intent_description="Test skill")
        conv._data.skill_name = "test_skill"
        conv._data.output_format = "pptx"
        conv._data.normalised_intent = {"output_kind": "pptx"}
        conv._data.source_capability = [
            {"source_id": "confluence:test", "available_fields": [
                {"field": "slide_title", "type": "string", "confidence": "high", "evidence": "title"}
            ]}
        ]
        conv._llm = mock_llm
        conv._skill_store = None
        return conv

    def test_prose_layout_raises_at_design_time(self):
        """Non-catalog prose layout value is rejected loud at _run_design_skill."""
        prose_layout = (
            "Standard executive order single-slide: title/status first; then RAG summary"
        )
        conv = self._make_conv_with_design(prose_layout)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            with pytest.raises(RuntimeError, match="DECISION-019 RC2"):
                conv._run_design_skill()

    def test_valid_internal_id_weekly_exec_review_passes(self):
        """Valid catalog id weekly_exec_review_v1 passes validation without error."""
        conv = self._make_conv_with_design("weekly_exec_review_v1")

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            # Must not raise
            turn = conv._run_design_skill()
        # After successful design, state advances
        assert conv._data.design is not None

    def test_valid_internal_id_default_passes(self):
        """Valid catalog id 'default' passes validation without error."""
        conv = self._make_conv_with_design("default")

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            turn = conv._run_design_skill()
        assert conv._data.design is not None

    def test_null_layout_passes(self):
        """Null layout (no layout selection) passes validation."""
        conv = self._make_conv_with_design(None)

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            turn = conv._run_design_skill()
        assert conv._data.design is not None

    def test_unrecognised_id_raises_loud(self):
        """Unrecognised (but ID-looking) layout string is rejected loud."""
        conv = self._make_conv_with_design("some_unregistered_layout_v99")

        with patch("framework.orchestrator.shim_kb.ShimKb") as mock_shim_cls:
            mock_shim_cls.return_value.cards_visible_to.return_value = []
            mock_shim_cls.return_value.all_cards.return_value = []
            with pytest.raises(RuntimeError, match="not a registered catalog internal_id"):
                conv._run_design_skill()


class TestRC2PromptDecision014Mitigation:
    """DECISION-014 mitigation: internal_ids appear ONLY in OUTPUT SCHEMA section, not reasoning rules."""

    def _get_design_skill_template(self) -> str:
        import yaml
        from pathlib import Path
        path = Path(__file__).resolve().parents[3] / "framework" / "config" / "prompts" / "skill_builder.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data["prompts"]["design_skill"]["template"]

    def test_layout_valid_ids_placeholder_in_output_schema_section(self):
        """The {layout_valid_ids} placeholder must appear in the OUTPUT SCHEMA section."""
        template = self._get_design_skill_template()
        # Verify it's present
        assert "{layout_valid_ids}" in template, (
            "design_skill template must reference {layout_valid_ids} placeholder — "
            "DECISION-019 RC2 fix not applied"
        )

    def test_layout_valid_ids_in_required_vars(self):
        """layout_valid_ids must be in design_skill required_vars."""
        import yaml
        from pathlib import Path
        path = Path(__file__).resolve().parents[3] / "framework" / "config" / "prompts" / "skill_builder.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        required = data["prompts"]["design_skill"]["required_vars"]
        assert "layout_valid_ids" in required, (
            f"layout_valid_ids not in required_vars: {required}"
        )

    def test_hardcoded_weekly_exec_review_v1_not_in_rules_section(self):
        """DECISION-014 mitigation: no hardcoded preset id appears in the reasoning Rules section."""
        template = self._get_design_skill_template()
        # The OUTPUT SCHEMA CONSTRAINT section contains {layout_valid_ids} (injected at runtime).
        # The Rules section must NOT have hardcoded identifiers like 'weekly_exec_review_v1'.
        # Split on the Rules: marker to check only that section.
        rules_section = ""
        if "Rules:" in template:
            rules_section = template[template.index("Rules:"):].lower()
        assert "weekly_exec_review_v1" not in rules_section, (
            "DECISION-014 violation: hardcoded preset id 'weekly_exec_review_v1' appears "
            "in the Rules reasoning section. It must only appear in the output schema enum "
            "(injected via {layout_valid_ids} placeholder)."
        )

    def test_version_is_1_3(self):
        """design_skill prompt must be bumped to v1.3 for DECISION-019 RC2."""
        import yaml
        from pathlib import Path
        path = Path(__file__).resolve().parents[3] / "framework" / "config" / "prompts" / "skill_builder.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        version = data["prompts"]["design_skill"]["version"]
        assert version == "1.3", (
            f"design_skill version must be 1.3 (DECISION-019 RC2), got {version!r}"
        )

    def test_layout_preset_catalog_still_present_for_reasoning(self):
        """The layout_preset_catalog injection (human descriptions) must still be present
        for the LLM to reason over descriptions before mapping to an id."""
        template = self._get_design_skill_template()
        assert "{layout_preset_catalog}" in template, (
            "design_skill must still inject {layout_preset_catalog} for LLM reasoning "
            "(DECISION-014: LLM reasons over descriptions, outputs an id)"
        )


# ===========================================================================
# Finding-B Tests — PptxRenderer hard-fails on unresolvable layout
# ===========================================================================


class TestFindingBRendererHardFail:
    """PptxRenderer.render() raises ValueError for unresolvable layout ids."""

    def test_unresolvable_layout_raises_value_error(self):
        """An unresolvable layout id raises ValueError (no stub fallback)."""
        from framework.renderers.pptx_renderer import PptxRenderer
        renderer = PptxRenderer()
        data = {
            "title": "Test",
            "layout": "some_prose_that_is_not_a_catalog_id",
            "sections": {"s": "content"},
        }
        with pytest.raises(ValueError, match="not a registered catalog internal_id"):
            renderer.render(data)

    def test_unresolvable_layout_error_message_is_actionable(self):
        """The ValueError message must be actionable (DECISION-019 Finding-B)."""
        from framework.renderers.pptx_renderer import PptxRenderer
        renderer = PptxRenderer()
        data = {"layout": "prose_layout_value", "title": "Test", "sections": {}}
        with pytest.raises(ValueError) as exc_info:
            renderer.render(data)
        msg = str(exc_info.value)
        # Must mention the layout id and DECISION-019
        assert "prose_layout_value" in msg
        assert "DECISION-019" in msg

    def test_default_layout_still_renders_successfully(self):
        """Skills with layout=default are completely unaffected by Finding-B."""
        pytest.importorskip("pptx")
        from framework.renderers.pptx_renderer import PptxRenderer
        renderer = PptxRenderer()
        data = {
            "title": "Default deck",
            "layout": "default",
            "sections": {"Section A": "Content A"},
        }
        result = renderer.render(data)
        assert isinstance(result, bytes)
        assert len(result) > 100, "Rendered PPTX must be non-trivial bytes"

    def test_weekly_exec_review_v1_still_renders_successfully(self):
        """Skills with layout=weekly_exec_review_v1 are completely unaffected."""
        pytest.importorskip("pptx")
        from framework.renderers.pptx_renderer import PptxRenderer
        renderer = PptxRenderer()
        data = {
            "title": "Exec Review",
            "layout": "weekly_exec_review_v1",
            "sections": {"scope": "Test scope"},
        }
        result = renderer.render(data)
        assert isinstance(result, bytes)
        assert len(result) > 100

    def test_no_layout_renders_default_successfully(self):
        """Empty/absent layout (no layout key) renders default without error."""
        pytest.importorskip("pptx")
        from framework.renderers.pptx_renderer import PptxRenderer
        renderer = PptxRenderer()
        data = {"title": "No layout", "sections": {"s": "content"}}
        result = renderer.render(data)
        assert isinstance(result, bytes)
        assert len(result) > 100


# ===========================================================================
# RC1-A Tests — display-by-title URL pinned_ref matching (DECISION-019)
# ===========================================================================
# Root cause: _CONFLUENCE_PAGE_REF_PATTERNS has NO pattern for
# /display/{SPACE}/{Title} URL form → _resolve_page_id returns URL unchanged
# → _passage_matches_page_id compared URL against ingested passages' numeric
# pageId → 0 matches → hard-fail ConfluencePageNotInKBError.
# Fix: _passage_matches_page_id detects display URL form and delegates to
# _passage_matches_display_url() for space+title matching.
# ===========================================================================


class TestRC1ADisplayUrlHelpers:
    """_is_display_url, _extract_display_url_parts, _passage_matches_display_url."""

    def test_is_display_url_matches_standard_form(self):
        """Standard Confluence display URL is detected."""
        from framework.workflow_runtime.executor import _is_display_url
        assert _is_display_url(
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )

    def test_is_display_url_matches_lowercase_display(self):
        """Case-insensitive match on /display/ segment."""
        from framework.workflow_runtime.executor import _is_display_url
        assert _is_display_url("https://confluence.example.com/display/MYSPACE/Some+Page")

    def test_is_display_url_rejects_pageid_url(self):
        """?pageId=<numeric> URL is NOT a display URL."""
        from framework.workflow_runtime.executor import _is_display_url
        assert not _is_display_url(
            "https://confluence.oraclecorp.com/pages/18625350641?pageId=18625350641"
        )

    def test_is_display_url_rejects_bare_numeric_id(self):
        """Bare numeric page ID string is not a display URL."""
        from framework.workflow_runtime.executor import _is_display_url
        assert not _is_display_url("18625350641")

    def test_extract_display_url_parts_space_and_title(self):
        """Returns (SPACE_KEY, decoded_title) for a standard display URL."""
        from framework.workflow_runtime.executor import _extract_display_url_parts
        parts = _extract_display_url_parts(
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        assert parts is not None
        space, title = parts
        assert space == "OCIFACP"
        assert "kiwi" in title.lower()  # URL-decoded + normalised

    def test_extract_display_url_parts_title_with_percent_encoding(self):
        """URL-encoded chars in title are decoded."""
        from framework.workflow_runtime.executor import _extract_display_url_parts
        parts = _extract_display_url_parts(
            "https://confluence.example.com/display/ENG/My%20Page%20Title"
        )
        assert parts is not None
        _, title = parts
        assert "my" in title.lower()
        assert "page" in title.lower()

    def test_extract_display_url_parts_none_for_non_display_url(self):
        """Returns None for non-display URLs."""
        from framework.workflow_runtime.executor import _extract_display_url_parts
        assert _extract_display_url_parts("https://confluence.example.com/pages/12345") is None

    def test_passage_matches_display_url_via_metadata(self):
        """Match via passage metadata space + title (priority 1 path)."""
        from framework.workflow_runtime.executor import _passage_matches_display_url
        passage = {
            "text": "FAaaS Kiwi Project overview",
            "citation": "wiki://OCIFACP/faaas-kiwi-project",
            "metadata": {"space": "OCIFACP", "title": "FAaaS Kiwi Project", "page_id": "12345678901"},
        }
        assert _passage_matches_display_url(passage, "OCIFACP", "FAaaS Kiwi Project")

    def test_passage_matches_display_url_case_insensitive_title(self):
        """Title match is case-insensitive."""
        from framework.workflow_runtime.executor import _passage_matches_display_url
        passage = {
            "text": "content",
            "citation": "",
            "metadata": {"space": "OCIFACP", "title": "faaas kiwi project"},
        }
        assert _passage_matches_display_url(passage, "OCIFACP", "FAaaS Kiwi Project")

    def test_passage_matches_display_url_wrong_space_rejected(self):
        """Different space key is rejected even if title matches."""
        from framework.workflow_runtime.executor import _passage_matches_display_url
        passage = {
            "text": "content",
            "citation": "",
            "metadata": {"space": "OTHER", "title": "FAaaS Kiwi Project"},
        }
        assert not _passage_matches_display_url(passage, "OCIFACP", "FAaaS Kiwi Project")

    def test_passage_matches_display_url_no_metadata_falls_back_to_citation(self):
        """When metadata is missing, citation-based fallback is used."""
        from framework.workflow_runtime.executor import _passage_matches_display_url
        passage = {
            "text": "content",
            "citation": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
            "metadata": {},
        }
        # Title has 3 significant words (>3 chars): faaas, kiwi, project
        # Space in citation + at least 2 of those words → match
        assert _passage_matches_display_url(passage, "OCIFACP", "FAaaS Kiwi Project")

    def test_passage_no_match_different_page(self):
        """Completely different page returns False."""
        from framework.workflow_runtime.executor import _passage_matches_display_url
        passage = {
            "text": "content",
            "citation": "https://confluence.example.com/display/ENG/Something+Else",
            "metadata": {"space": "ENG", "title": "Something Else"},
        }
        assert not _passage_matches_display_url(passage, "OCIFACP", "FAaaS Kiwi Project")


class TestRC1APassageMatchesPageIdDisplayUrl:
    """_passage_matches_page_id routes to display URL matching when pinned_ref is a display URL.

    This is the core RC1-A fix: when _resolve_page_id returns the original URL unchanged
    (because no numeric pageId is extractable), the numeric pageId path fails.
    _passage_matches_page_id now detects the display URL form and delegates to
    _passage_matches_display_url() instead.
    """

    DISPLAY_URL = (
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
    )

    def test_display_url_pinned_ref_matches_metadata_passage(self):
        """Display URL pinned_ref matches passage by space+title metadata."""
        from framework.workflow_runtime.executor import _passage_matches_page_id
        passage = {
            "text": "Kiwi Project content",
            "citation": "wiki://OCIFACP/faaas-kiwi",
            "metadata": {"space": "OCIFACP", "title": "FAaaS Kiwi Project", "page_id": "12345678901"},
        }
        # requested_page_id IS the display URL (resolve failed → returned unchanged)
        assert _passage_matches_page_id(passage, self.DISPLAY_URL)

    def test_display_url_pinned_ref_no_match_different_space(self):
        """Display URL does NOT match passage from a different space."""
        from framework.workflow_runtime.executor import _passage_matches_page_id
        passage = {
            "text": "content",
            "citation": "",
            "metadata": {"space": "OTHER", "title": "FAaaS Kiwi Project"},
        }
        assert not _passage_matches_page_id(passage, self.DISPLAY_URL)

    def test_display_url_in_metadata_page_id_matches(self):
        """SearchWikiRetriever stores display URL as metadata.page_id.

        RC1-A Phase-B fix (2026-05-18): _passage_matches_display_url now also
        checks metadata.page_id when it is itself a display URL.  Without this,
        passages from SearchWikiRetriever (which sets page_id=display_url but no
        space field) never matched, causing path-A eval failures for skills with
        display-URL pinned_refs.
        """
        from framework.workflow_runtime.executor import _passage_matches_page_id
        # SearchWikiRetriever result: page_id=display_url, no space field in metadata
        passage = {
            "text": "Kiwi Project status content",
            "citation": "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=20382503622",
            "metadata": {
                "page_id": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
                "title": "FAaaS Kiwi Project",
                "path": "/Users/.kbf/wiki/ocifacp/...",
                "persona": "tpm",
                # NOTE: no 'space' field — this is what breaks the original Check 1
            },
        }
        assert _passage_matches_page_id(passage, self.DISPLAY_URL)

    def test_numeric_page_id_still_works(self):
        """Numeric pageId path is unaffected by the RC1-A fix."""
        from framework.workflow_runtime.executor import _passage_matches_page_id
        passage = {
            "text": "content",
            "citation": "https://confluence.oraclecorp.com/pages/12345678901",
            "metadata": {"page_id": "12345678901"},
        }
        assert _passage_matches_page_id(passage, "12345678901")

    def test_numeric_page_id_in_citation_still_works(self):
        """Numeric pageId in citation (not metadata) still matches."""
        from framework.workflow_runtime.executor import _passage_matches_page_id
        passage = {
            "text": "content",
            "citation": "https://confluence.oraclecorp.com/pages/viewpage.action?pageId=12345678901",
            "metadata": {},
        }
        assert _passage_matches_page_id(passage, "12345678901")

    def test_non_display_url_not_matched_by_display_path(self):
        """A non-display URL with no numeric ID does not falsely match."""
        from framework.workflow_runtime.executor import _passage_matches_page_id
        passage = {
            "text": "content",
            "citation": "https://confluence.example.com/pages/SPACE/page.html",
            "metadata": {"space": "SPACE", "title": "Some Page"},
        }
        # Not a display URL, not a numeric ID, citation doesn't contain the URL
        assert not _passage_matches_page_id(
            passage,
            "https://confluence.example.com/pages/OTHER/page.html"
        )


class TestRC1ARetrieveAuthorFixedPinnedDisplayUrl:
    """_retrieve_author_fixed_pinned handles display-URL pinned_ref (RC1-A e2e)."""

    DISPLAY_URL = (
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
    )

    def _make_executor(self, retrievers=None, shim_kb=None):
        from framework.workflow_runtime.executor import WorkflowExecutor
        return WorkflowExecutor(
            store=None,
            llm=None,
            retrievers=retrievers or {},
            shim_kb=shim_kb,
            confluence_adapter=None,
        )

    def test_display_url_pinned_ref_found_in_kb_returns_passages(self):
        """When pinned_ref is a display URL and matching passages are in KB, returns them.

        This is the synth-tpm-f62888a8 scenario: the skill was authored with a
        /display/OCIFACP/FAaaS+Kiwi+Project URL but _resolve_page_id returned the
        URL unchanged.  The fix makes _passage_matches_page_id route to space+title
        matching, so passages are found.
        """
        from framework.workflow_runtime.executor import WorkflowExecutor

        # Passage from KB has numeric page_id + space+title metadata
        matching_passage = MagicMock()
        matching_passage.text = "FAaaS Kiwi Project — Q3 update"
        matching_passage.citation_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        matching_passage.metadata = {
            "space": "OCIFACP",
            "title": "FAaaS Kiwi Project",
            "page_id": "12345678901",
        }

        mock_retriever = MagicMock(return_value=[matching_passage])
        mock_shim_kb = MagicMock()
        mock_shim_kb.all_cards.return_value = [
            {
                "name": "faaas_kiwi_project_pptx",
                "persona": "tpm",
                "retrieval_tools": ["search_wiki"],
            }
        ]

        executor = self._make_executor(
            retrievers={"search_wiki": mock_retriever},
            shim_kb=mock_shim_kb,
        )
        cfg = {
            "workflow_skill": "tpm.faaas_kiwi_project_pptx",
            "persona": "tpm",
            "source_binding": {
                "mode": "author_fixed",
                "source_type": "confluence_page",
                "pinned_ref": self.DISPLAY_URL,
                "space_allow_list": ["OCIFACP"],
                "ingest_on_demand": False,
            },
            "requires_extractions": [{"kb": "tpm.faaas_kiwi_project_pptx"}],
        }
        source_binding = cfg["source_binding"]
        passages = executor._retrieve_author_fixed_pinned(
            cfg, {"input": "kiwi project status"}, source_binding
        )

        assert len(passages) == 1
        assert passages[0]["text"] == "FAaaS Kiwi Project — Q3 update"

    def test_display_url_pinned_ref_not_in_kb_raises_confluence_error(self):
        """When display-URL pinned page is not in KB, hard-fails with ConfluencePageNotInKBError."""
        from framework.workflow_runtime.executor import WorkflowExecutor, ConfluencePageNotInKBError

        # Retriever returns a passage from a DIFFERENT space (not OCIFACP)
        non_matching_passage = MagicMock()
        non_matching_passage.text = "Some other content"
        non_matching_passage.citation_url = "https://confluence.example.com/display/OTHER/Other+Page"
        non_matching_passage.metadata = {"space": "OTHER", "title": "Other Page"}

        mock_retriever = MagicMock(return_value=[non_matching_passage])
        mock_shim_kb = MagicMock()
        mock_shim_kb.all_cards.return_value = [
            {
                "name": "faaas_kiwi_project_pptx",
                "persona": "tpm",
                "retrieval_tools": ["search_wiki"],
            }
        ]

        executor = self._make_executor(
            retrievers={"search_wiki": mock_retriever},
            shim_kb=mock_shim_kb,
        )
        cfg = {
            "workflow_skill": "tpm.faaas_kiwi_project_pptx",
            "persona": "tpm",
            "source_binding": {
                "mode": "author_fixed",
                "source_type": "confluence_page",
                "pinned_ref": self.DISPLAY_URL,
                "space_allow_list": ["OCIFACP"],
                "ingest_on_demand": False,
            },
            "requires_extractions": [{"kb": "tpm.faaas_kiwi_project_pptx"}],
        }
        source_binding = cfg["source_binding"]

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_author_fixed_pinned(
                cfg, {"input": "kiwi project"}, source_binding
            )
        # Error message must contain the display URL (RC1-A: actionable error)
        assert "OCIFACP" in str(exc_info.value) or "FAaaS" in str(exc_info.value) or "display" in str(exc_info.value).lower()
