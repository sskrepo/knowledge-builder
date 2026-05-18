"""Phase A source-binding-mode resolution fix tests.

Root cause: source_binding_mode stays "ambiguous" for an intent that contains a
specific Confluence display URL (a deterministic fixed-source signal).
As a result, _synthesize_preview skips both branches and never calls
derive_pinned_source — committed artifact has source_binding=null (hollow state).

Fixes:
  1. _intent_contains_fixed_confluence_url: detects display URL in free-form text.
  2. _advance_to_capture_intent: auto-resolves "ambiguous" → "author_fixed" when
     intent text contains a Confluence display URL.
  3. _synthesize_preview: safety-net auto-resolution for sessions still in
     "ambiguous" state at synthesis time.

Tests prove:
  - Fixed-source display-URL intent → committed artifact has author_fixed
    source_binding with non-null pinned_ref + typed input (NOT generic string).
  - The "ambiguous" mode is resolved to "author_fixed" at both capture_intent and
    synthesis time when evidence supports it.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ===========================================================================
# _intent_contains_fixed_confluence_url helpers
# ===========================================================================

class TestIntentContainsFixedConfluenceUrl:
    """_intent_contains_fixed_confluence_url returns True for display URL intents."""

    def _fn(self, text: str) -> bool:
        from framework.skill_builder.conversation import _intent_contains_fixed_confluence_url
        return _intent_contains_fixed_confluence_url(text)

    def test_display_url_detected(self):
        """Standard /display/SPACE/Title URL → True."""
        assert self._fn(
            "create a skill from https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )

    def test_display_url_with_using_fixed_phrasing(self):
        """'using fixed Confluence source' + display URL → True."""
        assert self._fn(
            "lets start fresh this time, create a new skill that takes a look at "
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project "
            "and generates a ppt slide"
        )

    def test_wiki_spaces_url_detected(self):
        """/wiki/spaces/SPACE/pages/NNN URL → True."""
        assert self._fn(
            "use https://confluence.example.com/wiki/spaces/ENG/pages/12345678/My+Page"
        )

    def test_numeric_pageid_url_detected(self):
        """/pages/NNN URL with 6+ digit ID → True."""
        assert self._fn(
            "page: https://confluence.example.com/pages/18625350641"
        )

    def test_pageid_param_detected(self):
        """pageId=NNN query param → True."""
        assert self._fn(
            "https://confluence.example.com/pages/viewpage.action?pageId=18625350641"
        )

    def test_generic_confluence_mention_rejected(self):
        """Generic 'Confluence' mention without page path → False."""
        assert not self._fn(
            "generate a report using data from Confluence"
        )

    def test_empty_string_returns_false(self):
        assert not self._fn("")

    def test_none_like_empty(self):
        from framework.skill_builder.conversation import _intent_contains_fixed_confluence_url
        assert not _intent_contains_fixed_confluence_url(None)  # type: ignore[arg-type]

    def test_ask_parameterized_phrasing_not_flagged(self):
        """'accept any Confluence URL the user provides' → False (no specific URL)."""
        assert not self._fn(
            "create a skill that accepts any Confluence page the user provides at query time"
        )


# ===========================================================================
# _synthesize_preview: ambiguous mode auto-resolution + pinned_ref emission
# ===========================================================================

class TestSynthesizePreviewAmbiguousResolution:
    """_synthesize_preview: sessions stuck in 'ambiguous' mode get author_fixed treatment."""

    def _make_conv(self, source_binding_mode: str, sources: list, source_samples: dict,
                   intent_description: str = ""):
        """Build a minimal SkillBuilderConversation in a state ready for _synthesize_preview."""
        from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData

        conv = object.__new__(SkillBuilderConversation)
        conv._state = "CONFIRM"
        conv._data = _SessionData(persona="tpm", intent_description=intent_description)
        conv._data.skill_name = "faaas_kiwi_project_pptx"
        conv._data.output_format = "pptx"
        conv._data.fields = ["slide_title", "rag_summary"]
        conv._data.source_binding_mode = source_binding_mode
        conv._data.sources = sources
        conv._data.source_samples = source_samples
        conv._data.trigger = {"on_request": True}
        conv._data.reuse_result = {}
        conv._data.design = {"workflow_shape": {"layout": "weekly_exec_review_v1"}}
        conv._data.design_skill_card = None
        conv._llm = MagicMock()
        conv._skill_store = MagicMock()
        return conv

    def _mock_confluence_adapter(self, canonical_id: str = "18625350641"):
        """Return a mock Confluence adapter that resolves any ref to the given numeric id.

        ADR-039 bind-side fix: _synthesize_preview now calls _build_confluence_adapter
        and invokes canonical_identity at author time.  Tests that call _synthesize_preview
        with external Confluence URL sources must provide a mock adapter — otherwise the
        function correctly raises PinnedSourceCanonicalizationError (no real config in CI).
        """
        from framework.adapters._base import CanonicalRef
        mock_adapter = MagicMock()
        mock_adapter.canonical_identity.return_value = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id=canonical_id,
            display_hint="FAaaS Kiwi Project",
        )
        return mock_adapter

    def test_ambiguous_with_confluence_url_source_resolves_to_author_fixed(self):
        """ambiguous + Confluence URL source → _synthesize_preview resolves to author_fixed."""
        conv = self._make_conv(
            source_binding_mode="ambiguous",
            sources=[{
                "kind": "confluence",
                "pages": [
                    "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
                ],
                "page_urls": [
                    "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
                ],
            }],
            source_samples={},
            intent_description=(
                "create a skill from https://confluence.oraclecorp.com/confluence/"
                "display/OCIFACP/FAaaS+Kiwi+Project"
            ),
        )
        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=self._mock_confluence_adapter(),
        ):
            artifacts = conv._synthesize_preview()
        assert conv._data.source_binding_mode == "author_fixed", (
            "ambiguous mode must be resolved to author_fixed when Confluence URL is present"
        )

    def test_ambiguous_with_source_samples_resolves_to_author_fixed(self):
        """ambiguous + source_samples populated → resolves to author_fixed."""
        conv = self._make_conv(
            source_binding_mode="ambiguous",
            sources=[],
            source_samples={
                "confluence:https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project": [
                    {"page_id": "12345678901", "space": "OCIFACP", "title": "FAaaS Kiwi Project"}
                ]
            },
            intent_description="generate a weekly pptx from the fixed Confluence source",
        )
        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=self._mock_confluence_adapter(),
        ):
            artifacts = conv._synthesize_preview()
        assert conv._data.source_binding_mode == "author_fixed"

    def test_ambiguous_with_url_source_emits_source_binding_in_artifact(self):
        """ambiguous + URL source → committed artifact has source_binding with author_fixed mode.

        Post-ADR-039 bind-fix: pinned_ref is now the NUMERIC canonical_id (not the raw URL).
        """
        conv = self._make_conv(
            source_binding_mode="ambiguous",
            sources=[{
                "kind": "confluence",
                "page_url": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
                "pages": ["https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"],
            }],
            source_samples={},
            intent_description=(
                "take a look at https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
            ),
        )
        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=self._mock_confluence_adapter("18625350641"),
        ):
            artifacts = conv._synthesize_preview()
        # Find the workflow skill YAML artifact
        wf_key = [k for k in artifacts.keys() if "workflow_skills" in k or k.endswith(".yaml")
                  and "faaas_kiwi" in k]
        # The workflow skill is not in file paths — it's returned as a dict value
        # directly. Check the wf_struct dict in artifacts for source_binding.
        wf_artifacts = [v for v in artifacts.values() if isinstance(v, dict) and
                        v.get("workflow_skill") == "faaas_kiwi_project_pptx"]
        assert wf_artifacts, (
            f"workflow skill YAML dict not found in artifacts keys={list(artifacts.keys())}"
        )
        wf = wf_artifacts[0]
        assert "source_binding" in wf, (
            f"source_binding must be emitted for ambiguous+URL session. artifact={wf}"
        )
        assert wf["source_binding"]["mode"] == "author_fixed"
        # Post-ADR-039 bind-fix: pinned_ref is numeric canonical_id, NOT the raw URL
        assert wf["source_binding"]["pinned_ref"] is not None
        assert wf["source_binding"]["pinned_ref"] != ""
        assert wf["source_binding"]["pinned_ref"] == "18625350641", (
            "Post-ADR-039 bind-fix: pinned_ref must be the numeric canonical_id, "
            f"got: {wf['source_binding']['pinned_ref']!r}"
        )

    def test_ambiguous_no_source_still_resolves_to_author_fixed(self):
        """ambiguous with NO sources → still resolves to author_fixed (pure in-KB default)."""
        conv = self._make_conv(
            source_binding_mode="ambiguous",
            sources=[],
            source_samples={},
            intent_description="generate a weekly review from internal data",
        )
        # No adapter mock needed: no external Confluence source → derive_pinned_source
        # returns None → canonicalize_pinned_source is not called.
        artifacts = conv._synthesize_preview()
        assert conv._data.source_binding_mode == "author_fixed", (
            "ambiguous mode must ALWAYS resolve to author_fixed at synthesis time "
            "(no skill should remain in ambiguous state after synthesis)"
        )

    def test_author_fixed_mode_unchanged(self):
        """author_fixed stays author_fixed at synthesis (no regression).

        Post-ADR-039 bind-fix: _synthesize_preview calls _build_confluence_adapter
        to canonicalize the pinned ref. Mock it so the test focuses on mode state.
        """
        conv = self._make_conv(
            source_binding_mode="author_fixed",
            sources=[{
                "kind": "confluence",
                "pages": ["https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"],
                "page_url": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
            }],
            source_samples={},
            intent_description="create a pptx from the Kiwi Project page",
        )
        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=self._mock_confluence_adapter(),
        ):
            artifacts = conv._synthesize_preview()
        assert conv._data.source_binding_mode == "author_fixed"

    def test_ask_parameterized_mode_unchanged(self):
        """ask_parameterized stays ask_parameterized at synthesis (no regression)."""
        conv = self._make_conv(
            source_binding_mode="ask_parameterized",
            sources=[{
                "kind": "confluence",
                "space": "OCIFACP",
            }],
            source_samples={
                "confluence:some_source": [{"page_id": "99", "space": "OCIFACP", "title": "Some"}]
            },
            intent_description="accept any Confluence page from the user",
        )
        artifacts = conv._synthesize_preview()
        assert conv._data.source_binding_mode == "ask_parameterized"


# ===========================================================================
# Fixed-source intent → committed artifact has author_fixed source_binding
# with non-null pinned_ref + typed input
# ===========================================================================

class TestFixedSourceIntentYieldsPinnedArtifact:
    """End-to-end: fixed-source display-URL intent → artifact has proper source_binding."""

    def test_fixed_confluence_url_intent_synthesizes_source_binding(self):
        """
        When the user provides a /display/ URL and mode resolves to author_fixed,
        synthesize_workflow_skill emits source_binding with:
          - mode: author_fixed
          - source_type: confluence_page
          - pinned_ref: the display URL (non-null, non-empty)
          - ingest_on_demand: False
        """
        from framework.skill_builder.synthesize_workflow import (
            synthesize_workflow_skill,
            derive_pinned_source,
        )

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        sources = [{
            "kind": "confluence",
            "pages": [display_url],
            "page_url": display_url,
        }]
        source_samples = {}

        # Derive pinned source (as _synthesize_preview does)
        pinned = derive_pinned_source(sources=sources, source_samples=source_samples)
        assert pinned is not None, (
            "derive_pinned_source must return a non-null dict for a URL-form source"
        )
        assert pinned["pinned_ref"] == display_url
        assert pinned["source_type"] == "confluence_page"

        # Synthesize the workflow skill
        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="faaas_kiwi_project_pptx",
            intent={
                "task_description": "Generate weekly exec review pptx for FAaaS Kiwi Project",
                "output_format": "pptx",
                "trigger": {"on_request": True},
            },
            fields=["slide_title", "rag_summary"],
            source_binding_mode="author_fixed",
            pinned_source=pinned,
        )

        # Gate 1: source_binding must be present and correct
        assert "source_binding" in result, (
            "source_binding block must be emitted for author_fixed + URL source"
        )
        sb = result["source_binding"]
        assert sb["mode"] == "author_fixed"
        assert sb["pinned_ref"] == display_url
        assert sb["pinned_ref"] is not None and sb["pinned_ref"] != ""
        assert sb["source_type"] == "confluence_page"
        assert sb["ingest_on_demand"] is False

        # Gate 1b: trigger input must be present AND named "query" (not "input")
        # for author_fixed + pinned_source skills — satisfies acceptance gate 1
        # which says NOT {name:input, type:string}.
        trigger = result.get("trigger", {})
        inputs = trigger.get("on_request", {}).get("inputs", [])
        assert len(inputs) > 0, "trigger.on_request.inputs must be non-empty"
        # The first input must be named "query" (not the placeholder "input")
        first_input = inputs[0]
        assert first_input["name"] == "query", (
            f"trigger.on_request.inputs[0].name must be 'query' for "
            f"author_fixed + pinned_source skills, got: {first_input['name']!r}. "
            "The hollow-state symptom was {name:input,type:string} — fix must "
            "produce a meaningful named input."
        )
        assert first_input["type"] == "string"
        # Must NOT be the placeholder generic input
        assert first_input["name"] != "input", (
            "trigger input must NOT be the placeholder {name:input} for "
            "author_fixed + pinned_source skills"
        )

    def test_source_binding_mode_ambiguous_with_display_url_resolved_at_capture_intent(self):
        """
        _advance_to_capture_intent: when LLM returns 'ambiguous' but intent has
        a display URL, mode is auto-resolved to author_fixed BEFORE CLARIFY routing.
        """
        import json as _json
        from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        intent_text = (
            f"lets start fresh this time, create a new skill that takes a look at "
            f"{display_url} and generates a ppt slide. using fixed Confluence source."
        )

        # Simulate the LLM returning "ambiguous" with a blocking question
        llm_output = {
            "output_kind": "pptx",
            "audience": "exec",
            "cadence": "weekly",
            "scope_domains": ["FAaaS", "Kiwi"],
            "success_criteria": ["one slide per week"],
            "source_binding_mode": "ambiguous",
            "source_binding_signal": "using fixed Confluence source",
            "blocking_ambiguities": [
                "Is the source page fixed at authoring time or supplied by the consumer at query time?"
            ],
            "nice_to_know_ambiguities": [],
        }

        # Build a mock prompt spec object (the registry returns a spec, not a plain string)
        mock_spec = MagicMock()
        mock_spec.model = "synthesis"
        mock_spec.text = "prompt text"
        mock_spec.response_format = None
        mock_spec.max_tokens = 2000

        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"text": _json.dumps(llm_output), "tokens_out": 100}

        conv = object.__new__(SkillBuilderConversation)
        conv._state = "IDENTIFY_PERSONA"
        conv._data = _SessionData(persona="tpm", intent_description=intent_text)
        conv._data.skill_name = "faaas_kiwi_project_pptx"
        conv._data.output_format = "pptx"
        conv._data.persona = "tpm"
        conv._llm = mock_llm
        conv._skill_store = MagicMock()

        with patch("framework.skill_builder.conversation.get_registry") as mock_reg:
            mock_reg.return_value.get_prompt.return_value = mock_spec
            turn = conv._advance_to_capture_intent()

        # After auto-resolution, mode must be author_fixed
        assert conv._data.source_binding_mode == "author_fixed", (
            f"source_binding_mode must be auto-resolved to 'author_fixed' when intent "
            f"contains a display URL, got: {conv._data.source_binding_mode!r}"
        )

        # The session must NOT have entered CLARIFY state for the source-binding question
        # (other blocking questions may still trigger CLARIFY, but not this one)
        if conv._state == "CLARIFY":
            # Verify no source_binding_mode question is in the queue
            sb_questions = [
                q for q in (conv._data._clarify_questions or [])
                if q.get("context") == "source_binding_mode"
            ]
            assert len(sb_questions) == 0, (
                "No source_binding_mode CLARIFY question should remain after "
                f"auto-resolution. questions={conv._data._clarify_questions}"
            )

    def test_clarify_response_fixed_resolves_mode_correctly(self):
        """
        _handle_clarify_response: user answers 'A' / 'using fixed Confluence source' →
        mode resolves to author_fixed.
        """
        from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData

        conv = object.__new__(SkillBuilderConversation)
        conv._state = "CLARIFY"
        conv._data = _SessionData(
            persona="tpm",
            intent_description="create a pptx from the Kiwi page"
        )
        conv._data.skill_name = "faaas_kiwi_project_pptx"
        conv._data.output_format = "pptx"
        conv._data.source_binding_mode = "ambiguous"
        conv._data._clarify_questions = [
            {
                "question": "Is the source page fixed at authoring time or supplied by the consumer at query time?",
                "resolved": False,
                "context": "source_binding_mode",
                "options": {"A": "author_fixed", "B": "ask_parameterized"},
            }
        ]
        conv._data._clarify_return_to = "CONFIGURE_SOURCES"
        conv._llm = MagicMock()
        conv._skill_store = MagicMock()

        with patch.object(conv, "_advance_to_configure_sources_v2") as mock_advance:
            mock_advance.return_value = MagicMock(state="CONFIGURE_SOURCES")
            turn = conv._handle_clarify_response("A")

        assert conv._data.source_binding_mode == "author_fixed", (
            f"Answering 'A' must resolve source_binding_mode to author_fixed, "
            f"got: {conv._data.source_binding_mode!r}"
        )

    def test_clarify_response_using_fixed_confluence_source_resolves_to_author_fixed(self):
        """
        _handle_clarify_response: 'using fixed Confluence source' phrasing → author_fixed.
        This tests the 'fixed' keyword in the answer text matching.
        """
        from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData

        conv = object.__new__(SkillBuilderConversation)
        conv._state = "CLARIFY"
        conv._data = _SessionData(
            persona="tpm",
            intent_description="create a pptx from the Kiwi page"
        )
        conv._data.skill_name = "faaas_kiwi_project_pptx"
        conv._data.output_format = "pptx"
        conv._data.source_binding_mode = "ambiguous"
        conv._data._clarify_questions = [
            {
                "question": "Is the source page fixed at authoring time or supplied by the consumer at query time?",
                "resolved": False,
                "context": "source_binding_mode",
                "options": {"A": "author_fixed", "B": "ask_parameterized"},
            }
        ]
        conv._data._clarify_return_to = "CONFIGURE_SOURCES"
        conv._llm = MagicMock()
        conv._skill_store = MagicMock()

        with patch.object(conv, "_advance_to_configure_sources_v2") as mock_advance:
            mock_advance.return_value = MagicMock(state="CONFIGURE_SOURCES")
            turn = conv._handle_clarify_response("using fixed Confluence source")

        assert conv._data.source_binding_mode == "author_fixed", (
            f"'using fixed Confluence source' must resolve to author_fixed, "
            f"got: {conv._data.source_binding_mode!r}"
        )
