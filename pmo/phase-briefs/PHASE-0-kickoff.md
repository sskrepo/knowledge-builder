---
title: PHASE 0 — Kickoff Brief
phase: 0
status: awaiting-external-setup
filed: 2026-05-04
owner: tpm
contributors: [architect, pm]
tags: [phase:0, kickoff]
---

# PHASE 0 — Setup — Kickoff Brief

## 📋 Phase summary
Phase 0 nails down the tech-stack baseline (Oracle 23ai Autonomous DB + OpenAI + LangGraph on OCI), the §6 interface contract, the persona-builder config schema, and the eval harness skeleton. **No production code in Phase 0.** Exit gate: ADRs approved by you + one passing eval gold-set entry. See [phases.md](../phases.md).

## 🔴 EXTERNAL DEPENDENCIES — ONLY YOU CAN DO THESE

These have real-world lead time. Start in parallel with the team's documentation work so Phase 1 isn't gated.

### 🚨 Critical path — start within 24 hours

#### 1. Oracle 23ai Autonomous Database instance (dev tier)
- **What:** Provision an Autonomous Database instance in OCI for the framework's converged store. Dev tier is fine; production sizing decided at Phase 1 entry.
- **Why:** Phase 1 cannot start without somewhere to write `kb_incidents`. ADR-002's schemas need a real DB to be created in.
- **Lead time:** ~30 min provisioning; minutes for initial schema setup.
- **Your time investment:** 30–60 min initial, then nothing.
- **How:**
  1. OCI Console → Oracle Database → Autonomous Database → Create
  2. Workload type: Transaction Processing (handles vector + graph + JSON)
  3. Version: 23ai (required for native vector search)
  4. Network: VCN-attached if you want private; public + ACL is fine for dev
  5. Save the wallet zip + admin password to OCI Vault (do not email/share)
- **Where:** [OCI Console](https://cloud.oracle.com/db/adb)
- **Done when:** You can `sqlplus admin@<service>` and connect.
- **Deliver to agents:** Vault path for the wallet + admin password (e.g., `vault://kb/adb-admin`).

#### 2. OpenAI API access — production tier
- **What:** OpenAI org with access to `gpt-4o` and `text-embedding-3-large`. Billing set up.
- **Why:** Every parser and synthesizer call goes through this. Phase 1 cannot run without it.
- **Lead time:** Same-day if Oracle has already certified your account; up to 1 week if procurement has to engage.
- **Your time investment:** ~1 hour (org creation, billing, key generation).
- **How:**
  1. Confirm with Oracle's vendor management that OpenAI usage is approved for this project (DECISION-003 references certification).
  2. Create / locate the OpenAI org for this project.
  3. Set spend cap (suggest $200/month for v1) and rate-limit tier.
  4. Generate a project-scoped API key.
  5. Store key in OCI Vault.
- **Where:** [OpenAI Platform](https://platform.openai.com/)
- **Done when:** A `curl` against `/v1/embeddings` returns a 3072-dim vector.
- **Deliver to agents:** Vault path (e.g., `vault://kb/openai-api-key`).

#### 3. OCI Vault setup
- **What:** A Vault + master key for the framework's secrets.
- **Why:** Every credential above needs a home. Hard-coded secrets are an automatic Phase 1 blocker.
- **Lead time:** ~30 min.
- **Your time investment:** 30 min.
- **How:**
  1. OCI Console → Identity & Security → Vault → Create
  2. Create a master encryption key (HSM-backed for prod; software-backed fine for dev)
  3. Create initial secrets: `adb-admin`, `openai-api-key`, `confluence-readonly`, `jira-readonly`, `git-readonly`
- **Where:** [OCI Vault](https://cloud.oracle.com/security/kms/vaults)
- **Done when:** Each secret resolves with the right scope.

### 🟡 Mid-phase — start within 1-2 weeks

#### 4. Confluence read-only API token
- **What:** A Confluence service account or API token with read access to the spaces relevant for v1 (incident-related; PM/TPM spaces deferred to Phase 3).
- **Why:** Phase 1 incident KB pulls log links/related design docs; Phase 3 PM/TPM ingestion will need broader access.
- **Lead time:** ~1 week if your Confluence is locked down (workplace IT request).
- **How:** Confluence admin creates an API token scoped to the relevant spaces. Store in OCI Vault as `confluence-readonly`.

#### 5. Jira read-only API token
- **What:** Service account or API token with read access to incident projects (`P2T`, `INC`).
- **Why:** Phase 1 ingestion source.
- **Lead time:** ~1 week.
- **How:** Same flow as Confluence. Store in Vault as `jira-readonly`.

#### 6. OCI Object Storage bucket
- **What:** Bucket for raw source dumps, parser audit artifacts, and eval run artifacts.
- **Why:** ADR-001 references this for raw dumps; spec §10 cost telemetry depends on it.
- **Lead time:** ~15 min.
- **How:** OCI Console → Object Storage → Buckets → Create. Name: `kb-raw-{env}`. Lifecycle policy: 90-day retention for raw dumps.

### 🟢 Nice-to-have / can wait

#### 7. OCI Streaming (Kafka) topics
- Defer to Phase 1 if webhooks suffice initially.

#### 8. OCI Functions app
- Defer to Phase 1 — only needed when ingestion workers go live.

## 🟡 What agents are doing in parallel

| Work | Owner | Status |
|---|---|---|
| ADRs 001–005 (tech-stack baseline, storage shape, core interfaces, persona-builder, eval harness) | Architect | ✅ Drafted by TPM (acting); awaiting your Gate-1 review |
| project-overview, personas, 6 module pages | PM | ✅ Drafted by TPM (acting); awaiting your Gate-1 review |
| Persona-builder + extraction-schema templates | Architect | ✅ Drafted at `framework/persona_builders/_template.yaml` and `framework/parsers/schemas/_template.json` |
| Incident extraction schema v1 | Architect | ✅ Drafted at `framework/parsers/schemas/incidents/v1.json` |
| Eval gold-set seed (5 incident questions) | QA | ✅ Drafted at `eval/gold_sets/incidents.jsonl` |
| Phase 1 backlog | PM (Phase 0 close) | ⏳ Pending your Phase-0 sign-off |

## ✅ Already in place from prior phases
- Repo bootstrapped from dev-agent-team v0.1.5
- Spec + meeting notes registered in `manifests/raw_sources.csv`
- CLAUDE.md + KICKOFF.md customized for the framework
- `docs/wiki/persona-knowledge-builder.md` seeded with the per-persona builder concept
- DECISIONs 001–004 filed (decided)

## 📋 Phase exit criteria
- [ ] You approve ADRs 001–005 (Gate 1)
- [ ] You approve PM's project-overview + personas + 6 module pages (Gate 1)
- [ ] Oracle 23ai Autonomous DB instance provisioned (item 1)
- [ ] OpenAI API key issued and stored in OCI Vault (items 2 + 3)
- [ ] One passing entry on the seed eval gold-set (placeholder until DB + API are live; technical exit)
- [ ] Phase 1 backlog drafted (PM)

## 🔭 Heads-up for the NEXT phase (Phase 1)
- **Confluence + Jira tokens** (items 4 + 5) — if not done by Phase 0 exit, Phase 1 incident ingestion is blocked.
- **Allowlist for `text_to_sql`** (Phase 2 but worth thinking about now) — which UDAP/Sentinel views become "knowledge"? PM + Architect to draft the policy in Phase 1 free time.
- **Open problem §8.2** (code accessibility for remote agents) — Architect to file DECISION-005 at Phase 2 kickoff. Not Phase 1 critical path but flag now to avoid surprise.
