---
title: ADR-007 — Persona context skill contract
status: accepted
created: 2026-05-05
owner: architect
tags: [adr, skill, orchestration, phase-1]
related: [ADR-003, ADR-004, ADR-006, ADR-008, PDD]
---

# ADR-007 — Persona context skill contract

## Status
Accepted (2026-05-05). Defines the input/output contract every persona context skill obeys. PDD §7 ("Skills vs Agents") is the binding rationale.

## Context
Per the PDD, persona-level workers are **skills by default** — bounded functions with predictable cost, latency, and tool calls. Only autonomous flows (Aira, the Orchestrator) are agents. This ADR formalizes what a persona context skill must implement so the Orchestrator can dispatch them uniformly.

## Decision

### Skill signature
```python
@runtime_checkable
class PersonaContextSkill(Protocol):
    persona: str                                  # matches shim_faaas persona id

    def __call__(
        self,
        query: str,
        intent_signal: IntentSignal,             # from orchestrator's classifier
        budget: Budget,                          # tokens, latency, dollars, hops
    ) -> ContextPacket: ...
```

### Inputs
```python
@dataclass
class IntentSignal:
    primary_persona: str                        # this skill's persona
    secondary_personas: list[str]               # for cross-persona attribution
    functional_area: list[str] | None           # multi-valued from query
    resources: list[str] | None                 # multi-valued from query
    services: list[str] | None
    kind: list[str] | None                      # concept | procedure | runbook | …
    time_window: tuple[datetime, datetime] | None
    confidence: float                           # classifier confidence

@dataclass
class Budget:
    max_tokens_in: int
    max_tokens_out: int
    max_latency_ms: int
    max_dollars: float
    max_tool_calls: int                         # hard ceiling on retrieval calls
```

### Output
```python
@dataclass
class ContextPacket:
    persona: str                                # who produced this
    passages: list[Passage]                     # retrieved evidence
    citations: list[Citation]                   # one per passage
    used_kbs: list[str]                         # which KBs the skill queried
    used_tools: list[str]                       # which retrieval tools fired
    cost: CostReport                            # tokens, $, latency
    confidence: float                           # skill's self-assessment
    notes: str | None                           # for orchestrator to merge intelligently

@dataclass
class Passage:
    text: str
    score: float                                # retrieval score (vector distance, BM25, etc.)
    citation: Citation
    metadata: dict                              # functional_area, resources, kind, etc.

@dataclass
class Citation:
    kind: str                                   # "wiki" | "jira" | "confluence" | "udap" | …
    url: str                                    # always present (spec §10)
    content_id: str
    chunk_id: str | None
    excerpt_offset: tuple[int, int] | None      # for verifying the quote
```

**Invariant:** `len(passages) == len(citations)` and every passage has a non-empty `citation.url`. Violation = bug.

### Internal flow (every persona skill follows this)
1. **Read shim_kb_filtered** — only this persona's KBs. The skill's system prompt embeds these `kb_card` blocks (~1–3 KB).
2. **Pick KBs to query.** Single LLM call ("which KBs from {kb_cards} should I query for {query}?"). Output: a small JSON `{kbs: [...], tools_per_kb: {...}, filters: {...}}`.
3. **Apply filters from intent signal.** Functional area, resources, services, kind. These narrow each tool call.
4. **Dispatch retrieval tools** (parallel by default; sequential only if a graph traversal needs the output of a vector search as a starting node).
5. **Dedupe + score-merge** results across KBs. Drop passages outside the budget.
6. **Return ContextPacket.** Skill does **not** synthesize — that's the orchestrator's job.

### Prompt template (per persona)
Each persona-builder may declare a custom system prompt fragment. Default boilerplate is shared:

```python
PERSONA_SKILL_SYSTEM_PROMPT = """
You are the {persona_display_name} Context Skill.

Your job: pick the right knowledge_bases from the list below to answer the user's query.
You DO NOT answer the query. You retrieve evidence; the orchestrator synthesizes.

Available knowledge_bases for this persona:
{shim_kb_filtered_yaml}

Rules:
- Pick the smallest set of KBs that covers the query.
- Apply intent_signal filters (functional_area, resources, kind) to every tool call.
- If no KB matches, return empty passages with confidence=0 and notes explaining why.
- Never invent citations. Every passage must come from a real retrieval result.

Output JSON only:
{{"kbs_to_query": [...], "tools_per_kb": {{"kb_name": ["tool1", ...]}}, "filters": {{...}}}}
"""
```

### Budget enforcement
- Pre-check: skill computes upper-bound token cost before dispatching tools. Aborts if > `budget.max_tokens_in`.
- Per-tool-call timeout = `budget.max_latency_ms / budget.max_tool_calls`.
- If a tool times out, skill returns partial results with a `notes` flag — does not retry indefinitely.
- Total dollars = `sum(tokens * price_per_token)` from `eval/prices.yaml`. If exceeded, skill returns immediately with what it has.

