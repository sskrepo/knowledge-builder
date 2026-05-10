---
title: PDD V2 — Knowledge Builder Framework
status: in-review
created: 2026-05-09
supersedes: PDD-Knowledge-Builder-Framework.md (V1)
owner: pm
contributors: [tpm, architect]
tags: [pdd, framework, v2]
spec_source: docs/raw/knowledge-builder-framework-spec.md
---

# Product Definition Document V2 — Knowledge Builder Framework

> **Status:** Draft V2 — supersedes V1 with the design refinements from the 2026-05-06 → 2026-05-09 architecture conversations. V1 stays in `pdd/` for history.
> **Audience:** Product/engineering leadership; persona team leads; executive sponsors.
> **What's new in V2:** the two-flow model (Knowledge Builder + Consumption), three-shim architecture, four-tier routing with graceful degradation, workflow skills as first-class persona-owned outputs, extraction–workflow linking, skill-by-demonstration onboarding, the skill suggestion loop, and the precise authoring-scope vs read-scope distinction.

---

## 1. The two flows — V2's organizing principle

V1 framed the framework as "ingestion → storage → retrieval → orchestration." V2 makes the two principal flows explicit and gives them names:

```
┌──────────────────────────────────────────────────────────────────────┐
│  KNOWLEDGE BUILDER FLOW   (authoring time — by persona team)         │
│                                                                      │
│  Persona expresses a TASK they want done                             │
│         + sample sources                                             │
│         + an example outcome (a deliverable they produce today)      │
│                                ↓                                     │
│         Skill Builder Agent (LLM-driven; conversational)             │
│         • analyzes example outcome                                   │
│         • derives required fields                                    │
│         • checks if existing extraction skills cover them            │
│         • SYNTHESIZES new extraction skill(s) for any gaps           │
│         • SYNTHESIZES the workflow skill                             │
│         • LINKS them via requires_extractions / provides_fields      │
│                                ↓                                     │
│         Two committed artifacts (or one + reuse):                    │
│         • workflow_skills/{persona}/{name}.yaml                      │
│         • persona_builders/{persona}.yaml + extraction schema(s)     │
└──────────────────────────────────────────────────────────────────────┘

         (skills are now LIVE; extraction populates KBs continuously)

┌──────────────────────────────────────────────────────────────────────┐
│  CONSUMPTION FLOW   (runtime — user / cron / event)                  │
│                                                                      │
│  User asks question / requests task / cron fires / event triggers   │
│                                ↓                                     │
│         Orchestrator (uses shim_faaas to pick persona)              │
│                                ↓                                     │
│         Persona Context Skill (the per-persona retrieval brain)      │
│         decides between:                                             │
│         • TIER 1 — workflow skill match (curated output)             │
│         • TIER 2 — KB retrieval (cited synthesis from raw context)   │
│                                ↓                                     │
│         Returns: cited answer | rendered artifact | honest "no"      │
└──────────────────────────────────────────────────────────────────────┘
```

The two flows are coupled but independently evolving — extraction skills are populated continuously; workflow skills are added incrementally as patterns emerge.

---

## 2. The three-shim architecture

V1 had a single `shim_index`. V2 layers three shims, each owned by a different concern:

| Shim | Scope | Used by | Purpose |
|---|---|---|---|
| **shim_faaas** | global / domain ontology | orchestrator's classifier | Pick persona(s) for a query |
| **shim_workflows** | per-persona (authoring scope) | persona context skill — Tier 1 | Match user request to a workflow skill |
| **shim_kb** | per-persona (read scope, ACL-driven) | persona context skill — Tier 2 | Find KBs whose data fits the query |

```
                       Orchestrator
                         (shim_faaas)
                              │
                              ▼
                    Persona Context Skill                    ← always present
                              │                                framework component
                              │
                ┌─────────────┴─────────────┐
                │                           │
       shim_workflows                  shim_kb
       (this persona's                 (KBs visible to this
        authored skills)                persona via ACL)
                │                           │
                ▼                           ▼
            Tier 1                       Tier 2
       Workflow skill match         KB retrieval + synthesis
       (curated output —            (cited passages →
        PPT/DOCX/email/answer)       synthesized answer)
```

