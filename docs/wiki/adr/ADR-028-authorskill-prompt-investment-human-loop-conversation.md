---
title: "ADR-028 — authorSkill: Prompt Investment, Human-in-the-Loop Enforcement, and Conversational Clarification"
status: proposed
created: 2026-05-15
owner: architect
deciders: user, tpm
supersedes: ~
tags: [arch, skill-builder, prompts, ux, conversation, adr-027]
---

## Context

### Background — the ADR-026 vs ADR-027 PPT density regression

ADR-027 (2026-05-14) replaced the 15-state ADR-026 machine with a 16-state
design-first flow. In end-to-end validation the ADR-027-produced PPTX was
noticeably thinner than an earlier ADR-026-produced one for the same persona.
Root cause: the DESIGN_SKILL prompt only included fields whose source could be
confirmed via the capability inventory. The WBS table (which held status, risks,
and next-steps) was reachable via synthesis but the prompt had no instruction to
attempt synthesis when a field was not a verbatim label match. The capability
inventory correctly flagged those fields as "missing" and they were never
designed into the schema.

That investigation surfaced three structural concerns about the authoring flow
that go beyond any single schema gap. The user raised them explicitly in the
session that followed.

### The three observations (verbatim)

**Observation 1 — Prompt investment + persona-awareness:**
"We haven't invested enough time on what we are sending/asking the LLM to do in
each step. We need to dump a complete example [of every prompt]. And also clearly
determine: are we dynamically generating the prompt based on persona, or — no
matter what persona — do we send the same prompt to the LLM with [only] the
user-provided use case substituted?"

**Observation 2 — Client-side human review is being skipped:**
"On the client side, given the client is a smart client like Claude Code or
codex: we are not instructing that client to show what we are sending for user
review actually TO THE USER. The client doing some enrichment is OK, but it
should still review with the actual user before reverting back. We're missing
crucial back-and-forth with the real human. It's like the user provides a
requirement, and the system is an 'agent team' that understands and automates it
— if we only deal with the intermediary (Claude Code / codex), we miss the
opportunity to clarify and go back and forth with the actual user."

**Observation 3 — JSON-for-review vs a real conversation:**
"We are sending a JSON for review. We should be having a CONVERSATION with the
user for feedback / understanding the requirement. Which means we must also
instruct the LLM: don't assume things; if the requirement is not clear, ask
meaningful questions to the user, go back and forth, THEN move forward with skill
creation / extraction."

---

## Findings

### Item 1 — Prompt investment and persona-awareness

**Verdict: confirmed — every prompt is a static template; persona is a bare
string label that does not shape instructions.**

The full prompt dump is at
`docs/wiki/authorskill-prompts.md` (living reference, filed alongside this ADR).

Evidence:

1. `_CAPTURE_INTENT_PROMPT` (`conversation.py:150`) — the only format kwargs are
   `persona` (label) and `intent` (user data). The output_kind / audience /
   cadence inference rules are identical for `tpm` and `ops_eng`.

2. `_DESIGN_SKILL_PROMPT` (`conversation.py:245`) — the highest-value prompt in
   the flow. Format kwargs: `persona` (label), `normalised_intent` (data),
   `source_capability` (data), `artifact_layout` (data), `existing_kb_cards`
   (persona-filtered list, but the rules governing what to do with them are
   generic). There is no instruction such as "for a TPM skill, always include
   ORM status, RAG summary, and next-steps synthesised from the WBS table".

3. `_REVIEW_DESIGN_REPLAN_PROMPT` (`conversation.py:298`) — persona is not even
   passed as a kwarg. A TPM replan and an ops_eng replan are given identical
   instructions.

4. `_CONFIGURE_SOURCES_SUGGEST_PROMPT` (`conversation.py:176`) — the
   `adapter_list` is built from the persona's YAML at `_get_persona_adapters()`
   (conversation.py:1005), so the source-kind set varies. But the instructions
   for how to propose sources from that list are persona-agnostic.

5. `_REVIEW_EXTRACT_PROMPT` (`review.py:110`) and `_INSPECT_SOURCES_PROMPT`
   (`conversation.py:208`) — no persona context at all; purely data-driven.

