---
id: DECISION-007
title: OPS skill auditor implementation option — Option 2 chosen
status: decided
created: 2026-05-12
decided: 2026-05-12
owner: architect
tags: [ops, quality, kbf_ops, skill-builder]
related: [ADR-023, ADR-024-future]
---

# DECISION-007 — OPS skill auditor implementation option

## Context

Manual review of `synth-tpm-ec2fad6d` identified 5 server-side bugs and 4 enhancement gaps
purely through cross-artifact qualitative analysis. The team wants this review to be automated,
repeatable, and LLM-powered (deep qualitative analysis, not just structural checks). Three
options were evaluated.

## Decision

**Option 2 — `kbf_ops` persona + `reviewSkillSession` MCP tool + LLM synthesis. Decided 2026-05-12.**

Rationale: team is in early stages. Qualitative failures (wrong field count, hallucinated KB
reference, two overlapping skills, routing descriptors that won't hit threshold) require LLM
reasoning to detect reliably — structural invariant checks would miss them. Option 2 provides
the depth needed now.

## Option 1 deferred → ADR-024

Option 1 (Python audit module + background loop + deterministic checks) is explicitly NOT
abandoned. It is the right complementary layer once:
- Known bug classes stabilise enough to encode as structural invariants
- LLM review cost at scale warrants a cheap pre-filter
- CI gate for every new skill commit is needed

File **ADR-024 — deterministic audit layer** when any of the above trigger. The check
functions from Option 1 can also act as a pre-filter inside Option 2's review engine:
structural checks first (free), LLM critique only if structural checks pass.

## Option 3 noted

Option 3 (CLI command + workflow YAML cron) is the right pattern if the team moves away from
the asyncio server model. Not a priority now.

## Implementation

See ADR-023 for full design. Backend Dev to implement:
1. `KBF_AUDIT_RUNS` DDL (migration-006)
2. `KbfOpsSessionLoader` — ADB SQL queries for all synth_id data
3. `KbfOpsReviewEngine` — LLM critique with structured output
4. `review_skill_session.yaml` + output schema v1.json
5. `reviewSkillSession` as 5th external MCP tool
6. Tests for all components
