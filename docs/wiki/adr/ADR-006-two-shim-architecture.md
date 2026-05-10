---
title: ADR-006 — Two-shim layered architecture (shim_kb + shim_faaas)
status: accepted
created: 2026-05-05
owner: architect
tags: [adr, architecture, orchestration, phase-0]
related: [ADR-003, ADR-004, ADR-007, PDD]
---

# ADR-006 — Two-shim layered architecture

## Status
Accepted (2026-05-05). Replaces spec §6.6's single `shim_index` with two specialized shims.

## Context
Spec §6.6 proposed one shim_index — a meta-wiki of every store/wiki tree loaded into the Context Builder's prompt. As the FAaaS scope grew during design discussions (multiple personas × functional areas × resources × services × kinds-of-knowledge), the single-shim model became overloaded:
- The orchestrator needed *world knowledge* (what personas/services/resources exist) to route a query.
- The persona skills needed *KB knowledge* (how to query each of their KBs) to retrieve.
- Cramming both into one prompt bloated the orchestrator and forced persona skills to filter by keyword.

## Decision
Split the shim into two layers, each owned by a different concern.

### shim_faaas — the FAaaS domain ontology
**Purpose**: lets the orchestrator route a query to the right persona(s).
**Contents**:
- Personas (PM, TPM, Architect, Eng Mgr, Developer, Ops Mgr, Ops Eng, Service Owner; consumer agents like Aira)
- Services (ADP, FACP, HCP)
- Resources (POD, PODDB, EXADATA, BLOCK_VOLUME, NETWORK) with parent/child relationships
- Functional areas (REFRESH, PROVISIONING, UPDATE, PATCHING, DB_INFRA_PATCHING, DR)
- Kinds of knowledge (concept, procedure, decision, runbook, incident_history, postmortem, design)
- Cross-references: which personas typically own which functional areas, which services own which resources

**Storage**: `framework/config/shim_faaas.yaml` (committed; Architect-owned). Mirror copy in `kb_shim.faaas` table (DB) for runtime queries.
**Update cadence**: when org reality changes (new persona, new service, new resource type). Reviewed in Architect PRs.
**Loaded into**: orchestrator's system prompt at startup; refreshed on git commit hook + signal.

### shim_kb — per-KB self-description
**Purpose**: lets each persona context skill (and the orchestrator on bypass paths) decide which KB(s) within a persona to query.
**Contents**: aggregated `kb_card` blocks from every persona-builder config (per ADR-004).
- For each KB: `summary`, `use_when`, `input_shape`, `output_shape`, optional `do_not_use_for`, `freshness`, `expected_volume`
- The persona it belongs to
- The retrieval tools that can be called against it
- Allowed metadata filters (functional_area, resources, kind, etc.)

**Storage**: derived from `framework/persona_builders/*.yaml` at config-load. Mirror in `kb_shim.kb_cards` table.
**Update cadence**: on every persona-builder config change (CI hook auto-rebuilds).
**Loaded into**: each persona context skill's system prompt at invocation; orchestrator only when it bypasses persona skills.

### Layer separation diagram
```
                                  ┌──────────────────────────────────┐
        Orchestrator agent  ←─────┤  shim_faaas — domain ontology    │
                │                 │  (personas, services, resources, │
                │ routes by       │   functional areas, relationships) │
                │ persona+intent  └──────────────────────────────────┘
                ▼
   ┌──────────────────────────┐
   │ Persona Context Skill    │ ←───── shim_kb (filtered to its KBs)
   │   (PM, TPM, Eng Mgr, …) │        — KB cards: when to query me,
   └─────────┬────────────────┘          with what shape
             │ dispatches retrieval tools
             ▼
   ┌──────────────────────────┐
   │ KBs (vector/wiki/graph/  │
   │      sql/code)           │
   └──────────────────────────┘
```

## Why two shims, not one
| | Single shim (spec §6.6) | Two shims (this ADR) |
|---|---|---|
| Orchestrator prompt size | grows with every KB across every persona | constant — only the FAaaS ontology |
| KB-card visibility to wrong persona | yes (PM agent sees Ops KB cards) | no (each skill sees only its own) |
| Update locality | every KB change touches the orchestrator's prompt | KB changes touch only the persona skill |
| Cost telemetry | global only | per-shim, per-persona |
| Future ACL | hard (everything in one place) | natural (shim_kb is already persona-scoped) |

## Behavior at runtime

