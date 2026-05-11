"""conversation — interactive skill-builder session.

Per ADR-015 §Conversation contract. Implements a state machine that drives the
authoring flow from INIT through COMMITTED. Works in stub LLM mode with
template-based responses — no external services required.

State machine:
  INIT → ANALYZE_ARTIFACT → REVIEW_FIELDS → CHECK_REUSE →
  CONFIGURE_SOURCES → CONFIGURE_TRIGGERS → PREVIEW → CONFIRM → COMMITTED
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Ordered state list for reference
STATES = [
    "INIT",
    "ANALYZE_ARTIFACT",
    "REVIEW_FIELDS",
    "CHECK_REUSE",
    "CONFIGURE_SOURCES",
    "CONFIGURE_TRIGGERS",
    "PREVIEW",
    "CONFIRM",
    "COMMITTED",
]


@dataclass
class ConversationTurn:
    """Return value from every state handler."""

    state: str
    message: str
    options: list[str] | None = None
    artifacts_preview: dict | None = None
    done: bool = False


@dataclass
class _SessionData:
    """Mutable session state threaded through the conversation."""

    intent_description: str = ""
    artifact_path: str = ""
    fields: list[str] = field(default_factory=list)
    slide_mapping: dict | None = None
    reuse_result: dict = field(default_factory=lambda: {"covered": {}, "gaps": []})
    sources: list[dict] = field(default_factory=list)
    trigger: dict = field(default_factory=lambda: {"on_request": True})
    output_format: str = "markdown"
    persona: str = ""
    skill_name: str = ""
    synthesized_artifacts: dict = field(default_factory=dict)


class SkillBuilderConversation:
    """Interactive skill-builder session.

    Drives the authoring flow through a multi-step conversation.  Each call to
    start() / submit_artifact() / respond() returns a ConversationTurn describing
    what to display to the user and what the current state is.

    In stub LLM mode responses are template-based; real LLM integration would
    replace the _* handlers while keeping the state machine intact.
    """

    def __init__(self, persona: str, llm=None):
        self._persona = persona
        self._llm = llm
        self._state = "INIT"
        self._data = _SessionData(persona=persona)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, intent_description: str = "") -> ConversationTurn:
        """Begin the session. Call once before any respond() calls."""
        self._data.intent_description = intent_description.strip()
        if self._data.intent_description:
            self._data.skill_name = _slugify(self._data.intent_description)
            self._state = "ANALYZE_ARTIFACT"
            return self._handle_analyze_artifact_prompt()
        self._state = "INIT"
        return ConversationTurn(
            state="INIT",
            message=(
                f"Welcome to the Skill Builder for persona: {self._persona}.\n\n"
                "What task do you want automated? Describe it in plain English.\n"
                "Example: 'Produce a weekly project status PPT for exec review every Friday'"
            ),
            options=None,
        )

    def submit_artifact(self, artifact_path: str) -> ConversationTurn:
        """Supply an example outcome artifact. Moves state to REVIEW_FIELDS."""
        self._data.artifact_path = artifact_path
        self._state = "ANALYZE_ARTIFACT"
        return self._handle_analyze_artifact(artifact_path)

    def respond(self, user_input: str) -> ConversationTurn:
        """Submit a user response in the current state."""
        user_input = user_input.strip()

        if self._state == "INIT":
            return self._handle_init_response(user_input)
        if self._state == "ANALYZE_ARTIFACT":
            return self._handle_analyze_artifact(user_input)
        if self._state == "REVIEW_FIELDS":
            return self._handle_review_fields_response(user_input)
        if self._state == "CHECK_REUSE":
            return self._handle_check_reuse_response(user_input)
        if self._state == "CONFIGURE_SOURCES":
            return self._handle_configure_sources_response(user_input)
        if self._state == "CONFIGURE_TRIGGERS":
            return self._handle_configure_triggers_response(user_input)
        if self._state == "PREVIEW":
            return self._handle_preview_response(user_input)
        if self._state == "CONFIRM":
            return self._handle_confirm_response(user_input)
        if self._state == "COMMITTED":
            return ConversationTurn(
                state="COMMITTED",
                message="Session complete. All artifacts have been committed.",
                done=True,
            )
        return ConversationTurn(
            state=self._state,
            message=f"Unknown state {self._state!r}. This is a bug.",
            done=True,
        )

    def get_state(self) -> dict:
        """Return a snapshot of the current session state."""
        return {
            "state": self._state,
            "persona": self._data.persona,
            "skill_name": self._data.skill_name,
            "intent_description": self._data.intent_description,
            "artifact_path": self._data.artifact_path,
            "fields": list(self._data.fields),
            "reuse": dict(self._data.reuse_result),
            "sources": list(self._data.sources),
            "trigger": dict(self._data.trigger),
            "synthesized_artifacts": dict(self._data.synthesized_artifacts),
        }

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _handle_init_response(self, user_input: str) -> ConversationTurn:
        if not user_input:
            return ConversationTurn(
                state="INIT",
                message="Please describe the task in plain English.",
            )
        self._data.intent_description = user_input
        self._data.skill_name = _slugify(user_input)
        self._state = "ANALYZE_ARTIFACT"
        return self._handle_analyze_artifact_prompt()

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
            return self._advance_to_check_reuse()

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
        self._state = "COMMITTED"
        return ConversationTurn(
            state="COMMITTED",
            message=(
                f"Committed {len(committed_paths)} artifact(s) to git:\n"
                + "\n".join(f"  • {p}" for p in committed_paths)
                + "\n\nNext steps:\n"
                + f"  1. git diff — review the synthesized files\n"
                + f"  2. kb-cli validate framework/persona_builders/{self._data.persona}.yaml\n"
                + f"  3. kb-cli workflow-run {self._data.persona}.{self._data.skill_name} --inputs '{{}}'\n"
                + f"  4. Open a PR for team review."
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
