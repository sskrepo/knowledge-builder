"""Unit tests for review.py — BUG-queue-573e3 and BUG-queue-44364.

Coverage:
  - _escape_bare_control_chars: bare \\n inside quoted value is escaped to \\\\n;
    structural newlines between keys are preserved; already-escaped \\\\n is untouched;
    backslash-escaped quotes inside strings don't confuse the state machine.
  - _llm_extract end-to-end: mock LLM that returns pretty-printed JSON with a bare
    newline in one string value (>=30 keys, bad value on ~line 34) parses correctly.
  - _llm_extract hard-fail: irreparably broken LLM output raises ValueError (not
    returning {}), and the exception message is non-empty and actionable.
  - BUG-queue-44364 (max-token truncation): _llm_extract uses max_tokens=4096;
    truncated response with tokens_out==4096 raises a distinct truncation error;
    complete large schema with tokens_out well under ceiling parses correctly;
    non-ceiling parse failure uses corrected message that names both bug IDs.

Test style follows test_synthesizer_content_filter.py and
test_skill_builder_conversation.py (MagicMock + side_effect, pytest classes).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from framework.skill_builder.review import (
    _escape_bare_control_chars,
    _llm_extract,
    _EXTRACT_MAX_TOKENS,
    ContentFilterRejection,
    _is_content_filter_error,
)


class TestContentFilterRejection:
    """Fix #1: OCI content-safety 400 → clean ContentFilterRejection, no 500."""

    def test_is_content_filter_error_matches_oci_message(self):
        exc = Exception(
            "{'status': 400, 'message': 'Inappropriate content detected!!!'}"
        )
        assert _is_content_filter_error(exc) is True

    def test_is_content_filter_error_ignores_unrelated(self):
        assert _is_content_filter_error(Exception("connection timed out")) is False

    def test_llm_extract_raises_content_filter_rejection_not_raw(self):
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = Exception(
            "oci.exceptions.ServiceError {'status': 400, 'code': '400', "
            "'message': 'Inappropriate content detected!!!', "
            "'opc-request-id': 'SECRET-LEAK-DO-NOT-EXPOSE'}"
        )
        sample = {"content": "x" * 50, "source_citation": "wiki://20030556732"}
        schema = {"properties": {"f": {"type": "string", "description": "d"}},
                  "required": ["f"]}
        with pytest.raises(ContentFilterRejection) as ei:
            _llm_extract(sample, schema, mock_llm)
        msg = str(ei.value)
        assert ei.value.request_id.startswith("KBF-")
        # No provider internals leaked
        assert "opc-request-id" not in msg
        assert "SECRET-LEAK-DO-NOT-EXPOSE" not in msg
        assert "oci.exceptions" not in msg


# ---------------------------------------------------------------------------
# _escape_bare_control_chars
# ---------------------------------------------------------------------------


class TestEscapeBareControlChars:
    def test_bare_newline_inside_string_is_escaped(self):
        """A bare \\n inside a JSON string value becomes \\\\n."""
        s = '{"key": "line one\nline two"}'
        result = _escape_bare_control_chars(s)
        # Must parse as valid JSON now
        parsed = json.loads(result)
        assert parsed["key"] == "line one\nline two"

    def test_structural_newlines_between_keys_preserved(self):
        """Newlines between JSON keys (outside strings) are left untouched."""
        s = '{\n  "a": "alpha",\n  "b": "beta"\n}'
        result = _escape_bare_control_chars(s)
        # Must still be parseable
        parsed = json.loads(result)
        assert parsed == {"a": "alpha", "b": "beta"}
        # The structural newlines should still exist (not escaped)
        assert "\n" in result

    def test_already_escaped_newline_untouched(self):
        """A properly escaped \\\\n sequence is left as-is (no double-escaping)."""
        s = '{"key": "line one\\nline two"}'
        result = _escape_bare_control_chars(s)
        parsed = json.loads(result)
        # The model emitted \\n correctly; value should contain a literal newline
        assert parsed["key"] == "line one\nline two"
        # The raw output must not double-escape it to \\\\n
        assert "\\\\n" not in result

    def test_backslash_escaped_quote_does_not_confuse_state_machine(self):
        """A \\\" inside a string value must not be mistaken for end-of-string."""
        s = '{"msg": "she said \\"hello\\nworld\\""}'
        result = _escape_bare_control_chars(s)
        parsed = json.loads(result)
        assert parsed["msg"] == 'she said "hello\nworld"'

    def test_bare_carriage_return_escaped(self):
        """Bare \\r inside a string is escaped to \\\\r."""
        s = '{"key": "foo\rbar"}'
        result = _escape_bare_control_chars(s)
        parsed = json.loads(result)
        assert parsed["key"] == "foo\rbar"

    def test_bare_tab_inside_string_escaped(self):
        """Bare \\t inside a string is escaped to \\\\t."""
        s = '{"key": "col1\tcol2"}'
        result = _escape_bare_control_chars(s)
        parsed = json.loads(result)
        assert parsed["key"] == "col1\tcol2"

    def test_empty_string_is_identity(self):
        assert _escape_bare_control_chars("") == ""

    def test_string_outside_json_context_unchanged(self):
        """Plain text with a newline (not JSON) passes through unchanged."""
        plain = "hello\nworld"
        # Outside any JSON string quotes — newline is outside a quoted context
        result = _escape_bare_control_chars(plain)
        assert result == "hello\nworld"


