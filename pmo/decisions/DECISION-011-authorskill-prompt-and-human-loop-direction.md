---
title: "DECISION-011 — authorSkill: prompt investment, human-loop enforcement, and clarification strategy"
status: resolved
created: 2026-05-15
decided: 2026-05-15
owner: architect
deciders: user
outcome: "Item1=A, Item2=A, Item3=A, Item4=A — all items Option A. See ADR-028 (Accepted) and ADR-029 (Accepted)."
tags: [skill-builder, prompts, ux, conversation]
related_adr: ADR-028
---

# DECISION-011 — authorSkill: Prompt & Human Loop Direction

**Resolved — 2026-05-15. All items Option A.**

See ADR-028 (Accepted) for full decision rationale, implementation contract, and consequences.
See ADR-029 (Accepted) for the outcome-based EVAL acceptance loop that depends on Items 2 and 4.

## Locked choices

- **Item 1 = Option A** — per-persona prompt fragments in `framework/config/persona_prompts.yaml`.
  Concrete starter templates generated for all 9 personas (tpm, pm, architect, eng_mgr, developer,
  ops_eng, ops_mgr, service_owner, kbf_ops) framed in the fusion-apps cloud-platform domain.
  File committed for user review and editing.
- **Item 2 = Option A** — `awaiting_user` + `must_show_human` added to `ConversationTurn`.
  `authorSkill` tool description updated with hard "do not auto-answer" instruction.
- **Item 3 = Option A** — new `CLARIFY` state (17th). Does not advance while blocking questions
  are open. Prompts distinguish `blocking_ambiguities` from `nice_to_know`.
- **Item 4 = Option A** — `confidence=synthesisable` added to INSPECT_SOURCES capability
  inventory. DESIGN_SKILL may include synthesisable fields with explicit aggregation instructions.

## Implementation sequencing

Per ADR-028 recommended sequencing (and ADR-029 dependency graph):

1. Item 4 + Item 2 (parallel, ~1.5 days total) — independent, unblock everything else
2. Item 3 CLARIFY state (~2-3 days) — depends on Item 2; Item 1 persona prompts can run in
   parallel as a side stream
3. ADR-029 Phase 1 (artifact retention + text comparator + image-only hard-reject + gap report)
   — depends on Items 2 + 4
4. ADR-029 Phase 2 (constrained routing + guardrails) — depends on Phase 1

---

(Decision closed. See locked choices above.)
