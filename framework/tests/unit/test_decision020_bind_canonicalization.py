"""ADR-039 / DECISION-020 bind-side canonicalization gap-closure tests.

Root cause: derive_pinned_source() returned raw display URLs in pinned_ref.
The executor read path (session=None) could not resolve display-by-title URLs,
causing ConfluencePageNotInKBError at EVAL time.

Fix: canonicalize_pinned_source() is called at author/bind time (_synthesize_preview)
with the live Confluence adapter, replacing the raw URL with the numeric canonical_id
and stamping canonical_ref.  On Unresolvable, authoring HARD-FAILs per §4.

Tests:
  (a) author_fixed bind with display-URL ref + adapter returning CanonicalRef(numeric)
      -> committed source_binding.pinned_ref is the NUMERIC id, canonical_ref is stamped,
         original_ref holds the raw URL (non-authoritative).
  (b) adapter returns Unresolvable -> authoring HARD-FAILs at bind (typed error),
      raw URL is NOT stored, PinnedSourceCanonicalizationError raised.
  (c) round-trip: bind stamps canonical_id, INGEST stamps same canonical_id,
      executor _passage_matches_canonical matches -> Path-A resolves
      (no ConfluencePageNotInKBError) using mocked adapter/passages.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ===========================================================================
# (a) Bind with display-URL ref + adapter returning CanonicalRef(numeric)
# ===========================================================================

class TestBindCanonicalizationSuccess:
    """(a) canonicalize_pinned_source with CanonicalRef -> numeric id stored."""

    def _make_canonical_ref(self, canonical_id: str):
        from framework.adapters._base import CanonicalRef
        return CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id=canonical_id,
            display_hint="FAaaS Kiwi Project",
        )

    def test_pinned_ref_becomes_numeric_id(self):
        """pinned_ref must be the numeric canonical_id, NOT the raw display URL."""
        from framework.skill_builder.synthesize_workflow import canonicalize_pinned_source

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
        }
        canonical_ref = self._make_canonical_ref("18625350641")
        mock_canonicalize = MagicMock(return_value=canonical_ref)

        result = canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        assert result["pinned_ref"] == "18625350641", (
            f"pinned_ref must be the numeric canonical_id, got: {result['pinned_ref']!r}"
        )

    def test_canonical_ref_is_stamped(self):
        """canonical_ref dict must be present in the returned pinned_source."""
        from framework.skill_builder.synthesize_workflow import canonicalize_pinned_source
        from framework.adapters._base import canonical_ref_to_dict

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
        }
        canonical_ref = self._make_canonical_ref("18625350641")
        mock_canonicalize = MagicMock(return_value=canonical_ref)

        result = canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        assert "canonical_ref" in result, "canonical_ref must be stamped in result"
        cref = result["canonical_ref"]
        assert isinstance(cref, dict), "canonical_ref must be serialized as a dict"
        assert cref["canonical_id"] == "18625350641"
        assert cref["connector_id"] == "confluence"
        assert cref["resource_type"] == "page"

    def test_raw_url_kept_as_original_ref(self):
        """The original raw URL must be retained as original_ref (display hint only)."""
        from framework.skill_builder.synthesize_workflow import canonicalize_pinned_source

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
        }
        canonical_ref = self._make_canonical_ref("18625350641")
        mock_canonicalize = MagicMock(return_value=canonical_ref)

        result = canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        assert result.get("original_ref") == display_url, (
            "original_ref must hold the raw URL as a non-authoritative display hint"
        )

    def test_raw_url_not_used_as_pinned_ref(self):
        """The committed source_binding.pinned_ref must NOT be the raw URL."""
        from framework.skill_builder.synthesize_workflow import canonicalize_pinned_source

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
        }
        canonical_ref = self._make_canonical_ref("18625350641")
        mock_canonicalize = MagicMock(return_value=canonical_ref)

        result = canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        assert result["pinned_ref"] != display_url, (
            "pinned_ref must NOT be the raw display URL after canonicalization"
        )

    def test_synthesize_workflow_emits_canonical_ref_in_source_binding(self):
        """synthesize_workflow_skill emits canonical_ref in source_binding when present."""
        from framework.skill_builder.synthesize_workflow import (
            synthesize_workflow_skill,
            canonicalize_pinned_source,
        )
        from framework.adapters._base import CanonicalRef

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["OCIFACP"],
        }
        canonical_ref = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id="18625350641",
            display_hint="FAaaS Kiwi Project",
        )
        mock_canonicalize = MagicMock(return_value=canonical_ref)
        pinned = canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        result = synthesize_workflow_skill(
            persona="tpm",
            skill_name="faaas_kiwi_project_pptx",
            intent={
                "task_description": "Generate exec review pptx for FAaaS Kiwi Project",
                "output_format": "pptx",
                "trigger": {"on_request": True},
            },
            fields=["slide_title", "rag_summary"],
            source_binding_mode="author_fixed",
            pinned_source=pinned,
        )

        sb = result.get("source_binding")
        assert sb is not None, "source_binding must be present"
        assert sb["pinned_ref"] == "18625350641", (
            f"source_binding.pinned_ref must be numeric canonical_id, got: {sb['pinned_ref']!r}"
        )
        assert "canonical_ref" in sb, "source_binding must include canonical_ref"
        assert sb["canonical_ref"]["canonical_id"] == "18625350641"
        assert sb.get("original_ref") == display_url, (
            "source_binding must retain original_ref as display hint"
        )


# ===========================================================================
# (b) Adapter returns Unresolvable -> authoring HARD-FAILs at bind
# ===========================================================================

class TestBindCanonicalizationHardFail:
    """(b) Unresolvable -> PinnedSourceCanonicalizationError, raw URL NOT stored."""

    def _make_unresolvable(self, reason: str, retryable: bool):
        from framework.adapters._base import Unresolvable
        return Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference="https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
            reason=reason,
            detail="Display-by-title URL requires a live Confluence session to resolve.",
            retryable=retryable,
        )

    def test_transient_unresolvable_raises_typed_error(self):
        """Unresolvable(TRANSIENT) -> PinnedSourceCanonicalizationError raised."""
        from framework.skill_builder.synthesize_workflow import (
            canonicalize_pinned_source,
            PinnedSourceCanonicalizationError,
        )
        from framework.adapters._base import UNRESOLVABLE_TRANSIENT

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": [],
        }
        unresolvable = self._make_unresolvable(UNRESOLVABLE_TRANSIENT, retryable=True)
        mock_canonicalize = MagicMock(return_value=unresolvable)

        with pytest.raises(PinnedSourceCanonicalizationError) as exc_info:
            canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        err = exc_info.value
        assert err.retryable is True
        assert err.reference == display_url

    def test_not_found_unresolvable_raises_typed_error(self):
        """Unresolvable(NOT_FOUND) -> PinnedSourceCanonicalizationError raised."""
        from framework.skill_builder.synthesize_workflow import (
            canonicalize_pinned_source,
            PinnedSourceCanonicalizationError,
        )
        from framework.adapters._base import UNRESOLVABLE_NOT_FOUND

        display_url = "https://confluence.oraclecorp.com/confluence/display/DEAD/Missing+Page"
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": ["DEAD"],
        }
        unresolvable = self._make_unresolvable(UNRESOLVABLE_NOT_FOUND, retryable=False)
        mock_canonicalize = MagicMock(return_value=unresolvable)

        with pytest.raises(PinnedSourceCanonicalizationError) as exc_info:
            canonicalize_pinned_source(raw_pinned, mock_canonicalize)

        err = exc_info.value
        assert err.retryable is False
        assert err.reason == UNRESOLVABLE_NOT_FOUND

    def test_raw_url_not_stored_on_unresolvable(self):
        """On Unresolvable, the function must raise — NOT return a dict with raw URL."""
        from framework.skill_builder.synthesize_workflow import (
            canonicalize_pinned_source,
            PinnedSourceCanonicalizationError,
        )
        from framework.adapters._base import Unresolvable, UNRESOLVABLE_TRANSIENT

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )
        raw_pinned = {
            "pinned_ref": display_url,
            "source_type": "confluence_page",
            "space_allow_list": [],
        }
        unresolvable = Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference=display_url,
            reason=UNRESOLVABLE_TRANSIENT,
            detail="Test detail",
            retryable=True,
        )
        mock_canonicalize = MagicMock(return_value=unresolvable)

        # Must raise — returning any dict (including one with the raw URL) is wrong
        raised = False
        try:
            result = canonicalize_pinned_source(raw_pinned, mock_canonicalize)
            # If we get here, the raw URL may have been returned — that is the bug
            assert False, (
                f"Must raise PinnedSourceCanonicalizationError, but returned: {result!r}. "
                "This is the DECISION-020 §4 violation: raw URL must NOT be stored."
            )
        except PinnedSourceCanonicalizationError:
            raised = True
        assert raised, "PinnedSourceCanonicalizationError must be raised on Unresolvable"

    def test_synthesize_preview_hard_fails_when_adapter_returns_unresolvable(self):
        """_synthesize_preview raises PinnedSourceCanonicalizationError on Unresolvable."""
        from framework.skill_builder.conversation import SkillBuilderConversation, _SessionData
        from framework.skill_builder.synthesize_workflow import PinnedSourceCanonicalizationError
        from framework.adapters._base import Unresolvable, UNRESOLVABLE_TRANSIENT

        display_url = (
            "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project"
        )

        conv = object.__new__(SkillBuilderConversation)
        conv._state = "CONFIRM"
        conv._data = _SessionData(
            persona="tpm",
            intent_description=f"create a pptx from {display_url}",
        )
        conv._data.skill_name = "faaas_kiwi_project_pptx"
        conv._data.output_format = "pptx"
        conv._data.fields = ["slide_title", "rag_summary"]
        conv._data.source_binding_mode = "author_fixed"
        conv._data.sources = [{"kind": "confluence", "pages": [display_url], "page_url": display_url}]
        # source_samples carries the key that derive_pinned_source will use
        conv._data.source_samples = {
            f"confluence:{display_url}": [
                {"space": "OCIFACP", "title": "FAaaS Kiwi Project", "_live": True}
            ]
        }
        conv._data.trigger = {"on_request": True}
        conv._data.reuse_result = {}
        conv._data.design = {"workflow_shape": {}}
        conv._data.design_skill_card = None
        conv._llm = MagicMock()
        conv._skill_store = MagicMock()

        # Mock adapter that returns Unresolvable
        mock_adapter = MagicMock()
        mock_adapter.canonical_identity.return_value = Unresolvable(
            connector_id="confluence",
            resource_type="page",
            reference=display_url,
            reason=UNRESOLVABLE_TRANSIENT,
            detail="Display-by-title URL requires a live Confluence session.",
            retryable=True,
        )

        with patch(
            "framework.skill_builder.conversation._build_confluence_adapter",
            return_value=mock_adapter,
        ):
            with pytest.raises(PinnedSourceCanonicalizationError) as exc_info:
                conv._synthesize_preview()

        err = exc_info.value
        assert err.retryable is True, "Error must be retryable=True for TRANSIENT failure"
        assert display_url in err.reference, "Error must reference the raw URL"


# ===========================================================================
# (c) Round-trip: bind stamps canonical_id, INGEST stamps same, executor matches
# ===========================================================================

class TestRoundTripCanonicalMatch:
    """(c) bind canonical_id == ingest canonical_id -> executor Path-A matches."""

    def test_passage_matches_canonical_with_same_numeric_id(self):
        """_passage_matches_canonical returns True when canonical_ids match."""
        from framework.workflow_runtime.executor import _passage_matches_canonical
        from framework.adapters._base import CanonicalRef

        # Simulate: bind stored canonical_id="18625350641"
        bind_canonical = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id="18625350641",
            display_hint="FAaaS Kiwi Project",
        )

        # Simulate: INGEST stamped canonical_ref with same numeric id (via normalize())
        ingest_stamped_canonical_ref = {
            "connector_id": "confluence",
            "resource_type": "page",
            "canonical_id": "18625350641",
            "display_hint": "FAaaS Kiwi Project",
        }
        passage = {
            "text": "This is the FAaaS Kiwi Project content.",
            "citation": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
            "metadata": {"canonical_ref": ingest_stamped_canonical_ref},
        }

        assert _passage_matches_canonical(passage, bind_canonical), (
            "_passage_matches_canonical must return True when bind canonical_id == "
            "ingest canonical_id (both numeric '18625350641')"
        )

    def test_passage_does_not_match_wrong_canonical_id(self):
        """_passage_matches_canonical returns False when canonical_ids differ."""
        from framework.workflow_runtime.executor import _passage_matches_canonical
        from framework.adapters._base import CanonicalRef

        bind_canonical = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id="18625350641",
            display_hint="FAaaS Kiwi Project",
        )
        passage = {
            "text": "This is some OTHER page.",
            "citation": "https://confluence.oraclecorp.com/confluence/display/OTHER/Other+Page",
            "metadata": {
                "canonical_ref": {
                    "connector_id": "confluence",
                    "resource_type": "page",
                    "canonical_id": "99999999999",  # different id
                    "display_hint": "Other Page",
                }
            },
        }

        assert not _passage_matches_canonical(passage, bind_canonical), (
            "_passage_matches_canonical must return False when canonical_ids differ"
        )

    def test_passage_without_canonical_ref_does_not_match(self):
        """_passage_matches_canonical returns False when passage has no canonical_ref."""
        from framework.workflow_runtime.executor import _passage_matches_canonical
        from framework.adapters._base import CanonicalRef

        bind_canonical = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id="18625350641",
            display_hint="FAaaS Kiwi Project",
        )
        passage = {
            "text": "Some text without canonical_ref in metadata.",
            "citation": "",
            "metadata": {},  # no canonical_ref
        }

        assert not _passage_matches_canonical(passage, bind_canonical), (
            "_passage_matches_canonical must return False when passage has no canonical_ref"
        )

    def test_executor_retrieve_author_fixed_pinned_resolves_when_canonical_matches(self):
        """Full executor path: numeric pinned_ref + matching passage -> passages returned."""
        import sys

        from framework.workflow_runtime.executor import WorkflowExecutor
        from framework.adapters._base import CanonicalRef

        numeric_id = "18625350641"

        # Build mock adapter that returns CanonicalRef for the numeric id
        mock_adapter = MagicMock()
        mock_adapter.canonical_identity.return_value = CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id=numeric_id,
            display_hint="FAaaS Kiwi Project",
        )

        # Build a mock retriever that returns a passage with matching canonical_ref
        matching_passage = MagicMock()
        matching_passage.text = "FAaaS Kiwi Project content"
        matching_passage.citation_url = (
            f"https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId={numeric_id}"
        )
        matching_passage.metadata = {
            "canonical_ref": {
                "connector_id": "confluence",
                "resource_type": "page",
                "canonical_id": numeric_id,
                "display_hint": "FAaaS Kiwi Project",
            }
        }

        mock_retriever = MagicMock(return_value=[matching_passage])
        mock_kb = MagicMock()
        mock_kb.all_cards.return_value = [
            {
                "name": "faaas_kiwi_project_pptx",
                "persona": "tpm",
                "retrieval_tools": ["search_tpm_wiki"],
            }
        ]

        executor = WorkflowExecutor(
            retrievers={"search_tpm_wiki": mock_retriever},
            shim_kb=mock_kb,
            confluence_adapter=mock_adapter,
        )

        cfg = {
            "workflow_skill": "faaas_kiwi_project_pptx",
            "requires_extractions": [
                {"kb": "tpm.faaas_kiwi_project_pptx", "required_fields": ["slide_title"]}
            ],
            "source_binding": {
                "mode": "author_fixed",
                "source_type": "confluence_page",
                "pinned_ref": numeric_id,  # numeric id — as stored by the fix
                "canonical_ref": {
                    "connector_id": "confluence",
                    "resource_type": "page",
                    "canonical_id": numeric_id,
                    "display_hint": "FAaaS Kiwi Project",
                },
                "space_allow_list": ["OCIFACP"],
                "ingest_on_demand": False,
            },
        }

        # Patch resolve_to_numeric_id to bypass the fast-path-only check
        # and use the adapter directly (as the fix intends: numeric_id is already
        # canonical, so resolve_to_numeric_id returns it immediately via Step 1)
        from framework.adapters.confluence import shared as _shared
        original_resolve = _shared.resolve_to_numeric_id

        def _mock_resolve(reference, resource_type, session, base_url):
            # Numeric id fast-path: returns CanonicalRef without API call
            return CanonicalRef(
                connector_id="confluence",
                resource_type=resource_type,
                canonical_id=reference,  # already numeric
                display_hint="FAaaS Kiwi Project",
            )

        _shared.resolve_to_numeric_id = _mock_resolve
        try:
            passages = executor._retrieve_author_fixed_pinned(
                cfg=cfg,
                inputs={"query": "What is the project status?"},
                source_binding=cfg["source_binding"],
            )
        finally:
            _shared.resolve_to_numeric_id = original_resolve

        assert passages, (
            "executor must return passages when numeric canonical_id matches "
            "ingest-stamped canonical_ref (Path-A success — no ConfluencePageNotInKBError)"
        )
        assert passages[0]["text"] == "FAaaS Kiwi Project content"