**Crucial distinction:**
- **shim_workflows is authoring-scoped** — only TPM's authored workflow skills appear in TPM's view (TPM doesn't fire ops_eng's workflows)
- **shim_kb is ACL-scoped** — TPM can read PM's KBs and ops_eng's KBs as long as TPM is in their `persona_visibility`. Read scope is wider than authoring scope.

---

## 3. Four-tier routing with graceful degradation

V1 implied "match retrieval; otherwise fail." V2 makes routing explicit, with confidence-driven graceful degradation:

```
User query/task
    │
    ▼
Orchestrator (shim_faaas) — picks persona(s)
    │
    ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  Persona Context Skill                                       │
 │                                                              │
 │  TIER 1: shim_workflows match (authoring-scoped)             │
 │     ├── confidence ≥ 0.85?                                   │
 │     │     YES → invoke workflow skill                        │
 │     │            (procedural source discovery → extraction → │
 │     │             retrieval → synthesis → render → return)   │
 │     │     NO  → fall through                                 │
 │     ▼                                                        │
 │  TIER 2: shim_kb retrieval (ACL-scoped)                      │
 │     ├── confidence ≥ 0.6?                                    │
 │     │     YES → vector_search / search_wiki / graph_traverse │
 │     │            → ContextPacket → synthesizer               │
 │     │     NO  → fall through                                 │
 │     ▼                                                        │
 │  TIER 3: multi-persona fanout                                │
 │     ├── confidence ≥ 0.4?                                    │
 │     │     YES → dispatch to N persona context skills in     │
 │     │            parallel → merge ContextPackets →           │
 │     │            synthesize one cited answer with            │
 │     │            per-persona attribution                     │
 │     │     NO  → fall through                                 │
 │     ▼                                                        │
 │  TIER 4: honest "no answer"                                  │
 │     │     Return: "no grounded knowledge for this; closest   │
 │     │              matches: [...]; want me to scaffold a     │
 │     │              skill?" (TRIGGERS SKILL SUGGESTION LOOP)  │
 └──────────────────────────────────────────────────────────────┘
```

Each tier has a configurable confidence threshold (`framework/config/{env}.yaml :: orchestrator.routing_thresholds`). Skills are an **optimization layer over retrieval** — the framework always answers if it has the knowledge; skills are "fast lane for common patterns + special rendering."

---

## 4. Workflow skills

A **workflow skill** is a persona-owned, user-authored skill that produces a specific outcome (PPT, DOCX, email digest, Slack message, dashboard update, or just a structured answer). Three trigger models:

```
WORKFLOW SKILL TRIGGERS
  ├── ON_SCHEDULE   — cron-like; autonomous; output goes to destination
  ├── ON_REQUEST    — user asks; orchestrator matches; synchronous return
  └── ON_EVENT      — data event triggers (e.g., INC closes); Phase 4+
```

A single skill can declare any combination. The TPM exec-review skill probably has both `on_schedule` (Friday 4pm autonomous delivery) and `on_request` ("hey, give me an exec review for project X").

### Workflow skill anatomy

```yaml
# workflow_skills/tpm/weekly_exec_review.yaml
workflow_skill: weekly_exec_review
persona: tpm                                    # AUTHORING owner

trigger:
  on_schedule:
    cron: "0 16 * * 5"
    delivery:
      kind: oci_object_storage
      path: "kbf-outputs/weekly-exec-review/{week_id}/review.pptx"
      notification: { kind: email, to: [exec-team@company] }
  on_request:
    enabled: true
    inputs:
      - { name: project, type: string,
          description: "Project key (or 'all')" }
    output_format: pptx
    response_mode: artifact_url

skill_card:                                     # for shim_workflows matching
  use_when: |
    User asks for an exec review, weekly status deck, project status PPT.
  example_invocations:
    - "exec review for customer-events"
    - "give me the weekly status deck"
  do_not_use_for: |
    Single-fact lookups (use search_wiki). Live operational data (use query_fleet).

requires_extractions:                           # the LINK to extraction skill(s)
  - kb: tpm.weekly_project_status
    required_fields: [week_id, rag_status, top_milestones, blockers, exec_asks]

synthesis:
  template: synthesis/templates/weekly_exec_review.pptx
  slide_mapping: synthesis/mappings/weekly_exec_review.yaml

eval:
  gold_set: eval/gold_sets/tpm-weekly-exec-review.jsonl
  exit_criteria:
    field_accuracy: 0.85
    delivery_success_rate: 0.99
```

