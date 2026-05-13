"""Unit tests for OCI content-filter error sanitisation — BUG-queue-1b878/5b233.

Coverage:
  - Synthesizer.synthesize() catches OCI 400 "Inappropriate content detected!!!"
    and returns a clean _content_filtered no-answer (no OCI details in the dict).
  - _is_content_filter_error() correctly identifies the OCI error string.
  - ContextBuilder.answer() intercepts _content_filtered answer, returns tier_4
    no_answer dict with requestId and no OCI endpoint/SDK/opc-request-id.
  - The returned requestId format starts with 'KBF-'.
  - Non-content-filter exceptions are re-raised (not swallowed).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from framework.orchestrator.synthesizer import (
    Synthesizer,
    GENERIC_QA,
    INCIDENT_RCA,
    _is_content_filter_error,
)


# ---------------------------------------------------------------------------
# _is_content_filter_error helper
# ---------------------------------------------------------------------------


class TestIsContentFilterError:
    def test_detects_oci_inappropriate_content_message(self):
        exc = Exception(
            "ServiceError: status=400 message='Inappropriate content detected!!!' "
            "opc-request-id=abc123 endpoint=https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com"
        )
        assert _is_content_filter_error(exc) is True

    def test_detects_shorter_inappropriate_content_message(self):
        exc = Exception("Inappropriate content detected!!!")
        assert _is_content_filter_error(exc) is True

    def test_does_not_match_generic_400(self):
        # A plain HTTP 400 without "content" in the message is NOT a content filter
        exc = Exception("ServiceError: status=400 message='Bad Request'")
        assert _is_content_filter_error(exc) is False

    def test_does_not_match_unrelated_exception(self):
        exc = ConnectionError("Connection refused")
        assert _is_content_filter_error(exc) is False

    def test_does_not_match_500_error(self):
        exc = Exception("ServiceError: status=500 message='Internal Server Error'")
        assert _is_content_filter_error(exc) is False


# ---------------------------------------------------------------------------
# Synthesizer.synthesize() — content-filter path
# ---------------------------------------------------------------------------


def _make_passage(text="Passage text", url="https://wiki.example.com/p1"):
    passage = MagicMock()
    passage.text = text
    passage.citation.url = url
    return passage


class TestSynthesizerContentFilter:
    def _make_synthesizer(self, llm_side_effect):
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = llm_side_effect
        return Synthesizer(llm=mock_llm, model="gpt-4o")

    def test_content_filter_returns_no_answer_dict(self):
        synth = self._make_synthesizer(
            Exception("Inappropriate content detected!!!")
        )
        passages = [_make_passage()]
        result = synth.synthesize("bad query", passages, schema=GENERIC_QA)

        assert isinstance(result, dict)
        assert result.get("_content_filtered") is True

    def test_content_filter_result_has_request_id(self):
        synth = self._make_synthesizer(
            Exception("Inappropriate content detected!!!")
        )
        result = synth.synthesize("bad query", [_make_passage()], schema=GENERIC_QA)
        assert "_request_id" in result
        assert result["_request_id"].startswith("KBF-")

    def test_content_filter_result_has_no_oci_details(self):
        """No OCI endpoint, SDK version, or opc-request-id must appear in the result."""
        oci_error = Exception(
            "ServiceError: status=400 message='Inappropriate content detected!!!' "
            "opc-request-id=DEADBEEF1234 "
            "endpoint=https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com "
            "oracle-sdk-version=2.131.0"
        )
        synth = self._make_synthesizer(oci_error)
        result = synth.synthesize("bad query", [_make_passage()], schema=GENERIC_QA)

        result_str = str(result)
        assert "opc-request-id" not in result_str
        assert "oci.oraclecloud.com" not in result_str
        assert "oracle-sdk-version" not in result_str
        assert "DEADBEEF1234" not in result_str

    def test_content_filter_result_sections_are_no_answer(self):
        synth = self._make_synthesizer(
            Exception("Inappropriate content detected!!!")
        )
        result = synth.synthesize("bad query", [_make_passage()], schema=GENERIC_QA)
        # Each schema section should have the no-context fallback value
        for section in GENERIC_QA.sections:
            assert section.name in result
            assert result[section.name] == "(no relevant context found)"

    def test_non_content_filter_exception_is_reraised(self):
        synth = self._make_synthesizer(RuntimeError("Network timeout"))
        with pytest.raises(RuntimeError, match="Network timeout"):
            synth.synthesize("any query", [_make_passage()], schema=GENERIC_QA)

    def test_empty_passages_skips_llm_no_filter_needed(self):
        """With no passages, synthesize() returns early without calling LLM."""
        mock_llm = MagicMock()
        synth = Synthesizer(llm=mock_llm, model="gpt-4o")
        result = synth.synthesize("any query", passages=[], schema=GENERIC_QA)
        mock_llm.chat.assert_not_called()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# ContextBuilder.answer() — content-filter end-to-end
# ---------------------------------------------------------------------------


class TestContextBuilderContentFilter:
    """Verify context_builder.answer() returns clean tier_4 when synthesis is filtered."""

    def _make_context_builder(self, llm_side_effect):
        """Minimal ContextBuilder with mocked dependencies."""
        from framework.orchestrator.context_builder import ContextBuilder
        from framework.orchestrator.synthesizer import Synthesizer

        mock_llm = MagicMock()
        mock_llm.chat.side_effect = llm_side_effect

        synth = Synthesizer(llm=mock_llm, model="gpt-4o")

        mock_shim_faaas = MagicMock()
        mock_shim_faaas.all_cards.return_value = []
        mock_shim_kb = MagicMock()
        mock_shim_kb.all_cards.return_value = []

        # Tier 2 skill that returns one passage so synthesizer is actually called
        passage = _make_passage()
        mock_packet = MagicMock()
        mock_packet.passages = [passage]
        mock_packet.citations = []
        mock_packet.used_kbs = []
        mock_packet.used_tools = []
        mock_packet.cost = {}
        mock_packet.persona = "ops_eng"
        mock_packet.confidence = 0.5
        mock_packet.notes = None

        mock_skill = MagicMock(return_value=mock_packet)

        # Classifier always returns Tier 2
        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = MagicMock(
            tier=2, confidence=0.5, persona="ops_eng",
            personas=None, workflow_skill=None, reasoning="test",
        )

        cb = ContextBuilder(
            llm=mock_llm,
            shim_faaas=mock_shim_faaas,
            shim_kb=mock_shim_kb,
            skills_by_persona={"ops_eng": mock_skill},
            synthesizer=synth,
        )
        cb.classifier = mock_classifier
        return cb

    def test_content_filter_returns_tier_4(self):
        cb = self._make_context_builder(
            Exception("Inappropriate content detected!!!")
        )
        result = cb.answer("bad query")
        assert result["tier"] == 4

    def test_content_filter_result_has_request_id(self):
        cb = self._make_context_builder(
            Exception("Inappropriate content detected!!!")
        )
        result = cb.answer("bad query")
        assert "request_id" in result
        assert result["request_id"].startswith("KBF-")

    def test_content_filter_result_has_no_passages(self):
        cb = self._make_context_builder(
            Exception("Inappropriate content detected!!!")
        )
        result = cb.answer("bad query")
        assert result["passages"] == []
        assert result["citations"] == []

    def test_content_filter_answer_contains_request_id_message(self):
        cb = self._make_context_builder(
            Exception("Inappropriate content detected!!!")
        )
        result = cb.answer("bad query")
        answer_str = str(result["answer"])
        assert "Request ID" in answer_str or "KBF-" in answer_str

    def test_content_filter_no_oci_details_in_result(self):
        oci_error = Exception(
            "ServiceError: status=400 message='Inappropriate content detected!!!' "
            "opc-request-id=SECRETID999 "
            "endpoint=https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com"
        )
        cb = self._make_context_builder(oci_error)
        result = cb.answer("bad query")

        result_str = str(result)
        assert "SECRETID999" not in result_str
        assert "oci.oraclecloud.com" not in result_str
        assert "opc-request-id" not in result_str

    def test_non_content_filter_exception_propagates(self):
        """Non-content-filter exceptions from the LLM are not swallowed."""
        cb = self._make_context_builder(RuntimeError("ADB connection lost"))
        with pytest.raises(RuntimeError, match="ADB connection lost"):
            cb.answer("any query")
