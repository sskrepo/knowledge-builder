---
title: PM & TPM Persona Onboarding Workbook
audience: PM team leads, TPM team leads
purpose: A self-contained workbook for refining "what knowledge to extract" from PM and TPM sources
created: 2026-05-06
owner: pm
tags: [onboarding, persona, pm, tpm, workbook]
status: ready-for-team-input
---

# PM & TPM Persona Onboarding Workbook

> **Send this document to the PM and TPM team leads.** It explains what the Knowledge Builder Framework will do for them and asks the specific questions they need to answer so engineering can ship their persona's KB in Phase 3.

---

## Why this matters (1-paragraph context)

The Knowledge Builder Framework will ingest your team's sources (Confluence, Jira, etc.), run them through an LLM-driven parser, and produce a **persona-specific knowledge base** that downstream agents (Aira, internal portals, future per-persona agents) can query through one uniform interface — with citations, eval gates, and cost telemetry.

The framework provides the plumbing. **You decide what gets extracted from your raw content.** Specifically:
1. **Which sources to pull from** (Confluence space keys, Jira filters, git repos).
2. **What fields to extract** from each piece of content (a small JSON-Schema document that constrains what the LLM extracts).
3. **A small evaluation set** (5–25 example questions with expected citations) that proves the KB works.

This workbook walks through what we've drafted as a starting point, and the questions we need you to answer so we can refine the drafts before Phase 3 ingestion goes live.

---

## How extraction works (so you can shape it)

```
Raw source page (Confluence / Jira)
        │
        ▼
LLM Parser  (gpt-4o, given your JSON-Schema as a prompt constraint)
        │
        ▼
Structured ContentItem  (the fields you specified, populated by the LLM)
        │
        ▼
Knowledge base  (stored, indexed, searchable by downstream agents)
```

**The JSON-Schema you provide is the contract.** It tells the LLM exactly what fields to extract, with descriptions of each. The LLM doesn't get to decide what's important — you do.

**Three rules of thumb:**
1. **Keep schemas small.** ≤ 15 fields per schema. Larger schemas dilute LLM extraction quality.
2. **Each property's `description` is injected into the prompt.** Write them like you're briefing a smart intern.
3. **Use enums for controlled vocabularies.** "severity: ['sev1', 'sev2', 'sev3']" beats free-form "severity strings."

---

# PART 1 — Product Manager (PM) Knowledge Builder

## What we drafted as your starting point

The PM Knowledge Builder ships with **3 knowledge bases** (each is a separate logical store with its own extraction schema). All marked `status: draft` — your team refines, then we promote to `status: production`.

| KB | Storage shape | Sources we assumed | Extraction schema (draft) |
|---|---|---|---|
| `pm_briefs` | wiki (markdown + git body, metadata in DB) | Confluence space `PRODUCT` with labels `[prd, feature-brief]`; Jira `project = PM AND issuetype in (Feature, Epic)` | `pm/briefs/v1.json` |
| `pm_release_plans` | wiki | Confluence space `PRODUCT` with label `[release-plan]`; Jira `project = PM AND issuetype = "Release"` | `pm/release-plans/v1.json` |
| `pm_market_research` | vector (semantic recall) | Confluence space `PRODUCT` with labels `[research, competitive]` | `pm/research/v1.json` |

### Draft schema 1: `pm/briefs/v1.json`

What we currently extract from each PRD / feature brief:

```json
{
  "feature_name": "string (required, ≤100 chars)",
  "owner": "string — team or individual that owns this feature (required)",
  "target_release": "string — FA release id e.g. '25.01' or 'TBD' (required)",
  "personas_impacted": ["array of strings — user personas affected"],
  "summary": "string ≤800 chars — what the feature does and why (required)",
  "scope": {
    "in_scope": ["..."],
    "out_of_scope": ["..."]
  },
  "acceptance_criteria": ["array — Given/When/Then preferred"],
  "dependencies": ["array — external or other-feature dependencies"],
  "related_design_docs": ["array — URLs to design docs / ADRs / wireframes"]
}
```

**Questions for the PM team — `pm_briefs`:**

