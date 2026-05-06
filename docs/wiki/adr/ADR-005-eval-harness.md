---
title: ADR-005 — Eval harness
status: accepted
created: 2026-05-04
owner: architect
tags: [adr, eval, phase-0]
related: [ADR-001, ADR-004]
---

# ADR-005 — Eval harness

## Status
Accepted (2026-05-04). Source: spec §10 (eval is mandatory from v1) + DECISION-001 (Ragas + custom runner).

## Context
Spec §10 mandates eval as a v1 cross-cutting requirement: gold question sets per persona, recall@k, faithfulness, latency, cost — runs in CI on parser/store changes. Spec §12 acceptance criteria: "Eval harness runs on every PR; merge blocked on regression."

## Decision

### Composition
- **Custom Python runner** for: recall@k, MRR, latency p50/p95, token count, cost. These are deterministic and fast.
- **Ragas** for LLM-as-judge metrics: faithfulness, answer relevancy, context precision, context recall. Slower, costlier — used selectively (sample of N per run, full run on release branches).

### Module layout
```
framework/eval/
├── runner.py              # entrypoint: kb-cli eval ...
├── metrics/
│   ├── recall.py          # recall@k, MRR, hit@k
│   ├── latency.py         # p50/p95, percentile decay
│   ├── cost.py            # tokens × price table → $
│   └── faithfulness.py    # Ragas wrapper (with sampling)
├── reports/
│   ├── render.py          # markdown + JSON reports
│   └── diff.py            # current run vs. baseline → regressions
└── corpora/                # snapshots of test corpora (small) for repeatable runs
```

### Gold-set format
Location: `eval/gold_sets/{persona}.jsonl` (one JSON object per line)

```json
{
  "id": "incident-q-001",
  "persona": "aira",
  "question": "What incidents touched auth-service in the last 30 days?",
  "expected_citations": ["jira://INC-12345", "jira://INC-12399"],
  "expected_answer_includes": ["auth-service", "ORA-1017", "tenant-123"],
  "tags": ["service:auth", "kind:listing"],
  "min_recall_at_5": 0.8,
  "min_faithfulness": 0.85
}
```

Fields:
- `id` — stable, used in regression diffs
- `expected_citations` — content URN list; recall@k checks how many appear in top-k retrieved
- `expected_answer_includes` — substrings that should appear in the synthesized answer (cheap lexical floor)
- `tags` — segmentation for breakdowns
- `min_*` thresholds — per-question gates; harness fails if any per-question floor regresses

### Metrics

| Metric | Computed by | Notes |
|---|---|---|
| recall@k | custom | k=5 default; configurable per persona in builder config |
| MRR | custom | Sanity check on top-1 quality |
| latency p50 / p95 | custom | Wall-clock from query → response |
| tokens (in/out) | custom | Per request, summed per run |
| cost ($) | custom | Tokens × price table; OpenAI prices in `eval/prices.yaml` |
| faithfulness | Ragas | Sampled N=20 per run, full corpus on release branch |
| answer_relevancy | Ragas | Sampled |
| context_precision | Ragas | Sampled |
| context_recall | Ragas | Sampled |

### Pass/fail rule
Per persona builder's `eval.exit_criteria` (ADR-004). Default global thresholds:
- `recall_at_5 >= 0.80`
- `faithfulness >= 0.85` (when Ragas is run)
- `p95_latency_ms <= 800`
- `tokens_per_query <= 2000`

A PR is blocked if any persona's exit criteria regresses by more than the configured tolerance (default 2 percentage points absolute) vs. the baseline on `main`.

### Baseline + drift handling
- Baseline = last green run on `main`, stored in `eval/baselines/{persona}/`.
- A PR's eval run produces a diff report (`eval/reports/PR-{N}.md`) comparing each metric against baseline.
- A baseline can be updated only by a PR that explicitly bumps the baseline file with a justification in the commit message.

### CI wiring
- Triggered by changes under `framework/parsers/`, `framework/stores/`, `framework/retrievers/`, `framework/orchestrator/`, `framework/persona_builders/`, `framework/parsers/schemas/`.
- Skipped for pure docs/wiki changes.
- Runs on a tiny corpus (committed in `eval/corpora/`) for fast feedback; weekly cron runs full-scale eval against staging Autonomous DB.

### Cost budget
- Per-run budget: $0.50 default per persona. Configurable.
- Hard cap per CI run: $5. Exceeded → run aborts and PR is marked needs-review.

## Considered alternatives
- **Trulens** instead of Ragas — comparable; Ragas chosen for spec alignment and OpenAI-native defaults.
- **Pure custom (no LLM-judge)** — cheaper but loses faithfulness signal; rejected.
- **End-to-end-only** (no per-component metrics) — easier but slow to localize regressions. Rejected.

## Consequences
- Every parser/store/retriever PR pays the eval cost. Acceptable per spec §10 mandate.
- Persona teams MUST ship a gold set with their builder; CI enforces presence.
- Phase 0 ships only the seed gold set for incidents (5 questions) plus the runner skeleton; Phase 1 wires it into CI.

## Amendments

### Amendment 1 — Bootstrap gold sets from AIRA's eval harness (2026-05-06)
Per [aira-comparison.md §1.3.3](../aira-comparison.md): the AIRA team already runs an evaluation harness against production query/citation pairs. Rather than hand-curate the Phase-1 incidents gold set from scratch, **bootstrap with AIRA's existing benchmark**. Action item:
- Phase 0 close: contact AIRA team for ~50 query/expected-citation pairs from their eval harness
- Phase 1 Wk 2: pick 25 most representative; promote to `eval/gold_sets/incidents.jsonl` (replace placeholder STARTER questions)
- Phase 1 exit gate becomes apples-to-apples: same questions AIRA evaluates against, on the new framework

### Amendment 2 — Recency-weighted recall metric (2026-05-06)
Per AIRA's recency tiebreaker (`ORDER BY SCORE DESC, AGE ASC`) — recall@k as a binary hit/miss is too coarse for an operational system where stale matches are less useful than fresh ones. Add **recency-weighted recall@k**:

```
recency_weighted_recall@k = sum(  hit_i × age_weight_i  ) / k
where age_weight_i = exp(-age_days / TAU)   # TAU=180 days default
```

A 1-day-old hit weights ~1.0; a 1-year-old hit weights ~0.13. This punishes top-K positions that are correct-but-stale and rewards retrievers that surface fresh content.

Reported alongside plain recall@k. The plain recall@k is still the primary gate; recency-weighted recall is an SLI for operational quality.

### Amendment 3 — Stack/filter strictness in eval queries (2026-05-06)
Per [ADR-013](ADR-013-filter-strictness.md), every gold-set query may carry a `filters_strictness` field describing expected behavior:
```jsonc
{
  "id": "incident-q-001",
  "question": "...",
  "filters": {
    "service": {"values": ["auth-service"], "strictness": "hard"},
    "stack":   {"values": ["prod"],         "strictness": "soft"}
  },
  ...
}
```
The eval runner verifies the retriever applied filters at the expected strictness — soft-fallback fires shouldn't happen on hard-intent queries (that's a routing bug).

## References
- Spec §10 (cross-cutting eval requirement), §12 (Phase 1 acceptance criteria).
- [ADR-001](ADR-001-tech-stack-baseline.md), [ADR-004](ADR-004-persona-builder-config.md).
- [ADR-013 — Filter strictness](ADR-013-filter-strictness.md)
- [aira-comparison.md](../aira-comparison.md)
