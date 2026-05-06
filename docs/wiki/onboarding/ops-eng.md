---
title: Ops Engineer (Aira-equivalent) Persona Onboarding Workbook
audience: AIRA team, Ops Engineering / SRE leads
purpose: A self-contained workbook for refining the ops_eng knowledge base — the Phase 1 exit-gate persona
created: 2026-05-06
owner: pm
tags: [onboarding, persona, ops_eng, aira, workbook]
status: ready-for-team-input
---

# Ops Engineer (Aira-equivalent) Persona Onboarding Workbook

> **Send this to the AIRA team and Ops Engineering leads.** This is the Phase 1 exit-gate persona — its gold set is what determines whether Phase 1 ships. AIRA's team has already done most of the hard thinking here; this workbook captures what we'd carry forward, what we'd tighten based on AIRA's own roadmap, and the questions we need answered to finalize the pack.

---

## Why this matters (1-paragraph context)

The `ops_eng` persona in the Knowledge Builder Framework is the **AIRA-equivalent in our stack**. Aira (the agent) is a *consumer* — it queries this persona's knowledge bases when investigating an incident. The `ops_eng` skill is the *producer* — it ingests Jira tickets, runbooks, postmortems, and fleet state, and exposes them via MCP retrieval tools.

**This is the Phase 1 exit-gate persona.** Phase 1 ships when the framework matches or beats AIRA's current incident KB on a 25-question gold set with ≥80% recall@5 and ≥0.85 faithfulness. AIRA's team already has the eval infrastructure and the production query/citation pairs we need to bootstrap our own gold set.

**What this workbook covers** vs. the PM/TPM workbook:
- AIRA already proved much of this path — schemas are mostly inherited rather than designed from scratch
- More knowledge bases (5) and more storage shapes (vector, wiki, graph, sql_passthrough) than any other persona
- Migration story matters here: AIRA's existing tables stay running; framework writes in parallel; cutover after Phase 1 exit

---

## How extraction works (so you can shape it)

```
Raw source (Jira incident / Confluence runbook / git runbook repo)
        │
        ▼
LLM Parser  (gpt-4o, given your JSON-Schema as a prompt constraint)
        │
        ▼
Structured ContentItem (the fields you specified, populated by the LLM)
        │
        ▼
Knowledge base — Oracle 23ai vector + edges + graph (per ADR-002)
        │
        ▼
Aira / Ops Eng skill queries it via MCP retrieval tools
```

Plus two non-LLM-extracted KBs:
- `ops_dependencies` — derived graph (built from incident edges + service catalog)
- `ops_fleet_state` — sql_passthrough (read-through to UDAP/Sentinel; no ingestion)

---

# What we drafted as your starting point

The Ops Engineer Knowledge Builder ships with **5 knowledge bases** — more than any other persona. Each is a separate logical store, with the storage shape that fits its access pattern (per ADR-002 / ADR-008).

| KB | Storage shape | Source | Status |
|---|---|---|---|
| `ops_incidents` | vector | Jira (`OPS`, `P2T`) | **Aira-proven path; Phase 1 priority** |
| `ops_runbooks` | wiki (markdown + git) | Confluence `OPS-RUNBOOKS` + git `org/ops-runbooks` | Phase 1 |
| `ops_postmortems` | vector + wiki hybrid | Confluence `OPS-PM` | Phase 1 |
| `ops_dependencies` | graph | Derived (from incidents + service catalog) | Phase 2 |
| `ops_fleet_state` | sql_passthrough | UDAP / Sentinel views (read-through) | Phase 2 |