1. **Are these the right Confluence labels?** Specifically: do you actually use `prd` and `feature-brief` as labels, or different ones? Same for Jira issue types — is "Feature" + "Epic" right, or are there other types we should include (e.g., "Spike," "User Story")?
2. **Are there fields missing that you'd actively use?** Examples we considered but didn't add — let us know if any of these matter:
   - `priority` (must / should / could)
   - `phase` (which phase of the project this belongs to)
   - `revenue_impact_estimate`
   - `customer_segment` (enterprise / mid-market / SMB)
   - `regulatory_implications`
   - `metric_to_move` (the OKR / KPI this feature targets)
3. **Are the descriptions clear enough for an LLM to follow?** Read the schema as if you were briefing a new contractor. Anything ambiguous → tighten it.
4. **What controlled vocabularies should we add as enums?** Examples:
   - `target_release` could be an enum of known FA release IDs (e.g. `["24.05", "24.10", "25.01", "25.07"]`)
   - `customer_segment` enum
   - `priority` enum
5. **Do we want `personas_impacted` to be free-form, or pulled from a controlled list?** (Suggested: pull from `shim_faaas.personas` to align with the rest of the framework — pm, tpm, ops_eng, eng_mgr, etc.)
6. **Is `summary ≤ 800 chars` the right cap?** Some PRDs have rich context. Larger raises cost; smaller loses nuance.
7. **What gets a "feature" wrong?** Examples of pages that **look like PRDs but shouldn't be ingested as feature briefs** (so we add exclusion labels). E.g., archived features, internal-only experimental features, deprecated features.

---

### Draft schema 2: `pm/release-plans/v1.json`

What we currently extract from each release plan:

```json
{
  "release_id": "string ≤20 chars — e.g. '25.01' (required)",
  "target_date": "ISO date — target ship date (required)",
  "status": "enum: planning | in-progress | shipped | deferred",
  "scope_items": ["array — top-level scope items / features / programs (required)"],
  "gating_risks": ["array — risks that could delay or descope"],
  "owners": [
    {"scope_item": "feature_name", "owner": "owner_name"}
  ],
  "freeze_dates": {
    "feature_freeze": "ISO date",
    "code_freeze": "ISO date"
  }
}
```

**Questions for the PM team — `pm_release_plans`:**

1. **What are your real release IDs?** ("25.01" was a guess. Are they YY.QQ? `release-2026-Q2-fa-cloud`? Something else?)
2. **What status values do you actually use?** Our enum is `[planning, in-progress, shipped, deferred]`. Real options might include: `feature-freeze`, `code-freeze`, `deployed-to-staging`, `GA`, `cancelled`.
3. **Are there release-plan fields you'd want to query against?** Examples:
   - `regions_targeted` (NA / EMEA / APAC / global)
   - `customer_tiers_in_scope` (Tier-0 first, then expand)
   - `risk_register_link`
   - `RACI` (responsible / accountable / consulted / informed)
4. **What about a release retrospective?** Should we have a separate KB for post-release retrospectives (lessons learned, what shipped late and why)?
5. **Are release plans always in Confluence, or do some live in Jira releases / Aha! / Productboard?** (We need to know all the source systems.)

---

### Draft schema 3: `pm/research/v1.json`

What we currently extract from each market research / competitive scan:

```json
{
  "topic": "string ≤200 chars — research question or theme (required)",
  "summary": "string ≤1000 chars — what we learned (required)",
  "competitors": [
    {"name": "string", "url": "string", "approach": "string ≤300 chars"}
  ],
  "gaps": ["array — unmet customer needs"],
  "implications": ["array — what this means for our roadmap"],
  "sources": ["array — URLs"],
  "snapshot_date": "ISO date"
}
```

**Questions for the PM team — `pm_market_research`:**

1. **Do you do customer research differently from competitive research?** We lumped them. Often these are different doc types. Should we split into `pm_competitive` and `pm_customer_research`?
2. **For competitors, what fields matter most?** We have name/url/approach. Should we add: pricing, market share, customer base size, year founded, public/private?
3. **Does sentiment ever matter** — i.e., "what are customers saying about us in reviews"?
4. **Is `snapshot_date` enough**, or do you need finer time-tracking (e.g., "this is current as of FA 25.01")?

---

## PM team — questions for the wider conversation