1. **Query arrives at orchestrator.** Orchestrator's prompt includes `shim_faaas` (~3–5 KB compact YAML).
2. **Intent classification** (small LLM call). Output: `{personas: [...], functional_area, resources, kind, confidence}`.
3. **Skill dispatch** (parallel where possible). Each chosen persona context skill is invoked with the query + intent signal + budget. The persona skill's prompt embeds `shim_kb_filtered` — only that persona's KB cards.
4. **KB selection inside the skill.** The skill picks 1–N KBs from its `shim_kb_filtered` based on `use_when`. Calls the corresponding retrieval tools.
5. **Skill returns ContextPacket** to the orchestrator.
6. **Orchestrator merges packets**, dedupes, reranks, synthesizes with citations.

## Implementation map
- `framework/config/shim_faaas.yaml` — YAML source of truth for the domain ontology.
- `framework/orchestrator/shim_faaas.py` — loader + cache, hot-reload on signal.
- `framework/orchestrator/shim_kb.py` — aggregates `kb_card` blocks across persona-builder configs; persona-scoped views.
- `framework/orchestrator/intent_classifier.py` — small LLM call producing routing decision.
- `framework/orchestrator/context_builder.py` — top-level orchestrator graph (LangGraph).
- `kb_shim.faaas`, `kb_shim.kb_cards` — DB mirror tables for runtime introspection (`list_sources()` MCP tool).

## Considered alternatives
- **Single big shim** (spec §6.6) — rejected; bloats orchestrator and persona skills equally.
- **Per-persona-only shim, no FAaaS shim** — rejected; orchestrator can't route without world knowledge.
- **Global vector index over KB cards** — rejected; routing is a small classification problem, not retrieval. Adds cost.

## Consequences
- Two YAML files become foundational artifacts — `shim_faaas.yaml` (Architect-owned) and the aggregated `kb_card`s from persona builders (persona-team-owned).
- The Context Builder's prompt is bounded; cost stays predictable as new KBs are added (cost grows in persona-skill prompts, not orchestrator).
- ACL enforcement (Phase 4) lands naturally — the orchestrator filters `shim_faaas` by consumer agent's `persona_visibility`; persona skills filter their `shim_kb_filtered` similarly.

## References
- [PDD §4, §14](../pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-003 — Core interfaces](ADR-003-core-interfaces.md)
- [ADR-004 (v2) — Persona-builder config](ADR-004-persona-builder-config.md)
- [ADR-007 — Persona context skill contract](ADR-007-persona-context-skill.md)
- Spec §6.5, §6.6

---

## Amendment 2 — Three-shim architecture (2026-05-09; V2)

V2 splits per-persona routing into two layers: shim_workflows (authoring-scoped) for Tier-1 workflow-skill matching, and shim_kb (ACL-scoped per ADR-007 amend 6) for Tier-2 KB retrieval. The original two-shim model becomes:

| Shim | Scope | Used by | Purpose |
|---|---|---|---|
| `shim_faaas` | global / domain ontology | orchestrator's intent classifier | Pick persona(s) for a query |
| `shim_workflows` | per-persona (authoring) | persona context skill — Tier 1 | Match user request to a workflow skill |
| `shim_kb` | per-persona (ACL-driven read) | persona context skill — Tier 2 | Find KBs whose data fits the query |

**Two new modules added:**
- `framework/orchestrator/shim_workflows.py` — aggregates `skill_card` blocks from `framework/workflow_skills/{persona}/*.yaml`; persona-scoped views via `cards_for(persona)`
- `framework/orchestrator/shim_kb.py` (amended) — adds `cards_visible_to(persona)` and `cards_owned_by(persona)` distinguishing read scope from authoring scope

The orchestrator's classifier prompt now embeds shim_faaas (~3-5 KB) only; persona context skills internally embed their persona-filtered shim_workflows + shim_kb (~1-3 KB each).

## Amendment 3 — Tiered routing with confidence thresholds (2026-05-09; V2)

V2 introduces explicit four-tier routing with graceful degradation:

| Tier | Mechanism | Default confidence threshold |
|---|---|---|
| 1 | Workflow skill match (shim_workflows) | 0.85 |
| 2 | KB retrieval (shim_kb) | 0.6 |
| 3 | Multi-persona fanout | 0.4 |
| 4 | Honest "no answer" + skill suggestion (per ADR-018) | <0.3 |

Thresholds are configurable in `framework/config/{env}.yaml`:

```yaml
orchestrator:
  routing_thresholds:
    workflow_skill_match: 0.85
    persona_skill_match:  0.60
    multi_persona_fanout: 0.40
    no_answer_floor:      0.30
```

Skills are an **optimization layer over retrieval**, not a precondition. The framework always answers if it has the knowledge. Workflow skills are added as patterns crystallize (see ADR-018 skill suggestion loop).
