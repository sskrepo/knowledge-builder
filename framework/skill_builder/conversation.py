"""conversation — interactive skill-builder session (author_skill API).

Per ADR-015 §Conversation contract. Implements a state machine that drives the
full skill authoring lifecycle from IDENTIFY_PERSONA through DONE. Works in
stub LLM mode with template-based responses — no external services required.

State machine (14 states):
  IDENTIFY_PERSONA → ANALYZE_ARTIFACT → REVIEW_FIELDS → REVIEW_SCHEMA →
  CHECK_REUSE → CONFIGURE_SOURCES → CONFIGURE_TRIGGERS → PREVIEW → CONFIRM →
  COMMITTED → VALIDATE → INGEST → EVAL → PROMOTE → DONE

Session persistence: sessions are serializable via to_dict()/from_dict() for
storage in ADB (keyed by synth_id + user_id).  This enables resume across
client restarts and listing all in-progress authoring sessions per user.
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

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

STATES = [
    "IDENTIFY_PERSONA",
    "ANALYZE_ARTIFACT",
    "REVIEW_FIELDS",
    "REVIEW_SCHEMA",
    "CHECK_REUSE",
    "CONFIGURE_SOURCES",
    "CONFIGURE_TRIGGERS",
    "PREVIEW",
    "CONFIRM",
    "COMMITTED",
    "VALIDATE",
    "INGEST",
    "EVAL",
    "PROMOTE",
    "DONE",
]


@dataclass
class ConversationTurn:
    """Return value from every state handler."""

    synth_id: str = ""
    state: str = ""
    message: str = ""
    data: dict | None = None
    options: list[str] | None = None
    artifacts_preview: dict | None = None
    progress: dict | None = None
    done: bool = False


def _progress(state: str) -> dict:
    """Return a progress dict for the given state."""
    try:
        step = STATES.index(state) + 1
    except ValueError:
        step = 0
    return {"step": step, "total": len(STATES), "label": state.replace("_", " ").title()}


@dataclass
class _SessionData:
    """Mutable session state threaded through the conversation."""

    intent_description: str = ""
    artifact_path: str = ""
    fields: list[str] = field(default_factory=list)
    field_specs: dict[str, dict] = field(default_factory=dict)
    slide_mapping: dict | None = None
    reuse_result: dict = field(default_factory=lambda: {"covered": {}, "gaps": []})
    sources: list[dict] = field(default_factory=list)
    trigger: dict = field(default_factory=lambda: {"on_request": True})
    output_format: str = "markdown"
    persona: str = ""
    skill_name: str = ""
    user_id: str = ""
    synth_id: str = ""
    synthesized_artifacts: dict = field(default_factory=dict)
    committed_paths: list[str] = field(default_factory=list)
    validation_result: dict | None = None
    ingest_result: dict | None = None
    eval_result: dict | None = None
    created_at: str = ""
    updated_at: str = ""


class SkillBuilderConversation:
    """Interactive skill-builder session (author_skill API).

    Drives the full authoring lifecycle through a 14-state conversation.
    Each call to start() / respond() returns a ConversationTurn describing
    what to display to the user and the current state.

    The session is serializable (to_dict/from_dict) for persistence in ADB,
    enabling resume across client restarts.

    In stub LLM mode responses are template-based; real LLM integration would
    replace the _* handlers while keeping the state machine intact.
    """

    def __init__(self, persona: str = "", user_id: str = "", llm=None):
        self._persona = persona
        self._llm = llm
        self._state = "IDENTIFY_PERSONA"
        now = _now_iso()
        synth_id = _make_synth_id(persona, now)
        self._data = _SessionData(
            persona=persona,
            user_id=user_id,
            synth_id=synth_id,
            created_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, intent_description: str = "") -> ConversationTurn:
        """Begin the session. Call once before any respond() calls."""
        self._data.intent_description = intent_description.strip()
        self._data.updated_at = _now_iso()

        if self._data.persona and self._data.intent_description:
            self._data.skill_name = _slugify(self._data.intent_description)
            self._state = "ANALYZE_ARTIFACT"
            return self._turn(self._handle_analyze_artifact_prompt())
        if self._data.persona:
            self._state = "IDENTIFY_PERSONA"
            return self._turn(ConversationTurn(
                state="IDENTIFY_PERSONA",
                message=(
                    f"Skill Builder — persona: {self._data.persona}\n\n"
                    "What task do you want automated? Describe it in plain English.\n"
                    "Example: 'Produce a weekly project status PPT for exec review every Friday'"
                ),
            ))
        self._state = "IDENTIFY_PERSONA"
        return self._turn(self._prompt_identify_persona())

    def respond(self, user_input: str) -> ConversationTurn:
        """Submit a user response in the current state."""
        user_input = user_input.strip()
        self._data.updated_at = _now_iso()

        handler = {
            "IDENTIFY_PERSONA": self._handle_identify_persona,
            "ANALYZE_ARTIFACT": self._handle_analyze_artifact,
            "REVIEW_FIELDS": self._handle_review_fields_response,
            "REVIEW_SCHEMA": self._handle_review_schema_response,
            "CHECK_REUSE": self._handle_check_reuse_response,
            "CONFIGURE_SOURCES": self._handle_configure_sources_response,
            "CONFIGURE_TRIGGERS": self._handle_configure_triggers_response,
            "PREVIEW": self._handle_preview_response,
            "CONFIRM": self._handle_confirm_response,
            "COMMITTED": self._handle_committed_response,
            "VALIDATE": self._handle_validate_response,
            "INGEST": self._handle_ingest_response,
            "EVAL": self._handle_eval_response,
            "PROMOTE": self._handle_promote_response,
            "DONE": lambda _: ConversationTurn(
                state="DONE", message="Session complete.", done=True,
            ),
        }.get(self._state)

        if handler is None:
            return self._turn(ConversationTurn(
                state=self._state,
                message=f"Unknown state {self._state!r}. This is a bug.",
                done=True,
            ))
        return self._turn(handler(user_input))

    def get_state(self) -> dict:
        """Return a snapshot of the current session state (for GET endpoint)."""
        return {
            "synth_id": self._data.synth_id,
            "state": self._state,
            "persona": self._data.persona,
            "skill_name": self._data.skill_name,
            "intent_description": self._data.intent_description,
            "user_id": self._data.user_id,
            "artifact_path": self._data.artifact_path,
            "fields": list(self._data.fields),
            "field_specs": dict(self._data.field_specs),
            "reuse": dict(self._data.reuse_result),
            "sources": list(self._data.sources),
            "trigger": dict(self._data.trigger),
            "output_format": self._data.output_format,
            "committed_paths": list(self._data.committed_paths),
            "validation_result": self._data.validation_result,
            "ingest_result": self._data.ingest_result,
            "eval_result": self._data.eval_result,
            "created_at": self._data.created_at,
            "updated_at": self._data.updated_at,
            "progress": _progress(self._state),
        }

    def to_dict(self) -> dict:
        """Serialize entire session for DB persistence.

        NOTE: get_state() intentionally omits synthesized_artifacts and
        slide_mapping to keep the GET-endpoint snapshot lean (artifact content
        can be several KB of YAML/JSON).  We add them back here so that the
        PREVIEW → COMMIT and ANALYZE_ARTIFACT → REVIEW_SCHEMA transitions work
        correctly when a session is saved and resumed across separate MCP calls.
        Without this, _write_artifacts() iterates an empty dict and commits 0
        artifacts (BUG-004).
        """
        d = {"state": self._state, "persona": self._persona, **self.get_state()}
        d["synthesized_artifacts"] = dict(self._data.synthesized_artifacts)
        if self._data.slide_mapping is not None:
            d["slide_mapping"] = dict(self._data.slide_mapping)
        return d

    @classmethod
    def from_dict(cls, d: dict, llm=None) -> "SkillBuilderConversation":
        """Restore a session from a persisted dict."""
        obj = cls.__new__(cls)
        obj._persona = d.get("persona", "")
        obj._llm = llm
        obj._state = d.get("state", "IDENTIFY_PERSONA")
        obj._data = _SessionData(
            intent_description=d.get("intent_description", ""),
            artifact_path=d.get("artifact_path", ""),
            fields=list(d.get("fields", [])),
            field_specs=dict(d.get("field_specs", {})),
            slide_mapping=d.get("slide_mapping"),
            reuse_result=dict(d.get("reuse", {"covered": {}, "gaps": []})),
            sources=list(d.get("sources", [])),
            trigger=dict(d.get("trigger", {"on_request": True})),
            output_format=d.get("output_format", "markdown"),
            persona=d.get("persona", ""),
            skill_name=d.get("skill_name", ""),
            user_id=d.get("user_id", ""),
            synth_id=d.get("synth_id", ""),
            synthesized_artifacts=dict(d.get("synthesized_artifacts", {})),
            committed_paths=list(d.get("committed_paths", [])),
            validation_result=d.get("validation_result"),
            ingest_result=d.get("ingest_result"),
            eval_result=d.get("eval_result"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )
        return obj

    def _turn(self, turn: ConversationTurn) -> ConversationTurn:
        """Stamp synth_id and progress on every outgoing turn."""
        turn.synth_id = self._data.synth_id
        turn.progress = _progress(self._state)
        return turn

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _prompt_identify_persona(self) -> ConversationTurn:
        personas = _list_available_personas()
        persona_lines = "\n".join(
            f"  • {p['name']} — {p['display_name']} ({p['skill_count']} skills)"
            for p in personas
        )
        return ConversationTurn(
            state="IDENTIFY_PERSONA",
            message=(
                "Which persona is this skill for?\n\n"
                f"{persona_lines}\n\n"
                "Type the persona name and describe the task.\n"
                "Example: 'ops_eng — automate weekly ADB-S migration status updates'"
            ),
            data={"personas": personas},
            options=[p["name"] for p in personas],
        )

    def _handle_identify_persona(self, user_input: str) -> ConversationTurn:
        if not user_input:
            return self._prompt_identify_persona()

        parts = re.split(r"\s*[—–\-:]\s*", user_input, maxsplit=1)
        persona_candidate = _to_field_name(parts[0])
        intent = parts[1].strip() if len(parts) > 1 else ""

        known = [p["name"] for p in _list_available_personas()]
        if persona_candidate not in known and known:
            return ConversationTurn(
                state="IDENTIFY_PERSONA",
                message=f"Unknown persona '{persona_candidate}'. Available: {', '.join(known)}",
                options=known,
            )

        self._persona = persona_candidate
        self._data.persona = persona_candidate
        self._data.synth_id = _make_synth_id(persona_candidate, self._data.created_at)

        if intent:
            self._data.intent_description = intent
            self._data.skill_name = _slugify(intent)
            self._state = "ANALYZE_ARTIFACT"
            return self._handle_analyze_artifact_prompt()

        self._state = "IDENTIFY_PERSONA"
        return ConversationTurn(
            state="IDENTIFY_PERSONA",
            message=(
                f"Persona: {persona_candidate}\n\n"
                "What task do you want automated? Describe it in plain English.\n"
                "Example: 'Produce a weekly project status PPT for exec review every Friday'"
            ),
        )

    def _handle_analyze_artifact_prompt(self) -> ConversationTurn:
        return ConversationTurn(
            state="ANALYZE_ARTIFACT",
            message=(
                f"Great. You want to automate: '{self._data.intent_description}'.\n\n"
                "Now show me an example outcome. Provide the path to a PPT, DOCX, Markdown, "
                "or text file that represents the kind of output this skill should produce.\n\n"
                "If you don't have a file, type the field names separated by commas "
                "(e.g. 'week_id, rag_status, blockers, exec_asks')."
            ),
            options=[
                "framework/_dev_fixtures/skill_builder_intents/example_workflow.yaml",
                "Enter field names manually",
            ],
        )

    def _handle_analyze_artifact(self, user_input: str) -> ConversationTurn:
        from .analyze_artifact import analyze_artifact

        path = user_input.strip()

        if Path(path).exists() and Path(path).suffix in (".pptx", ".docx", ".md", ".txt"):
            fields, mapping = analyze_artifact(path)
            self._data.artifact_path = path
            self._data.fields = fields
            self._data.slide_mapping = mapping
            source = f"artifact at {path!r}"
        else:
            fields, mapping = self._parse_fields_from_input(user_input)
            self._data.fields = fields
            self._data.slide_mapping = mapping
            source = "your field list"

        self._state = "REVIEW_FIELDS"
        return self._handle_review_fields_prompt(source)

    def _handle_review_fields_prompt(self, source: str) -> ConversationTurn:
        fields = self._data.fields
        field_list = "\n".join(f"  • {f}" for f in fields)
        return ConversationTurn(
            state="REVIEW_FIELDS",
            message=(
                f"From {source} I found these fields:\n{field_list}\n\n"
                "Would you like to add, remove, or rename any? "
                "Type changes (e.g. 'add priority_score', 'remove details', "
                "'rename summary to executive_summary') or 'ok' to continue."
            ),
            options=["ok", "add <field>", "remove <field>", "rename <old> to <new>"],
        )

    def _handle_review_fields_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()

        if lowered in ("ok", "looks good", "continue", "done", "yes"):
            return self._advance_to_review_schema()

        edits = _parse_field_edits(user_input)
        for edit in edits:
            if edit[0] == "add":
                f = _to_field_name(edit[1])
                if f and f not in self._data.fields:
                    self._data.fields.append(f)
            elif edit[0] == "remove":
                f = _to_field_name(edit[1])
                self._data.fields = [x for x in self._data.fields if x != f]
            elif edit[0] == "rename":
                old, new = _to_field_name(edit[1]), _to_field_name(edit[2])
                self._data.fields = [new if x == old else x for x in self._data.fields]

        if not edits:
            return ConversationTurn(
                state="REVIEW_FIELDS",
                message=(
                    "I didn't understand that edit. Try 'add <field>', "
                    "'remove <field>', 'rename <old> to <new>', or 'ok'."
                ),
                options=["ok", "add <field>", "remove <field>"],
            )

        return self._handle_review_fields_prompt("updated list")

    # -- REVIEW_SCHEMA ---------------------------------------------------

    def _advance_to_review_schema(self) -> ConversationTurn:
        """Generate field specs (type + description) and present for review."""
        from .synthesize_schema import _infer_field_spec

        for f in self._data.fields:
            if f not in self._data.field_specs:
                self._data.field_specs[f] = _infer_field_spec(f)

        self._state = "REVIEW_SCHEMA"
        return self._prompt_review_schema()

    def _prompt_review_schema(self) -> ConversationTurn:
        lines = [
            "These extraction instructions tell the parser what to look for in each field.",
            "The description is the most important part — it controls extraction quality.\n",
        ]
        for f in self._data.fields:
            spec = self._data.field_specs.get(f, {})
            t = spec.get("type", "string")
            desc = spec.get("description", "")
            lines.append(f"  {f} [{t}]: {desc}")

        lines.append("")
        lines.append("Edit any field's extraction instructions:")
        lines.append("  describe <field> as <extraction instruction>")
        lines.append("  set type of <field> to <type>")
        lines.append("  set maxLength of <field> to <number>")
        lines.append("  set enum of <field> to <val1>, <val2>, ...")
        lines.append("")
        lines.append("Type 'ok' when the descriptions accurately capture what to extract.")

        field_data = [
            {"name": f, **self._data.field_specs.get(f, {})}
            for f in self._data.fields
        ]

        return ConversationTurn(
            state="REVIEW_SCHEMA",
            message="\n".join(lines),
            data={"field_specs": field_data},
            options=["ok", "describe <field> as <text>", "set type of <field> to <type>"],
        )

    def _handle_review_schema_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()

        if lowered in ("ok", "looks good", "continue", "done", "yes"):
            return self._advance_to_check_reuse()

        # describe <field> as <text>
        m = re.match(r"(?i)describe\s+(\S+)\s+as\s+(.+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            new_desc = m.group(2).strip().strip("'\"")
            if field_name in self._data.field_specs:
                self._data.field_specs[field_name]["description"] = new_desc
            elif field_name in self._data.fields:
                self._data.field_specs[field_name] = {
                    "type": "string", "description": new_desc, "maxLength": 500,
                }
            else:
                return ConversationTurn(
                    state="REVIEW_SCHEMA",
                    message=f"Unknown field '{field_name}'. Available: {', '.join(self._data.fields)}",
                )
            return self._prompt_review_schema()

        # set type of <field> to <type>
        m = re.match(r"(?i)set\s+type\s+of\s+(\S+)\s+to\s+(\S+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            new_type = m.group(2).strip()
            if new_type in ("string", "integer", "number", "boolean", "array", "object"):
                if field_name in self._data.field_specs:
                    self._data.field_specs[field_name]["type"] = new_type
                    return self._prompt_review_schema()
            return ConversationTurn(
                state="REVIEW_SCHEMA",
                message=f"Invalid type '{new_type}'. Valid: string, integer, number, boolean, array, object",
            )

        # set maxLength of <field> to <number>
        m = re.match(r"(?i)set\s+maxLength\s+of\s+(\S+)\s+to\s+(\d+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            if field_name in self._data.field_specs:
                self._data.field_specs[field_name]["maxLength"] = int(m.group(2))
                return self._prompt_review_schema()

        # set enum of <field> to <val1>, <val2>, ...
        m = re.match(r"(?i)set\s+enum\s+of\s+(\S+)\s+to\s+(.+)", user_input)
        if m:
            field_name = _to_field_name(m.group(1))
            vals = [v.strip().strip("'\"") for v in m.group(2).split(",") if v.strip()]
            if field_name in self._data.field_specs and vals:
                self._data.field_specs[field_name]["enum"] = vals
                return self._prompt_review_schema()

        return ConversationTurn(
            state="REVIEW_SCHEMA",
            message=(
                "I didn't understand that edit. Try:\n"
                "  'describe <field> as <extraction instruction>'\n"
                "  'set type of <field> to <type>'\n"
                "  'ok' to continue"
            ),
            options=["ok", "describe <field> as <text>"],
        )

    # -- CHECK_REUSE -----------------------------------------------------

    def _advance_to_check_reuse(self) -> ConversationTurn:
        from .reuse_detector import detect_reuse
        from ..orchestrator.shim_kb import ShimKb

        pb_dir = REPO_ROOT / "framework" / "persona_builders"
        try:
            shim = ShimKb(pb_dir)
            result = detect_reuse(self._data.fields, self._data.persona, shim)
        except Exception as e:
            log.warning("reuse detection failed: %s; assuming no reuse", e)
            result = {"covered": {}, "gaps": list(self._data.fields)}

        self._data.reuse_result = result
        self._state = "CHECK_REUSE"

        covered = result.get("covered", {})
        gaps = result.get("gaps", [])

        if not covered and not gaps:
            return self._advance_to_configure_sources()

        covered_lines = "\n".join(
            f"  • {f} → reusing from {kb}" for f, kb in covered.items()
        )
        gap_lines = "\n".join(f"  • {f}" for f in gaps)

        msg_parts = []
        if covered:
            msg_parts.append(f"Already covered by existing KBs ({len(covered)} fields):\n{covered_lines}")
        if gaps:
            msg_parts.append(f"New extraction needed for ({len(gaps)} fields):\n{gap_lines}")

        return ConversationTurn(
            state="CHECK_REUSE",
            message=(
                "\n\n".join(msg_parts)
                + "\n\nReady to configure sources for the new extraction. Continue?"
            ),
            options=["yes, continue", "no, let me revise fields"],
        )

    def _handle_check_reuse_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("yes", "continue", "ok", "go")):
            return self._advance_to_configure_sources()
        if any(kw in lowered for kw in ("no", "revise", "back")):
            self._state = "REVIEW_FIELDS"
            return self._handle_review_fields_prompt("current list")
        return ConversationTurn(
            state="CHECK_REUSE",
            message="Please respond with 'yes' to continue or 'no' to revise fields.",
            options=["yes, continue", "no, let me revise fields"],
        )

    def _advance_to_configure_sources(self) -> ConversationTurn:
        self._state = "CONFIGURE_SOURCES"
        return ConversationTurn(
            state="CONFIGURE_SOURCES",
            message=(
                "Where does the source data live?\n"
                "Describe one or more sources (you can add multiple):\n\n"
                "  • Confluence: 'confluence SPACE_KEY with labels: label1, label2'\n"
                "  • Jira: 'jira JQL: project = OPS AND labels = weekly-status'\n"
                "  • Git: 'git repo org/my-repo paths: **/*.md'\n\n"
                "Type 'done' when finished adding sources."
            ),
            options=[
                "confluence PRODUCT labels: weekly-status",
                "jira project = OPS AND labels = weekly-ops",
                "done",
            ],
        )

    def _handle_configure_sources_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if lowered == "done":
            if not self._data.sources:
                self._data.sources.append({"kind": "confluence", "space": "REPLACE_ME"})
            return self._advance_to_configure_triggers()

        source = _parse_source_descriptor(user_input)
        self._data.sources.append(source)

        return ConversationTurn(
            state="CONFIGURE_SOURCES",
            message=(
                f"Added source: {source}.\n"
                f"Current sources ({len(self._data.sources)}):\n"
                + "\n".join(f"  • {s}" for s in self._data.sources)
                + "\n\nAdd another source or type 'done'."
            ),
            options=["done"],
        )

    def _advance_to_configure_triggers(self) -> ConversationTurn:
        self._state = "CONFIGURE_TRIGGERS"
        return ConversationTurn(
            state="CONFIGURE_TRIGGERS",
            message=(
                "How should this skill be triggered?\n\n"
                "  1. on-request only (user asks → skill runs immediately)\n"
                "  2. scheduled only  (e.g. '0 16 * * 5' = every Friday 4pm)\n"
                "  3. both            (on-request + schedule)\n\n"
                "Also, what format should the output be? "
                "(markdown / pptx / docx / email / slack)\n\n"
                "Example: '3, pptx, 0 16 * * 5'"
            ),
            options=[
                "1, markdown",
                "2, pptx, 0 16 * * 5",
                "3, pptx, 0 16 * * 5",
            ],
        )

    def _handle_configure_triggers_response(self, user_input: str) -> ConversationTurn:
        trigger, output_format = _parse_trigger_input(user_input)
        self._data.trigger = trigger
        self._data.output_format = output_format
        return self._advance_to_preview()

    def _advance_to_preview(self) -> ConversationTurn:
        self._state = "PREVIEW"
        artifacts = self._synthesize_preview()
        self._data.synthesized_artifacts = artifacts

        artifact_summary: dict = {}
        for path, content in artifacts.items():
            if isinstance(content, (dict, list)):
                artifact_summary[path] = f"({type(content).__name__} with {len(content)} entries)"
            else:
                snippet = str(content)[:120].replace("\n", " ")
                artifact_summary[path] = snippet

        return ConversationTurn(
            state="PREVIEW",
            message=(
                "Here's what I'll commit:\n\n"
                + "\n".join(f"  • {path}: {summary}" for path, summary in artifact_summary.items())
                + "\n\nReview the artifacts above. Commit them?"
            ),
            options=["yes, commit", "no, let me adjust"],
            artifacts_preview=artifact_summary,
        )

    def _handle_preview_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("yes", "commit", "ok", "go", "looks good")):
            return self._handle_commit()
        return ConversationTurn(
            state="PREVIEW",
            message="What would you like to adjust? (type 'yes' to commit when ready)",
            options=["yes, commit"],
        )

    def _handle_confirm_response(self, user_input: str) -> ConversationTurn:
        return self._handle_commit()

    def _handle_commit(self) -> ConversationTurn:
        committed_paths = self._write_artifacts()
        self._data.committed_paths = committed_paths
        self._state = "COMMITTED"
        return ConversationTurn(
            state="COMMITTED",
            message=(
                f"Committed {len(committed_paths)} artifact(s):\n"
                + "\n".join(f"  • {p}" for p in committed_paths)
                + "\n\nReady to validate, ingest, run eval, and promote?\n"
                "Type 'yes' to run the full pipeline, or 'stop' to finish here."
            ),
            options=["yes, run full pipeline", "just validate", "stop here"],
        )

    def _handle_committed_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "later", "no", "done", "exit")):
            self._state = "DONE"
            return ConversationTurn(
                state="DONE",
                message=(
                    "Session paused after commit. Resume anytime to validate, ingest, and promote.\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )
        if "just validate" in lowered or "validate only" in lowered:
            return self._run_validate()
        return self._run_validate()

    # -- VALIDATE --------------------------------------------------------

    def _run_validate(self) -> ConversationTurn:
        from .validate_links import validate_workflow_links

        self._state = "VALIDATE"
        pb_dir = REPO_ROOT / "framework" / "persona_builders"
        wf_path = (
            REPO_ROOT / "framework" / "workflow_skills"
            / self._data.persona / f"{self._data.skill_name}.yaml"
        )

        try:
            errors = validate_workflow_links(str(wf_path), str(pb_dir))
            result = {"passed": len(errors) == 0, "errors": errors}
        except Exception as e:
            log.warning("validation failed: %s", e)
            result = {"passed": False, "errors": [str(e)]}

        self._data.validation_result = result
        passed = result.get("passed", False)

        if not passed:
            errors = result.get("errors", [])
            error_lines = "\n".join(f"  • {e}" for e in errors[:5])
            return ConversationTurn(
                state="VALIDATE",
                message=(
                    f"Validation FAILED:\n{error_lines}\n\n"
                    "Fix the issues and type 'retry', or 'skip' to continue anyway."
                ),
                data={"validation": result},
                options=["retry", "skip", "stop here"],
            )

        return ConversationTurn(
            state="VALIDATE",
            message=(
                "Validation PASSED — ADR-017 link check OK.\n\n"
                "Proceed to ingestion? This will run the extraction pipeline."
            ),
            data={"validation": result},
            options=["yes, ingest", "skip to eval", "stop here"],
        )

    def _handle_validate_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if "retry" in lowered:
            return self._run_validate()
        if any(kw in lowered for kw in ("stop", "done", "exit")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)
        if "skip" in lowered and "eval" in lowered:
            return self._run_eval()
        if "skip" in lowered:
            return self._run_ingest()
        return self._run_ingest()

    # -- INGEST ----------------------------------------------------------

    def _run_ingest(self) -> ConversationTurn:
        self._state = "INGEST"
        # In stub mode, simulate ingestion
        self._data.ingest_result = {
            "status": "completed",
            "items_processed": 0,
            "items_upserted": 0,
            "mode": "stub",
            "message": "Stub mode — no real ingestion. Connect ADB + sources to run for real.",
        }
        return ConversationTurn(
            state="INGEST",
            message=(
                "Ingestion complete (stub mode — no real sources connected).\n\n"
                "In production this would pull data from your configured sources, "
                "run it through the LLM parser with your extraction schema, "
                "and store ContentItems in the KB.\n\n"
                "Proceed to eval?"
            ),
            data={"ingest": self._data.ingest_result},
            options=["yes, run eval", "stop here"],
        )

    def _handle_ingest_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "done", "exit")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)
        return self._run_eval()

    # -- EVAL ------------------------------------------------------------

    def _run_eval(self) -> ConversationTurn:
        self._state = "EVAL"
        # In stub mode, report gold set counts
        from ..eval.gold_set_feeder import count_entries
        gold_count = count_entries(self._data.persona)
        skill_gold = f"eval/gold_sets/{self._data.persona}-{self._data.skill_name}-extraction.jsonl"
        wf_gold = f"eval/gold_sets/{self._data.persona}-{self._data.skill_name}-workflow.jsonl"

        self._data.eval_result = {
            "status": "stub",
            "persona_gold_count": gold_count,
            "extraction_gold_set": skill_gold,
            "workflow_gold_set": wf_gold,
            "metrics": {
                "recall_at_k": None,
                "faithfulness": None,
            },
            "exit_criteria": {
                "recall_threshold": 0.80,
                "faithfulness_threshold": 0.85,
                "passed": None,
            },
        }

        return ConversationTurn(
            state="EVAL",
            message=(
                f"Eval harness (stub mode):\n"
                f"  Gold set entries for {self._data.persona}: {gold_count}\n"
                f"  Extraction gold set: {skill_gold}\n"
                f"  Workflow gold set: {wf_gold}\n\n"
                "In production this would run queries against the KB and measure "
                "recall@5, faithfulness, latency, and cost.\n\n"
                "Exit criteria: recall ≥0.80, faithfulness ≥0.85\n\n"
                "Proceed to promote (draft → production)?"
            ),
            data={"eval": self._data.eval_result},
            options=["yes, promote", "stop here"],
        )

    def _handle_eval_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        if any(kw in lowered for kw in ("stop", "done", "exit", "no")):
            self._state = "DONE"
            return ConversationTurn(state="DONE", message="Session paused.", done=True)
        return self._run_promote()

    # -- PROMOTE ---------------------------------------------------------

    def _run_promote(self) -> ConversationTurn:
        self._state = "PROMOTE"
        # In stub mode, just report what would happen
        return ConversationTurn(
            state="PROMOTE",
            message=(
                f"Promote {self._data.persona}.{self._data.skill_name} from draft → production?\n\n"
                "This makes the skill live — the consumption flow will start routing to it.\n"
                "Type 'yes' to promote or 'no' to keep as draft."
            ),
            options=["yes, promote", "no, keep as draft"],
        )

    def _handle_promote_response(self, user_input: str) -> ConversationTurn:
        lowered = user_input.lower().strip()
        self._state = "DONE"

        if any(kw in lowered for kw in ("yes", "promote", "ok", "go")):
            return ConversationTurn(
                state="DONE",
                message=(
                    f"Skill {self._data.persona}.{self._data.skill_name} promoted to production.\n\n"
                    "The consumption flow will now route matching queries to this skill.\n"
                    f"Session ID: {self._data.synth_id}"
                ),
                done=True,
            )
        return ConversationTurn(
            state="DONE",
            message=(
                f"Skill {self._data.persona}.{self._data.skill_name} remains as draft.\n"
                "Promote later via: resume this session or kb-cli promote.\n"
                f"Session ID: {self._data.synth_id}"
            ),
            done=True,
        )

    # ------------------------------------------------------------------
    # Synthesis helpers
    # ------------------------------------------------------------------

    def _synthesize_preview(self) -> dict[str, Any]:
        from .synthesize_schema import synthesize_extraction_schema
        from .synthesize_builder import synthesize_persona_builder_diff
        from .synthesize_workflow import synthesize_workflow_skill
        from .gold_seed import seed_gold_set, seed_workflow_gold

        persona = self._data.persona
        skill_name = self._data.skill_name
        gaps = self._data.reuse_result.get("gaps", list(self._data.fields))

        artifacts: dict[str, Any] = {}

        if gaps:
            schema = synthesize_extraction_schema(gaps, persona, skill_name)
            if self._data.field_specs:
                for f in gaps:
                    if f in self._data.field_specs and f in schema.get("properties", {}):
                        schema["properties"][f] = dict(self._data.field_specs[f])
            schema_path = f"framework/parsers/schemas/{persona}/{skill_name}/v1.json"
            artifacts[schema_path] = schema

            pb_entry = synthesize_persona_builder_diff(
                persona=persona,
                kb_name=f"{persona}.{skill_name}",
                schema_path=schema_path,
                sources=self._data.sources,
                fields=gaps,
            )
            artifacts[f"framework/persona_builders/{persona}.yaml.new_kb"] = pb_entry

            gold_entries = seed_gold_set(
                persona=persona,
                kb_name=skill_name,
                artifact_path=self._data.artifact_path,
                extracted_fields={f: None for f in gaps},
            )
            artifacts[f"eval/gold_sets/{persona}-{skill_name}-extraction.jsonl"] = gold_entries

        intent = {
            "task_description": self._data.intent_description,
            "sources": self._data.sources,
            "trigger": self._data.trigger,
            "output_format": self._data.output_format,
            "reuse": self._data.reuse_result,
        }
        wf_struct = synthesize_workflow_skill(
            persona=persona,
            skill_name=skill_name,
            intent=intent,
            fields=self._data.fields,
            template_path=None,
        )
        artifacts[f"framework/workflow_skills/{persona}/{skill_name}.yaml"] = wf_struct

        wf_gold = seed_workflow_gold(
            persona=persona,
            skill_name=skill_name,
            task_description=self._data.intent_description,
            example_fields={f: None for f in self._data.fields},
        )
        artifacts[f"eval/gold_sets/{persona}-{skill_name}-workflow.jsonl"] = wf_gold

        return artifacts

    def _write_artifacts(self) -> list[str]:
        import json as _json

        committed: list[str] = []
        for rel_path, content in self._data.synthesized_artifacts.items():
            full = REPO_ROOT / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(content, dict):
                text = yaml.safe_dump(content, sort_keys=False, allow_unicode=True)
                if "schema" in rel_path or rel_path.endswith(".json"):
                    text = _json.dumps(content, indent=2)
            elif isinstance(content, list):
                text = "\n".join(_json.dumps(item) for item in content) + "\n"
            else:
                text = str(content)

            full.write_text(text)
            log.info("wrote %s", full)
            committed.append(rel_path)

        return committed

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_fields_from_input(self, user_input: str) -> tuple[list[str], dict | None]:
        parts = re.split(r"[,\n]+", user_input)
        fields: list[str] = []
        for p in parts:
            f = _to_field_name(p.strip())
            if f and f not in fields:
                fields.append(f)
        if not fields:
            fields = ["title", "summary", "details"]
        return fields, None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:50] or "unnamed_skill"


def _to_field_name(text: str) -> str:
    t = text.lower().replace(" ", "_").replace("-", "_")
    t = re.sub(r"[^a-z0-9_]", "", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t[:60]


def _parse_field_edits(user_input: str) -> list[tuple]:
    edits: list[tuple] = []
    for line in re.split(r"[,\n;]+", user_input):
        line = line.strip()
        m_add = re.match(r"(?i)add\s+(\S+)", line)
        m_remove = re.match(r"(?i)remove\s+(\S+)", line)
        m_rename = re.match(r"(?i)rename\s+(\S+)\s+to\s+(\S+)", line)
        if m_add:
            edits.append(("add", m_add.group(1)))
        elif m_remove:
            edits.append(("remove", m_remove.group(1)))
        elif m_rename:
            edits.append(("rename", m_rename.group(1), m_rename.group(2)))
    return edits


def _parse_source_descriptor(user_input: str) -> dict:
    lowered = user_input.lower()
    if "confluence" in lowered:
        space_m = re.search(r"confluence\s+([A-Z0-9_\-]+)", user_input, re.IGNORECASE)
        labels_m = re.search(r"labels?:\s*([\w,\s\-]+)", user_input, re.IGNORECASE)
        source: dict = {"kind": "confluence"}
        if space_m:
            source["space"] = space_m.group(1).upper()
        if labels_m:
            source["include_labels"] = [
                l.strip() for l in labels_m.group(1).split(",") if l.strip()
            ]
        return source
    if "jira" in lowered:
        jql_m = re.search(r"jira\s*:?\s*(.+)", user_input, re.IGNORECASE)
        return {"kind": "jira", "jql": jql_m.group(1).strip() if jql_m else "project = ?"}
    if "git" in lowered:
        repo_m = re.search(r"repo\s+(\S+)", user_input, re.IGNORECASE)
        paths_m = re.search(r"paths?:\s*([\w/*.*\s,]+)", user_input, re.IGNORECASE)
        source = {"kind": "git"}
        if repo_m:
            source["repo"] = repo_m.group(1)
        if paths_m:
            source["paths"] = [p.strip() for p in paths_m.group(1).split(",") if p.strip()]
        return source
    return {"kind": "unknown", "raw": user_input}


def _parse_trigger_input(user_input: str) -> tuple[dict, str]:
    output_formats = ["pptx", "docx", "email", "slack", "markdown"]
    output_format = "markdown"
    for fmt in output_formats:
        if fmt in user_input.lower():
            output_format = fmt
            break

    trigger: dict = {}
    if "1" in user_input or "on-request" in user_input.lower() or "request" in user_input.lower():
        trigger["on_request"] = True
    if "2" in user_input or "schedule" in user_input.lower() or "cron" in user_input.lower():
        cron_m = re.search(r"(\d+\s+\d+\s+\S+\s+\S+\s+\S+)", user_input)
        trigger["on_schedule"] = cron_m.group(1) if cron_m else "0 9 * * 1"
    if "3" in user_input and not trigger:
        trigger["on_request"] = True
        trigger["on_schedule"] = "0 9 * * 1"
    if not trigger:
        trigger["on_request"] = True

    return trigger, output_format


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_synth_id(persona: str, timestamp: str) -> str:
    import uuid
    unique = uuid.uuid4().hex[:8]
    return f"synth-{persona or 'new'}-{unique}"


def _list_available_personas() -> list[dict]:
    """List personas from persona_builders/*.yaml on disk."""
    pb_dir = REPO_ROOT / "framework" / "persona_builders"
    personas: list[dict] = []
    if not pb_dir.exists():
        return personas
    for p in sorted(pb_dir.glob("*.yaml")):
        name = p.stem
        try:
            with open(p) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
        display = cfg.get("display_name", name.replace("-", " ").replace("_", " ").title())
        kbs = cfg.get("knowledge_bases", [])
        skills_dir = REPO_ROOT / "framework" / "workflow_skills" / name
        skill_count = len(list(skills_dir.glob("*.yaml"))) if skills_dir.exists() else 0
        personas.append({
            "name": name,
            "display_name": display,
            "kb_count": len(kbs),
            "skill_count": skill_count,
        })
    return personas
