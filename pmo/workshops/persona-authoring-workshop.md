---
title: Persona Authoring Workshop — Facilitation Guide
status: ready-to-use
created: 2026-05-10
owner: pm
tags: [workshop, onboarding, skill-builder, persona-authoring]
audience: Workshop facilitator (PM or TPM); persona team leads
---

# Persona Authoring Workshop — Facilitation Guide

> **Purpose of this document:** A run-of-show guide for the ~90-minute skill-builder workshop described in PDD V2 §13. One session per persona team. This guide covers Ops Engineering and PM/TPM; use the per-persona prep sheets in Part D for persona-specific talking points.

---

## Part A — Workshop Overview

### Purpose

Persona teams no longer hand-edit JSON-Schema or YAML to define what the Knowledge Builder Framework extracts for them. Instead, they bring a real artifact they produce today (a PPT, DOCX, email digest, or Confluence page) and describe what tasks they want automated. The `kb-cli skill-builder` analyzes the artifact, proposes an extraction schema and a workflow skill, and commits both to git for review.

This workshop produces:
- One extraction skill (per-KB JSON schema + persona builder YAML source block)
- One workflow skill YAML (`framework/workflow_skills/{persona}/{name}.yaml`)
- The first gold-set seed entry (the provided artifact becomes the first extraction-gold pair)
- A schema owner commitment from the persona team

### Duration

90 minutes. No break — the live Activity 3 must not be split across a break.

### Who attends

| Role | Persona team | Required |
|---|---|---|
| Facilitator | PM or TPM | Yes |
| Engineer (runs the CLI) | Dev Manager or senior engineer | Yes — must have laptop provisioned |
| Schema owner (designate) | Persona team | Yes — this person becomes the ongoing schema contact |
| Domain leads | 2-3 people who know what the team actually produces | Yes |
| Executive sponsor | Optional | No — inform afterward |

Keep the room to 6-8 people. Larger groups make Activity 3 lose focus.

### Prerequisites

Before the session can be scheduled:

1. Engineering provisioning is complete: ADB credentials, OpenAI/OCI GenAI API key in Vault, Confluence/Jira tokens (PDD V2 §13, step 1).
2. The engineer's laptop passes the laptop quickstart: `kb-cli skill-builder --self-test` returns OK.
3. The persona team has been sent their onboarding workbook and the pre-workshop checklist (Part C below) at least 3 business days in advance.
4. The persona team has selected and brought at least one example artifact to the session (see Part C).

---

## Part B — Agenda (~90 min)

### Segment 1 — Intro: what the framework does for you (10 min)

**Who leads:** Facilitator.

**Goal:** Give the persona team a mental model of the two flows before any demos. This is the orientation, not a sales pitch.

**Script outline:**

"The framework has two flows. The first is the Knowledge Builder flow — that's what we're doing today. Your team tells the system what tasks you want done and shows it an example of what you produce. The system derives the extraction schema and workflow skill from that artifact, not from you editing YAML by hand.

The second is the Consumption flow — that's runtime. Once the skills are live, downstream systems (Aira, your own queries, scheduled jobs) pull from your knowledge base through the same MCP retrieval interface every persona shares.

The key contract: the framework owns the plumbing. Your team owns what gets extracted and what gets produced."

**Slides / materials:**

- One-slide diagram: two flows from PDD V2 §1 (Knowledge Builder flow and Consumption flow)
- One-slide table: what the framework gives you vs. what you own

**Do not:** Go into storage shapes, vector vs. wiki, ADRs, or LangGraph. That is Architect territory and derails the workshop.

---

### Segment 2 — Demo: the two starter skills running on laptop (15 min)

**Who leads:** Engineer (running the CLI). Facilitator narrates.

**Goal:** Show — concretely — what a completed skill looks like and what the CLI does. Demystify before asking the team to try it.

**Demo sequence:**

1. Show the two starter workflow skill YAMLs in the editor (30 seconds each):
   - `framework/workflow_skills/ops_eng/incident_summary.yaml` — an on-request skill that produces a structured incident summary markdown
   - `framework/workflow_skills/pm/release_brief.yaml` — an on-request skill that produces a release brief DOCX

   Point out the key sections: `trigger`, `skill_card` (especially `use_when` and `example_invocations`), `requires_extractions`, `synthesis`, `delivery`, and `eval`. These are the artifacts the skill-builder synthesizes — the persona team does not author them by hand.

