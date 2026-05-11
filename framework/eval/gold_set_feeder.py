"""gold_set_feeder — interactive CLI state machine for populating eval gold sets.

Used during persona-authoring workshops to collect query/citation pairs from
persona team members.  Works in fully stub mode — no LLM, no ADB required.

State machine:
  INIT → ENTRY → CITATION → EXPECTED_FIELDS → REVIEW → NEXT → DONE

Gold set JSONL entries are accumulated in memory and written atomically at DONE.
New entries are appended when the target file already exists.

Usage (programmatic):
    feeder = GoldSetFeeder(persona="ops_eng", skill_name="incident_summary")
    turn = feeder.start()
    while not turn.done:
        print(turn.message)
        turn = feeder.respond(input("> "))

Usage (count existing entries for workshop tracking):
    count = count_entries("ops_eng")
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_SETS_DIR = REPO_ROOT / "framework" / "eval" / "gold_sets"

STATES = [
    "INIT",
    "ENTRY",
    "CITATION",
    "EXPECTED_FIELDS",
    "REVIEW",
    "NEXT",
    "DONE",
]


@dataclass
class FeedTurn:
    """Return value from every state handler."""

    state: str
    message: str
    options: list[str] | None = None
    done: bool = False
    entry_count: int = 0


@dataclass
class _EntryDraft:
    """Mutable buffer for the entry currently being assembled."""

    question: str = ""
    expected_citations: list[str] = field(default_factory=list)
    expected_fields: dict[str, str] = field(default_factory=dict)
    must_match_fields: list[str] = field(default_factory=list)
    notes: str = ""

    def reset(self) -> None:
        self.question = ""
        self.expected_citations = []
        self.expected_fields = {}
        self.must_match_fields = []
        self.notes = ""

    def to_entry(self, persona: str, skill_name: str, kb: str) -> dict:
        """Serialise to the JSONL gold set entry format."""
        uid = _entry_id(persona, self.question)
        return {
            "id": uid,
            "persona": persona,
            "question": self.question,
            "expected_citations": list(self.expected_citations),
            "expected_fields": dict(self.expected_fields),
            "must_match_fields": list(self.must_match_fields),
            "kb": kb or f"{persona}.{skill_name}",
            "skill": skill_name,
            "notes": self.notes,
            "added_by": "workshop",
            "added_at": _now_iso(),
        }


class GoldSetFeeder:
    """Interactive gold-set feeding state machine.

    Each public call returns a FeedTurn describing what to display to the
    workshop participant and the current state.  The feeder accumulates
    entries in memory; they are written to disk only when state reaches DONE.
    """

    def __init__(self, persona: str, skill_name: str = "") -> None:
        self._persona = persona.strip()
        self._skill_name = skill_name.strip()
        self._kb = f"{self._persona}.{self._skill_name}" if self._skill_name else self._persona
        self._state = "INIT"
        self._entries: list[dict] = []
        self._draft = _EntryDraft()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> FeedTurn:
        """Begin the session.  Call once before any respond() calls."""
        if self._persona and self._skill_name:
            # All metadata already supplied — jump straight to ENTRY
            self._state = "ENTRY"
            return self._prompt_entry()
        self._state = "INIT"
        return FeedTurn(
            state="INIT",
            message=(
                "Gold Set Feeder — workshop mode\n\n"
                "Which persona and KB / skill is this gold set for?\n"
                "Example: 'ops_eng incident_summary'  or  'ops_eng'\n"
            ),
            options=["ops_eng incident_summary", "pm release_brief", "tpm weekly_exec_review"],
            done=False,
            entry_count=0,
        )

    def respond(self, user_input: str) -> FeedTurn:
        """Submit a user response in the current state."""
        user_input = user_input.strip()

        if self._state == "INIT":
            return self._handle_init(user_input)
        if self._state == "ENTRY":
            return self._handle_entry(user_input)
        if self._state == "CITATION":
            return self._handle_citation(user_input)
        if self._state == "EXPECTED_FIELDS":
            return self._handle_expected_fields(user_input)
        if self._state == "REVIEW":
            return self._handle_review(user_input)
        if self._state == "NEXT":
            return self._handle_next(user_input)
        if self._state == "DONE":
            return FeedTurn(
                state="DONE",
                message="Session complete.",
                done=True,
                entry_count=len(self._entries),
            )
        return FeedTurn(
            state=self._state,
            message=f"Unknown state {self._state!r}. This is a bug.",
            done=True,
            entry_count=len(self._entries),
        )

    def get_entries(self) -> list[dict]:
        """Return accumulated entries (does not write to disk)."""
        return list(self._entries)

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _handle_init(self, user_input: str) -> FeedTurn:
        if not user_input:
            return FeedTurn(
                state="INIT",
                message="Please provide a persona name (e.g. 'ops_eng incident_summary').",
                entry_count=len(self._entries),
            )
        parts = user_input.split()
        self._persona = parts[0]
        self._skill_name = parts[1] if len(parts) > 1 else ""
        self._kb = f"{self._persona}.{self._skill_name}" if self._skill_name else self._persona
        self._state = "ENTRY"
        return self._prompt_entry()

    def _prompt_entry(self) -> FeedTurn:
        skill_label = f" / skill: {self._skill_name}" if self._skill_name else ""
        count = len(self._entries)
        goal_note = f"  ({count} entries so far — workshop target: 25)\n" if count else ""
        return FeedTurn(
            state="ENTRY",
            message=(
                f"Persona: {self._persona}{skill_label}\n"
                f"{goal_note}\n"
                "Type the question a team member would ask the KB.\n"
                "Example: 'What incidents affected pod refresh in the last 2 weeks?'"
            ),
            options=None,
            done=False,
            entry_count=count,
        )

    def _handle_entry(self, user_input: str) -> FeedTurn:
        if not user_input:
            return FeedTurn(
                state="ENTRY",
                message="Please enter a question.",
                entry_count=len(self._entries),
            )
        if user_input.lower() in ("done", "finish", "quit", "exit"):
            return self._do_done()

        question = _extract_question(user_input)
        self._draft.reset()
        self._draft.question = question
        self._state = "CITATION"
        return self._prompt_citation()

    def _prompt_citation(self) -> FeedTurn:
        return FeedTurn(
            state="CITATION",
            message=(
                f"Question: {self._draft.question!r}\n\n"
                "What citations (source URLs or doc paths) should appear in the answer?\n"
                "Enter one or more, comma-separated.  Example:\n"
                "  jira://OPS-1234, confluence://space/page\n"
                "\nType 'skip' if citations are not required for this entry."
            ),
            options=["skip"],
            done=False,
            entry_count=len(self._entries),
        )

    def _handle_citation(self, user_input: str) -> FeedTurn:
        lowered = user_input.lower().strip()
        if lowered not in ("skip", "none", ""):
            self._draft.expected_citations = _parse_citations(user_input)
        self._state = "EXPECTED_FIELDS"
        return self._prompt_expected_fields()

    def _prompt_expected_fields(self) -> FeedTurn:
        return FeedTurn(
            state="EXPECTED_FIELDS",
            message=(
                "Any specific field values that must appear in the answer? (optional)\n"
                "Enter key=value pairs, comma-separated.  Example:\n"
                "  severity=P1, root_cause=memory leak\n"
                "\nType 'skip' to omit."
            ),
            options=["skip"],
            done=False,
            entry_count=len(self._entries),
        )

    def _handle_expected_fields(self, user_input: str) -> FeedTurn:
        lowered = user_input.lower().strip()
        if lowered not in ("skip", "none", ""):
            fields, must_match = _parse_key_value_pairs(user_input)
            self._draft.expected_fields = fields
            self._draft.must_match_fields = must_match
        self._state = "REVIEW"
        return self._prompt_review()

    def _prompt_review(self) -> FeedTurn:
        lines = [
            "Review this entry before committing:\n",
            f"  question:           {self._draft.question!r}",
            f"  expected_citations: {self._draft.expected_citations or '(none)'}",
            f"  expected_fields:    {self._draft.expected_fields or '(none)'}",
            f"  must_match_fields:  {self._draft.must_match_fields or '(none)'}",
            "",
            "Type 'ok' to accept, or edit it:",
            "  change question to <new question>",
            "  add citation <url>",
            "  remove citation <url>",
            "  add field <key>=<value>",
        ]
        return FeedTurn(
            state="REVIEW",
            message="\n".join(lines),
            options=["ok", "change question to ...", "add citation ...", "add field ..."],
            done=False,
            entry_count=len(self._entries),
        )

    def _handle_review(self, user_input: str) -> FeedTurn:
        lowered = user_input.lower().strip()

        if lowered in ("ok", "looks good", "yes", "done", "commit", "save"):
            entry = self._draft.to_entry(self._persona, self._skill_name, self._kb)
            self._entries.append(entry)
            self._state = "NEXT"
            return self._prompt_next()

        # Edit: change question
        m = re.match(r"(?i)change\s+question\s+to\s+(.+)", user_input)
        if m:
            self._draft.question = m.group(1).strip().strip('"\'')
            return self._prompt_review()

        # Edit: add citation
        m = re.match(r"(?i)add\s+citation\s+(.+)", user_input)
        if m:
            new_cit = m.group(1).strip()
            if new_cit not in self._draft.expected_citations:
                self._draft.expected_citations.append(new_cit)
            return self._prompt_review()

        # Edit: remove citation
        m = re.match(r"(?i)remove\s+citation\s+(.+)", user_input)
        if m:
            target = m.group(1).strip()
            self._draft.expected_citations = [
                c for c in self._draft.expected_citations if c != target
            ]
            return self._prompt_review()

        # Edit: add field
        m = re.match(r"(?i)add\s+field\s+(.+)", user_input)
        if m:
            new_fields, new_must = _parse_key_value_pairs(m.group(1))
            self._draft.expected_fields.update(new_fields)
            for f in new_must:
                if f not in self._draft.must_match_fields:
                    self._draft.must_match_fields.append(f)
            return self._prompt_review()

        # Unrecognised — re-show
        return FeedTurn(
            state="REVIEW",
            message=(
                "I didn't understand that edit. Try:\n"
                "  'ok' to accept\n"
                "  'change question to <new question>'\n"
                "  'add citation <url>'\n"
                "  'remove citation <url>'\n"
                "  'add field <key>=<value>'"
            ),
            options=["ok", "change question to ...", "add citation ..."],
            done=False,
            entry_count=len(self._entries),
        )

    def _prompt_next(self) -> FeedTurn:
        count = len(self._entries)
        return FeedTurn(
            state="NEXT",
            message=(
                f"Entry #{count} saved.\n\n"
                "Add another question, or type 'done' to finish and write the file.\n"
                "You can also type a question directly here."
            ),
            options=["done"],
            done=False,
            entry_count=count,
        )

    def _handle_next(self, user_input: str) -> FeedTurn:
        lowered = user_input.lower().strip()
        if lowered in ("done", "finish", "quit", "exit", "no", "stop"):
            return self._do_done()
        # User typed another question directly — auto-route to ENTRY
        self._state = "ENTRY"
        return self._handle_entry(user_input)

    def _do_done(self) -> FeedTurn:
        """Write all entries to disk and signal completion."""
        if not self._entries:
            self._state = "DONE"
            return FeedTurn(
                state="DONE",
                message="No entries to save. Session finished.",
                done=True,
                entry_count=0,
            )
        out_path = _gold_set_path(self._persona)
        written = _append_jsonl(out_path, self._entries)
        self._state = "DONE"
        return FeedTurn(
            state="DONE",
            message=(
                f"Wrote {written} new entries to:\n  {out_path}\n\n"
                f"Total in file: {count_entries(self._persona)} entries.\n"
                "Workshop target: 25 per persona."
            ),
            done=True,
            entry_count=written,
        )


# ---------------------------------------------------------------------------
# Public utility
# ---------------------------------------------------------------------------

def count_entries(persona: str) -> int:
    """Return the number of entries in the gold set file for *persona*.

    Returns 0 if the file does not exist yet.
    """
    path = _gold_set_path(persona)
    if not path.exists():
        return 0
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    return len(lines)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _gold_set_path(persona: str) -> Path:
    return GOLD_SETS_DIR / f"{persona}.jsonl"


def _append_jsonl(path: Path, entries: list[dict]) -> int:
    """Append entries to a JSONL file; creates the file if absent.  Returns count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info("appended %d entries to %s", len(entries), path)
    return len(entries)


