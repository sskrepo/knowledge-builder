"""Tests for the ADR-013 filter strictness builder."""
from framework.stores.incident_vector_store import IncidentVectorStore

class _StubStore(IncidentVectorStore):
    def __init__(self):
        # bypass __init__
        pass

def test_hard_filter_emits_in_clause():
    s = _StubStore()
    where, score, binds = s._build_where_and_score([
        {"field": "kind", "values": ["incident_history"], "strictness": "hard"}
    ])
    assert "ci.kind IN" in where
    assert score == "1.00"

def test_soft_filter_emits_case_in_score():
    s = _StubStore()
    where, score, binds = s._build_where_and_score([
        {"field": "kind", "values": ["incident_history"], "strictness": "soft", "soft_multiplier": 0.85}
    ])
    assert where == "1=1"
    assert "CASE WHEN" in score
    assert "0.85" in score

def test_off_filter_skipped():
    s = _StubStore()
    where, score, binds = s._build_where_and_score([
        {"field": "kind", "values": ["x"], "strictness": "off"}
    ])
    assert where == "1=1"
    assert score == "1.00"
