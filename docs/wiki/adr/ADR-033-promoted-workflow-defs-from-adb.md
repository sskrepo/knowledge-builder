---
title: ADR-033 — Promoted Workflow Skill Definitions Resolved from ADB, Not Disk
status: accepted
created: 2026-05-16
accepted: 2026-05-16
owner: architect
deciders: user, backend-dev
tags: [adr, skill-builder, consumption, workflow-skills, shim-workflows, adb, routing]
related: [ADR-015, ADR-016, ADR-032, ADR-033]
supersedes: ~
---

# ADR-033 — Promoted Workflow Skill Definitions Resolved from ADB, Not Disk

## Status

**Accepted — 2026-05-16.**  Fixes the silent routing failure where a skill promoted
in ADB was unreachable at Tier-1 (returned `tier_used=4 no_answer`) whenever its
on-disk YAML byproduct was absent, stale, or lacked `source_binding`.

---

## A. Context — The Failure

### The Observed Failure (JSON-RPC id 34, session synth-tpm-5b3e690f)

After session `synth-tpm-5b3e690f` successfully promoted skill
`tpm.project_tracking_weekly_stakeholder_meeting_email` (status=promoted, 5 artifacts,
`source_binding.mode=ask_parameterized`, space OCIFACP, input `page_id`), a request to
`askKnowledgeBase` with `page_id=18625350641` returned `tier_used=4 no_answer` instead
of routing to Tier-1 and executing the promoted skill.

### Root Cause — Disk-vs-ADB Inconsistency in `shim_workflows.py`

Before ADR-033, `shim_workflows.py` resolved promotion status from ADB
(`list_promoted_workflow_skills()`) but built the **card body** (name, `use_when`,
`example_invocations`, inputs, `output_format`, trigger, and critically
`source_binding`) from the **on-disk YAML file** at
`framework/workflow_skills/{persona}/{skill_name}.yaml`.

This created a source-of-truth split:

| Source | Held | Authoritative? |
|---|---|---|
| ADB (`KBF_SKILL_ARTIFACTS`) | Promotion status, committed YAML artifact | YES |
| Disk YAML | Card body used for routing | NO — transient byproduct |

A skill was only reachable at Tier-1 if:
1. ADB reported it as promoted (correct), AND
2. Its disk YAML existed AND was not stale AND contained the correct `source_binding`

When condition (2) failed — because the disk file was cleaned, missing, or committed
before ADR-032's `source_binding` block was added — the promoted skill had no card
body and was **silently invisible** to the Tier-1 LLM classifier.  The router fell
through to Tier-4 "no_answer" with no indication that a promoted skill existed.

The execution path (`maybe_render_artifact` in `ask.py`) had the same flaw: it loaded
the skill cfg from disk for `execute()`, so even if routing had succeeded, the executor
would have used a stale cfg lacking `source_binding` — causing `ask_parameterized`
ephemeral fetch to fail.

### Why This Matters

This is a **silent wrong/no-output** defect (severity HIGH per DECISION-013): the
consumer gets a tier-4 no_answer with no signal that a promoted skill exists and was
promoted for exactly this use case.  ADB is supposed to be the authoritative source,
but a transient disk byproduct was silently overriding it.

This mirrors exactly the problem ADR-015 Option B solved for knowledge bases:
`shim_kb.py` reads promoted KB entries from ADB, not from `persona_builders/*.yaml`.
`shim_workflows.py` had not been brought to the same standard.

---

## B. Decision

**Resolve both the promotion status AND the card body from ADB, not disk.**

Specifically: when `skill_store` is wired, `shim_workflows.py` calls
`read_artifact(persona, skill_name, "workflow_skill")` for each pair returned by
`list_promoted_workflow_skills()` to obtain the committed YAML text, then parses it
to build the card dict.  Disk YAML is not consulted for promoted skills.

This mirrors ADR-015 Option B exactly:

