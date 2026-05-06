---
title: ADR-013 — Filter strictness contract (hard / soft-with-multiplier)
status: accepted
created: 2026-05-06
owner: architect
tags: [adr, retrieval, filters, phase-1]
related: [ADR-007, ADR-008, aira-comparison]
---

# ADR-013 — Filter strictness contract

## Status
Accepted (2026-05-06). Generalizes the AIRA stack-preference pattern (see [aira-comparison.md §2.4](../aira-comparison.md)).

## Context
ADR-008 introduced multi-axis dimensions (functional_area, resources, services, kind, time_window) on every ContentItem. Our default retrieval applies these as **hard `WHERE` clauses** — narrow, precise. AIRA's existing system applies stack as a **soft preference**: `prod` matches get full score; non-prod matches keep their score multiplied by 0.90. The reason: when no `prod` knowledge exists, AIRA wants to fall back to `preprod` rather than return empty.

Both patterns are right in different contexts:
- *"Show me sev1 incidents in prod"* → hard filter (do NOT return preprod)
- *"How to refresh PODDB on prod"* → soft preference (preprod runbooks are useful fallback)
- *"What's the SOC2 status of tenant-99"* → hard filter (compliance is binary)
- *"Find similar incidents to this NPE"* → no filter on stack (cross-stack patterns are useful)

Forcing one global policy loses information.

## Decision
**Filter strictness is a per-filter, per-intent attribute.** A retrieval call carries:

```python
@dataclass
class RetrievalFilter:
    field: str                       # e.g. "services", "stack", "functional_area"
    values: list[str]                # multi-valued; OR within
    strictness: str                  # "hard" | "soft" | "off"
    soft_multiplier: float = 0.90    # used when strictness == "soft"
    soft_default: bool = False       # if True, missing field is treated as match
                                     # (e.g., older rows without functional_area
                                     #  shouldn't be excluded)
```

### Behaviour matrix

| `strictness` | SQL effect | Use when |
|---|---|---|
| `hard` | `WHERE field IN (:values)` | Filter is dispositive — the user genuinely wants exclusion |
| `soft` | `CASE WHEN field IN (:values) THEN 1.00 ELSE :multiplier END * raw_score` | Filter expresses preference; out-of-set results still acceptable, just demoted |
| `off` | (no SQL clause) | Filter present but disabled (debugging/exploration) |

### Default strictness per dimension

Sensible defaults (overridable per intent):

| Dimension | Default | Rationale |
|---|---|---|
| `kind` | hard | Querying for `runbook` shouldn't return `incident_history` |
| `services` | hard | Service is usually load-bearing identity |
| `functional_area` | hard | Workstream scope is rarely fuzzy |
| `resources` | soft (0.85) | Resource hierarchy means a query on `pod` may benefit from `poddb` results too (graph hop is the better tool, but soft fallback as backup) |
| `stack` | soft (0.90) | Per AIRA's experience — fallback to preprod is useful |
| `time_window` | hard | Time windows are typically explicit |
| `tenant_id` | hard | Tenant boundaries are security-relevant |
| `severity` | soft (0.85) | A user asking about sev1 may benefit from related sev2s |
| `region` | soft (0.95) | Regional fallback is rare but possible |

### Where strictness is set

Two layers, in priority order:

1. **Intent classifier output** (orchestrator). The intent classifier may infer per-query strictness from the question's wording:
   - "stack=prod ONLY" or "in prod" with quotes → hard
   - "in prod" plain → keep default (soft)
   - "regardless of stack" → off

2. **Persona-builder default** (`framework/persona_builders/{persona}.yaml`). Each persona team can override the framework defaults for their consumer:
```yaml
retrieval_defaults:
  filter_strictness:
    stack: hard            # ops_mgr persona always wants exact stack
    functional_area: soft  # cross-area patterns sometimes valid
```

3. **Framework default** (this ADR's table) used when neither overrides.

### SQL generation

```python
def _build_where_and_score(filters: list[RetrievalFilter]) -> tuple[str, str, dict]:
    where_clauses = []
    score_factors = ["1.00"]
    binds = {}

    for f in filters:
        if f.strictness == "off" or not f.values:
            continue
        if f.strictness == "hard":
            placeholders = ", ".join(f":{f.field}_v{i}" for i in range(len(f.values)))
            if f.soft_default:
                # Allow NULL or missing; only filter when field is set
                where_clauses.append(
                    f"({f.field} IS NULL OR {f.field} IN ({placeholders}))"
                )
            else:
                where_clauses.append(f"{f.field} IN ({placeholders})")
            for i, v in enumerate(f.values):
                binds[f"{f.field}_v{i}"] = v
        elif f.strictness == "soft":
            placeholders = ", ".join(f":{f.field}_v{i}" for i in range(len(f.values)))
            score_factors.append(
                f"CASE WHEN {f.field} IN ({placeholders}) "
                f"THEN 1.00 ELSE {f.soft_multiplier:.2f} END"
            )
            for i, v in enumerate(f.values):
                binds[f"{f.field}_v{i}"] = v

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    score_factor_sql = " * ".join(score_factors)
    return where_sql, score_factor_sql, binds
```

The retriever assembles SQL like:
```sql
SELECT ...,
       (1 / (1 + VECTOR_DISTANCE(embedding, :qv, COSINE))) * <score_factor_sql> AS score
FROM   ...
WHERE  <where_sql>
ORDER BY score DESC, <recency_expr> ASC
FETCH FIRST :k ROWS ONLY
```

## Considered alternatives
- **Hard filters only** (our previous default): rejected; loses AIRA's well-justified fallback behavior on stack
- **Soft filters only** (AIRA's current pattern): rejected; loses precision when user genuinely wants exclusion
- **Single global mode**: rejected; one-size-fits-all destroys the per-dimension nuance
- **LLM decides strictness inline**: considered; rejected for v1 because it adds an LLM call to every retrieval. Move to ADR-014 candidate if eval shows the heuristic defaults underperform.

## Consequences
- Every retriever (`vector_search`, `search_wiki`, `query_fleet`, `graph_traverse`) accepts a `filters: list[RetrievalFilter]` parameter
- Eval harness must track per-filter behavior (soft fallbacks should rarely fire on hard-intent queries — that's a routing bug)
- Persona-builder configs gain an optional `retrieval_defaults.filter_strictness` block
- Documentation: each persona's KB cards should hint at expected strictness for clarity to the orchestrator's intent classifier

## References
- [aira-comparison.md §2.4](../aira-comparison.md)
- [ADR-007 — Persona context skill contract](ADR-007-persona-context-skill.md)
- [ADR-008 — Functional-area + resources dimensions](ADR-008-functional-area-and-resources.md)
- AIRA source: `docs/raw/aira-vector-search-detailed-explained (1).html` §"KB_VECTOR_SEARCH" (the prod/demo soft-preference pattern)