# ---------------------------------------------------------------------------
# _llm_extract — end-to-end with mock LLM
# ---------------------------------------------------------------------------


def _make_schema_with_n_fields(n: int = 32) -> dict:
    """Build a JSON Schema with n string fields."""
    props = {f"field_{i:02d}": {"type": "string", "description": f"Field {i}"} for i in range(n)}
    required = [f"field_{i:02d}" for i in range(5)]  # first 5 required
    return {"type": "object", "properties": props, "required": required}


def _make_sample(content: str = "Sample content for extraction.") -> dict:
    return {
        "content": content,
        "source_citation": "https://confluence.example.com/pages/99999",
    }


def _make_mock_llm(response_text: str, tokens_out: int | None = None) -> MagicMock:
    mock = MagicMock()
    result: dict = {"text": response_text}
    if tokens_out is not None:
        result["tokens_out"] = tokens_out
    mock.chat.return_value = result
    return mock


class TestLlmExtractEndToEnd:
    def test_parses_json_with_bare_newline_in_string_value(self):
        """Mirrors BUG-queue-573e3: OCI emits bare \\n inside a string on line ~34."""
        schema = _make_schema_with_n_fields(32)

        # Build a JSON response that has a bare newline in field_33 (which would
        # land around line 34 in pretty-printed output).  json.dumps then manual
        # injection replicates what the OCI model actually emits.
        data = {f"field_{i:02d}": f"value {i}" for i in range(32)}
        # Inject a bare newline into the last field — simulates the OCI bug.
        data["field_31"] = "first line of table cell\nsecond line of table cell"

        # Simulate what OCI emits: json.dumps produces valid JSON, but we then
        # corrupt it by replacing the escaped \\n inside the string value with a
        # real newline (what OCI JSON_OBJECT mode actually does).
        good_json = json.dumps(data, indent=2)
        # Replace escaped newline in the value with a real bare newline.
        # The escaped sequence is '\\n' (two chars: backslash + n).
        corrupted_json = good_json.replace(
            "first line of table cell\\nsecond line of table cell",
            "first line of table cell\nsecond line of table cell",
        )
        # Confirm this is now broken
        with pytest.raises(json.JSONDecodeError):
            json.loads(corrupted_json)

        sample = _make_sample()
        mock_llm = _make_mock_llm(corrupted_json)

        result = _llm_extract(sample, schema, mock_llm)

        assert isinstance(result, dict)
        assert result["field_31"] == "first line of table cell\nsecond line of table cell"
        # Other fields must also be present
        assert result["field_00"] == "value 0"

    def test_clean_json_parsed_without_mutation(self):
        """Well-formed JSON is returned as-is (sanitizer fast-path not needed)."""
        schema = _make_schema_with_n_fields(5)
        data = {f"field_0{i}": f"val{i}" for i in range(5)}
        clean_json = json.dumps(data)
        sample = _make_sample()
        mock_llm = _make_mock_llm(clean_json)

        result = _llm_extract(sample, schema, mock_llm)

        assert result == data

    def test_json_inside_markdown_fence_parsed(self):
        """JSON wrapped in ```json...``` fences is stripped and parsed."""
        schema = _make_schema_with_n_fields(3)
        data = {"field_00": "alpha", "field_01": "beta", "field_02": "gamma"}
        fenced = f"```json\n{json.dumps(data, indent=2)}\n```"
        sample = _make_sample()
        mock_llm = _make_mock_llm(fenced)

        result = _llm_extract(sample, schema, mock_llm)

        assert result == data

    def test_json_with_bare_newline_inside_markdown_fence(self):
        """Bare newline inside a fenced block is also handled correctly."""
        schema = _make_schema_with_n_fields(3)
        data = {"field_00": "line1\nline2", "field_01": "x", "field_02": "y"}
        # Produce corrupted JSON: embed actual newline, not \\n
        good = json.dumps(data, indent=2)
        corrupted = good.replace("line1\\nline2", "line1\nline2")
        fenced = f"```json\n{corrupted}\n```"

        sample = _make_sample()
        mock_llm = _make_mock_llm(fenced)
        result = _llm_extract(sample, schema, mock_llm)

        assert result["field_00"] == "line1\nline2"