### Execution model

When invoked (any trigger):

```
1. Resolve source set
   • via procedural discovery (Adapter.discover() per ADR-011 amend)
   • OR static inputs (e.g., {project: "customer-events"})
2. Fetch sources via persona-builder's adapter
3. Extract via paired extraction skill (the linked KB's schema)
   — usually retrieves cached ContentItems (already extracted by extraction skill's pipeline);
     falls back to fresh extraction if cache miss
4. Synthesize via slide_mapping + synthesis schema
5. Render via output_format renderer (pptx / docx / email / etc.)
6. Deliver via deliverer (Object Storage / email / Slack / sync return)
7. Cost telemetry written; eval gold-set entry recorded
```

---

## 5. Extraction–workflow linking

Every workflow skill declares which KBs it consumes from; every KB declares which fields it provides. The framework verifies the link at promote time:

```yaml
# Workflow skill requires fields:
requires_extractions:
  - kb: tpm.weekly_project_status
    required_fields: [week_id, rag_status, top_milestones, blockers, exec_asks]

# Extraction skill (in persona-builder) provides fields:
knowledge_bases:
  - name: weekly_project_status
    kind: wiki
    extraction_schema: parsers/schemas/tpm/weekly-project-status/v1.json
    provides_fields: [week_id, rag_status, top_milestones, blockers, exec_asks, source_page_url]
```

`kb-cli promote` verifies:
1. ✓ Workflow's `required_fields` ⊆ ⋃ (linked KBs' `provides_fields`)
2. ✓ Workflow's owning persona is in each linked KB's `metadata_defaults.persona_visibility` (read access)

If either fails → block promote with a clear error.

This is the N:M relationship between workflows and extractions:
- A workflow skill can require fields from **multiple** extraction skills
- An extraction skill can be referenced by **multiple** workflow skills

Skill builder's job during authoring: detect when fields are already covered by existing extraction (reuse) vs need new extraction (synthesize new schema).

---

## 6. Skill-by-demonstration onboarding

V1 had persona teams hand-author YAML configs and JSON-Schemas. V2 makes the **conversational interface the primary path**; YAML editing becomes the advanced fallback.

### The interaction shape

```
$ kb-cli skill-builder

> I usually look at Confluence pages for each project and produce
  an exec review PPT every Friday. Here's an example PPT.
  [user uploads or describes example artifact]

✓ Analyzing example PPT…
✓ Required fields: week_id, rag_status, top_milestones, blockers, exec_asks
✓ Checking existing knowledge bases for these fields…
   - 0 of 5 fields covered by existing extractions
✓ Will create:
   1. NEW extraction skill: tpm.weekly_project_status
      • source: Confluence PROJECT-* / weekly-status pages
      • schema: 6 fields, derived from your example PPT
      • gold: your example PPT becomes the first extraction-gold pair
   2. NEW workflow skill: weekly_exec_review
      • triggers: schedule (Fri 4pm) + on_request
      • output: pptx via slide_mapping derived from your PPT
      • delivery: OCI Object Storage + email exec-team
   3. LINK: workflow.requires_extractions → [tpm.weekly_project_status]

Confirm? (y / refine N)
```

The persona team never edits a JSON-Schema or persona-builder YAML. The skill builder agent synthesizes those artifacts from the example outcome + intent. The artifacts still land in git (PR-reviewable, lint-checked, eval-gated) — but authoring them is conversational.

### What gets analyzed and synthesized

1. **Example artifact analysis** (PPT/DOCX/email mock):
   - Parse structure (slides, sections)
   - For each text block, infer: what data field would populate this?
   - Build extraction-schema fields + synthesis-schema slide_mapping
2. **Procedural source rules** (from natural-language description):
   - "For each project space, find the latest weekly-status page" → multi-step Adapter.discover() recipe
