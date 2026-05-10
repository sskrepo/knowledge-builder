---
title: ADR-018 — Skill suggestion loop
status: accepted
created: 2026-05-09
owner: architect
tags: [adr, skill-builder, workflow-skills, phase-4]
related: [ADR-006, ADR-015, ADR-016]
---

# ADR-018 — Skill suggestion loop

## Status
Accepted (2026-05-09). Phase 4 deliverable.

## Context
The framework can answer questions through Tier 2 (KB retrieval + cited synthesis) without requiring any pre-authored workflow skill — that's the strength of the four-tier routing in ADR-006 amend 3. But a Tier-2 path means:

- More tokens than a curated workflow skill (synthesis runs every time vs. cached template)
- More variable output shape (ad-hoc synthesis vs. typed renderer)
- No scheduled / automated path (workflow skills enable cron/event)

The natural pattern: **as queries become recurring, they should be crystallized into workflow skills.** Manual identification is error-prone and slow. The framework can detect this signal automatically.

## Decision

### Two signals trigger a skill candidate

1. **Tier-4 misses** (no Tier 1, 2, or 3 produced an answer above threshold) — the user got "I don't know" or a low-confidence partial response. High-information signal.
2. **Repeated Tier-2 queries with similar shape** — same query pattern keeps hitting Tier 2 from the same persona. Hint: this should be a workflow skill (faster, consistent, schedulable).

Both signals land in `kb_shim.skill_candidates`:

```sql
CREATE TABLE skill_candidates (
  id              VARCHAR2(64)     NOT NULL,
  persona         VARCHAR2(32)     NOT NULL,
  query_pattern   VARCHAR2(500)    NOT NULL,    -- LLM-clustered query pattern
  signal_kind     VARCHAR2(20)     NOT NULL,    -- "tier_4_miss" | "tier_2_repeat"
  occurrence_count NUMBER          DEFAULT 1,
  first_seen      TIMESTAMP        DEFAULT SYSTIMESTAMP,
  last_seen       TIMESTAMP        DEFAULT SYSTIMESTAMP,
  example_queries JSON,                          -- 3-5 actual queries that hit this pattern
  proposed_skill  JSON,                          -- pre-filled skill_builder intent
  status          VARCHAR2(20)     DEFAULT 'pending',  -- pending|accepted|rejected|deferred
  CONSTRAINT pk_skill_candidates PRIMARY KEY (id)
);
```

### Three feedback paths

#### 1. Inline at miss time (immediate)

When Tier 4 fires, the response includes a soft-call-to-action:

```json
{
  "answer": "I don't have grounded knowledge for this question.",
  "skill_suggestion": {
    "id": "sc-abc123",
    "message": "It looks like this query is in your TPM persona's domain.
                I can scaffold a workflow skill for queries like this if
                you give me an example output.",
    "next_step": "kb-cli skill-builder --resume sc-abc123",
    "confidence": 0.62
  }
}
```

The persona team can act immediately or ignore.

#### 2. Weekly digest per persona team

A weekly cron pulls top-5 skill candidates per persona by occurrence count:

```
SUBJECT: TPM persona — 5 skill candidates from last week

Top queries you couldn't answer well last week:

1. "summarize ECARs that expire in next 30 days"  (12 queries)
   → Looks like a candidate for a TPM workflow skill.
   → kb-cli skill-builder --resume sc-abc123

2. "weekly status for blocked initiatives"  (8 queries)
   → Tier 2 fired but recall@5 averaged 0.61 (below 0.80 target).
   → Could be a workflow skill with custom retrieval.
   → kb-cli skill-builder --resume sc-def456

...

Reply 'reject N' to dismiss any candidate, or run skill-builder to author.
```

#### 3. Pre-filled skill-builder

When a persona team accepts a candidate (`kb-cli skill-builder --resume sc-abc123`), the skill builder pre-loads the conversation:

- Persona: pre-filled from the candidate
- Intent description: pre-filled from the clustered query pattern
- Example queries: shown as starting examples
- The team only needs to provide an example outcome (PPT/DOCX/answer template)

This lets the suggestion → skill conversion happen in 5-10 minutes vs. starting from scratch.

### Query clustering

`framework/workflow_runtime/skill_suggester.py` runs nightly:

1. Pull all Tier-4 misses + low-recall Tier-2 hits from cost_log + retrieval_log
2. Embed each query (text-embedding-3-large via OCI GenAI per ADR-014)
3. Cluster by cosine distance (DBSCAN, eps=0.15)
4. For each cluster ≥ 3 items, generate `query_pattern` via gpt-4o (one-line description)
5. Insert/update `skill_candidates` rows; bump occurrence count if pattern already exists

Cost: ~$0.05 per persona per nightly run. Cheap relative to the value of identifying real demand.

### Lifecycle

```
new query miss  ──→  log to cost_log/retrieval_log
                            ↓
              nightly skill_suggester clustering
                            ↓
                  skill_candidate row (status: pending)
                            ↓
                ┌───────────┴───────────┐
                │                       │
        weekly digest             inline at miss time
                │                       │
                ↓                       ↓
          persona team reviews — three options:
                            ↓
       ┌────────────────────┼────────────────────┐
       │                    │                    │
   accept                 defer               reject
       │                    │                    │
       ↓                    ↓                    ↓
 kb-cli skill-          status: deferred    status: rejected
 builder --resume        (re-surfaces       (suppressed for
                          in 30 days)       similar future queries)
       │
       ↓
 skill committed; status: accepted
```

### Observability — per-persona skill coverage

Dashboard surfaces:
- **Tier-4 miss rate** per persona (target: trending toward 0)
- **Tier-2 repeat-pattern count** (signals workflow-skill opportunities)
- **Pending skill_candidates** per persona (work to do)
- **Skill creation cadence** (skills accepted per week)

A persona team's framework maturity is loosely indicated by Tier-4 rate trending down + skill creation rate trending up over time.

## Considered alternatives

- **No suggestion loop** (V1 default): rejected; loses signal; persona teams have to anticipate every pattern up front
- **Auto-create skills without human review**: rejected; quality of LLM-generated workflow skills is not yet trustworthy enough to bypass human approval
- **Static suggestion based on query keyword matching**: rejected; misses semantic clusters (e.g., "blocked initiatives" and "stalled programs" should cluster together; keyword match would miss it)

## Consequences

- New table: `kb_shim.skill_candidates`
- New module: `framework/workflow_runtime/skill_suggester.py` (~200 LOC + tests)
- Cost: ~$0.05 per persona per night (clustering + pattern generation)
- New CLI: `kb-cli skill-builder --resume <candidate_id>`
- New dashboard panel per persona
- New email digest (uses existing email deliverer per ADR-016)

## References
- [PDD V2 §7 — Skill suggestion loop](../pdd/PDD-Knowledge-Builder-Framework-v2.md)
- [ADR-006 amend 3 — four-tier routing](ADR-006-two-shim-architecture.md)
- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md)
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md)