2. Run `kb-cli skill-builder --demo` (or equivalent quickstart demo mode if available). Walk through the interaction shape from PDD V2 §6:
   ```
   $ kb-cli skill-builder
   > I produce an exec review PPT every Friday...
   > [uploads/describes example artifact]
   ✓ Analyzing example...
   ✓ Required fields: week_id, rag_status, top_milestones, blockers, exec_asks
   ✓ Will create: extraction skill + workflow skill + link
   Confirm? (y / refine N)
   ```

3. Show the committed artifacts that resulted from a prior run:
   - `persona_builders/pm.yaml` (the source block that was added)
   - `parsers/schemas/pm/release-plans/v1.json` (the synthesized schema)
   - `workflow_skills/pm/release_brief.yaml` (the synthesized workflow skill)

4. Run one invocation against the demo data:
   ```
   $ kb-cli run workflow pm.release_brief --release-id DEMO-25.01
   ```
   Show the output file (DOCX or markdown depending on what demo mode produces).

**Time checkpoint:** Segment 2 must end by minute 25. If the CLI demo is slow to respond, narrate what it is doing rather than waiting silently.

---

### Segment 3 — Activity 1: "What tasks do you want automated?" (15 min)

**Who leads:** Facilitator. Engineer is quiet; they are listening for technical flags.

**Goal:** Surface 5-10 candidate tasks. Do not commit to all of them — this is a brainstorm. The team will pick one in Activity 3.

**Format:** Silent brainstorm 3 min → share-out round-robin → facilitator groups on whiteboard.

**Prompt to the room:**

"Think about the recurring deliverables you produce: status decks, exec briefs, incident summaries, release plans, runbook lookups, anything you produce more than once. Write one per sticky (or one per line in the doc). Don't self-edit — if it feels repetitive, write it down."

**Grouping:** Cluster into: (a) on-request queries, (b) scheduled outputs, (c) triggered outputs (alert fires, incident opens), (d) search / lookup.

**Facilitator notes for Activity 1:**

- Watch for "we want everything automated." Respond: "Great — we'll get there. Today we ship one skill end-to-end. The rest become the next skill queue. Which one would save you the most time this week?"
- Watch for confusion between querying the KB and producing a deliverable. Clarify: workflow skills produce a durable artifact (DOCX, Slack message, markdown); retrieval queries are ad-hoc lookups. Both are supported but they are different skill types.
- If the team is slow to brainstorm, seed with examples from their onboarding workbook (see Part D).
- The engineer should flag tasks that sound like they span multiple, not-yet-ingested sources — note those as Phase 2+ candidates rather than blocking Activity 1.

---

### Segment 4 — Activity 2: "Show me an example outcome" (20 min)

**Who leads:** Facilitator. Engineer pulls up the artifact on screen.

**Goal:** Ground the skill in a real artifact the team produces today. This artifact becomes the first gold-set seed entry and the basis for the skill builder's field inference.

**Format:** One or two team members walk through their artifact while the facilitator asks the structured questions below.

**Structured questions per artifact:**

1. "What is this document called? When do you produce it, and who receives it?"
2. "Walk me through each section — what data goes into this section, and where does that data come from right now?"
3. "Which sections would an agent need to fill in automatically? Which sections do you fill in manually with judgment that the agent cannot replicate?"
4. "If this document were wrong, which field would cause the most damage to the reader? That's the field where accuracy matters most — and that's where we'll spend the most eval effort."
5. "What's a question someone would ask that should return this document?"

**Engineer's role in Activity 2:** While the facilitator runs the discussion, the engineer is silently mapping artifact fields to the onboarding workbook's draft schema. Mark which fields the workbook already has, which are new, and which the team says they do not need.

**Artifacts to accept:** PPT/PPTX, DOCX, PDF, Confluence page URL, email thread (printed or screen-shared), markdown file. The skill builder can analyze all of these. Do not turn someone away because their artifact is a Slack message — screen-capture it.

**Facilitator notes for Activity 2:**