# ---------------------------------------------------------------------------
# _llm_extract — hard-fail path
# ---------------------------------------------------------------------------


class TestLlmExtractHardFail:
    def test_raises_on_irreparably_broken_json(self):
        """Completely unparseable text raises ValueError — never returns {}."""
        schema = _make_schema_with_n_fields(3)
        sample = _make_sample()
        # Something that cannot be fixed by the sanitiser or regex slice
        garbage = "this is not json at all !!!"
        mock_llm = _make_mock_llm(garbage)

        with pytest.raises(ValueError) as exc_info:
            _llm_extract(sample, schema, mock_llm)

        msg = str(exc_info.value)
        # Must be actionable — reference the bug
        assert msg  # non-empty
        assert "BUG-queue-573e3" in msg

    def test_raises_on_truncated_json(self):
        """Truncated JSON (mid-string) raises ValueError, not returns {}."""
        schema = _make_schema_with_n_fields(3)
        sample = _make_sample()
        truncated = '{"field_00": "hello, my name is'
        mock_llm = _make_mock_llm(truncated)

        with pytest.raises(ValueError):
            _llm_extract(sample, schema, mock_llm)

    def test_raised_exception_message_is_actionable(self):
        """The error message must include enough context to diagnose the issue."""
        schema = _make_schema_with_n_fields(3)
        sample = _make_sample()
        bad = "<<<NOT JSON>>>"
        mock_llm = _make_mock_llm(bad)

        with pytest.raises(ValueError) as exc_info:
            _llm_extract(sample, schema, mock_llm)

        msg = str(exc_info.value)
        # Must mention parse failure and the BUG ID
        assert "parse" in msg.lower() or "json" in msg.lower()
        assert "BUG-queue-573e3" in msg


# ---------------------------------------------------------------------------
# BUG-queue-44364 — max-token truncation detection
# ---------------------------------------------------------------------------