3. **Skill cards**:
   - `use_when` derived from intent description
   - `example_invocations` derived from natural patterns ("exec review for X", "status deck", etc.)
4. **Eval gold-set seed**:
   - The provided artifact becomes the first extraction-gold pair AND the first workflow output gold

### Iteration loop

- "Add a field for outage_minutes_total" → schema + synthesis regenerated
- "Field X should be enum, not free-form" → schema regenerated
- "Pull from JIRA OPS too" → sources updated
- Show preview extractions on real samples → user approves or corrects

---

## 7. Skill suggestion loop

When the framework hits Tier 4 (no good answer) — or even Tier 2 with low confidence repeatedly for the same query pattern — it surfaces the failed query as a **skill candidate**:

```
"I couldn't find a specialized way to handle this query. But it looks
 like the data is in your TPM persona's KBs.

 → Want me to scaffold a workflow skill for queries like this?
   I can analyze a few past examples and synthesize the skill.

 [start skill builder] [no thanks]"
```

This is the **flywheel**: real-world usage tells persona teams which skills to author next, instead of trying to anticipate every pattern up front.

Failed queries are logged to a dedicated table (`kb_shim.skill_candidates`) with persona attribution + frequency counts. A weekly digest goes to each persona team: "queries you couldn't answer well last week — top 5."

---

## 8. Authoring scope vs read scope (load-bearing distinction)

| | Authoring scope | Read scope |
|---|---|---|
| Controls | Who owns / maintains the artifact | Who can read the data |
| Mechanism | `persona:` field on the persona-builder YAML | `persona_visibility:` array on every ContentItem |
| Used by | Skill builder during authoring (`shim_kb.cards_owned_by(persona)`) | Persona context skill at retrieval (`shim_kb.cards_visible_to(persona)`) |
| Example | PM owns `pm_briefs` | PM's briefs are visible to PM, TPM, Architect, Dev Mgr, Eng Mgr |

**This is why a TPM workflow skill can read from ops_eng's incident KB** (TPM is in ops_eng's `persona_visibility`) without TPM needing to own that data. The whole point of a polyglot KB is that any persona can pull whatever's relevant.

Promote-time validation:
- Workflow skill's owning persona must be in each linked KB's `persona_visibility`

If TPM tries to author a workflow consuming a KB they don't have read access to → blocked at promote with clear error.

---

## 9. Updated tech stack (no change from V1)

The Phase 0 decisions hold:
- **Oracle 23ai Autonomous Database** — converged store (vector + SQL + graph + JSON)
- **OCI Generative AI Inference** as LLM proxy to OpenAI (per ADR-014)
- **LangGraph on OCI Compute** for orchestration
- **Git** for wiki content (bodies in git, metadata in DB)
- **OCI Vault / Object Storage / Streaming / Functions / Container Instances**

V2 adds:
- **OCI Container Instances** for workflow skill execution sandboxes (when workflow skills need isolated compute, e.g., heavy rendering or external tool execution)
- **Renderers** (`framework/renderers/`) for output formats: pptx, docx, email, Slack, markdown
- **Deliverers** (`framework/deliverers/`) for output destinations: OCI Object Storage, email (OCI Email Delivery), Slack, Teams
- **Workflow runtime** (`framework/workflow_runtime/`) for trigger dispatch + skill execution

---

## 10. Module map (V2 additions)