1. **Sources we missed.** Are there PM sources we didn't include? Examples: Productboard, Aha!, Pendo, customer interview transcripts, sales call recordings, support tickets, NPS survey data, internal wiki space outside `PRODUCT`?
2. **Source ownership.** Who creates these documents day-to-day? (Helps us understand where webhook integrations should fire.)
3. **Update cadence.** Are PRDs static once shipped, or do they evolve (e.g., post-launch updates)? This affects whether re-ingestion needs to re-extract or just append.
4. **Authoring quality bar.** What % of PM docs are well-structured (have headings, owner, acceptance criteria) vs. ad-hoc? If lots are ad-hoc, the LLM extraction will struggle and we may need to add data-cleanup steps.
5. **What downstream consumers will use this KB?** PMs themselves? Sales? Customer success? Each consumer may want different fields surfaced.
6. **Privacy / classification.** Are any PM documents customer-confidential (NDA-protected) or competitor-confidential? We currently default to `classification: internal`.

---

## Gold-set seed for PM (5 starter questions)

We seeded `eval/gold_sets/pm.jsonl` with placeholder questions. **Replace these with 25 real PM questions with known-correct expected citations.** Format:

```jsonc
{
  "id": "pm-q-001",
  "question": "What's the rollout plan for customer-events feature in 25.01?",
  "expected_citations": ["confluence://PRODUCT/page/12345"],   // real page IDs
  "expected_answer_includes": ["customer-events", "25.01"],
  "tags": ["release:25.01", "feature:customer-events"],
  "min_recall_at_5": 0.80,
  "min_faithfulness": 0.85
}
```

**Ask the PM team to brainstorm 25 real questions** they'd want to ask their KB. Pick a mix:
- Specific ("What's planned for 25.01?")
- Cross-feature ("Which features touch the auth service?")
- Strategic ("How are we differentiating from Competitor X?")
- Status-checking ("What's blocked right now?")

Each question needs a **known-correct expected citation** — the actual page that should be returned when that question is asked.

---

# PART 2 — Technical Program Manager (TPM) Knowledge Builder

## What we drafted as your starting point

The TPM Knowledge Builder ships with **3 knowledge bases**:

| KB | Storage shape | Sources we assumed | Extraction schema (draft) |
|---|---|---|---|
| `tpm_weekly_ops` | wiki | Confluence space `TPM` with labels `[weekly-ops, ops-summary]` | `tpm/weekly-ops/v1.json` |
| `tpm_ecars` | wiki | Confluence space `TPM` with labels `[ecar, compliance]` | `tpm/ecars/v1.json` |
| `tpm_dependencies` | wiki | Confluence space `TPM` with labels `[dependency, cross-team]`; Jira `project = OPS AND labels = cross-team-dependency` | `tpm/dependencies/v1.json` |

### Draft schema 1: `tpm/weekly-ops/v1.json`

What we currently extract from each weekly ops summary:

```json
{
  "week_id": "string ≤12 chars — ISO week e.g. '2026-W17' (required)",
  "summary": "string ≤800 chars — headline narrative",
  "top_incidents": [
    {
      "incident_id": "INC-...",
      "service": "service name",
      "severity": "enum: sev1 | sev2 | sev3 | sev4 | near-miss",
      "summary": "string"
    }
  ],
  "blockers": ["array — cross-team blockers needing exec attention (required)"],
  "exec_asks": ["array — specific asks for executives"],
  "services_touched": ["array — service ids"],
  "metrics": {
    "any_metric_name": "number"
  }
}
```

**Questions for the TPM team — `tpm_weekly_ops`:**

1. **What's a "week" for you?** ISO week (W17), calendar week (May 5), or fiscal/ops cadence (Sprint-22)? Lock the format.
2. **Are weekly summaries authored in Confluence, or in another tool (Slack post, email digest, Notion, in-meeting docs)?**
3. **What metrics do you actually track week-to-week?** Examples: incident count by severity, MTTR per service, customer-impacting hours, pages by team, on-call escalations, capacity utilization, deploys completed. Tell us the 5–10 metrics that matter and we'll lock the keys.
4. **Are there standard sections we missed?** (e.g., "what's coming next week," "kudos," "exec callouts," "vendor updates," "on-call rotations.")
5. **How do you reference incidents in summaries?** ID-only ("INC-12345") or with embedded summary? This affects whether we need to cross-link.
6. **Region / service partitioning.** Do you publish ONE global weekly summary or per-region / per-service? (Affects how we shape the corpus.)

---

### Draft schema 2: `tpm/ecars/v1.json` (Engineering Change Approval Request — compliance/risk exceptions)

What we currently extract from each ECAR:

