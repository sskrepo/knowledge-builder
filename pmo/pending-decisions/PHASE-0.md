---
title: Phase 0 — Pending User Items
phase: 0
owner: tpm
updated: 2026-05-04 by tpm
status: active
tags: [pending, user, phase:0]
---

# Phase 0 — Pending User Items

**Phase status:** Setup in progress; tech-stack baselined; awaiting your Gate-1 review and external-service provisioning.
**Open count:** 🚨 3 blocking · 🟡 3 mid-phase · 📝 1 open Q · ✅ 4 done
**What's gated:** Phase 1 (incident KB end-to-end) cannot start until 🚨 items are delivered.

Canonical setup: [PHASE-0-kickoff.md](../phase-briefs/PHASE-0-kickoff.md).

## 🚨 Blocking

| # | Item | Why it matters | Where to get it | Env vars / inputs |
|---|------|----------------|-----------------|-------------------|
| 1 | Oracle 23ai Autonomous DB instance (dev tier) | All `kb_*` schemas land here per ADR-002 | [OCI Console → ADB](https://cloud.oracle.com/db/adb) | Wallet zip + admin pwd → OCI Vault `vault://kb/adb-admin` |
| 2 | OpenAI API key (gpt-4o + text-embedding-3-large) | Every parser/synthesis call (DECISION-003) | [OpenAI Platform](https://platform.openai.com/) | OCI Vault `vault://kb/openai-api-key` |
| 3 | OCI Vault + master key | Houses all credentials above | [OCI Vault](https://cloud.oracle.com/security/kms/vaults) | Vault OCID for downstream config |

## 🟡 Mid-phase

| # | Item | Why it matters | Notes |
|---|------|----------------|-------|
| 4 | Confluence read-only API token | Phase 1 incident KB pulls related design docs; Phase 3 needs broader access | Workplace IT may take ~1 week — start now. Vault path `vault://kb/confluence-readonly`. |
| 5 | Jira read-only API token | Primary Phase 1 source | Same lead time as #4. Vault path `vault://kb/jira-readonly`. |
| 6 | OCI Object Storage bucket | Raw dumps + audit artifacts + eval run blobs (ADR-001) | Bucket name `kb-raw-{env}`; 90-day lifecycle. |

## 📝 Open product questions

| # | Question | Default placeholder if no answer |
|---|----------|----------------------------------|
| 7 | Approve Phase 0 Gate 1 — ADRs 001–005 + PM ingest (project-overview, personas, 6 module pages) | **Needed before Phase 1 kickoff.** Reply: `GATE-1-PHASE-0: approved` (or per-artifact: `ADR-001: approved`). |

## 🔮 Future-phase pre-knowns

| Phase | Item | When |
|-------|------|------|
| 1 | Webhook endpoint to receive Jira change events | Late Phase 1; could start setup mid-Phase-0 |
| 2 | DECISION-005 (code accessibility for remote agents — spec §8.2) | Phase 2 kickoff; Architect to file |
| 3 | DECISION-006 (LLM wiki storage approach — spec §8.1) | Phase 3 kickoff |
| 3 | PM + TPM extraction schemas | Phase 3; persona teams own these per ADR-004 |

## ✅ Done

| When | Item | Notes |
|------|------|-------|
| 2026-05-04 | DECISION-001 — Oracle tech-stack commitment level | Full Oracle + OpenAI + LangGraph |
| 2026-05-04 | DECISION-002 — Converged DB vs §2.1 polyglot | Logical-polyglot, physical-converged |
| 2026-05-04 | DECISION-003 — LLM provider | OpenAI (Oracle-certified) |
| 2026-05-04 | DECISION-004 — Initial persona-builder set | PM + TPM + Aira |

## How to mark items done

When you deliver an item, drop a one-line note in chat (e.g., "Vault is up at OCID … with secrets X, Y, Z"). The next session will:
1. Move the row to ✅ Done
2. Reconcile dashboard + current-status
3. Append to `docs/wiki/log.md`
4. Surface what just unblocked