```
framework/
├── core/                                # unchanged from V1
├── adapters/                            # ADR-011 amend: + .discover() method
├── parsers/                             # unchanged
├── stores/                              # unchanged
├── retrievers/                          # unchanged
├── orchestrator/
│   ├── shim_faaas.py                    # outer routing (unchanged)
│   ├── shim_kb.py                       # ACL-driven; cards_visible_to / cards_owned_by
│   ├── shim_workflows.py                # NEW: workflow skill cards aggregator
│   ├── intent_classifier.py             # AMENDED: classifies query → tier 1/2/3
│   ├── context_builder.py               # AMENDED: tiered routing
│   └── synthesizer.py                   # AMENDED: structured output schema (ADR-007 amend 2)
├── persona_skills/
│   ├── _base.py                         # AMENDED: Tier 1 vs Tier 2 dispatch
│   └── {persona}.py × 8                 # unchanged, inherit from _base
├── skill_builder/                       # NEW MODULE
│   ├── conversation.py                  # LangGraph state machine
│   ├── synthesize_schema.py             # examples → JSON-Schema
│   ├── synthesize_builder.py            # intent + sources → persona-builder YAML
│   ├── synthesize_workflow.py           # outcome example → workflow skill YAML
│   ├── analyze_artifact.py              # parse PPT/DOCX/email; infer fields
│   ├── sampler.py                       # fetch real source samples for review
│   ├── review.py                        # show extractions; accept corrections
│   └── gold_seed.py                     # bootstrap gold sets from examples
├── workflow_runtime/                    # NEW MODULE
│   ├── trigger_dispatcher.py            # schedule / request / event → invoke
│   ├── executor.py                      # source discovery → extract → retrieve → synthesize → render → deliver
│   ├── skill_registry.py                # scans workflow_skills/*.yaml; exposes as MCP tools
│   └── skill_suggester.py               # logs Tier-4 failures; weekly digest
├── renderers/                           # NEW MODULE
│   ├── _base.py                         # Renderer Protocol
│   ├── pptx_renderer.py                 # python-pptx (we have working precedent in this session)
│   ├── docx_renderer.py                 # docx-js (same)
│   ├── email_renderer.py                # MJML / HTML
│   ├── slack_renderer.py                # block-kit
│   └── markdown_renderer.py             # generic
├── deliverers/                          # NEW MODULE
│   ├── _base.py                         # Deliverer Protocol
│   ├── object_storage.py                # OCI Object Storage
│   ├── email.py                         # OCI Email Delivery / SMTP
│   ├── slack.py                         # webhook
│   └── sync_return.py                   # synchronous artifact URL response
├── workflow_skills/                     # NEW: user-authored YAML configs
│   └── {persona}/{skill_name}.yaml
├── synthesis/
│   ├── templates/                       # NEW: PPT/DOCX templates
│   └── mappings/                        # NEW: field → slide/section mapping
├── ingestion/                           # unchanged
├── eval/                                # AMENDED: adds workflow-skill output eval
├── persona_builders/                    # AMENDED: provides_fields per KB
├── config/                              # AMENDED: orchestrator.routing_thresholds
├── deploy/                              # AMENDED: MCP server registers workflow skills as tools
├── scripts/
└── cli/
```

---

## 11. New phase plan

V1's four-phase plan stays mostly intact. V2 adds skill-builder + workflow runtime work to Phase 2 and Phase 3, splitting Phase 3 into two sub-phases.

| Phase | Goal | Headline new content (V2) |
|---|---|---|
| **0 — Setup** ✅ | Tech-stack baseline + interface contract + persona-builder contract + eval harness skeleton | (unchanged from V1) |
| **1 — Skeleton + incident KB** | Match Aira's incident KB on 25-question gold set | (unchanged from V1) |
| **2 — Fleet + code wiki + skill-builder Phase A** | Mixed-source queries work; skill-builder synthesizes extraction skills | **NEW**: skill_builder module Phase A — synthesize extraction skills from intent + samples (no workflow skills yet) |
| **3 — PM/TPM persona builders + workflow runtime** | First non-incident persona KBs in production; first workflow skills shipping | **NEW**: workflow_runtime, renderers, deliverers, shim_workflows; skill-builder Phase B (workflow synthesis from example artifacts); first three workflow skills (TPM weekly_exec_review, ops_eng stuck_poddb_alert, PM release_brief) |
| **4 — Permissions, FA semantic graph, skill suggestion loop, polish** | v2-ready ops posture | `persona_visibility` enforced at retrieval (read-scope contract); FA semantic graph; **skill suggestion loop** (failed queries → candidate skills); cost dashboards |

### Phase 2 detail (V2)

