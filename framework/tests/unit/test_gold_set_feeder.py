"""Unit tests for framework.eval.gold_set_feeder."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from framework.eval.gold_set_feeder import (
    GoldSetFeeder,
    FeedTurn,
    count_entries,
    _extract_question,
    _parse_citations,
    _parse_key_value_pairs,
    _gold_set_path,
    _entry_id,
)


# ---------------------------------------------------------------------------
# Helper — run a full happy-path session without touching disk
# ---------------------------------------------------------------------------

def _feed_through(
    persona: str = "ops_eng",
    skill: str = "incident_summary",
    question: str = "What incidents affected pod refresh last week?",
    citations: str = "jira://OPS-1234, confluence://space/page",
    fields: str = "severity=P1, root_cause=memory leak",
) -> tuple[GoldSetFeeder, list[FeedTurn]]:
    """Drive a single-entry session and return (feeder, turns)."""
    feeder = GoldSetFeeder(persona=persona, skill_name=skill)
    turns: list[FeedTurn] = []

    turn = feeder.start()
    turns.append(turn)
    assert turn.state == "ENTRY"  # persona+skill supplied → skip INIT

    turn = feeder.respond(question)
    turns.append(turn)
    assert turn.state == "CITATION"

    turn = feeder.respond(citations)
    turns.append(turn)
    assert turn.state == "EXPECTED_FIELDS"

    turn = feeder.respond(fields)
    turns.append(turn)
    assert turn.state == "REVIEW"

    turn = feeder.respond("ok")
    turns.append(turn)
    assert turn.state == "NEXT"

    return feeder, turns


# ---------------------------------------------------------------------------
# State machine: INIT
# ---------------------------------------------------------------------------

class TestInitState:
    def test_start_without_args_returns_init(self):
        feeder = GoldSetFeeder(persona="")
        turn = feeder.start()
        assert turn.state == "INIT"
        assert not turn.done

    def test_init_response_sets_persona_and_advances(self):
        feeder = GoldSetFeeder(persona="")
        feeder.start()
        turn = feeder.respond("ops_eng incident_summary")
        assert turn.state == "ENTRY"
        assert feeder._persona == "ops_eng"
        assert feeder._skill_name == "incident_summary"

    def test_init_with_persona_only(self):
        feeder = GoldSetFeeder(persona="")
        feeder.start()
        turn = feeder.respond("tpm")
        assert turn.state == "ENTRY"
        assert feeder._persona == "tpm"
        assert feeder._skill_name == ""

    def test_start_with_both_args_skips_init(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="incident_summary")
        turn = feeder.start()
        assert turn.state == "ENTRY"

    def test_empty_init_response_stays_in_init(self):
        feeder = GoldSetFeeder(persona="")
        feeder.start()
        turn = feeder.respond("")
        assert turn.state == "INIT"


# ---------------------------------------------------------------------------
# State machine: ENTRY
# ---------------------------------------------------------------------------

class TestEntryState:
    def test_entry_extracts_plain_question(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="incident_summary")
        feeder.start()
        turn = feeder.respond("What incidents affected pod refresh last week?")
        assert turn.state == "CITATION"
        assert feeder._draft.question == "What incidents affected pod refresh last week?"

    def test_entry_extracts_question_with_prefix(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        turn = feeder.respond("this question: what broke last tuesday?")
        assert turn.state == "CITATION"
        assert feeder._draft.question == "what broke last tuesday?"

    def test_entry_done_word_finishes_with_no_entries(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        turn = feeder.respond("done")
        assert turn.state == "DONE"
        assert turn.done
        assert turn.entry_count == 0

    def test_empty_entry_prompts_again(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        turn = feeder.respond("")
        assert turn.state == "ENTRY"


# ---------------------------------------------------------------------------
# State machine: CITATION
# ---------------------------------------------------------------------------

class TestCitationState:
    def _at_citation_state(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        feeder.respond("What went wrong?")
        return feeder

    def test_citation_parsed_correctly(self):
        feeder = self._at_citation_state()
        turn = feeder.respond("jira://OPS-1, confluence://space/page")
        assert turn.state == "EXPECTED_FIELDS"
        assert feeder._draft.expected_citations == ["jira://OPS-1", "confluence://space/page"]

    def test_citation_skip_advances_with_empty_list(self):
        feeder = self._at_citation_state()
        turn = feeder.respond("skip")
        assert turn.state == "EXPECTED_FIELDS"
        assert feeder._draft.expected_citations == []

    def test_citation_none_also_skips(self):
        feeder = self._at_citation_state()
        turn = feeder.respond("none")
        assert turn.state == "EXPECTED_FIELDS"
        assert feeder._draft.expected_citations == []


# ---------------------------------------------------------------------------
# State machine: EXPECTED_FIELDS
# ---------------------------------------------------------------------------

class TestExpectedFieldsState:
    def _at_fields_state(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        feeder.respond("What went wrong?")
        feeder.respond("jira://OPS-1")
        return feeder

    def test_fields_parsed_correctly(self):
        feeder = self._at_fields_state()
        turn = feeder.respond("severity=P1, root_cause=memory leak")
        assert turn.state == "REVIEW"
        assert feeder._draft.expected_fields == {"severity": "P1", "root_cause": "memory leak"}
        assert "severity" in feeder._draft.must_match_fields
        assert "root_cause" in feeder._draft.must_match_fields

    def test_fields_skip_advances_empty(self):
        feeder = self._at_fields_state()
        turn = feeder.respond("skip")
        assert turn.state == "REVIEW"
        assert feeder._draft.expected_fields == {}

    def test_single_field(self):
        feeder = self._at_fields_state()
        feeder.respond("severity=P2")
        assert feeder._draft.expected_fields == {"severity": "P2"}


# ---------------------------------------------------------------------------
# State machine: REVIEW
# ---------------------------------------------------------------------------

class TestReviewState:
    def _at_review_state(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="incident_summary")
        feeder.start()
        feeder.respond("What incidents affected pod refresh last week?")
        feeder.respond("jira://OPS-1234")
        feeder.respond("severity=P1")
        return feeder

    def test_ok_commits_entry_and_advances_to_next(self):
        feeder = self._at_review_state()
        turn = feeder.respond("ok")
        assert turn.state == "NEXT"
        assert turn.entry_count == 1
        entries = feeder.get_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["question"] == "What incidents affected pod refresh last week?"
        assert e["expected_citations"] == ["jira://OPS-1234"]
        assert e["expected_fields"] == {"severity": "P1"}
        assert e["persona"] == "ops_eng"
        assert e["skill"] == "incident_summary"
        assert e["id"].startswith("gs-")
        assert e["added_by"] == "workshop"

    def test_looks_good_also_commits(self):
        feeder = self._at_review_state()
        turn = feeder.respond("looks good")
        assert turn.state == "NEXT"
        assert len(feeder.get_entries()) == 1

    def test_change_question_edits_draft(self):
        feeder = self._at_review_state()
        turn = feeder.respond("change question to What is the MTTR for P1 incidents?")
        assert turn.state == "REVIEW"
        assert feeder._draft.question == "What is the MTTR for P1 incidents?"

    def test_add_citation_edits_draft(self):
        feeder = self._at_review_state()
        turn = feeder.respond("add citation confluence://runbooks/pod-refresh")
        assert turn.state == "REVIEW"
        assert "confluence://runbooks/pod-refresh" in feeder._draft.expected_citations

    def test_remove_citation_edits_draft(self):
        feeder = self._at_review_state()
        feeder.respond("add citation extra://doc")
        feeder.respond("remove citation jira://OPS-1234")
        assert "jira://OPS-1234" not in feeder._draft.expected_citations

    def test_add_field_edits_draft(self):
        feeder = self._at_review_state()
        turn = feeder.respond("add field status=resolved")
        assert turn.state == "REVIEW"
        assert feeder._draft.expected_fields.get("status") == "resolved"

    def test_unrecognised_edit_shows_help(self):
        feeder = self._at_review_state()
        turn = feeder.respond("blahblah")
        assert turn.state == "REVIEW"
        assert "ok" in turn.message.lower() or "ok" in (turn.options or [])

    def test_add_citation_no_duplicates(self):
        feeder = self._at_review_state()
        feeder.respond("add citation jira://OPS-1234")
        assert feeder._draft.expected_citations.count("jira://OPS-1234") == 1


# ---------------------------------------------------------------------------
# State machine: NEXT
# ---------------------------------------------------------------------------

class TestNextState:
    def _at_next_state(self):
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        feeder.respond("Question one?")
        feeder.respond("skip")
        feeder.respond("skip")
        feeder.respond("ok")
        return feeder

    def test_done_triggers_write_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder = self._at_next_state()
        turn = feeder.respond("done")
        assert turn.state == "DONE"
        assert turn.done
        assert turn.entry_count == 1

    def test_direct_question_at_next_auto_routes_to_entry(self):
        feeder = self._at_next_state()
        turn = feeder.respond("What else can go wrong?")
        assert turn.state == "CITATION"

    def test_finish_also_done(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder = self._at_next_state()
        turn = feeder.respond("finish")
        assert turn.state == "DONE"


# ---------------------------------------------------------------------------
# Full happy path + disk write
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_single_entry_written_to_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder, _ = _feed_through()
        turn = feeder.respond("done")
        assert turn.state == "DONE"
        assert turn.done

        out_file = tmp_path / "ops_eng.jsonl"
        assert out_file.exists()
        lines = [ln for ln in out_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["persona"] == "ops_eng"
        assert entry["skill"] == "incident_summary"
        assert entry["expected_citations"] == ["jira://OPS-1234", "confluence://space/page"]
        assert entry["expected_fields"]["severity"] == "P1"
        assert entry["must_match_fields"] == ["severity", "root_cause"]

    def test_two_entries_appended_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder, _ = _feed_through()
        # Add second entry from NEXT state
        feeder.respond("What is the mean time to detect for network issues?")
        feeder.respond("skip")
        feeder.respond("skip")
        feeder.respond("ok")
        turn = feeder.respond("done")
        assert turn.entry_count == 2

        lines = [ln for ln in (tmp_path / "ops_eng.jsonl").read_text().splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_entries_appended_across_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        # Session 1
        f1, _ = _feed_through(persona="ops_eng", skill="inc")
        f1.respond("done")

        # Session 2
        f2, _ = _feed_through(persona="ops_eng", skill="inc", question="Second question?")
        f2.respond("done")

        lines = [ln for ln in (tmp_path / "ops_eng.jsonl").read_text().splitlines() if ln.strip()]
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# count_entries utility
# ---------------------------------------------------------------------------

class TestCountEntries:
    def test_returns_zero_for_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        assert count_entries("nonexistent_persona") == 0

    def test_counts_correct_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        (tmp_path / "pm.jsonl").write_text(
            '{"id":"gs-a","question":"q1"}\n{"id":"gs-b","question":"q2"}\n'
        )
        assert count_entries("pm") == 2

    def test_ignores_blank_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        (tmp_path / "pm.jsonl").write_text(
            '{"id":"gs-a"}\n\n{"id":"gs-b"}\n\n'
        )
        assert count_entries("pm") == 2


# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------

class TestExtractQuestion:
    def test_plain_question(self):
        assert _extract_question("What broke last week?") == "What broke last week?"

    def test_with_question_prefix(self):
        assert _extract_question("question: what is latency?") == "what is latency?"

    def test_with_this_question_prefix(self):
        assert _extract_question("this question: any incidents today?") == "any incidents today?"

    def test_with_q_prefix(self):
        assert _extract_question("q: memory leak?") == "memory leak?"

    def test_strips_quotes(self):
        assert _extract_question('"what happened?"') == "what happened?"


class TestParseCitations:
    def test_comma_separated(self):
        result = _parse_citations("jira://OPS-1, confluence://space/page")
        assert result == ["jira://OPS-1", "confluence://space/page"]

    def test_newline_separated(self):
        result = _parse_citations("jira://OPS-1\nconfluence://space/page")
        assert result == ["jira://OPS-1", "confluence://space/page"]

    def test_single_citation(self):
        result = _parse_citations("jira://OPS-1")
        assert result == ["jira://OPS-1"]

    def test_strips_whitespace(self):
        result = _parse_citations("  jira://A  ,  confluence://B  ")
        assert result == ["jira://A", "confluence://B"]


class TestParseKeyValuePairs:
    def test_single_pair(self):
        fields, must = _parse_key_value_pairs("severity=P1")
        assert fields == {"severity": "P1"}
        assert must == ["severity"]

    def test_multi_pair(self):
        fields, must = _parse_key_value_pairs("severity=P1, root_cause=memory leak")
        assert fields["severity"] == "P1"
        assert fields["root_cause"] == "memory leak"
        assert set(must) == {"severity", "root_cause"}

    def test_no_pairs_returns_empty(self):
        fields, must = _parse_key_value_pairs("skip")
        assert fields == {}
        assert must == []


class TestEntryId:
    def test_deterministic(self):
        a = _entry_id("ops_eng", "What broke?")
        b = _entry_id("ops_eng", "What broke?")
        assert a == b

    def test_starts_with_gs(self):
        assert _entry_id("ops_eng", "q").startswith("gs-")

    def test_different_questions_differ(self):
        assert _entry_id("ops_eng", "q1") != _entry_id("ops_eng", "q2")


# ---------------------------------------------------------------------------
# DONE state idempotency
# ---------------------------------------------------------------------------

class TestDoneState:
    def test_done_responds_gracefully_on_second_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder, _ = _feed_through()
        feeder.respond("done")
        # Calling respond() again after DONE
        turn = feeder.respond("anything")
        assert turn.state == "DONE"
        assert turn.done

    def test_no_entries_done_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder = GoldSetFeeder(persona="ops_eng", skill_name="s")
        feeder.start()
        turn = feeder.respond("done")
        assert turn.state == "DONE"
        assert turn.done
        assert turn.entry_count == 0
        # File should NOT be created when there are no entries
        assert not (tmp_path / "ops_eng.jsonl").exists()


# ---------------------------------------------------------------------------
# Entry schema validation
# ---------------------------------------------------------------------------

class TestEntrySchema:
    REQUIRED_KEYS = {
        "id", "persona", "question", "expected_citations",
        "expected_fields", "must_match_fields", "kb", "skill",
        "notes", "added_by", "added_at",
    }

    def test_entry_has_all_required_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder, _ = _feed_through()
        feeder.respond("done")
        line = (tmp_path / "ops_eng.jsonl").read_text().splitlines()[0]
        entry = json.loads(line)
        missing = self.REQUIRED_KEYS - set(entry.keys())
        assert not missing, f"Entry missing keys: {missing}"

    def test_kb_defaults_to_persona_dot_skill(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "framework.eval.gold_set_feeder.GOLD_SETS_DIR", tmp_path
        )
        feeder, _ = _feed_through(persona="ops_eng", skill="incident_summary")
        feeder.respond("done")
        line = (tmp_path / "ops_eng.jsonl").read_text().splitlines()[0]
        entry = json.loads(line)
        assert entry["kb"] == "ops_eng.incident_summary"
