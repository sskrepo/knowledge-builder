"""Unit tests for framework/deploy/skill_prompt.py.

Coverage:
  - get_skill_prompt_messages() returns list with one message
  - message role is "user", content type is "text"
  - prompt text contains "reportBug", "requestId", "isError", and version string
  - SKILL_PROMPT_NAME and SKILL_PROMPT_DESCRIPTION are non-empty strings
  - SKILL_PROMPT_VERSION follows semver pattern (N.N.N)
"""
from __future__ import annotations

import re

import pytest

from framework.deploy.skill_prompt import (
    SKILL_PROMPT_DESCRIPTION,
    SKILL_PROMPT_NAME,
    SKILL_PROMPT_VERSION,
    get_skill_prompt_messages,
)


class TestSkillPromptConstants:
    def test_name_is_kbf_skill_prompt(self):
        assert SKILL_PROMPT_NAME == "kbf-skill-prompt"

    def test_description_is_nonempty(self):
        assert isinstance(SKILL_PROMPT_DESCRIPTION, str)
        assert len(SKILL_PROMPT_DESCRIPTION) > 0

    def test_version_follows_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+$", SKILL_PROMPT_VERSION), (
            f"Expected semver, got: {SKILL_PROMPT_VERSION!r}"
        )

    def test_version_is_1_2_0(self):
        assert SKILL_PROMPT_VERSION == "1.2.0"


class TestGetSkillPromptMessages:
    def test_returns_list(self):
        messages = get_skill_prompt_messages()
        assert isinstance(messages, list)

    def test_returns_exactly_one_message(self):
        messages = get_skill_prompt_messages()
        assert len(messages) == 1

    def test_message_role_is_user(self):
        message = get_skill_prompt_messages()[0]
        assert message["role"] == "user"

    def test_message_content_type_is_text(self):
        message = get_skill_prompt_messages()[0]
        assert message["content"]["type"] == "text"

    def test_message_content_text_is_nonempty(self):
        message = get_skill_prompt_messages()[0]
        text = message["content"]["text"]
        assert isinstance(text, str)
        assert len(text) > 100  # substantial prompt content

    def test_prompt_contains_report_bug(self):
        text = get_skill_prompt_messages()[0]["content"]["text"]
        assert "reportBug" in text

    def test_prompt_contains_request_id(self):
        text = get_skill_prompt_messages()[0]["content"]["text"]
        assert "requestId" in text

    def test_prompt_contains_is_error(self):
        text = get_skill_prompt_messages()[0]["content"]["text"]
        assert "isError" in text

    def test_prompt_contains_version_string(self):
        text = get_skill_prompt_messages()[0]["content"]["text"]
        assert SKILL_PROMPT_VERSION in text

    def test_prompt_contains_author_skill(self):
        text = get_skill_prompt_messages()[0]["content"]["text"]
        assert "authorSkill" in text

    def test_prompt_contains_ask_knowledge_base(self):
        text = get_skill_prompt_messages()[0]["content"]["text"]
        assert "askKnowledgeBase" in text

    def test_each_call_returns_same_content(self):
        """get_skill_prompt_messages is pure — repeated calls return equal result."""
        a = get_skill_prompt_messages()
        b = get_skill_prompt_messages()
        assert a == b