| Wk | Track A — Ingest | Track B — Retrieve | Track C — Eval/Ops | Track D — Skill Builder Phase A |
|----|---|---|---|---|
| 9 | Fleet read-through adapter (UDAP) | `query_fleet`, `text_to_sql` MCP tools | Cost report dashboard | (waits) |
| 10 | Code wiki CI builder | `read_code_page`, `find_symbol` MCP tools | Cross-source eval queries | `analyze_artifact.py` (PPT/DOCX parser) |
| 11 | OpenAPI structured index | (waits) | Multi-source-query gold set | `synthesize_schema.py` (samples → JSON-Schema) |
| 12 | DECISION-005 filed (code write-path substrate) | (waits) | Phase 2 exit eval | `synthesize_builder.py` (intent + sources → persona-builder YAML) |
| 13-14 | Hardening | Hardening | Hardening | Skill builder Phase A integration tests |
| 15 | (waits) | (waits) | (waits) | First persona team uses skill builder for an extraction-only skill (no workflow yet); validation |
| 16 | (Phase 2 exit) | (Phase 2 exit) | (Phase 2 exit) | Skill builder Phase A documented in onboarding |

### Phase 3 detail (V2)

| Wk | Track A — Wiki | Track B — Workflow Runtime | Track C — Skill Builder Phase B | Track D — Persona Builds |
|----|---|---|---|---|
| 17 | WikiMetadataStore + git-backed wiki bodies | `workflow_runtime/executor.py` skeleton | `synthesize_workflow.py` from example artifact | (waits) |
| 18 | Confluence → wiki ingestion (via skill builder) | `trigger_dispatcher.py` (schedule + on_request) | `analyze_artifact.py` PPT mode | TPM persona authors first extraction skill via skill builder |
| 19 | shim_workflows aggregator | First renderer: pptx_renderer.py | `synthesize_workflow.py` PPT mode | TPM authors weekly_exec_review workflow skill |
| 20 | Tiered routing in persona context skills | First deliverer: object_storage.py | Skill builder Phase B integration | PM persona authors extraction skill (briefs, release-plans) |
| 21 | Intent classifier picks Tier 1 vs Tier 2 | docx_renderer.py + email_renderer.py | (waits) | PM authors release_brief workflow skill |
| 22 | (waits) | slack_renderer.py + email deliverer | Skill builder UX polish | ops_eng authors stuck_poddb_alert workflow skill |
| 23 | (waits) | Workflow execution observability | Skill suggestion loop scaffolding (off by default) | Eval all 3 first workflow skills |
| 24-25 | Hardening | Hardening | Hardening | Phase 3 exit eval — all three personas live |
| 26-28 | Multi-persona fanout (Tier 3) | Workflow caching + idempotency | Skill builder gallery (showcase) | Hardening + handoff |

### Phase 4 detail (V2)

| Wk | Track A — Permissions | Track B — Graph | Track C — Suggestion Loop | Track D — Ops |
|----|---|---|---|---|
| 29 | persona_visibility enforced at retrieval (filter at SQL level) | FA semantic graph store activate | `skill_suggester.py` log Tier-4 misses | Cost dashboards live |
| 30 | Consumer manifest enforcement | Dave's POC integrated | Weekly digest per persona team | Latency SLOs published |
| 31 | Audit log retention 90d | Resource ontology bootstrap | First skill suggested + accepted | Eval CI hardened |
| 32 | (waits) | Graph traversal MCP tool | Skill suggestion → kb-cli skill-builder pre-fill | (waits) |
| 33-36 | Hardening + retro | Hardening | Hardening | Hardening + Phase-4 exit |

### Resource asks

| Phase | FTE | Calendar |
|---|---|---|
| 0 | 1 (architect lead) | done |
| 1 | 4 (2 BE devs, 1 dev mgr, 1 QA, 0.5 architect) | 8 weeks |
| 2 | 5 (+1 BE dev for skill-builder track D) | 8 weeks |
| 3 | 6 (+1 BE dev for workflow runtime, sustained skill-builder) | 12 weeks |
| 4 | 5 (back to baseline + 1 SRE for ACL/dashboards) | 8 weeks |

Total to Phase-4 exit: ~9 months (unchanged from V1) with 5–6 FTE peak in Phase 3.

---

## 12. Acceptance criteria — what V2 ships beyond V1

### Phase 1 (unchanged)
- Match Aira's 25-question incident gold set; ≥80% recall@5; ≥0.85 faithfulness; <500ms p95