def _entry_id(persona: str, question: str) -> str:
    digest = hashlib.sha1(f"{persona}:{question}".encode()).hexdigest()[:8]
    return f"gs-{digest}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_question(text: str) -> str:
    """Extract the actual question text from natural-language input.

    Handles patterns like:
      'this question: what incidents affected pod refresh?'
      'question: what incidents...'
      'q: what...'
      Just the question itself.
    """
    # Strip common prefixes
    m = re.match(r"(?i)(?:this\s+)?(?:question|q)\s*:\s*(.+)", text, re.DOTALL)
    if m:
        return m.group(1).strip().strip('"\'')
    return text.strip().strip('"\'')


def _parse_citations(text: str) -> list[str]:
    """Split comma-separated citation strings; strip whitespace."""
    parts = [p.strip() for p in re.split(r"[,\n]+", text) if p.strip()]
    return parts


def _parse_key_value_pairs(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse 'key=value, key2=value2' into (fields_dict, must_match_list).

    All keys become must_match_fields so the eval harness checks them.
    Values may contain spaces (everything after '=' up to the next comma/newline).
    """
    fields: dict[str, str] = {}
    # Split on commas that are not inside a value segment
    parts = re.split(r",\s*(?=[^=]+=)", text + ",_sentinel=_")
    # Remove sentinel
    parts = [p for p in parts if "_sentinel" not in p]
    for part in parts:
        part = part.strip()
        if "=" in part:
            key, _, val = part.partition("=")
            key = key.strip().replace(" ", "_")
            val = val.strip()
            if key and val:
                fields[key] = val
    must_match = list(fields.keys())
    return fields, must_match