- Teams often bring a "clean" version of their artifact rather than a real one. Push: "Can you pull up the one from last Friday?" Real artifacts have the messiness that matters for schema design.
- If multiple people bring different artifacts for the same use case, that is a schema alignment problem. Do not paper over it — note the divergence and ask the team to pick one canonical format before the dry-run.
- Watch for PII in the artifact (names, customer IDs, tenant IDs). If present, note it for the schema's `classification` field (default is `internal`; restricted artifacts need a flag).
- If the team cannot produce any artifact because the task is "we want to start doing this," that is fine — pivot to Activity 3 with a natural-language description only. The skill builder will generate a schema from intent; the team provides the first gold example within one week.

---

### Segment 5 — Activity 3: Live skill-builder session (20 min)

**Who leads:** Engineer drives the CLI. Facilitator manages the conversation. Team provides guidance.

**Goal:** Produce a synthesized extraction skill + workflow skill from the artifact chosen in Activity 2. Commit to git by end of session.

**Step-by-step:**

1. Engineer launches `kb-cli skill-builder` (live, not demo mode).
2. Team describes their task in natural language when prompted: "What do you want the framework to do for you?"
3. Team uploads or pastes the artifact when prompted.
4. The CLI outputs its analysis:
   - Required fields it inferred
   - Whether existing extraction skills cover those fields
   - Proposed new extraction skill (schema + source block)
   - Proposed workflow skill (trigger type, output format, delivery target)
5. Facilitator reads the proposed fields to the room: "Does this look right? Is anything missing? Is anything wrong?"
6. Iterate (the CLI supports `refine N` to correct a specific item):
   - Missing field: "Add a field for X"
   - Wrong enum: "X should be one of [A, B, C] not free-form"
   - Wrong source: "Pull from Jira OPS too"
   - Wrong output format: "We need PPTX, not DOCX"
7. When the team says "that looks right," confirm: `y`
8. The CLI commits the synthesized artifacts to a branch, prints the PR URL.

**Engineer notes for Activity 3:**

- Have the `--dry-run` flag ready. If the live skill-builder call is slow or the team's artifact is unusually large, run with `--dry-run` first to show the analysis without committing, then commit.
- If the CLI produces a schema with more than 15 fields, surface that immediately: "The workbook guidance is ≤15 fields per schema — which 5 of these 17 would you drop?"
- If synthesis fails (timeout, API error), fall back to the template YAML and manually fill in the fields the team agreed on. The outcome of the session is the field list, not the CLI output per se.
- Copy the proposed extraction schema fields to a shared doc in real time so the team can see them without looking at the terminal.

**Facilitator notes for Activity 3:**

- Timebox the refinement loop to 10 minutes. If the team is still arguing about field names at minute 35, call it: "We'll lock the schema in the dry-run review next week — let's commit what we have and move on."
- Do not let the session end without a committed artifact. Even a rough draft committed is better than a perfect schema that exists only in the team's heads.
- The schema owner (designated before the session) is the decision-maker during refinement. If two team members disagree, the schema owner's call stands.

---

### Segment 6 — Wrap-up: review synthesized artifacts, next steps, eval gate (10 min)

**Who leads:** Facilitator.

**What to cover:**

1. **Show the committed artifacts:** Pull up the PR. Briefly show the three files (persona builder YAML source block, extraction schema JSON, workflow skill YAML). "These are exactly what engineering will use. You can comment on the PR if you see something to fix."

2. **Explain the dry-run:** "Within the next week, engineering will run the schema against 5 real samples from your source system. You'll review the extracted output — are the right fields populated with accurate values? That's the dry-run review."

3. **Gold set ask:** "We need 25 real questions with known-correct answers — the Confluence page or Jira ticket that should come back when that question is asked. The schema owner takes point on collecting these. We can work from your artifact from today as the first one."

4. **Eval gate:** "Phase graduation requires ≥80% recall@5 and ≥0.85 faithfulness on the 25-question gold set. The eval runs automatically via `kb-cli eval`. Engineering will share the result report."

5. **Timeline:** Walk through the post-workshop timeline (see Part E).

6. **Schema owner confirmation:** Get explicit verbal confirmation of who the schema owner is and their availability for the dry-run review.

---

## Part C — Pre-Workshop Checklist (for persona teams)

Send this to the persona team at least 3 business days before the session.

---

### What to bring to the workshop

**Required:**