### Failure modes & contract
| Failure | Skill behavior | Orchestrator behavior |
|---|---|---|
| No KB matches intent | empty passages, `confidence=0`, notes | continues with other personas |
| Budget exceeded mid-flight | partial passages, notes flag | merges what's there; surfaces the cap to user |
| Tool error / timeout | passage list excludes that tool, notes flag | continues |
| Upstream MCP unavailable (per ADR-011) | passage list excludes affected KBs, notes flag | continues |
| Hallucinated citation | (must not happen — invariant violation) | hard-fail in eval; CI block |

## Considered alternatives
- **Persona AGENTS** (loops with own tool selection) — rejected per PDD §7. ~5x cost, unbounded latency, harder to debug, harder to budget. Promote to agent only if a specific persona's work is autonomous (Aira is the only one in v1).
- **Single fat orchestrator** that calls retrieval tools directly without persona skills — rejected; bloats orchestrator prompt with all personas' KB cards, defeats shim_kb separation, blocks ACL.
- **No prompt template; every skill writes its own** — rejected; we want consistency for eval and observability.

## Consequences
- Every persona's "skill" is small (~200 LOC) — boilerplate inherits from `BasePersonaSkill`; persona overrides only `persona`, optional prompt customization, and (rarely) custom dedup logic.
- Eval can measure skill behavior in isolation: routing accuracy, tool selection, budget compliance, citation rate.
- Adding a new persona is config + prompt customization, not code change.
- Aira stays as an autonomous agent that calls multiple persona skills via the orchestrator (or directly via MCP).

## Amendments

### Amendment 1 — `max_context_chars` budget field (2026-05-06)
Per AIRA's 50,000-char context cap (see [aira-comparison.md §2.2.3](../aira-comparison.md)): token budget alone doesn't cap context bytes deterministically. Add to `Budget`:

```python
@dataclass
class Budget:
    max_tokens_in: int = 8000
    max_tokens_out: int = 1500
    max_latency_ms: int = 3000
    max_dollars: float = 0.10
    max_tool_calls: int = 6
    max_context_chars: int = 50000   # NEW: hard byte cap on assembled passages
```

Persona skill enforces post-retrieval: after dedup, truncate the passage list (dropping lowest-scored) so that `sum(len(p.text)) <= max_context_chars`. Surfaces in `ContextPacket.notes` if truncation occurred.

### Amendment 2 — Structured synthesis output schema (2026-05-06)
Per AIRA's `Root_Cause / Resolution / Similar ticket` parseable output (see [aira-comparison.md §2.2.4](../aira-comparison.md)): consumer agents benefit from typed fields rather than blob text. The synthesizer accepts an optional output schema declared by the consumer manifest.

```python
@dataclass
class SynthesisSchema:
    name: str                          # "incident_rca" | "release_summary" | ...
    sections: list[SynthesisSection]   # required and optional sections

@dataclass
class SynthesisSection:
    name: str                          # "root_cause" | "resolution" | ...
    description: str                   # what to write here
    required: bool = True
    max_chars: int | None = None
```

Built-in schemas:
- `incident_rca`: root_cause, resolution, similar_ticket_reference (mirrors AIRA's contract)
- `release_summary`: scope, gating_risks, owners
- `runbook_lookup`: trigger_match, recommended_steps, escalation
- (others added by persona teams via consumer_manifest.yaml)

Synthesizer prompt is templated to require all `required` sections and to mark output with section delimiters callers can parse.

### Amendment 3 — Filter strictness in skill input (2026-05-06)
Per [ADR-013 — Filter strictness contract](ADR-013-filter-strictness.md): the `IntentSignal` carries strictness per filter, not just values:

```python
@dataclass
class IntentFilter:
    field: str
    values: list[str]
    strictness: str = "hard"          # hard | soft | off
    soft_multiplier: float = 0.90

@dataclass
class IntentSignal:
    primary_persona: str
    secondary_personas: list[str]
    filters: list[IntentFilter]       # replaces the previous flat fields
    time_window: tuple[datetime, datetime] | None = None
    confidence: float = 0.0
```

Persona skill passes filters straight through to retriever calls without reinterpretation.

## References
- [PDD §7, §8](../pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-003 — Core interfaces](ADR-003-core-interfaces.md)
- [ADR-004 (v2) — Persona-builder config](ADR-004-persona-builder-config.md)
- [ADR-006 — Two-shim layered architecture](ADR-006-two-shim-architecture.md)
- [ADR-013 — Filter strictness](ADR-013-filter-strictness.md)
- [aira-comparison.md §2.2](../aira-comparison.md)