**Why this is a real problem:** A TPM authoring a weekly exec review cares about
ORM status, RAG summary, stakeholder asks, and executive-safe language. An
ops_eng authoring an incident summary cares about MTTR, severity, affected
services, and root cause. The current prompts produce a generic extraction schema
in both cases and rely on the source capability inventory alone to differentiate.
That is insufficient when (a) the source contains synthesisable content that is
not a verbatim label match, and (b) the quality bar for what counts as a
"complete" field description differs per persona.

### Item 2 — Client-side human review

**Verdict: confirmed — nothing in the MCP tool description, the ConversationTurn
contract, or any documentation explicitly instructs the smart client to surface
each turn to the real human before advancing.**

Evidence:

1. `authorSkill` tool description (`mcp_tools.py:130-141`):
   > "Single entry point for the knowledge builder flow. Pass-through pattern:
   > call with no synthId to start a new session; pass the returned synthId on
   > subsequent calls to advance the state machine. Repeat until done=true."
   >
   > "IMPORTANT for client LLMs: pass the user's input VERBATIM. Do not
   > summarize URLs, paraphrase Confluence/Jira links, or paste 'pageId=N' in
   > place of a link."
   
   The only client instruction is about URL verbatim-passing (a data-integrity
   fix for BUG-queue-d3ec0). There is no instruction to (i) surface the turn's
   `message` to the actual human, (ii) block on receiving a real human answer,
   or (iii) not auto-answer turns.

2. `ConversationTurn` dataclass (`conversation.py:400-411`):
   ```python
   @dataclass
   class ConversationTurn:
       synth_id: str = ""
       state: str = ""
       message: str = ""
       data: dict | None = None
       options: list[str] | None = None
       artifacts_preview: dict | None = None
       progress: dict | None = None
       done: bool = False
   ```
   There is no `awaiting_user` field, no `must_show_human` flag, no
   `turn_type` taxonomy, and no confirmation token. A smart client has no
   machine-readable signal distinguishing a turn that *must* block on a human
   from a turn that can be auto-acknowledged.

3. CAPTURE_INTENT turn options (`conversation.py:883`): `["ok"] + (["clarify"] if ambiguities else [])`.
   The client sees `options: ["ok", "clarify"]` when ambiguities exist. Nothing
   prevents an agentic client from immediately sending `"ok"` without showing
   the ambiguities list to the human.

4. REVIEW_DESIGN turn options (`conversation.py:1499`): `["ok", "describe <field> as <text>",
   "remove field <name>"]`. The entire schema is in `data.design`. A smart client
   could send `"ok"` without ever displaying the schema to the human.

5. The failure mode is real and was observed: during the ADR-027 walk-through
   the user typed `"ok"` at CAPTURE_INTENT while the turn showed three
   ambiguities. The state machine advanced silently. The ambiguities were
   effectively dropped — they were not re-surfaced at DESIGN_SKILL.

### Item 3 — JSON-for-review vs conversation and "don't assume, ask"

**Verdict: confirmed — the user sees a formatted JSON dump at REVIEW_DESIGN,
not a conversational exchange; and no prompt instructs the LLM to ask blocking
clarifying questions when requirements are ambiguous.**

Evidence:

1. `_prompt_review_design()` (`conversation.py:1426-1500`) formats the design
   as a wall of text with section headers (`=== Skill Design ===`,
   `Schema (N fields):`, `Workflow shape:`, `Reuse (N fields from existing KBs):`,
   `Cannot extract (N fields):`, `Open questions (N):`). It is readable but is
   a structured dump, not a dialogue. The edit interface is a mini-DSL:
   `describe <field> as <text>`, `set type of <field> to <type>`,
   `rename field <old> to <new>`, `remove field <name>`, `set trigger to <cron>`.
   This is closer to a config editor than a conversation.

2. `_CAPTURE_INTENT_PROMPT` emits an `ambiguities` list (confirmed). However:
   - The prompt instruction says only `"ambiguities": ["anything unclear"]` —
     it does not distinguish blocking ambiguities from nice-to-know ones.
   - `_handle_capture_intent()` (`conversation.py:886-901`) treats `"ok"` as
     immediate advance to CONFIGURE_SOURCES regardless of how many ambiguities
     are in the normalised_intent. There is no check `if ambiguities: require_explicit_ack()`.
   - The `ambiguities` field is stored in `normalised_intent` but none of the
     downstream prompts (`_CONFIGURE_SOURCES_SUGGEST_PROMPT`,
     `_INSPECT_SOURCES_PROMPT`, `_DESIGN_SKILL_PROMPT`) explicitly receive or
     act on unresolved ambiguities. They see the normalised_intent blob but
     treat it as opaque data.