- [ ] At least one real artifact you produce today — the messier the better. Examples:
  - A status deck or exec review PPT from last week
  - A Confluence page you drafted for an exec or stakeholder audience
  - A release brief, runbook, or incident summary
  - An email digest you send on a regular cadence
- [ ] A list of the source systems your team reads to produce that artifact (e.g., "I look at Jira OPS filter, Confluence space TPM, and a Slack channel")
- [ ] A rough list of the 5-10 recurring deliverables your team produces that feel repetitive (we use this in Activity 1)

**Nice to have:**

- [ ] The Confluence space keys and Jira project keys you use most (e.g., `PRODUCT`, `TPM`, `project = OPS`)
- [ ] Any Confluence labels you use to tag documents by type (e.g., `weekly-ops`, `prd`, `release-plan`)
- [ ] Any controlled vocabulary your team uses: severity levels, release ID format, status enums (e.g., `sev1/sev2/sev3`, `planning/in-progress/shipped`)

**Questions to have rough answers to before you arrive** (from your onboarding workbook):

- [ ] Which sources are canonical? (If runbooks are in both Confluence and git, which is the one to trust?)
- [ ] Who on your team should be the schema owner — the person who will review dry-run output and iterate the schema?
- [ ] Who will author the 25 gold-set questions after the workshop?
- [ ] Any content that should be excluded from ingestion? (Archived pages, deprecated spaces, restricted documents)

**What you do NOT need to bring:**

- JSON, YAML, or any technical configuration. That is what the workshop produces.
- A complete requirements list. Partial is fine — the tool iterates.

---

## Part D — Facilitator Notes per Activity (summary + pitfalls)

### General pitfalls

| Pitfall | What to do |
|---|---|
| "We want everything automated" | Anchor to: "Which one task would save you the most time this week?" Get one skill shipped. The rest queue up. |
| "Can you just read our Confluence space entirely?" | Clarify: ingestion without a schema extracts nothing useful. The schema is what tells the parser what matters. |
| Schema sprawl (>15 fields proposed) | Apply the workbook rule: ≤15 fields per schema. Ask: "Which 5 fields would you query most often?" Cut the rest to a later iteration. |
| Missing canonical source | Note the gap, assign to schema owner, proceed with best-guess source. Do not block the session on infra questions. |
| PII in artifact | Flag for `classification: restricted` or `internal`. Do not ingest without a classification decision. |
| Disagreement on field names | Schema owner decides. Note the disagreement in the PR for the team to resolve. |
| CLI takes too long | Use `--dry-run`. Show the analysis. Commit the artifacts manually from the agreed field list if needed. |

---

## Part E — Post-Workshop Deliverables

### Immediately after the workshop (same day)

| Deliverable | Owner | Done when |
|---|---|---|
| PR opened with synthesized extraction skill + workflow skill | Engineer | PR URL shared in the team's Slack channel |
| Session notes (decisions made, open items, schema owner confirmed) | Facilitator | Shared as Confluence page or email within 2 hours |
| Onboarding workbook updated with answers from the session | Facilitator | Updated before EOD |

### Week 1 — Dry-run review

| Deliverable | Owner | Done when |
|---|---|---|
| Engineering runs `kb-cli ingest --dry-run --sample 5 framework/persona_builders/{persona}.yaml` | Engineer | Within 3 business days of workshop |
| Dry-run extraction output shared with persona team | Engineer | Same day as dry-run |
| Schema owner reviews extraction output: fields correct? values accurate? | Schema owner | Within 2 business days of receiving output |
| Schema iteration (add/remove/rename fields based on dry-run review) | Schema owner + engineer | Within 1 week of workshop |
| PR updated and merged | Engineer | After schema owner sign-off |

### Week 1-2 — Gold set authoring

| Deliverable | Owner | Done when |
|---|---|---|
| Schema owner collects 25 real questions with known-correct citations | Schema owner | Within 2 weeks of workshop |
| Engineering formats into `eval/gold_sets/{persona}.jsonl` | Engineer | Within 1 day of receiving questions |
| The workshop artifact entered as the first gold-set seed entry | Engineer | As part of the skill-builder synthesis (automated) |

### Week 2-3 — Eval and promote