class TestBugQueue44364MaxTokenTruncation:
    def test_max_tokens_is_4096(self):
        """_llm_extract must call llm.chat with max_tokens=4096 (not 2048).

        This asserts the constant _EXTRACT_MAX_TOKENS is 4096 AND that it is
        actually passed through to llm.chat, closing the drift risk between
        review.py and WorkflowExecutor._llm_extract_fields (executor.py ~495).
        """
        assert _EXTRACT_MAX_TOKENS == 4096

        schema = _make_schema_with_n_fields(5)
        data = {f"field_0{i}": f"v{i}" for i in range(5)}
        mock_llm = _make_mock_llm(json.dumps(data))
        sample = _make_sample()

        _llm_extract(sample, schema, mock_llm)

        call_kwargs = mock_llm.chat.call_args
        # Accept both positional and keyword passing
        if call_kwargs.kwargs:
            passed = call_kwargs.kwargs.get("max_tokens")
        else:
            # fall back to positional if called that way (shouldn't happen)
            passed = None
        assert passed == 4096, (
            f"_llm_extract called llm.chat with max_tokens={passed}, expected 4096. "
            "This is BUG-queue-44364 — review.py and executor.py are drifted."
        )

    def test_truncated_response_raises_truncation_error(self):
        """Truncated JSON + tokens_out==4096 raises a BUG-queue-44364 error.

        Simulates a 32-field schema where the model hit the token ceiling and
        the response is cut mid-key (fields 19 onward missing, as observed).
        Assert the error message identifies truncation, names BUG-queue-44364,
        and does NOT claim control chars are a definite cause.
        """
        schema = _make_schema_with_n_fields(32)
        sample = _make_sample("Large document with lots of content to extract from.")

        # Build a response that is truncated mid-key after ~19 fields
        partial = {f"field_{i:02d}": f"extracted value {i}" for i in range(19)}
        truncated_json = json.dumps(partial, indent=2)[:-5]  # slice off closing }}\n
        truncated_json += '  "m'  # mid-key, exactly as observed in the bug report

        # tokens_out == 4096 = the ceiling
        mock_llm = _make_mock_llm(truncated_json, tokens_out=4096)

        with pytest.raises(ValueError) as exc_info:
            _llm_extract(sample, schema, mock_llm)

        msg = str(exc_info.value)
        assert "truncated" in msg.lower(), f"Expected 'truncated' in: {msg}"
        assert "BUG-queue-44364" in msg, f"Expected BUG-queue-44364 in: {msg}"
        # Must NOT falsely assert control chars are the definite cause
        assert "OCI JSON_OBJECT mode may have emitted" not in msg, (
            "Error message incorrectly implies control chars as definite cause "
            "for a truncation failure."
        )

    def test_complete_large_schema_parses(self):
        """A valid 32-field JSON response with tokens_out well under ceiling parses OK.

        This is the happy path after raising max_tokens from 2048 to 4096 —
        the full response fits and all 32 fields are returned.
        """
        schema = _make_schema_with_n_fields(32)
        data = {f"field_{i:02d}": f"extracted value {i}" for i in range(32)}
        complete_json = json.dumps(data)

        # tokens_out well under the 4096 ceiling
        mock_llm = _make_mock_llm(complete_json, tokens_out=1800)
        sample = _make_sample("Large document with lots of content to extract from.")

        result = _llm_extract(sample, schema, mock_llm)

        assert isinstance(result, dict)
        assert len(result) == 32
        for i in range(32):
            assert result[f"field_{i:02d}"] == f"extracted value {i}"

    def test_parse_failure_not_ceiling_uses_corrected_message(self):
        """Parse failure with tokens_out < max_tokens uses the non-truncation path.

        The error message must name both BUG-queue-573e3 and BUG-queue-44364
        as possible causes and must NOT assert that either is the definite cause.
        The old wording "OCI JSON_OBJECT mode may have emitted unescaped control
        characters" was misleading for non-control-char failures; the new message
        phrases both as possibilities.
        """
        schema = _make_schema_with_n_fields(5)
        sample = _make_sample()
        # Unparseable output that is NOT a truncation (tokens_out well below ceiling)
        garbage = "<<< NOT JSON — random model hallucination >>>"
        mock_llm = _make_mock_llm(garbage, tokens_out=200)

        with pytest.raises(ValueError) as exc_info:
            _llm_extract(sample, schema, mock_llm)

        msg = str(exc_info.value)

        # Both bug IDs named as possibilities
        assert "BUG-queue-573e3" in msg, f"Expected BUG-queue-573e3 in: {msg}"
        assert "BUG-queue-44364" in msg, f"Expected BUG-queue-44364 in: {msg}"

        # Must NOT say "truncated" (that's the ceiling path)
        assert "truncated at max_tokens" not in msg, (
            "Non-ceiling error path should not claim truncation."
        )

        # Must use hedged language — "possible" not "definitely"
        assert "possible" in msg.lower() or "may" in msg.lower(), (
            f"Error message should hedge on root cause, not assert definitively: {msg}"
        )
