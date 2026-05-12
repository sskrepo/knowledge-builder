"""Unit tests for BasePersonaSkill tool dispatch branches (GAP-D1)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_skill(extra_retrievers=None):
    """Build a concrete BasePersonaSkill subclass with mocked dependencies."""
    from framework.persona_skills._base import BasePersonaSkill
    from framework.core.interfaces import Result

    class TestSkill(BasePersonaSkill):
        persona = "ops_eng"
        PROMPT_FRAGMENT = "Test skill."

    llm = MagicMock()
    # LLM returns a stub plan pointing to the KB and tool under test
    llm.chat.return_value = {"text": json.dumps({
        "kbs_to_query": [{"name": "test_kb", "tools": list((extra_retrievers or {}).keys())}],
        "filters": [],
        "reasoning": "test",
    })}

    shim_kb = MagicMock()
    shim_kb.render_for_persona_prompt.return_value = ""
    shim_kb.cards_visible_to.return_value = []
    shim_kb.all_cards.return_value = []

    retrievers = dict(extra_retrievers or {})

    skill = TestSkill(llm=llm, shim_kb=shim_kb, retrievers=retrievers)
    return skill


def _make_intent(confidence=0.7):
    from framework.orchestrator.intent_classifier import IntentSignal
    return IntentSignal(primary_persona="ops_eng", confidence=confidence)


def _make_budget():
    from framework.orchestrator.budget import Budget
    return Budget()


def _make_result(content_id="c1", text="some text", score=0.9,
                 citation_url="https://example.com/ticket"):
    from framework.core.interfaces import Result
    return Result(
        content_id=content_id,
        chunk_id=None,
        text=text,
        score=score,
        citation_url=citation_url,
        metadata={"title": "Test"},
    )


# ---------------------------------------------------------------------------
# vector_search dispatch
# ---------------------------------------------------------------------------

def test_vector_search_dispatch_calls_tool_with_correct_args():
    mock_tool = MagicMock(return_value=[_make_result()])
    skill = _make_skill({"vector_search": mock_tool})
    intent = _make_intent()

    packet = skill(query="show incidents", intent_signal=intent, budget=_make_budget())

    mock_tool.assert_called_once()
    call_kwargs = mock_tool.call_args.kwargs
    assert call_kwargs["query"] == "show incidents"
    assert call_kwargs["corpus"] == "test_kb"
    assert len(packet.passages) == 1


# ---------------------------------------------------------------------------
# get_incident_summary dispatch
# ---------------------------------------------------------------------------

def test_get_incident_summary_extracts_incident_id():
    mock_tool = MagicMock(return_value=[_make_result()])
    skill = _make_skill({"get_incident_summary": mock_tool})

    packet = skill(
        query="What happened during INC-12345?",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(incident_id="INC-12345")
    assert len(packet.passages) == 1


def test_get_incident_summary_falls_back_to_full_query_when_no_id():
    mock_tool = MagicMock(return_value=[_make_result()])
    skill = _make_skill({"get_incident_summary": mock_tool})

    skill(
        query="what happened with the outage?",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(incident_id="what happened with the outage?")


# ---------------------------------------------------------------------------
# search_wiki dispatch
# ---------------------------------------------------------------------------

def test_search_wiki_dispatch():
    mock_tool = MagicMock(return_value=[_make_result()])
    skill = _make_skill({"search_wiki": mock_tool})

    packet = skill(
        query="find runbooks",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(query="find runbooks", persona="ops_eng", max_results=10)
    assert len(packet.passages) == 1


def test_search_wiki_empty_returns_no_passages():
    mock_tool = MagicMock(return_value=[])
    skill = _make_skill({"search_wiki": mock_tool})

    packet = skill(
        query="something not found",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    assert packet.passages == []


# ---------------------------------------------------------------------------
# read_wiki_page dispatch
# ---------------------------------------------------------------------------

def test_read_wiki_page_dispatch_with_result():
    mock_tool = MagicMock(return_value=_make_result(text="Wiki body content."))
    skill = _make_skill({"read_wiki_page": mock_tool})

    packet = skill(
        query="any query",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(path="test_kb")
    assert len(packet.passages) == 1
    assert packet.passages[0].text == "Wiki body content."


def test_read_wiki_page_dispatch_none_returns_no_passages():
    mock_tool = MagicMock(return_value=None)
    skill = _make_skill({"read_wiki_page": mock_tool})

    packet = skill(
        query="any query",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    assert packet.passages == []


# ---------------------------------------------------------------------------
# query_fleet dispatch
# ---------------------------------------------------------------------------

def test_query_fleet_dispatch():
    fleet_record = {
        "pod_id": "pod-alpha-001",
        "status": "healthy",
        "citation_url": "udap://fleet/pod/pod-alpha-001",
    }
    mock_tool = MagicMock(return_value=[fleet_record])
    skill = _make_skill({"query_fleet": mock_tool})

    packet = skill(
        query="fleet status",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(resource_type="pod", filters=None, limit=10)
    assert len(packet.passages) == 1
    assert packet.passages[0].citation.url == "udap://fleet/pod/pod-alpha-001"


# ---------------------------------------------------------------------------
# text_to_sql dispatch
# ---------------------------------------------------------------------------

def test_text_to_sql_dispatch_with_results():
    sql_result = {
        "sql": "SELECT * FROM pod_health",
        "results": [{"pod_id": "pod-001", "status": "healthy"}],
        "citation": "udap://fleet/view/pod_health",
        "view": "pod_health",
        "matched_pattern": True,
    }
    mock_tool = MagicMock(return_value=sql_result)
    skill = _make_skill({"text_to_sql": mock_tool})

    packet = skill(
        query="show pod health",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(nl_query="show pod health", limit=100)
    assert len(packet.passages) == 1


def test_text_to_sql_dispatch_not_implemented_degrades_gracefully():
    mock_tool = MagicMock(side_effect=NotImplementedError("LLM path not ready"))
    skill = _make_skill({"text_to_sql": mock_tool})

    # Should not raise; should just produce no passages
    packet = skill(
        query="some query",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )
    assert packet.passages == []


# ---------------------------------------------------------------------------
# find_symbol dispatch
# ---------------------------------------------------------------------------

def test_find_symbol_dispatch():
    symbol_records = [{
        "symbol": "MyClass",
        "file": "framework/core/interfaces.py",
        "line": 0,
        "kind": "class",
        "signature": "class MyClass",
        "citation_url": "code://framework/core/interfaces.py#MyClass",
    }]
    mock_tool = MagicMock(return_value=symbol_records)
    skill = _make_skill({"find_symbol": mock_tool})

    packet = skill(
        query="MyClass",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(symbol_name="MyClass", limit=20)
    assert len(packet.passages) == 1
    assert "code://" in packet.passages[0].citation.url


# ---------------------------------------------------------------------------
# read_code_page dispatch
# ---------------------------------------------------------------------------

def test_read_code_page_dispatch():
    code_page = {
        "module": "framework.core.interfaces",
        "file": "framework/core/interfaces.py",
        "summary": "Protocols every adapter implements.",
        "docstring": "Protocols ...",
        "classes": ["Result", "Query"],
        "functions": [],
        "citation_url": "code://framework/core/interfaces.py",
    }
    mock_tool = MagicMock(return_value=code_page)
    skill = _make_skill({"read_code_page": mock_tool})

    packet = skill(
        query="any query",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    mock_tool.assert_called_once_with(module_path="test_kb")
    assert len(packet.passages) == 1
    assert "Protocols" in packet.passages[0].text


# ---------------------------------------------------------------------------
# unknown tool name silently returns no passages
# ---------------------------------------------------------------------------

def test_unknown_tool_name_returns_no_passages():
    mock_tool = MagicMock(return_value=[])
    skill = _make_skill({"some_future_tool": mock_tool})

    packet = skill(
        query="some query",
        intent_signal=_make_intent(),
        budget=_make_budget(),
    )

    assert packet.passages == []