| Deliverable | Owner | Done when |
|---|---|---|
| Engineering runs `kb-cli eval framework/persona_builders/{persona}.yaml` | Engineer | Within 2 weeks of workshop |
| Eval report shared: recall@5, faithfulness, per-question breakdown | Engineer | Same day as eval run |
| If gate passes (≥80% recall@5, ≥0.85 faithfulness): flip `status: draft` → `production` | Engineer | Immediately |
| If gate fails: schema iteration + re-eval cycle | Schema owner + engineer | Until gate passes |

### Timeline summary

```
Workshop day        → PR opened with synthesized skills
+3 business days    → Dry-run on 5 real samples
+1 week             → Schema iteration complete, PR merged
+2 weeks            → Gold set authored (25 questions)
+3 weeks            → Eval run; promote if gate passes
```

---

## Part F — Per-Persona Prep Sheets

---

### F1 — Ops Engineering (Aira-equivalent)

**Context for the facilitator:**

The `ops_eng` persona is the Phase 1 exit-gate persona. AIRA's team already runs a production incident KB. This workshop is about confirming that the framework's extraction matches what AIRA already produces (and improves on AIRA's roadmap items) — not designing from scratch.

The single most important outcome of this session is: AIRA team commits to exporting ~50 query/citation pairs from their existing eval harness. The framework's 25-question gold set is drawn from those 50.

**Pre-session anchor question to send to AIRA team lead:**

"AIRA's current eval harness has existing query/citation pairs. Can you export ~50 of those before our workshop? We'll use 25 for our gold set and 25 for hold-out validation."

**Starter deliverables to demo in Segment 2:**

- `framework/workflow_skills/ops_eng/incident_summary.yaml` — incident summary on-request skill. Produces: structured markdown with RCA, resolution, similar incidents. Trigger: on-request with an INC-ID input.

**Activity 1 seed questions (if the team is slow to brainstorm):**

From the onboarding workbook, these are the recurring tasks most likely to resonate:
- "Summarize an incident for a quick RCA brief" (→ `incident_summary` workflow skill, already drafted)
- "What incidents touched auth-service in the last 30 days?"
- "How do I roll back a stuck PODDB refresh?" (→ runbook lookup)
- "What did we learn from cross-region failover incidents?" (→ postmortem patterns)
- "What's the fleet state for tenants impacted by INC-X?" (→ cross-source query, Phase 2)

**Suggested Activity 2 artifact:**

Ask the team to bring:
- A real postmortem Confluence page (best candidate — has structured sections that map directly to the `ops_postmortems` schema)
- OR a runbook from the git repo or Confluence `OPS-RUNBOOKS` space
- OR a recent incident ticket from Jira with a reasonably complete root-cause and resolution field

The incident ticket is the fastest path to Activity 3 because the extraction schema (`incidents/v1.json`) is already concrete and AIRA-proven. Start there if the team is uncertain.

**Schema decisions to drive during Activity 2 / 3:**

The onboarding workbook identifies several open questions that the session should close. The most consequential ones:

1. `failureType`, `errorFamily`, `failureStage` fields: AIRA's roadmap proposes adding these. Do we add them as explicit fields in `incidents/v1.json` now, or defer? (Recommendation: add `errorFamily` and `failureType` as enums now — these are AIRA's highest-weighted scoring dimensions: 0.30 and 0.25 respectively.)