```json
{
  "ecar_id": "string ≤30 chars (required)",
  "scope": "string ≤500 chars — what is excepted and where (service / tenant / region) (required)",
  "risk_level": "enum: low | medium | high | critical (required)",
  "mitigation": "string ≤500 chars — compensating controls",
  "owner": "accountable individual or team (required)",
  "approver": "who approved the exception",
  "expires": "ISO date",
  "tenants_affected": ["array of tenant IDs"]
}
```

**Questions for the TPM team — `tpm_ecars`:**

1. **What's the actual ECAR ID format?** ECAR-2026-018? RC-... (Risk Compliance)? RFC-...?
2. **What risk-level vocabulary do you use?** Our enum is `[low, medium, high, critical]`. You may use SEV (1-4), tier (T0-T3), or DREAD scoring.
3. **What approval levels are in play?** Single approver, dual sign-off, multi-team? Should we model that?
4. **Are there fields we're missing?**
   - `framework_violated` (SOC2 / ISO27001 / PCI / HIPAA / internal-policy)
   - `business_justification`
   - `linked_risk_register_id`
   - `review_cadence` (weekly / monthly / quarterly / on-trigger)
   - `exit_criteria` (what conditions close the ECAR)
5. **Is there an ECAR lifecycle status field we should track?** (proposed / approved / active / expired / superseded / cancelled)
6. **Privacy / classification.** Are ECARs typically `restricted` (limited audience)? We default to `internal`.

---

### Draft schema 3: `tpm/dependencies/v1.json`

What we currently extract from each cross-team dependency record:

```json
{
  "initiative": "string ≤100 chars (required)",
  "depends_on": [
    {
      "target": "what / who is the dependency",
      "kind": "enum: initiative | team | deliverable | approval",
      "eta": "ISO date"
    }
  ],
  "blocked_by": ["array of strings"],
  "owner": "string",
  "status": "enum: green | yellow | red"
}
```

**Questions for the TPM team — `tpm_dependencies`:**

1. **Where do dependencies actually live?** Confluence pages? Jira links? A central tracker? An Excel spreadsheet?
2. **Is "dependency" the right unit?** Should we split into:
   - `tpm_dependencies_active` (currently blocking)
   - `tpm_dependencies_history` (resolved; useful for retros)
3. **What dependency kinds do you actually have?** Our enum is `[initiative, team, deliverable, approval]`. Real ones might include: vendor-delivery, regulatory-approval, customer-decision, infrastructure-readiness, hiring-completion.
4. **Status traffic-light is a guess.** Do you use red/yellow/green, or RAG (red/amber/green), or other (committed / at-risk / slipped / canceled)?
5. **Is ETA tracking critical?** If so, should we capture **change history** of ETAs (slips matter) rather than just the latest?

---

## TPM team — questions for the wider conversation

1. **Sources we missed.** Are there TPM sources we didn't include? Examples: program review decks, vendor-management trackers, Slack channel summaries, incident review docs, Jira release plans, Smartsheet, MS Project, Asana?
2. **Cross-org dependency tracking.** TPMs often track dependencies that span teams that don't use shared tools. How do you currently capture those? (We can ingest emails / Slack threads with adapters if needed.)
3. **Source ownership.** Who creates ECARs vs. weekly ops vs. dependency records? Are these the same people or different functional roles?
4. **Update cadence.** Weekly ops are weekly (obvious); ECARs change rarely; dependencies update… how often? This affects re-ingestion frequency.
5. **What's a "TPM agent" use case?** (When this KB is queried by an Aira-like agent, what kinds of questions does it answer? Helps us prioritize KBs.)
6. **Compliance interactions.** Some TPM content (ECARs, audit responses) is compliance-grade. Do we need stricter ACL or audit-log retention than `internal`?

---

## Gold-set seed for TPM (5 starter questions)

We seeded `eval/gold_sets/tpm.jsonl` with placeholder questions. **Replace these with 25 real TPM questions**. Format same as PM above.

**Suggestions for the kinds of questions to capture:**
- Status-checking ("What's blocking the FA 25.01 release?")
- Compliance ("Which ECARs apply to tenant-99?")
- Historical ("Summarize the last 4 weeks of ops issues for customer-events")
- Cross-team ("Who owns the dependency to the auth-service refactor?")
- Forward-looking ("What ETA slips do we have in flight right now?")

---

# PART 3 — How we'll work with you

## Process (proposed; refine with team leads)

