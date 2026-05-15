---
title: "DECISION-011 — authorSkill: prompt investment, human-loop enforcement, and clarification strategy"
status: open
created: 2026-05-15
owner: architect
deciders: user
tags: [skill-builder, prompts, ux, conversation]
related_adr: ADR-028
---

# DECISION-011 — authorSkill: Prompt & Human Loop Direction

**Awaiting user decision.**

ADR-028 documents the audit findings and options. This decision file asks the
user to choose one option per item so the Dev Manager can pick up implementation.

---

## What to decide

### Item 1 — Persona-aware prompts

Every LLM call in the authorSkill flow uses a static template. Persona is a
label string only. It does not shape instructions.

Choose one:

- **Option A (recommended)**: per-persona prompt fragments in a central YAML
  playbook (`framework/config/persona_prompts.yaml`). Key fields, extraction
  style, and few-shot guidance per persona, injected into DESIGN_SKILL and
  CAPTURE_INTENT. ~2-3 days.

- **Option B**: few-shot exemplars attached to each persona builder YAML. Persona
  teams own the examples. ~3-4 days (requires persona team input).

- **Option C**: dynamic wiki retrieval — DESIGN_SKILL queries the framework's own
  wiki KB for persona-specific extraction guidance. ~1 day to wire + ongoing
  wiki authoring by persona teams. Has bootstrapping problem.

### Item 2 — Client-side human review

The MCP `authorSkill` tool has no instruction to show turns to the actual human,
and `ConversationTurn` has no machine-readable field that tells the client "this
turn must block on a human response."

Choose one:

- **Option A (recommended)**: add `awaiting_user: bool` and `must_show_human: bool`
  to `ConversationTurn`. Update tool description with explicit instruction not to
  auto-answer `must_show_human=true` turns. ~0.5 days.

- **Option B**: add a `turn_type` enum (informational / decision / review /
  confirmation) to ConversationTurn. Review and decision turns are non-skippable.
  ~1 day.

- **Option C**: confirmation token — the server includes a random token on
  must-show-human turns; the client must echo it in the next call. Hard
  enforcement at the server level. ~1.5 days. Disruptive to CLI UX.

### Item 3 — Clarification loop

The current flow lets "ok" steamroll past ambiguities at CAPTURE_INTENT and past
open_questions at REVIEW_DESIGN. No prompt instructs the LLM to refuse to
proceed when the requirement is structurally ambiguous.

Choose one:

- **Option A (recommended)**: add a `CLARIFY` state (17th state) after CAPTURE_INTENT
  (and optionally after DESIGN_SKILL). The state will not advance while blocking
  questions are unresolved. Prompts updated to distinguish blocking vs nice-to-know
  ambiguities. ~2-3 days.

- **Option B**: prompt-level instruction — update CAPTURE_INTENT and DESIGN_SKILL
  to return `schema=null` when requirements are ambiguous. The existing states
  loop in question-asking mode rather than advancing. ~1-1.5 days. Lower
  implementation cost but relies on LLM reliably refusing to guess.

- **Option C**: prose conversation flow — replace the REVIEW_DESIGN JSON dump with
  a question-by-question dialogue. Higher UX quality; 4-5 days. Significant
  rework.

### Item 4 (architect-surfaced) — Synthesisable fields

DESIGN_SKILL only includes fields whose source can provide them verbatim.
"Synthesisable" fields (e.g. risks derived from WBS status cells) are excluded
as if unavailable. This was the root cause of the PPT thinness regression.

Choose one:

- **Option A (recommended)**: add `confidence=synthesisable` to the capability
  inventory. DESIGN_SKILL allowed to include synthesisable fields with explicit
  aggregation instructions. ~1 day.

- **Option B**: separate "synthesis hints" LLM call before DESIGN_SKILL. ~1.5 days.

---

## How to decide

Reply: "DECISION-011: Item1=A, Item2=A, Item3=A, Item4=A" (or choose different
options per item). Once decided, the architect updates ADR-028 status to
`accepted` and the Dev Manager picks up implementation tasks.
