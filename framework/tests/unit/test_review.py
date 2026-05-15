"""Unit tests for review.py — BUG-queue-573e3.

Coverage:
  - _escape_bare_control_chars: bare \\n inside quoted value is escaped to \\\\n;
    structural newlines between keys are preserved; already-escaped \\\\n is untouched;
    backslash-escaped quotes inside strings don't confuse the state machine.
  - _llm_extract end-to-end: mock LLM that returns pretty-printed JSON with a bare
    newline in one string value (>=30 keys, bad value on ~line 34) parses correctly.
  - _llm_extract hard-fail: irreparably broken LLM output raises ValueError (not
    returning {}), and the exception message is non-empty and actionable.

Test style follows test_synthesizer_content_filter.py and
test_skill_builder_conversation.py (MagicMock + side_effect, pytest classes).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from framework.skill_builder.review import _escape_bare_control_chars, _llm_extract


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


def _make_mock_llm(response_text: str) -> MagicMock:
    mock = MagicMock()
    mock.chat.return_value = {"text": response_text}
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