1. **Workshop — 1 hour per persona team.**
   - Walk through this workbook
   - Get rough answers to the questions above
   - Identify any sources / fields / vocabularies that radically change the schema
   - Identify a "schema owner" on the team (someone who'll iterate the schema as we go)

2. **Schema iteration — 1 week.**
   - Persona team's schema owner edits `parsers/schemas/{persona}/<kb>/v1.json`
   - Adds / removes / renames fields
   - Adds enums for controlled vocabularies
   - Sharpens descriptions
   - Updates `persona_builders/{persona}.yaml` with real source list

3. **Dry-run validation — 1 day.**
   - Engineering runs `kb-cli ingest --dry-run --sample 5 persona_builders/<persona>.yaml`
   - Output: 5 real source items extracted into ContentItems
   - Persona team reviews extracted output: are the right fields populated? Are values accurate?
   - Iterate on schema until dry-run output is satisfactory

4. **Gold-set authoring — 1 week.**
   - Persona team brainstorms 25 real questions
   - For each: identify the actual source (page / ticket) that contains the answer
   - Engineering formats into `eval/gold_sets/{persona}.jsonl`

5. **Eval run + promote — 1 day.**
   - `kb-cli eval persona_builders/<persona>.yaml`
   - If recall@5 ≥ 80% and faithfulness ≥ 0.85 on the gold set → flip `status: draft` → `production`
   - If not, iterate (usually: schema field tweaks + better source filtering + label cleanup)

6. **Production monitoring — ongoing.**
   - Weekly cost report (your KB's $/day usage)
   - Monthly drift report (any out-of-vocab values appearing in extractions)
   - Quarterly schema review (does it still capture what matters?)

## Resources

- **Per-persona starter pack already in repo** (status: draft):
  - PM: [`framework/persona_builders/pm.yaml`](../../../framework/persona_builders/pm.yaml) and `framework/parsers/schemas/pm/{briefs,release-plans,research}/v1.json`
  - TPM: [`framework/persona_builders/tpm.yaml`](../../../framework/persona_builders/tpm.yaml) and `framework/parsers/schemas/tpm/{weekly-ops,ecars,dependencies}/v1.json`
- **Gold-set placeholders** in `eval/gold_sets/pm.jsonl`, `eval/gold_sets/tpm.jsonl`
- **Framework architecture (deeper read if curious):**
  - PDD: [`docs/wiki/pdd/PDD-Knowledge-Builder-Framework.md`](../pdd/PDD-Knowledge-Builder-Framework.md) (or .docx)
  - Persona-builder contract: [`ADR-004 v2`](../adr/ADR-004-persona-builder-config.md)
  - Functional-area + resources dimensions: [`ADR-008`](../adr/ADR-008-functional-area-and-resources.md)

---

# Appendix — Quick checklist for team leads

Before our workshop, please come prepared to answer:

**Sources**
- [ ] Confirm Confluence space keys (current draft assumes `PRODUCT` for PM, `TPM` for TPM)
- [ ] Confirm Confluence labels we should pull on (current drafts list them per KB)
- [ ] Confirm Jira projects + filters (current drafts assume `project = PM` and `project = OPS`)
- [ ] List any additional sources we missed
- [ ] Identify any sources that should be **excluded** (deprecated spaces, archived projects)

**Schema**
- [ ] Walk through each KB's schema and mark fields as: keep / remove / rename
- [ ] Identify fields we missed
- [ ] Identify enums (controlled vocabularies) where we currently have free-form strings
- [ ] Sharpen the descriptions on each property — these get fed to the LLM verbatim

**Vocabulary**
- [ ] Confirm release ID format
- [ ] Confirm severity levels
- [ ] Confirm risk levels (for ECARs)
- [ ] Confirm status values

**Process**
- [ ] Identify the "schema owner" on your team
- [ ] Identify reviewers for dry-run output
- [ ] Identify question authors for gold set

**Operational**
- [ ] Update frequency expectations (real-time? hourly? daily? weekly?)
- [ ] Privacy / classification per KB (default is `internal`)
- [ ] Any compliance constraints we need to know about

When you've worked through this with your team, send back the marked-up YAMLs / JSONs and we'll wire them into the framework for the dry-run.

---

*This workbook is a living artifact. As we iterate with PM and TPM teams, we'll update with answered questions, new questions surfaced, and lessons learned that should propagate to other persona teams (Eng Mgr, Ops Mgr, Service Owner, etc.) as they onboard in Phase 4.*