3. `_DESIGN_SKILL_PROMPT` does produce an `open_questions` field — these are
   displayed at REVIEW_DESIGN. But the prompt's rules do not say "if you cannot
   answer this question from the source inventory, do NOT design a schema; ask
   the user first." The current instruction is to produce a best-guess schema
   and flag the questions as a post-design annotation.

4. At REVIEW_DESIGN, `"ok"` immediately transitions to CONFIGURE_TRIGGERS
   (`_handle_review_design_response`, conversation.py:1506). Open questions in
   the design are discarded — they are not carried forward or re-surfaced.

### Item 4 (architect-surfaced) — Synthesis vs extraction gap in DESIGN_SKILL

This was the direct trigger for this ADR but deserves explicit capture. The
`_DESIGN_SKILL_PROMPT` rules say:
> "Include ONLY fields that at least one source can support (confidence high or
> medium)."

This correctly enforces source-grounding. However, the rule uses the capability
inventory literally: if INSPECT_SOURCES did not identify a field as
`available_fields[confidence=high|medium]`, it is excluded. But some valuable
fields are *synthesisable* from the source — they require an LLM to combine or
aggregate content that is present but not in a single labelled field. For
example: "risks" extracted from a WBS table's status cells, or "next_steps"
synthesised from a set of open action items. The current rules treat
"synthesisable" the same as "unavailable" — both are excluded. This is why the
ADR-027 PPT was thinner than the ADR-026 PPT, which happened to include those
fields because ANALYZE_ARTIFACT copied them as headings before the source was
consulted.

---

## Decision

Status: **Proposed — pending user direction.**

This ADR does not pre-commit an implementation. It captures the three (plus one)
structural problems and presents options for each. The user will choose the
options to implement before the Dev Manager picks up tasks.

---

## Options

### Item 1 — Persona-aware prompts

**Option A — Per-persona prompt fragments (recommended)**

Maintain a YAML playbook at `framework/config/persona_prompts.yaml` with
per-persona stanzas. Each stanza provides:
- `key_fields`: the 3-5 fields every skill for this persona should try to include
- `extraction_style`: tone/format guidance (e.g. "exec-safe language, RAG colours")
- `common_sources`: source hints beyond what the adapter list provides
- `few_shot_example`: one worked example of a good field description for this persona

All prompts that accept a `persona` kwarg inject the relevant stanza into the
system instructions section of the prompt (not just the data section).

Pros: high leverage per unit of effort; the playbook is human-editable by persona
teams; no code-structure changes beyond string injection. Cons: playbook must be
maintained as personas evolve; adds prompt length (+200-400 tokens per call).

Estimated effort: 2-3 days (framework dev + persona teams contributing playbook
entries).

**Option B — Persona-specific few-shot exemplars in DESIGN_SKILL**

Instead of a global playbook, attach a `few_shot_examples` block to each persona
builder YAML. DESIGN_SKILL reads the first 1-2 examples for the target persona
and injects them into the prompt as "here is an example of a well-designed
{persona} skill schema."

Pros: the example is authoritative (persona team owns it in their own YAML).
Cons: persona teams must produce real worked examples before this works; harder
to get started than a central playbook.

Estimated effort: 3-4 days (schema extension + per-persona example authoring).

**Option C — Persona "playbook table" the prompt consumes dynamically**

DESIGN_SKILL calls a small retrieval step: `search_wiki(query="extraction
guidance for {persona}")` to pull any relevant guidance the persona team has
written into their wiki KB. The result is injected as context.

Pros: leverages the framework's own KB for self-improvement; no hardcoded tables.
Cons: requires the wiki KB to be populated first (a bootstrapping problem); adds
latency; depends on retrieval quality.

Estimated effort: 1 day to wire + persona teams must author wiki pages (ongoing).

---

### Item 2 — Client-side human review

