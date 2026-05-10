---
title: ADR-015 — Skill-by-demonstration onboarding
status: accepted
created: 2026-05-09
owner: architect
tags: [adr, onboarding, skill-builder, phase-2, phase-3]
related: [PDD-v2, ADR-004, ADR-016, ADR-017]
---

# ADR-015 — Skill-by-demonstration onboarding

## Status
Accepted (2026-05-09). Promotes the conversational synthesis flow to the **primary** persona-team onboarding path. YAML/JSON authoring becomes the advanced fallback.

## Context
V1 (per ADR-004) had persona teams hand-author `framework/persona_builders/{persona}.yaml` + `framework/parsers/schemas/{persona}/{kb}/v1.json`. This works for engineering-comfortable teams but is high-friction for the typical persona owner (a TPM, an ops manager, a service owner). The V1 onboarding workbooks reflected this — they were workshop-driven schema-authoring exercises.

The V2 design reframes onboarding around the natural unit of persona-team thinking: **a task they want done, with sources they look at and an example outcome they produce today.** The framework synthesizes the underlying artifacts (extraction skill + workflow skill) from those inputs.

## Decision

### Primary onboarding flow: `kb-cli skill-builder`

A conversational LLM-driven agent that:
1. Asks the persona team to describe a task in natural language
2. Asks for sample sources (URLs, labels, JQL, repo paths) AND an example outcome (a deliverable artifact: PPT, DOCX, email, or just a structured answer they wrote)
3. Synthesizes:
   - A new **extraction skill** (persona-builder KB entry + JSON-Schema + extraction gold) — OR detects existing extraction skills cover the required fields and reuses them
   - A new **workflow skill** (workflow_skills/{persona}/{name}.yaml + synthesis template/mapping) when the task produces a specific outcome (cron-driven artifact, or on-request deliverable)
   - The **link** between them via `requires_extractions` / `provides_fields`
4. Shows preview extractions on real samples
5. Iterates on user corrections
6. Commits to git (artifacts are committable, diffable, PR-reviewable)
7. Runs dry-run + eval; flips to production once gates pass

### What gets synthesized — three artifact families

| Artifact | Example file | Purpose |
|---|---|---|
| Extraction skill | `framework/persona_builders/{persona}.yaml` (KB entry) + `framework/parsers/schemas/{persona}/{kb}/v1.json` | Populates a knowledge base from sources |
| Workflow skill | `framework/workflow_skills/{persona}/{name}.yaml` + `framework/synthesis/templates/...` + `framework/synthesis/mappings/...` | Produces a specific outcome (PPT/DOCX/email/answer) |
| Gold sets | `eval/gold_sets/{persona}-extraction.jsonl` and `eval/gold_sets/{persona}-{workflow}.jsonl` | Bootstrapped from the user's example sources + outcomes |

### Module layout

```
framework/skill_builder/
├── conversation.py           # LangGraph state machine (gather intent → samples → outcome → review → commit)
├── analyze_artifact.py       # Parse PPT/DOCX/email → infer required fields + slide_mapping
├── synthesize_schema.py      # (sources, target_fields) → JSON-Schema
├── synthesize_builder.py     # Intent + sources → persona-builder YAML diff
├── synthesize_workflow.py    # Outcome example + intent → workflow skill YAML + synthesis template
├── sampler.py                # Fetch real source samples via existing adapters
├── review.py                 # Show extracted fields; accept user corrections; regenerate
├── gold_seed.py              # Bootstrap extraction-gold and workflow-output-gold from user inputs
└── reuse_detector.py         # Search shim_kb for fields that already exist; suggest reuse
```

### Conversation contract (LangGraph state machine)

```
[INIT]
  ↓ ask: which persona? (must match shim_faaas.personas[].id)

[GATHER_INTENT]
  ↓ ask: describe the task in plain English
  ↓ ask: sources (Confluence space / Jira filter / git repo) — OR procedural rule
  ↓ classify: ingestion-only | workflow (output-producing) | both

[GATHER_OUTCOME]                  # only if workflow
  ↓ ask: example outcome you produce today
  ↓ accept: PPT/DOCX upload, or hand-typed structured fields, or natural-language description

[ANALYZE]
  ↓ if outcome is artifact: analyze_artifact.py extracts required fields + slide_mapping
  ↓ search shim_kb for fields already covered by existing extraction skills (reuse_detector)
  ↓ surface: NEW extraction needed for X; REUSING existing Y

[SYNTHESIZE]
  ↓ generate: extraction schema (JSON-Schema)
  ↓ generate: persona-builder YAML diff (KB entry added)
  ↓ generate: workflow skill YAML (if workflow) + synthesis template/mapping
  ↓ generate: gold-set seeds

[PREVIEW]
  ↓ fetch N real samples; run LLM parser with synthesized schema
  ↓ render preview output (e.g., draft PPT) if workflow
  ↓ show user

[REVIEW]
  ↓ accept corrections: "add field X", "X should be enum", "pull from JIRA OPS too"
  ↓ regenerate affected pieces

[COMMIT]
  ↓ git commit synthesized artifacts
  ↓ run kb-cli validate
  ↓ run kb-cli ingest --dry-run --sample 5
  ↓ run kb-cli eval (extraction gold + workflow gold)
  ↓ if pass: kb-cli promote
```