The starter pack is at:
- [`framework/persona_builders/ops-eng.yaml`](../../../framework/persona_builders/ops-eng.yaml)
- [`framework/parsers/schemas/incidents/v1.json`](../../../framework/parsers/schemas/incidents/v1.json) — already concrete (matches AIRA's pattern)
- [`framework/parsers/schemas/ops-eng/runbooks/v1.json`](../../../framework/parsers/schemas/ops-eng/runbooks/v1.json)
- [`framework/parsers/schemas/ops-eng/postmortems/v1.json`](../../../framework/parsers/schemas/ops-eng/postmortems/v1.json)
- [`eval/gold_sets/ops-eng.jsonl`](../../../eval/gold_sets/ops-eng.jsonl) — 5 placeholder questions (replace with 25 real ones from AIRA's eval harness)

---

# KB 1: `ops_incidents` (vector) — the Aira-proven path

## What we currently extract

Per [`incidents/v1.json`](../../../framework/parsers/schemas/incidents/v1.json):

```json
{
  "root_cause_summary":  "string ≤500 chars (REQUIRED)",
  "impact": {
    "blast_radius":      "string ≤300 chars (REQUIRED) — who/what was affected",
    "duration_minutes":  "integer ≥0 (REQUIRED)",
    "severity":          "enum: sev1|sev2|sev3|sev4|near-miss"
  },
  "resources_affected":  "array of strings (REQUIRED) — POD/PODDB/EXADATA/BLOCK_VOLUME",
  "ora_codes":           "array of strings matching ^ORA-[0-9]+$",
  "tenant_ids":          "array of strings",
  "service_owner":       "string — primary owning team",
  "resolution_summary":  "string ≤500 chars",
  "links":               "array of URLs — related design docs, postmortems"
}
```

This deliberately **mirrors what AIRA already extracts today**, so the gold-set queries from AIRA's eval harness work apples-to-apples.

## What AIRA's own roadmap suggests we add (per their doc §"Future Improvement")

AIRA's team already identified gaps in the current schema. Their proposal is to add a `PROPERTIES` JSON column with:

```json
{
  "failureContext": "rotate pod db / resource validation",
  "tags": {
    "failureType":   "null-state",
    "errorFamily":   "null-pointer",
    "resourceType":  "pod-db",
    "operationArea": "workflow",
    "failureStage":  "resource-validation"
  }
}
```

In our framework these map cleanly to existing typed fields (per [`aira-comparison.md`](../aira-comparison.md) §1.1):

| AIRA's proposed field | Our equivalent | Where in our schema |
|---|---|---|
| `failureContext` | `kind` + `functional_area` | top-level + multi-axis dim |
| `errorFamily` (weight 0.30) | `kind` enum value or new field | could add to `incidents/v1.json` |
| `failureType` (weight 0.25) | `kind` enum value or new field | same |
| `resourceType` (weight 0.20) | `resources` (multi-axis dim) | already exists |
| `operationArea` (weight 0.15) | `functional_area_all` | already exists |
| `failureStage` (weight 0.10) | possible new field | new |

**Questions for the AIRA team — `ops_incidents`:**

1. **Are these fields equivalent?** Walk through the mapping above. Anything that doesn't map cleanly?
2. **Do we add `failureType`, `errorFamily`, `failureStage` as explicit fields in `incidents/v1.json`, or rely on tags in the existing dimensions?**
3. **What's the controlled vocabulary for `errorFamily`?** Examples: `null-pointer`, `timeout`, `auth-failure`, `oom`, `disk-full`, `network-partition`, `db-deadlock`, `quota-exceeded`. Lock the enum.
4. **What's `failureStage`?** AIRA's example was `resource-validation`. Are stages a standard enum (e.g., `init`, `pre-check`, `execution`, `commit`, `cleanup`)?
5. **`severity` enum check:** we use `[sev1, sev2, sev3, sev4, near-miss]`. AIRA's tickets — is that the enum, or do you also use `Sev0`, `Sev5`, `severity-1` style?
6. **`stack` field:** we don't capture this in the incident schema today, but AIRA's retrieval applies stack as a soft filter. Should `stack` be:
   - (a) a tag on every incident extracted from the Jira ticket's `stack` field?
   - (b) inferred from environment / cluster reference in the ticket body?
   - (c) ignored at extract time, applied as a retrieval-time filter only?
7. **`resources_affected` vocabulary:** is `[POD, PODDB, EXADATA, BLOCK_VOLUME, NETWORK]` complete? What about `LOAD_BALANCER`, `VAULT`, `OBJECT_STORAGE`, `STREAMING`, `KMS`, etc.?
8. **`tenant_ids`:** is the tenant ID format consistent (e.g., `tenant-NN`) or varies?
9. **Body content:** today we put a JSON-encoded summary in `body`. AIRA stores the full Jira-shaped JSON in `CHUNK_TEXT`. Do we want to (a) match AIRA's pattern (full ticket JSON in body) for direct compatibility, or (b) keep our cleaner typed extraction (current default)?

---

# KB 2: `ops_runbooks` (wiki) — operational procedures

## What we currently extract

Per [`ops-eng/runbooks/v1.json`](../../../framework/parsers/schemas/ops-eng/runbooks/v1.json):

```json
{
  "title":              "string ≤200 (REQUIRED)",
  "trigger":            "string ≤300 (REQUIRED) — when this runbook applies",
  "preconditions":      "array of strings",
  "steps":              "array of strings (REQUIRED)",
  "rollback":           "string ≤500 — how to undo if needed",
  "functional_area":    "string — REFRESH/PROVISIONING/PATCHING/DR",
  "resources":          "array of strings — POD/PODDB/...",
  "escalation_contact": "string"
}
```

## Sources we assumed
- Confluence space `OPS-RUNBOOKS`
- Git repo `org/ops-runbooks` (markdown files)

**Questions for the Ops Eng team — `ops_runbooks`:**

1. **Source authority:** Are runbooks primarily in Confluence or git? Both? If both, which is canonical?
2. **Are runbooks **structured** (sections like "Trigger", "Steps", "Rollback") or **prose**?** Structured is much easier to extract; if prose, we'll need wider extraction tolerance.
3. **`trigger`:** is there a controlled list of triggers (alerts, on-call pages, customer escalations) or is it free-form?
4. **`preconditions`:** worth extracting or noisy?
5. **What's missing?** Common runbook fields we considered:
   - `expected_duration_minutes` (helps incident commander pace)
   - `permissions_required` (which teams/individuals can execute)
   - `validation_after` (how to confirm rollback succeeded)
   - `last_rehearsed_date` (DR runbooks especially)
   - `automation_hooks` (whether part of the runbook is scriptable)
6. **Per-FA runbook split:** are there standalone REFRESH runbooks vs PATCHING runbooks vs DR runbooks, or is the same runbook tagged for multiple FAs?
7. **Internal vs vendor runbooks:** any Oracle-vendor runbooks in scope? (Different `classification` if so.)

---

# KB 3: `ops_postmortems` (vector + wiki hybrid) — incident retrospectives

## What we currently extract

Per [`ops-eng/postmortems/v1.json`](../../../framework/parsers/schemas/ops-eng/postmortems/v1.json):

```json
{
  "incident_id":           "string (REQUIRED)",
  "summary":               "string ≤800 (REQUIRED)",
  "timeline":              "array of {ts, event}",
  "contributing_factors":  "array (REQUIRED)",
  "what_went_well":        "array",
  "what_went_poorly":      "array",
  "action_items":          "array of {description, owner, due}"
}
```

**Questions for the Ops Eng team — `ops_postmortems`:**

1. **Source location:** Confluence space `OPS-PM` is a guess. Confirm or correct.
2. **Postmortem template:** does your team use a standard template? If yes, link us — schemas should match.
3. **Are postmortems **always** linked to a Jira incident**, or do some standalone "near-miss" reviews exist?
4. **Action-item tracking:** are action items in a side-system (Jira tickets) that we should cross-link, or just text in the doc?
5. **Blameless framing rules:** any words/phrases we should avoid extracting (e.g., personal names in `contributing_factors`)? Compliance with internal blameless-postmortem policy.
6. **Are postmortems shared with customers** (`classification: public`?) or strictly internal?

---

# KB 4: `ops_dependencies` (graph) — derived

## What this is
A **derived graph** built from edges that other ingestion paths produce — primarily from `ops_incidents`'s `incident → service → owner → tenant` edges, plus the FAaaS resource ontology in `shim_faaas`.

Nothing is extracted directly here; this KB is *materialized* from the others.

**Questions for the Ops Eng team — `ops_dependencies`:**

1. **Cross-service deps:** beyond what we get from incident edges, do you maintain an explicit service-dependency catalog? (E.g., a YAML / database / MS Visio map.)
2. **Direction:** for "blast radius" queries, is *who-depends-on-X* (downstream) or *what-X-depends-on* (upstream) the more frequent question?
3. **Edge weights:** should some dependencies be marked as critical (sev1-blast) vs nice-to-have? Useful for ranking blast-radius results.
4. **Owner-team relationship:** are services 1:1 with owner teams or 1:N (multiple teams own a service)?

---

# KB 5: `ops_fleet_state` (sql_passthrough) — live UDAP/Sentinel

## What this is
Read-through to your existing UDAP/Sentinel views. **No ingestion**; the framework wraps allowlisted views as the `query_fleet` and `text_to_sql` MCP tools.

Default views in [`framework/retrievers/fleet_views.yaml`](../../../framework/retrievers/fleet_views.yaml):
- `pod_health` — per-POD health status
- `restart_counts` — pod restart counts by team/week
- `refresh_progress` — PODDB refresh state
- `fleet_inventory` — live customer instances
- `patching_status` — per-resource patching state

**Questions for the Ops Eng team — `ops_fleet_state`:**

1. **View allowlist:** are the 5 views above the right starting set? What other views get queried in incidents commonly?
2. **View ownership:** UDAP team owns the views; do they need to bless additions to the allowlist?
3. **Per-tenant scoping:** any privacy requirements (e.g., a query may only return rows for tenants the consumer agent is authorized to see)? This becomes a Phase 4 ACL concern but worth noting now.
4. **Real-time vs cached:** are these queries always live, or does some get cached for cost reasons?
5. **`text_to_sql` guardrails:** we hard-block DDL/DML and any non-allowlisted table reference. Is that the right policy, or do you want stricter (e.g., only `SELECT` against materialized views, never base tables)?

---

# AIRA migration & integration

This is the most distinctive section vs. PM/TPM — there's an existing production system to coordinate with.

## What stays unchanged for now
- AIRA's `<SERVICE>_DATASETS` + `<SERVICE>_DATASETS_VECTOR` tables continue to operate
- AIRA's existing retrieval path (`KB_VECTOR_SEARCH` DB function + Java `ContextBuilderNode`) keeps working for current consumers
- AIRA's eval harness keeps reporting metrics on the existing path

## What changes when our framework lands
- The framework writes a **parallel** copy of new incidents to `kb_incidents` (our converged schema)
- New ingestion goes through both paths in parallel for the duration of Phase 1 — letting us verify correctness apples-to-apples
- The framework's `vector_search` MCP tool is exposed; consumers can opt in
- AIRA's own queries gradually shift to the framework once eval gate is green

## Questions for the AIRA team — migration

1. **Dual-write window:** are you OK with the framework dual-writing for ~2 weeks during Phase 1 backfill validation? Storage cost is small.
2. **Cutover trigger:** what eval-gate result would convince you to flip a consumer to the framework's `vector_search`? (Recall@5 ≥ 80% on 25-question set is our default; you may want stricter.)
3. **Existing 50K-ticket backfill:** can you share a Jira filter that captures the same tickets currently in `<SERVICE>_DATASETS_VECTOR`? We backfill once and verify identity.
4. **Eval queries:** can you share ~50 query/expected-citation pairs from your existing eval harness? We use 25 for our gold set + 25 for hold-out validation. (Per [ADR-005 amendment 1](../adr/ADR-005-eval-harness.md).)
5. **Score threshold:** AIRA filters at score ≥ 0.50. Is that tuned per-tenant, per-service, or globally? We default to global; happy to override if you want.
6. **Char cap:** AIRA caps context at 50K chars before sending to GenAI. We've adopted the same cap (per [ADR-007 amendment 1](../adr/ADR-007-persona-context-skill.md)). Is 50K still right, or has experience suggested different?
7. **Stack handling:** AIRA's stack is **soft preference** (×0.90 multiplier). Per [ADR-013](../adr/ADR-013-filter-strictness.md) we keep stack as soft. Any reason to make it hard for some flows?
8. **`PROPERTIES`/tags work:** is the AIRA team actively planning to add this to existing tables, or paused? If paused, our framework can effectively ship that improvement first.

---

# Wider-conversation questions for ops_eng leadership

1. **Aira-as-consumer:** Aira's agent code currently calls `KB_VECTOR_SEARCH` directly. After Phase 1 exit, would it call our `/mcp/tools/call` endpoint, or do you want us to expose the same interface AIRA's existing client uses?
2. **DR drill data:** does ops_eng knowledge include DR drill outcomes (separate from incidents)? Worth a 6th KB?
3. **Vendor incidents:** Oracle Cloud Infrastructure's own incidents (the platform underneath) — are they part of `ops_incidents` or out of scope?
4. **Customer-impact scoring:** is there an internal CIS (customer impact score) that should be a top-level field?
5. **On-call shift handoff:** is there a structured "shift report" doc that should be its own KB? (Bridges weekly_summary territory if so — discuss with TPM team.)
6. **Compliance interactions:** do incidents that touch tenant data require special `classification` (e.g., `restricted`)? Default is `internal`.
7. **Knowledge-quality bar:** what % of Jira incidents have a clean root-cause field today? If many are "we don't know yet" — the LLM extraction will reflect that. Do we filter those out at parse time?

---

# Gold-set authoring (Phase 1 exit gate)

This is **the** gate. The framework ships when it hits ≥80% recall@5 + ≥0.85 faithfulness on a 25-question gold set of real ops_eng questions with known-correct expected citations.

## Format

`eval/gold_sets/ops-eng.jsonl` (already exists with 5 placeholders):

```jsonc
{
  "id": "opse-q-001",
  "persona": "ops_eng",
  "question": "What incidents touched auth-service in the last 30 days?",
  "expected_citations": ["jira://INC-2026-001234", "jira://INC-2026-001288"],   // real INC IDs
  "expected_answer_includes": ["auth-service"],
  "tags": ["service:auth", "kind:incident_history", "time-window:30d"],
  "filters": {
    "services": {"values": ["auth-service"], "strictness": "hard"},
    "stack":    {"values": ["prod"], "strictness": "soft"}
  },
  "min_recall_at_5": 0.8,
  "min_faithfulness": 0.85
}
```

## How to bootstrap (per [ADR-005 amendment 1](../adr/ADR-005-eval-harness.md))

1. **Ask AIRA team for ~50 query/expected-citation pairs** from their existing eval harness
2. **Pick the 25 most representative** (mix of question types — see below)
3. Replace the 5 placeholders in `eval/gold_sets/ops-eng.jsonl`

## Question-type mix we want (25 total)

| Type | % | Example |
|---|---|---|
| Service-scoped historical | 30% | "Incidents on auth-service in last N days" |
| Error-code-scoped | 15% | "Resolutions for ORA-1017 errors on tenant-99" |
| Blast-radius / dependency | 15% | "What's affected if customer-events Kafka topic goes down" |
| Severity + time window | 10% | "Sev1 incidents in week 17 affecting payments" |
| Resource-state queries | 10% | "Stuck PODDB refreshes right now" |
| Postmortem patterns | 10% | "What did we learn from cross-region failover incidents" |
| Runbook lookups | 10% | "How do I roll back a stuck PODDB refresh" |

## Expected citations format

| Source | URL pattern |
|---|---|
| Jira incidents | `jira://INC-NNNN` or `jira://OPS-NNNN` |
| Confluence runbooks | `confluence://OPS-RUNBOOKS/<page-id>` |
| Confluence postmortems | `confluence://OPS-PM/<page-id>` |
| UDAP views | `udap://<view_name>` |
| Graph results | `urn:faaas:resource:<id>` or `urn:faaas:service:<id>` |

---

# Process — proposed workflow

1. **Workshop (90 min)** with AIRA team + Ops Eng leads
   - Walk through this workbook
   - Reach decisions on the schema additions (`failureType`, `errorFamily`, `failureStage`)
   - Confirm sources, vocabularies, fleet view allowlist
   - Identify schema owners (one for incidents, one for runbooks, one for postmortems)
   - Migration commitments: dual-write window, cutover trigger

2. **Schema iteration (1 week)**
   - Schema owners refine `incidents/v1.json`, `runbooks/v1.json`, `postmortems/v1.json`
   - Add controlled vocabularies (enums) where free-form was a guess
   - AIRA team reviews against their existing schema for compatibility

3. **AIRA gold-set bootstrap (parallel, 1 week)**
   - AIRA team exports ~50 query/citation pairs from existing eval harness
   - Engineering picks 25; populates `eval/gold_sets/ops-eng.jsonl` with real values

4. **Dry-run (1 day)**
   - `kb-cli ingest --dry-run --sample 5 framework/persona_builders/ops-eng.yaml`
   - AIRA team reviews extraction output side-by-side with their existing extraction
   - Iterate

5. **Backfill (1-3 days, depending on rate-tier)**
   - `kb-cli backfill --source jira --since 2023-01-01 --persona ops-eng`
   - Resumable; cost-projected before start

6. **Eval run (1 day)**
   - `kb-cli eval framework/persona_builders/ops-eng.yaml`
   - Pass/fail vs gold-set thresholds
   - Iterate on parser schema if recall < target

7. **Promote + cutover plan (1 week)**
   - `kb-cli promote framework/persona_builders/ops-eng.yaml`
   - AIRA + framework parallel-write for 2 weeks
   - Cutover triggered by AIRA team's confidence + a stable green eval baseline

8. **Production monitoring (ongoing)**
   - Weekly cost report
   - Per-question-type recall trend (alert on regression)
   - Drift report for new resource/error-code values

---

# Quick checklist for team leads

Before our workshop, please come prepared to answer:

**Schema (`ops_incidents`)**
- [ ] Confirm severity enum (`sev1`-`sev4` + `near-miss`?)
- [ ] Confirm resource enum (`POD/PODDB/EXADATA/BLOCK_VOLUME/NETWORK` + others?)
- [ ] Decide on `failureType`, `errorFamily`, `failureStage` fields (per AIRA's roadmap)
- [ ] Decide whether `stack` is captured at extraction time or applied only at retrieval
- [ ] Tenant ID format

**Schema (`ops_runbooks`)**
- [ ] Source authority (Confluence vs git)
- [ ] Are runbooks structured or prose?
- [ ] Standard fields beyond what we drafted

**Schema (`ops_postmortems`)**
- [ ] Confluence space name (we guessed `OPS-PM`)
- [ ] Standard postmortem template fields
- [ ] Internal-only or shared with customers (classification)

**Fleet (`ops_fleet_state`)**
- [ ] Allowlisted views — confirm or extend the 5 starter views
- [ ] UDAP team gating for allowlist additions
- [ ] Per-tenant ACL requirements

**AIRA migration**
- [ ] Existing eval harness 50 query/citation pairs (export to us)
- [ ] Dual-write window OK?
- [ ] Cutover trigger criteria
- [ ] Score threshold (we default to ≥ 0.50)
- [ ] Char cap (we default to 50K)

**Process**
- [ ] Schema owner per KB (incidents / runbooks / postmortems)
- [ ] Reviewer for dry-run output
- [ ] Question authors for the 25-question gold set

---

# Resources

- **Per-persona starter pack** (already in repo, status: draft):
  - Builder config: [`framework/persona_builders/ops-eng.yaml`](../../../framework/persona_builders/ops-eng.yaml)
  - Schemas: [`framework/parsers/schemas/incidents/v1.json`](../../../framework/parsers/schemas/incidents/v1.json), [`ops-eng/runbooks/v1.json`](../../../framework/parsers/schemas/ops-eng/runbooks/v1.json), [`ops-eng/postmortems/v1.json`](../../../framework/parsers/schemas/ops-eng/postmortems/v1.json)
  - Gold set: [`eval/gold_sets/ops-eng.jsonl`](../../../eval/gold_sets/ops-eng.jsonl) — replace 5 placeholders with 25 real questions
  - Fleet allowlist: [`framework/retrievers/fleet_views.yaml`](../../../framework/retrievers/fleet_views.yaml)

- **AIRA-specific reference docs:**
  - **[`../aira-comparison.md`](../aira-comparison.md)** — full extraction + retrieval comparison; what we borrow, what we improve, the migration story
  - Source: `docs/raw/aira-vector-search-detailed-explained (1).html` — the original AIRA-team walkthrough

- **Framework architecture (deeper read):**
  - PDD: [`pdd/PDD-Knowledge-Builder-Framework.md`](../pdd/PDD-Knowledge-Builder-Framework.md) (also `.docx`)
  - ADR-002 — Storage shape per data type
  - ADR-007 (v2) — Persona context skill contract; structured synthesis output (incident_rca matches AIRA's `Root_Cause / Resolution / Similar ticket`)
  - ADR-008 — Functional-area + resources dimensions
  - ADR-012 — In-DB embedding via DBMS_VECTOR (the AIRA pattern for embeddings)
  - ADR-013 — Filter strictness (hard / soft-with-multiplier per intent)
  - ADR-014 — LLM access via OCI Generative AI Inference (also AIRA-pattern)

- **Phase 1 dev guide & runbook:**
  - [`../engineering/dev-guide.md`](../engineering/dev-guide.md)
  - [`../engineering/runbook.md`](../engineering/runbook.md)

---

*This workbook captures the AIRA team's existing wisdom, applies AIRA's own roadmap (PROPERTIES/tags), and points at the eval gate that determines Phase 1 ship. The migration story keeps AIRA's existing system stable while the framework comes online in parallel — no flag-day cutover required.*