**Option A — `awaiting_user` field + `must_show_human` on ConversationTurn (recommended)**

Add two fields to `ConversationTurn`:
- `awaiting_user: bool` — True on every turn that requires a human response
  (all turns except auto-transitions like DESIGN_SKILL → REVIEW_DESIGN)
- `must_show_human: bool` — True for turns the client must never auto-answer:
  CAPTURE_INTENT (when ambiguities > 0), REVIEW_DESIGN, PREVIEW_EXTRACTION

Update the `authorSkill` tool description to include:
> "CRITICAL: when `mustShowHuman=true` in the response, you MUST display the
> full `message` to the actual human user and wait for their typed response
> before calling authorSkill again. Do NOT auto-answer or paraphrase. The human
> must see and respond to this turn."

Pros: machine-readable signal the client can enforce; no client-side heuristics
needed; low implementation cost. Cons: a malicious or poorly configured client
can ignore the flag (no server-side enforcement possible via MCP).

Estimated effort: 0.5 days.

**Option B — Turn-type taxonomy with non-skippable category**

Add a `turn_type` field with values: `informational | decision | review |
confirmation`. The tool description states that `review` and `decision` turns
must not be auto-answered. Clients are told to treat `confirmation` turns as
skippable (user only needs to type "ok").

Pros: richer semantics than a boolean; enables future UI differentiation.
Cons: more schema surface; requires consistent classification in every state handler.

Estimated effort: 1 day.

**Option C — Confirmation token the human must echo**

For `must_show_human` turns, the server includes a short random `confirm_token`
(e.g. `"token": "XK7"`) in the turn response. The next call to `authorSkill`
must include `"confirm_token": "XK7"` in the input or the server rejects it with
`HTTP 409 Conflict / token_required`. This forces the client to surface the turn
to a human who types the token.

Pros: cryptographically enforces human-in-loop at the server level; no trust in
client instructions. Cons: disruptive UX for CLI flows; adds roundtrip complexity;
users find random tokens annoying. This is a last-resort option if Options A/B
prove insufficient.

Estimated effort: 1.5 days.

---

### Item 3 — Conversational clarification loop

**Option A — CLARIFY state with blocking questions (recommended)**

Add a `CLARIFY` state (inserted after CAPTURE_INTENT and after DESIGN_SKILL)
that will not advance while `blocking_questions` are open:

1. Extend `_CAPTURE_INTENT_PROMPT` to distinguish `blocking_ambiguities` (must
   resolve before proceeding) from `nice_to_know` (proceed with assumption).
2. If `blocking_ambiguities` is non-empty, transition to `CLARIFY` instead of
   `CONFIGURE_SOURCES`. The CLARIFY handler asks one question at a time and
   marks it resolved when the user answers.
3. Similarly, extend `_DESIGN_SKILL_PROMPT` to return `blocking_questions`
   (questions where the answer changes the schema structure) vs `open_questions`
   (cosmetic). If `blocking_questions` is non-empty, transition to `CLARIFY`
   after DESIGN_SKILL before REVIEW_DESIGN.
4. `CLARIFY` sets `must_show_human=true` (Item 2) and emits a conversational
   message ("I need to clarify one thing before proceeding: ...") rather than
   a JSON blob.

Pros: directly addresses the "steamroll on ok" problem; keeps the flow
conversational; the human is never bypassed on consequential ambiguities. Cons:
increases state machine from 16 to 17-18 states; requires prompt engineering to
reliably distinguish blocking from nice-to-know; can feel slow if over-triggered.

Estimated effort: 2-3 days.

**Option B — Prompt-level instruction: "refuse to assume; ask"**

Without adding a new state, update CAPTURE_INTENT and DESIGN_SKILL prompts with
an explicit instruction:
> "If the intent contains genuinely ambiguous requirements that would change the
> schema structure, do NOT produce a best-guess schema. Instead, set
> `open_questions` to the list of questions and set `schema` to null. The caller
> will ask the user to answer these questions before retrying."

Update `_handle_capture_intent` and `_run_design_skill` to detect the
`schema=null` / `open_questions non-empty` condition and enter a question-asking
loop in the same state rather than advancing.