### Phase 2
- Mixed-source queries work (e.g., "show fleet state for tenants impacted by INC-X")
- **NEW**: a persona team authors an extraction skill end-to-end via `kb-cli skill-builder` without editing YAML/JSON by hand
- DECISION-005 filed (code-access write-path substrate)

### Phase 3
- **NEW**: first 3 workflow skills in production:
  - `tpm.weekly_exec_review` (schedule + on_request, pptx output)
  - `ops_eng.stuck_poddb_alert` (on_event, Slack output)
  - `pm.release_brief` (on_request, docx output)
- Tiered routing live: persona context skills dispatch Tier 1 vs Tier 2
- TPM and PM persona builders graduated to `status: production`
- Multi-source query latency p95 <2s

### Phase 4
- `persona_visibility` enforced at retrieval (not just metadata)
- FA semantic graph integrated
- **NEW**: skill suggestion loop fires; ≥1 skill authored from a Tier-4 suggestion
- Cost dashboards + per-persona/per-skill cost roll-ups

---

## 13. The persona team experience (V2)

A persona team's onboarding now looks like:

1. **Engineering provisioning lands** (ADB, OpenAI/OCI GenAI, Vault, Confluence/Jira tokens)
2. **Skill builder workshop** (~90 min):
   - "What tasks do you want this framework to do for you?"
   - "Show me an example outcome you produce today"
3. **Skill builder synthesizes** the extraction + workflow skills (committed to git, PR-reviewable)
4. **Dry-run + review**: persona team verifies the synthesized extraction on real samples
5. **Eval gate**: gold set from example artifact passes thresholds
6. **Promote**: skill flips to production
7. **Iterate via skill suggestion loop**: failed queries become next skill candidates

The persona team **never directly edits JSON-Schema or YAML** unless they want to (advanced fallback). The framework hides the underlying artifacts behind a conversational interface but keeps them in git so engineers retain full review/diff/lint power.

---

## 14. Open problems (carried from V1)

| # | Problem | Phase | V2 disposition |
|---|---|---|---|
| §8.1 | LLM wiki retrieval for remote agents | 3 | TOC + on-demand fetch + BM25 (Oracle Text); DECISION-006 at Phase 3 kickoff |
| §8.2 | Code accessibility for remote agents | 2/3 | Hybrid: pre-built code wiki for reads (Phase 2) + sandbox for writes (Phase 3); DECISION-005 at Phase 2 close |
| §8.3 | TPM/PM extraction schemas | 3 | **Resolved by skill-by-demonstration** — persona teams don't author schemas by hand at all |

---

## 15. Updated v1 → v2 diff summary

| Area | V1 | V2 |
|---|---|---|
| Onboarding | Persona teams hand-edit YAML + JSON-Schema | `kb-cli skill-builder` synthesizes from intent + example outcome |
| Skills | Single concept (persona context skill) | Three: extraction (ingestion config), workflow (user-authored output), persona-context (framework retrieval brain) |
| Routing | Implicit; "match retrieval; otherwise fail" | Four explicit tiers with confidence-driven graceful degradation |
| Shim | Single `shim_index` | Three: shim_faaas (orchestrator), shim_workflows + shim_kb (per-persona) |
| Workflow skills | Implicit (Aira does its own thing) | First-class concept; persona-owned; three trigger models; renderers + deliverers |
| Linking | None | Explicit `requires_extractions` / `provides_fields` validated at promote |
| Read scope | Implicitly persona-scoped (sloppy) | Explicit ACL-driven: `persona_visibility` controls cross-persona access |
| Failed queries | Just fail | Skill suggestion loop — become candidates for new skill authoring |

---

## 16. References

- V1 PDD: [`PDD-Knowledge-Builder-Framework.md`](PDD-Knowledge-Builder-Framework.md)
- Source spec: [`docs/raw/knowledge-builder-framework-spec.md`](../../raw/knowledge-builder-framework-spec.md)
- AIRA comparison: [`aira-comparison.md`](../aira-comparison.md)
- Code access story: [`code-access-story.md`](../code-access-story.md)
- Persona onboarding workbooks: [`onboarding/`](../onboarding/)
- All ADRs: [`adr/`](../adr/) — V2 introduces ADRs 015–018 + amendments to 006/007/011