> "shim_kb reads promoted KB entries from ADB (skill_store), not from
>  persona_builders/*.yaml."

And extends the same principle to workflow skills:

> "shim_workflows reads promoted workflow skill card bodies from ADB
>  (read_artifact), not from workflow_skills/*.yaml."

### Explicit laptop/no-store path (documented, not silent)

When `skill_store is None`, `shim_workflows` serves all on-disk cards and logs at
INFO:

```
ShimWorkflows: no skill_store wired; serving all N on-disk workflow cards
(laptop mode — ADB not required).
```

This is an explicit design decision, not a silent fallback.  The INFO log makes the
mode visible in server startup logs.

### Store-error path (no silent draft promotion)

If `list_promoted_workflow_skills()` raises, `all_cards()` returns `[]` and logs at
WARNING.  Draft skills never reach the Tier-1 classifier under any error condition.

---

## C. Implementation

### Files Changed

| File | Change |
|---|---|
| `framework/orchestrator/shim_workflows.py` | New `_cfg_to_card()` shared helper; `load()` now builds card bodies from ADB artifact for each promoted pair; `all_cards()` simplified (self._cards always correct); `all_cards_including_draft()` returns `self._disk_cards` (disk scan kept for tooling) |
| `framework/deploy/routes/ask.py` | `maybe_render_artifact()` resolves skill cfg from `skill_store.read_artifact()` first; disk fallback only when skill_store is None (laptop) or ADB read fails; calls `executor.execute_from_config(cfg)` when cfg came from ADB |
| `framework/workflow_runtime/executor.py` | New `execute_from_config(cfg, inputs)` public method; internal `_execute_cfg()` shared by both `execute()` and `execute_from_config()`; `_any_promoted_skill_requires_ephemeral()` gains `skill_store` param and checks ADB artifacts for promoted skills rather than all disk files |
| `framework/deploy/mcp_server.py` | Passes `skill_store` to `_any_promoted_skill_requires_ephemeral()`; internal tool registry registers promoted skills from ADB artifact cfg rather than disk path callables |

### Shared `_cfg_to_card()` helper

A new module-level function `_cfg_to_card(cfg, source, path)` is shared by both paths:

- **ADB path**: called with `source="adb"`, `path=""` for each promoted pair's artifact
- **Disk path**: called with `source="disk"`, `path=str(path)` for laptop/introspection

The card dict includes `source_binding` (load-bearing for `ask_parameterized` skills),
`_cfg` (full cfg for executor), and `_source` ("adb" or "disk").

### Routing + Execution Both ADB-Sourced (the seam this closes)

Before ADR-033, the routing and execution paths had independent disk reads:

1. Routing: `shim_workflows.load()` → disk YAML → card (missing `source_binding`)
2. Execution: `maybe_render_artifact()` → disk YAML → `executor.execute(path)` (stale cfg)

After ADR-033, both paths use the ADB artifact:

1. Routing: `shim_workflows.load()` → `read_artifact()` → ADB YAML → card (correct `source_binding`)
2. Execution: `maybe_render_artifact()` → `read_artifact()` → ADB YAML → `executor.execute_from_config(cfg)`

---

## D. Alternatives Considered

### Option A — Keep disk as card body source; enforce disk stays in sync with ADB

Rejected because:
- Disk files are generated byproducts of `authorSkill` sessions, not managed artifacts.
- Any session recovery, machine wipe, or repo clean removes them.
- No mechanism to enforce sync — the invariant is unenforceable in practice.
- Produces exactly the observed failure class (silent tier-4) whenever sync breaks.

### Option B — Keep disk as primary; fall through to ADB when disk is absent

Rejected because this is a **silent fallback** — precisely what ADR-031 prohibits.
A partial disk file (exists but lacks `source_binding`) would be served without
warning, producing a broken skill card that cannot route ask_parameterized requests.

---

## E. Consequences

### Positive

- A skill promoted in ADB is ALWAYS reachable at Tier-1, regardless of disk state.
- `source_binding` is always correct in the card (read from the same committed artifact
  that passed `_validate_source_binding_contract`).
- The routing and execution paths both use the same ADB-authoritative definition —
  no split source-of-truth.
- Disk cleaning/repo maintenance cannot silently break promoted skills.
- Mirrors ADR-015 Option B — single mental model for both KB and workflow-skill shims.

### Negative

- `ShimWorkflows.load()` makes one `read_artifact()` call per promoted skill on startup
  and on `reload()`.  For N promoted skills this is N ADB round-trips (vs. N disk
  reads).  At the current scale (3–5 promoted skills), this is negligible.  If N grows
  to hundreds, consider a bulk artifact fetch API on `SkillStore`.
- `all_cards_including_draft()` still reflects disk (not ADB) — intentional for tooling
  use.  If the disk is clean, this returns an empty list.  This is the correct
  behavior: draft visibility is a tooling concern, not a routing concern.

### Reversibility

Setting `skill_store=None` in `ShimWorkflows` reverts to the old all-disk path for
that instance.  No database migration required.

---

## F. Test Coverage

`framework/tests/unit/test_shim_workflows_adb.py` (22 tests):

- **T1** — Card body comes from ADB artifact (summary includes "ADB version"), not disk
- **T2** — Disk-absent-but-ADB-promoted skill appears in `all_cards()` (the exact failure mode)
- **T3** — `source_binding` carried through from ADB artifact (`mode`, `input_param`, etc.)
- **T4** — `read_artifact` returns None → skill skipped with WARNING
- **T5–T7** — Promotion gating: only promoted skills in `all_cards()`
- **T8–T9** — `all_cards_including_draft()` always returns disk cards; laptop mode
- **T10** — Store error → `all_cards()` returns `[]`, WARNING logged
- **T11–T13** — `cards_for()`, `render_for_persona_prompt()`, `reload()` correctness
- **FilestoreSkillStore** (7 tests) — `list_promoted_workflow_skills` filtering

---

## G. Related ADRs

- **ADR-015** — Skill-by-demonstration; ShimKb Option B (the model this mirrors)
- **ADR-016** — Workflow skills YAML schema; `source_binding` block defined here
- **ADR-032** — Ask-time source ingestion (`ask_parameterized`, ephemeral fetch);
  ADR-033 ensures the routing card carries the correct `source_binding` so ADR-032's
  execution path can resolve the page reference without touching disk.
  Cross-ref: ADR-032 §E.2 (`_retrieve_ask_parameterized` reads `source_binding.input_param`
  from the card); ADR-033 guarantees that field is present on promoted cards.

---

## References

- `framework/orchestrator/shim_workflows.py`
- `framework/deploy/routes/ask.py`
- `framework/workflow_runtime/executor.py`
- `framework/deploy/mcp_server.py`
- `framework/tests/unit/test_shim_workflows_adb.py`
- BUG-queue-\<uuid\> (filed in Step 5 — ADB routing defect record)
- Session synth-tpm-5b3e690f — the triggering user request (JSON-RPC id 34)