Pros: no new state; simpler implementation. Cons: relies on the LLM reliably
returning `schema=null` — LLMs tend to produce a best-guess even when instructed
not to. Needs a fallback heuristic for when the LLM ignores the instruction.

Estimated effort: 1-1.5 days (lower risk than Option A if the LLM cooperates;
higher risk if it doesn't).

**Option C — Conversation-style message contract instead of JSON-for-review**

Replace the REVIEW_DESIGN structured dump with a prose summary and a
turn-by-turn question loop:
1. DESIGN_SKILL produces schema (as now).
2. REVIEW_DESIGN sends: "I've designed an 8-field schema for your weekly exec
   review. The key fields are: [list]. There is one question I need answered
   before we proceed: [first blocking question]. What should the 'risks' field
   contain — the highest-priority project risks from the WBS table, or the
   full RAID register?"
3. User answers. REVIEW_DESIGN asks next question (if any). When all questions
   are answered, show the full schema for final approval.

Pros: maximally conversational; easy for non-technical users. Cons: significantly
more complex state management; increases number of turns; harder to implement
correctly.

Estimated effort: 4-5 days.

---

### Item 4 — Synthesis vs extraction gap (architect-surfaced)

**Option A — Add a "synthesisable" confidence level in INSPECT_SOURCES**

Extend the capability inventory's `available_fields` confidence taxonomy with a
fourth level: `synthesisable` (field value must be derived by combining or
aggregating content, not read verbatim). Update `_DESIGN_SKILL_PROMPT` to allow
fields with `confidence=synthesisable` and add a rule:
> "For synthesisable fields, the extraction instruction must explicitly say 'Derive
> this value by [aggregating/combining/summarising] the following content: ...'"

Pros: fixes the root cause of the PPT thinness regression; minimal schema
changes. Cons: requires `_INSPECT_SOURCES_PROMPT` update and `_DESIGN_SKILL_PROMPT`
update; the LLM must reliably tag synthesisable vs unavailable.

Estimated effort: 1 day.

**Option B — Add a "synthesis hint" pass in DESIGN_SKILL**

After the capability inventory is assembled, run a second LLM call: "Given this
capability inventory and these intent fields, which fields could be synthesised
from existing content (not available as labelled fields)?" Inject the synthesis
hints into `_DESIGN_SKILL_PROMPT` as a separate section.

Pros: cleaner separation of concerns. Cons: extra LLM call per session; more
latency.

Estimated effort: 1.5 days.

---

## Consequences

### What depends on what

- Item 2 (ConversationTurn schema) must land first — it is a low-effort
  prerequisite that unblocks reliable human-loop testing.
- Item 3 Option A (CLARIFY state) depends on Item 2 (the CLARIFY turns set
  `must_show_human=true`).
- Item 1 Option A (persona prompt fragments) is independent and can be done in
  parallel with Items 2 and 3.
- Item 4 Option A (synthesisable confidence level) is independent and
  fixes the direct regression from this investigation.

### Recommended sequencing (architect view)

1. Item 4 Option A — fix the synthesis gap (1 day, direct regression fix)
2. Item 2 Option A — add `awaiting_user` + `must_show_human` to ConversationTurn
   (0.5 days, unlocks human-loop testing)
3. Item 3 Option A — CLARIFY state (2-3 days, highest user-facing leverage)
4. Item 1 Option A — persona prompt playbook (2-3 days, schema quality uplift)

Total: ~6-8 days of framework dev work across two sprints.

### Risk if deferred

- Without Item 2, any smart-client integration (Claude Code, Codex, Cursor)
  risks silently bypassing REVIEW_DESIGN, making every promoted skill
  effectively unreviewed by the human who requested it.
- Without Item 3, the "ok steamroll" problem means ambiguities are silently
  dropped at CAPTURE_INTENT and open_questions are silently dropped at
  REVIEW_DESIGN — the schema quality gate has no teeth.

---

## Cross-references

- ADR-015 — Skill-by-demonstration (original conversation contract)
- ADR-026 — Source-grounded schema review + layout-aware PPTX
- ADR-027 — Design-first authorSkill (16-state machine)
- DECISION-010 — EVAL gold sets auto-generation (Option A chosen)
- `docs/wiki/authorskill-prompts.md` — full prompt dump (Item 1 evidence base)