2. `stack` field: is it captured at extraction time (tag on every incident derived from the Jira ticket's `stack` field) or applied as retrieval-time filter only? (ADR-013 keeps stack as a soft-filter multiplier — capture at extraction time.)

3. Severity enum: confirm `[sev1, sev2, sev3, sev4, near-miss]` matches AIRA's actual values in Jira.

4. Fleet view allowlist: are `[pod_health, restart_counts, refresh_progress, fleet_inventory, patching_status]` the right 5 starter views? What else gets queried during incidents?

**Activity 3 tip:**

For Ops Eng, run Activity 3 against a runbook or postmortem artifact rather than an incident ticket. The incident schema is already in git and close to correct — validating it in the dry-run is faster than re-deriving it via the skill builder. Use the session's 20 minutes to synthesize a skill for a less-covered area (runbooks or postmortems).

**AIRA migration talking points:**

Frame the dual-write approach positively: "AIRA's existing system keeps running unchanged. The framework writes a parallel copy of new incidents. We compare extraction side-by-side for 2 weeks. AIRA's team controls the cutover trigger — it fires when the eval gate is green and their team is confident."

The cutover trigger criteria should be decided in this session: "What eval-gate result would convince your team to flip a consumer to the framework's retrieval tool?" (Default: ≥80% recall@5 on 25-question gold set. AIRA team may want higher.)

**Phase 1 exit-gate reminder:**

The gold set gates Phase 1. The 25 questions must cover the question-type mix from the workbook:
- 30% service-scoped historical ("incidents on auth-service in last N days")
- 15% error-code-scoped ("ORA-1017 on tenant-99")
- 15% blast-radius / dependency ("what's affected if X goes down")
- 10% severity + time window
- 10% resource-state queries (these hit the sql_passthrough fleet KB)
- 10% postmortem patterns
- 10% runbook lookups

**Open items to close before leaving the room:**

- [ ] Schema owner for `ops_incidents` (name)
- [ ] Schema owner for `ops_runbooks` (name, can be same person)
- [ ] Schema owner for `ops_postmortems` (name)
- [ ] AIRA eval harness export — commitment to deliver 50 pairs and by when
- [ ] Dual-write window consent
- [ ] Cutover trigger criteria
- [ ] Fleet view allowlist: confirmed or extended

---

### F2 — PM / TPM

**Context for the facilitator:**

PM and TPM are Phase 3 personas. The workshop produces draft skills that will be promoted to production in Phase 3 — they are not the Phase 1 exit gate. That said, the sooner the skills are authored, the more data is being ingested and the more confident the eval run will be when Phase 3 arrives.

PM and TPM have separate onboarding workbooks but share enough structural similarity that a combined session is viable if both teams are small (≤4 people each). Run them separately if either team has strong opinions about scope that might derail the other team's 20 minutes.

**Pre-session anchor question to send to team leads:**

"What's the single recurring document you produce that you wish an agent could do for you? Bring a real copy to the workshop."

**Starter deliverables to demo in Segment 2:**

- `framework/workflow_skills/pm/release_brief.yaml` — release brief on-request skill. Produces: DOCX with release scope, gating risks, owners, and freeze dates for a given release ID. Trigger: on-request with a `release_id` input.

**PM — Activity 1 seed questions:**

From the onboarding workbook, these are the recurring tasks most likely to resonate with PM leads:
- "Release brief for 25.01 — what's the scope, who owns what, what are the gating risks?"
- "Which features touch the auth service?"
- "What's blocked right now across the roadmap?"
- "How are we differentiating from Competitor X?" (→ market research KB)
- "Status summary: what's planned for next quarter?"

**TPM — Activity 1 seed questions:**

From the onboarding workbook, these are the tasks most likely to resonate with TPM leads:
- "Summarize the last 4 weeks of ops issues for customer-events" (→ `tpm_weekly_ops`)
- "Which ECARs apply to tenant-99?" (→ `tpm_ecars`)
- "What ETA slips do we have in flight right now?" (→ `tpm_dependencies`)
- "What's blocking the FA 25.01 release?"
- "Who owns the dependency to the auth-service refactor?"

**Suggested Activity 2 artifact — PM:**

Ask PM to bring a real release brief or PRD. The `pm_release_plans` schema is the most concrete of the three PM KBs and maps cleanly to a release brief DOCX or Confluence page. Prioritize this artifact for Activity 3 — it has the most structured sections (scope items, gating risks, owners, freeze dates) and the skill builder will have the most to work with.

If the team brings a PRD (feature brief) instead, that maps to `pm_briefs`. Either works; pick whichever the team considers more valuable to automate.

**Suggested Activity 2 artifact — TPM:**

Ask TPM to bring a recent weekly ops summary from Confluence space `TPM`. This maps directly to the `tpm_weekly_ops` schema and is the highest-cadence document TPM produces (weekly), making it the best ROI for automation.

If the team produces an exec review deck (PPT), that is the canonical example from PDD V2 §6 — use it. The skill builder was literally designed around that artifact type.

**Schema decisions to drive during Activity 2 / 3 — PM:**

The PM workbook's most consequential open questions:

1. Source authority: are PRDs in Confluence space `PRODUCT` with labels `prd` and `feature-brief`? Or different labels? Lock the Confluence label set in this session.

2. Release ID format: the schema has `"25.01"` as an example. What is the actual format? (`24.05`? `release-2026-Q2-fa-cloud`?) Lock the enum if there is a finite set.

3. Separation of competitive research from customer research: the workbook asks whether to split `pm_market_research` into `pm_competitive` and `pm_customer_research`. Decide in this session.

4. Summary field length: `summary ≤ 800 chars` for feature briefs. Is that enough? Too small raises faithfulness risk; too large raises per-ingest cost. The default is reasonable — do not change without a concrete complaint.

**Schema decisions to drive during Activity 2 / 3 — TPM:**

1. Weekly summary format: authored in Confluence, or somewhere else (Slack, email, Notion)? The source adapter depends on this answer.

2. Metrics tracked week-to-week: the schema has `metrics: { any_metric_name: number }`. Lock the actual 5-10 metric keys so the schema is typed, not free-form (e.g., `incident_count_sev1`, `mttr_minutes_median`, `customer_impacting_hours`).

3. ECAR ID format: `ECAR-2026-018`? `RC-...`? Lock the format so the primary key is consistent.

4. ECAR risk vocabulary: the schema has `[low, medium, high, critical]`. Does TPM use a different scale?

5. Dependency status: the schema has `[green, yellow, red]`. Does TPM use RAG (red/amber/green) or something else?

**Gold-set guidance — PM:**

The workbook suggests a mix of:
- Specific queries: "What's planned for 25.01?"
- Cross-feature queries: "Which features touch auth service?"
- Strategic queries: "How are we differentiating from Competitor X?"
- Status-checking: "What's blocked right now?"

Each question needs a known-correct citation — the actual Confluence page or Jira ticket that contains the answer. The schema owner collects these in the week after the workshop.

**Gold-set guidance — TPM:**

The workbook suggests:
- Status-checking: "What's blocking the FA 25.01 release?"
- Compliance: "Which ECARs apply to tenant-99?"
- Historical: "Summarize the last 4 weeks of ops issues for customer-events"
- Cross-team: "Who owns the dependency to the auth-service refactor?"
- Forward-looking: "What ETA slips do we have in flight right now?"

Same requirement: each question needs a real Confluence page or Jira ticket as the expected citation.

**Open items to close before leaving the room — PM:**

- [ ] Confirmed Confluence space key (draft: `PRODUCT`) and labels per KB
- [ ] Confirmed Jira project + filter (draft: `project = PM`)
- [ ] Release ID format locked
- [ ] Decision on competitive vs. customer research split
- [ ] Schema owner (name)
- [ ] 25-question gold-set author (can be same person or different)

**Open items to close before leaving the room — TPM:**

- [ ] Confirmed Confluence space key (draft: `TPM`) and labels per KB
- [ ] Source for weekly ops summaries (Confluence, Slack, email?)
- [ ] Metric keys to lock in `tpm_weekly_ops` schema
- [ ] ECAR ID format
- [ ] Risk level vocabulary
- [ ] Dependency status vocabulary
- [ ] Schema owner (name)
- [ ] 25-question gold-set author

---

## Appendix — Quick Reference for the Engineer

### CLI commands used in this workshop

```bash
# Self-test before the session
kb-cli skill-builder --self-test

# Demo mode (uses canned data — safe for demos)
kb-cli skill-builder --demo

# Live skill-builder session
kb-cli skill-builder

# Dry-run ingest (5 samples from real source)
kb-cli ingest --dry-run --sample 5 framework/persona_builders/{persona}.yaml

# Eval run against gold set
kb-cli eval framework/persona_builders/{persona}.yaml

# Promote draft → production
kb-cli promote framework/persona_builders/{persona}.yaml
```

### Artifact locations after synthesis

```
framework/
  persona_builders/{persona}.yaml          # source block updated
  parsers/schemas/{persona}/{kb}/v1.json   # new extraction schema
  workflow_skills/{persona}/{skill}.yaml   # new workflow skill
eval/
  gold_sets/{persona}.jsonl                # first seed from artifact
```

### Eval gate thresholds (both personas)

| Metric | Threshold |
|---|---|
| recall@5 | ≥ 0.80 |
| faithfulness | ≥ 0.85 |
| field_accuracy (workflow) | ≥ 0.85 |
| delivery_success_rate (workflow) | ≥ 0.99 |

Schema field limit: ≤ 15 fields per extraction schema. Flag immediately if synthesis produces more.