### Reuse vs new-extraction logic (important)

When the persona team's outcome requires fields, `reuse_detector.py` searches shim_kb:

```python
def detect_reuse(required_fields: list[str], persona: str) -> dict:
    """Returns {covered: {field → existing_kb}, gaps: [field, ...]}.

    Honors ACL: only KBs visible to `persona` (per ADR-007 amend 6) count as reuse candidates.
    """
    visible_kbs = shim_kb.cards_visible_to(persona)
    covered = {}
    gaps = []
    for field in required_fields:
        match = next(
            (kb for kb in visible_kbs if field in kb.get("provides_fields", [])),
            None,
        )
        if match:
            covered[field] = match["name"]
        else:
            gaps.append(field)
    return {"covered": covered, "gaps": gaps}
```

If `gaps == []` → workflow skill links to existing extractions; no new extraction needed.
If `gaps != []` → skill builder offers to author a new extraction skill covering the gap fields.

### Schema synthesis from samples

`synthesize_schema.py` is the heart of skill-by-demonstration. Input: 3-5 raw source items + (optionally) example expected fields or example outcome. Output: a valid JSON-Schema 2020-12 document that captures the pattern.

Implementation:
1. **Bottom-up induction** — given samples, ask the LLM: "what structured fields appear consistently across these examples?"
2. **Top-down refinement** — given target fields (e.g., from outcome analysis), ask the LLM: "produce a JSON-Schema with these fields and infer types, enums, max-lengths from the samples"
3. **Validation** — generated schema must be valid JSON-Schema 2020-12; descriptions on every property; controlled vocabularies (enums) where samples show repeated discrete values

The user reviews the synthesized schema in plain English (the schema's `description` fields), not raw JSON. They can say "field X should be an enum" → regenerate.

### Artifact analysis (PPT/DOCX → fields + template)

`analyze_artifact.py` is a key primitive. Given a PPT (or DOCX, email mock):

1. Parse structure (slides, sections, headings)
2. Identify text blocks; for each, ask the LLM: "what data field would populate this?"
3. Trace each field back to source content (if user provided source samples too): "this slide's RAG status comes from this Confluence label color"
4. Build:
   - Field list → goes into extraction schema
   - Slide-template + field mapping → goes into synthesis template + mapping

We have working precedent in this session: the `pptx` and `docx` Anthropic skills + `pptxgenjs` package — re-used here for parsing direction.

## Considered alternatives

- **Hand-edited YAML + JSON-Schema only** (V1 default): retained as fallback for engineering-comfortable users; demoted from primary onboarding path
- **No reuse detection — always create new extraction**: simpler but wasteful. Reuse detection is cheap (shim_kb is a small in-memory dict)
- **Schema synthesis without examples (just description)**: works but quality is materially worse without sample inputs. Skill builder pushes back if user doesn't provide samples
- **Separate "extraction skill builder" and "workflow skill builder" CLIs**: rejected for cohesion. One conversation; framework decides whether one or both kinds of artifact get generated based on whether outcome is an artifact

## Consequences

- Persona teams onboard without engineering hand-holding; lower friction = wider adoption
- The skill builder agent itself is a non-trivial piece of code (~1,000 LOC across `framework/skill_builder/`); Phase 2-3 deliverable
- Existing artifacts (YAML, JSON-Schema, gold sets) don't change format — they're still committed to git, PR-reviewable, lint-checked. Skill builder is a different *authoring interface*, not a runtime change
- Engineering retains full review power (PR diff on synthesized YAML/schema/gold)
- New persona teams can come online in days, not weeks

## References
- [PDD V2 §6 — Skill-by-demonstration onboarding](../pdd/PDD-Knowledge-Builder-Framework-v2.md)
- [ADR-004 — Persona-builder config schema](ADR-004-persona-builder-config.md) — the underlying artifact contract
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md)
- [ADR-017 — Extraction-workflow linking](ADR-017-extraction-workflow-linking.md)
