---
title: Project Dashboard
source: derived from pmo/stories, pmo/decisions, pmo/handoffs
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-03
owner: tpm
tags: [meta, live]
status: current
---

# Knowledgebase — Dashboard

**Current phase:** Phase 1-3 + V3 + laptop mode — MCP wire-protocol live + ADR-021 artifact upload + OCI bucket live
**Updated:** 2026-05-12 by backend-dev (DECISION-005 resolved — OCI kbf-uploads bucket fully wired)

## 🌅 Session update — 2026-05-12 evening (OCI kbf-uploads bucket wired + DECISION-005 resolved)

**`kbf-uploads` OCI Object Storage bucket is live and fully wired.**

- Bucket: `kbf-uploads` | Namespace: `axq4m61mcei3` | Region: `eu-frankfurt-1` | Compartment: `adp_faops_network`
- OCID: `ocid1.bucket.oc1.eu-frankfurt-1.aaaaaaaah3kxe5ldbr5sny5phrtidoaxgorfbusqxojdnicpf46lxe6ckqrq`
- PUT / GET / DELETE probe verified ✅
- All three config files (`dev.yaml`, `staging.yaml`, `prod.yaml`) updated with real namespace + compartment OCID
- `OciArtifactStore` now auto-discovers namespace via SDK `get_namespace()` in production (InstancePrincipals) — no manual config needed on OCI VMs
- `KBF_ARTIFACT_OCI_NAMESPACE` and `KBF_ARTIFACT_OCI_PROFILE` env vars added as override path
- **DECISION-005 status → resolved** ✅

**DECISION-006 decided: Option A** — ADB only, no git-sync. Option B (git-sync) deferred to ADR-023 when/if author≠approver separation of duties is introduced. Backend Dev implementing now: `KBF_SKILL_ARTIFACTS` + `KBF_ERROR_LOG` + `KBF_BUG_REPORTS` + `KBF_COST_LOG` DDL migration, `AdbSkillStore`, `AdbErrorStore`, `AdbCostStore`, `kb-cli export-skills`.

## 🌅 Session update — 2026-05-12 afternoon (ADR-021 artifact upload + DECISION-005 decided)

**ADR-021 `uploadArtifact` MCP tool fully implemented and merged to main.**

New 4th MCP tool: `uploadArtifact` accepts base64 file bytes (≤10 MB, .pptx/.docx/.md/.txt) and returns an `artifactId`. Clients send `"artifact:<filename> id:<artifactId>"` in the next `authorSkill` turn; `_handle_analyze_artifact` resolves it via `ArtifactStore` and calls `analyze_artifact(local_path)` unchanged.

**ArtifactStore: dual-mode**
- `KBF_ENV=laptop` → `FilestoreArtifactStore` (local `~/.kbf/store/uploads/`)
- `KBF_ENV=staging|production` → `OciArtifactStore` (OCI SDK, InstancePrincipals; CLI subprocess for laptop OCI override)
- No lifecycle rule — application-driven `cleanup(synth_id)` on session DONE

**skill_prompt v1.2.0** — includes "Using local files as example artifacts" upload flow.

---

## 🌅 Morning briefing — 2026-05-12 (MCP Streamable HTTP live + authorSkill end-to-end)

**MCP Streamable HTTP transport (`POST /mcp`) is live and wired into the server.**
Full JSON-RPC 2.0 protocol: `initialize`, `tools/list`, `tools/call`, SSE streaming.
Any MCP client connects without configuration in laptop mode (`KBF_ENV=laptop` bypasses auth — anonymous consumer returned automatically). Production auth failures return HTTP 401 with `WWW-Authenticate` header and exact `.mcp.json` snippet in the body.

**Three MCP bugs found and fixed end-to-end during live user testing:**
- BUG-001 (prior): `AdbSessionStore` called `Connection.execute()` (wrong API). Fixed `d36d46b`.
- BUG-002 (today): `authorSkill` start worked, continuation failed — Oracle 23ai `oracledb` auto-deserialises `CLOB IS JSON` columns to Python `dict`; `json.loads(dict)` raised `TypeError`. Fixed `7cee283` with `isinstance` guard.
- BUG-003 (today): `authorSkill` continuation still failed after BUG-002 — Oracle plain `TIMESTAMP` strips timezone on write, returns timezone-naive `datetime` on read; comparing against `datetime.now(tz=timezone.utc)` raised `TypeError: can't compare offset-naive and offset-aware datetimes`. Fixed with `_as_utc()` normalisation helper in expiry check.

**What works end-to-end via `/mcp` today:**
- `tools/list` — tool discovery
- `authorSkill` start → `synth_id` returned, session persisted to ADB
- `authorSkill` continuation — pass `synthId` + `input`, session advances state machine

**Test gap (all three bugs share the same root):** `AdbSessionStore` pool-attached path has zero unit test coverage. `test_session_store.py` only exercises stub mode (`pool=None`). Next QA action: add `TestAdbSessionStorePoolPath` using a thin oracledb cursor fake covering both `dict` and `str` return shapes for `session_data`, and naive + aware `datetime` shapes for timestamp columns.

---

## 🌅 Morning briefing — 2026-05-11 (Laptop mode: bastion + Codex CLI transport)

**Phases 1-3 + V3 + laptop mode code complete.** 160+ Python files, ~16K LOC, 574 tests passing (107 new). Two new ADRs (019-020) designed and implemented: bastion auto-reconnect for ADB tunnels and Codex CLI as MCP transport for Confluence/Jira. Split-deployment model: authorSkill runs locally (laptop with Codex CLI auth), consumption runs remotely (VM), both write to shared ADB.

**New laptop-mode capabilities:**
- **Bastion auto-reconnect** (ADR-019) — when SSH tunnel expires (3h OCI limit), framework auto-creates new bastion session via `oci` CLI, restarts tunnel, retries ADB connection. Max 3 attempts. Non-laptop mode: exponential backoff only.
- **Codex CLI MCP transport** (ADR-020) — `mode: codex_cli` in adapter config. Reads MCP server spawn command from `~/.codex/config.toml`, spawns subprocess, speaks MCP JSON-RPC over stdio. No LLM intermediary. Laptop-only (KBF_ENV guard).

---

## 🌅 Earlier briefing — 2026-05-10 (V3 deployment architecture complete)

**Phases 1-3 code complete + V3 deployment architecture designed.** 132 Python files, ~13K LOC, 22 framework modules. Everything runs against filestore + stub LLM — no external provisioning needed. PDD V3 defines the complete external API surface. OCI deployment runbook ready.

**What's runnable on your laptop right now (no ADB / OpenAI / Vault required):**

```bash
# Bootstrap laptop dev mode
python -m framework.cli.kb_cli laptop-init

# Interactive skill-builder (workshop interface — conversational)
python -m framework.cli.kb_cli skill-builder --persona tpm

# Run any of the 3 workflow skills
python -m framework.cli.kb_cli workflow-run ops_eng.incident_summary --inputs '{"incident_id": "INC-EXAMPLE-001"}'
python -m framework.cli.kb_cli workflow-run pm.release_brief --inputs '{"release_id": "25.01"}'
python -m framework.cli.kb_cli workflow-run tpm.weekly_exec_review --inputs '{"project": "all"}'

# List skills, build code wiki, promote with link validation
python -m framework.cli.kb_cli skill-list
python -m framework.cli.kb_cli code-wiki-build
python -m framework.cli.kb_cli promote framework/persona_builders/ops-eng.yaml --validate-links
```

**What shipped (cumulative, Phases 0-3 + V3 design):**

| Phase | Scope | Status |
|-------|-------|--------|
| **0 — Setup** | ADRs 001-018, PDD V2, config plane, persona starters | Done |
| **1 — Skeleton + incident KB** | Adapters, parsers, stores, retrievers, orchestrator, eval, MCP server, CLI | Code complete (stub mode) |
| **2 — Fleet + code wiki + skill-builder** | Fleet adapter, code wiki, 4 MCP tools, skill-builder Phase A (module split per ADR-015), provides_fields backfill | Code complete |
| **3 — Workflow runtime + orchestrator** | conversation.py (15-state machine), WorkflowMCPTool, 4-tier routing, Tier 3 fanout, cost telemetry, validate_workflow_links, 3 workflow skills, WikiMetadataStore, Confluence ingestion | Code complete |
| **V3 — Deployment interaction layer** | PDD V3 design + full implementation: REST routes, MCP tools, auth, sessions, serialization, cost store, OpenAPI spec, OCI runbook | Code complete (468 tests, 0 failures) |
| **Laptop mode** | Bastion auto-reconnect (ADR-019), Codex CLI MCP transport (ADR-020), split-deployment topology | Code complete (107 new tests) |

**Key capabilities now available:**
- **2 MCP tools** — `askKnowledgeBase` (consumption, server routes internally) + `authorSkill` (knowledge builder, server-driven 15-state session)
- **9 REST endpoints** — POST /api/v1/ask + POST/GET/DELETE /api/v1/kb/authorSkill + GET /healthz + GET /api/v1/version + GET /api/v1/metrics/cost
- **Bearer token auth** — consumer manifests (YAML), SHA-256 token lookup, scope enforcement (read/write/admin), sliding-window RPM limiting
- **Session persistence** — FilestoreSessionStore (dev) + AdbSessionStore (prod), 7-day TTL, resume from any state, per-user isolation
- **camelCase serialization** — centralized snake↔camel boundary at API edge
- **Cost telemetry** — append-only JSONL store, query by persona/skill/date range
- **15-state author_skill session** — IDENTIFY_PERSONA → ... → PROMOTE → DONE
- **REVIEW_SCHEMA quality gate** — users edit extraction field descriptions (the #1 quality lever)
- **3 workflow skills** — incident_summary (MD), release_brief (DOCX), weekly_exec_review (PPTX)
- **4-tier intent routing** — workflow match (0.85) → KB retrieval (0.60) → multi-persona fanout (0.40) → honest "no" + suggestion (0.30)
- **OpenAPI 3.1 spec** — authoritative REST contract at framework/deploy/openapi.yaml
- **OCI deployment runbook** — empty OCI tenancy → live framework
- **Client-agnostic** — any MCP client (Claude Code, Codex, Cursor, etc.) works as a pass-through

**What gates integration testing (ask once, when ready):**
1. Oracle 23ai ADB (dev instance)
2. OCI Vault + secrets
3. OpenAI API key or OCI GenAI URL
4. Confluence API token + space keys
5. Jira API token + project keys
6. AIRA team's 50 query/citation pairs (for eval gold set)

**Honest gaps:**
- LLM is in `stub` mode — real synthesis quality requires OCI GenAI URL or OpenAI key
- Filestore uses lexical Jaccard overlap, not vector similarity — ADB + real embeddings come with provisioning
- Adapters run against fixture data only — real Confluence/Jira requires API tokens

---

## 🌅 Earlier briefing — 2026-05-06 (Phase 1 implementation complete)

**Phase 1 code is done.** ~4150 LOC across 84 Python files. Two final commits landed:
- ADRs 012/013 + ADR-005/007 amendments (per AIRA comparison)
- Phase 1 implementation pass: adapters, parsers, vector store, retrievers, orchestrator, persona skills, ingestion, eval harness, FastAPI MCP server, kb-cli, CI gate, unit tests, dev-guide, runbook, 22 backlog stories

**Honest status:** code is structurally complete and import-clean. Integration verification requires real provisioning. External-touchpoint markers in every file flag what needs ADB / OpenAI / live Jira / AIRA gold-set queries to run.

**What you can do today (no provisioning needed):**
1. Read `docs/wiki/engineering/dev-guide.md` for first-run instructions
2. Read `docs/wiki/onboarding/pm-tpm.md` (and `onboarding/ops-eng.md`) and share with persona team leads in parallel
3. Review the 22 stories in `pmo/stories/STORY-001..022.md`
4. Run `pytest framework/tests/unit/` (no external deps; verifies chunker, ids, urns, recall metrics, ADR-013 filter strictness)

**What unlocks Phase 1 exit gate (80% recall on 25-question gold set):**
1. Provision Oracle 23ai ADB (dev tier) + populate Vault + run `kb-cli migrate --schema kb_incidents --env dev`
2. OpenAI API key in Vault (Oracle-certified)
3. Confluence + Jira tokens in Vault
4. AIRA team shares ~50 query/citation pairs from their eval harness → bootstrap `eval/gold_sets/incidents.jsonl` to 25 entries
5. Run `kb-cli ingest` against a Jira sample → verify end-to-end works
6. Run `kb-cli eval` → if recall ≥ 80% and faithfulness ≥ 0.85, Phase 1 exits

---

## 🌅 Earlier briefing — 2026-05-05

**TL;DR.** Phase 0 documentation/config layer is **drafted in full**: 4 DECISIONs, 5 ADRs, 8 PM wiki pages, persona-builder + extraction-schema templates, an incident extraction schema (v1), a 5-question seed eval gold-set, the Phase-0 kickoff brief, the pending-decisions surface for all phases, an initial git commit, **and a consolidated PDD + executive brief** capturing all architectural decisions including knowledge_bases (rename of corpora), the 5-layer architecture, polyglot per persona, skills-default, functional-area + resources dimensions. **No production code was written** — Phase 0 is docs/configs only by design.

**📄 Two stakeholder documents** (also as office files):
- **[PDD — Knowledge Builder Framework](../docs/wiki/pdd/PDD-Knowledge-Builder-Framework.md)** ([.docx](../docs/wiki/pdd/PDD-Knowledge-Builder-Framework.docx))
- **[Executive Brief](../docs/wiki/exec-brief.md)** ([.pptx](../docs/wiki/exec-brief.pptx))

**🆕 Architect kickoff complete** (overnight 2026-05-05 → 2026-05-06):
- ADRs 006–011 published in [docs/wiki/adr/](../docs/wiki/adr/)
- ADR-004 amended (corpora → knowledge_bases, polyglot principle, KB cards)
- [architecture.md](../docs/wiki/architecture.md), [data-model.md](../docs/wiki/data-model.md), [api-design.md](../docs/wiki/api-design.md) authored
- Configuration plane built in [framework/config/](../framework/config/) — env yamls + adapter yamls (dual-mode Confluence/Jira) + shim_faaas + scripts
- Adapter stubs in [framework/adapters/](../framework/adapters/) — Confluence native + MCP, Jira native + MCP, Git, UDAP
- 8 persona context skill stubs in [framework/persona_skills/](../framework/persona_skills/)
- **Full Option-3 starter pack**: 8 persona builder configs + 22 extraction schemas + 8 gold sets across all producer personas (PM, TPM, Architect, Eng Mgr, Developer, Ops Mgr, Ops Eng, Service Owner). All marked `status: draft` for persona teams to refine.

**You are gating two things to unblock Phase 1:**
1. **Gate 1 review** — read the ADRs + PM ingest. Reply `GATE-1-PHASE-0: approved` (or per-artifact: `ADR-001: approved`, etc.). See "🔴 Approval gates" below.
2. **🚨 External provisioning** — 3 blocking items in [pending-decisions/PHASE-0.md](pending-decisions/PHASE-0.md) that only you can do (Oracle 23ai ADB, OpenAI key, OCI Vault). The Phase-0 kickoff brief has step-by-step instructions: [phase-briefs/PHASE-0-kickoff.md](phase-briefs/PHASE-0-kickoff.md).

**Where to read:**
- 30-second view: [docs/wiki/current-status.md](../docs/wiki/current-status.md)
- Full picture: [docs/wiki/project-overview.md](../docs/wiki/project-overview.md)
- Tech stack: [docs/wiki/adr/ADR-001-tech-stack-baseline.md](../docs/wiki/adr/ADR-001-tech-stack-baseline.md)
- Storage shape (the §2.1 polyglot reconciliation): [docs/wiki/adr/ADR-002-storage-shape.md](../docs/wiki/adr/ADR-002-storage-shape.md)
- Persona-builder contract: [docs/wiki/adr/ADR-004-persona-builder-config.md](../docs/wiki/adr/ADR-004-persona-builder-config.md)

**Notable decisions you should sanity-check when awake:**
- DECISION-002 — I read your "go full Oracle stack with Autonomous DB as converged DB" as *physical-converged, logical-polyglot* (each data type has its own schema and access pattern, sharing one DB). If you wanted strict §2.1 (separate DB instances per data type), say so and I'll revisit.
- DECISION-004 — v1 personas locked to PM + TPM + Aira. Architect/Dev Mgr/etc. are deferred to Phase 4+. Adjust if you want broader v1 scope.

## 🔴 Decisions awaiting your review

| # | Title | Why | Options |
|---|-------|-----|---------|
| (none open) — DECISIONs 001–004 are decided. | | | |

## 🟡 In-flight work

| Story | Module | Status | Owner | Blocked by |
|-------|--------|--------|-------|-----------|
| Integration testing | all | ⏳ awaiting provisioning | dev-team | ADB + API keys |
| Persona authoring workshops | workshops | ⏳ ready to schedule | pm | persona team availability |

## 📋 In-flight handoffs

(none)

## 🐞 Bugs

| # | Title | Severity | Status | Fixed in |
|---|-------|----------|--------|----------|
| [BUG-001](bugs/BUG-001-adb-session-store-conn-execute.md) | AdbSessionStore called non-existent `Connection.execute/fetchone/fetchall`; ISO strings bound to TIMESTAMP cols triggered ORA-01843 | blocker | verified | d36d46b |
| [BUG-002](bugs/BUG-002-oracle-json-clob-auto-deserialise.md) | `authorSkill` continuation fails — Oracle 23ai `oracledb` auto-deserialises `CLOB IS JSON` → `dict`; `json.loads(dict)` raises `TypeError` in `load()` and `list_for_user()` | blocker | verified | 7cee283 |
| [BUG-003](bugs/BUG-003-oracle-timestamp-naive-aware-comparison.md) | `authorSkill` continuation fails — Oracle plain `TIMESTAMP` returns timezone-naive `datetime`; `expires_dt < datetime.now(tz=utc)` raises `TypeError: can't compare offset-naive and offset-aware datetimes` | blocker | verified | 0c78232 |
| [BUG-004](bugs/BUG-004-synthesized-artifacts-not-persisted.md) | `authorSkill` commits 0 artifacts — `synthesized_artifacts` omitted from `to_dict()` serialization; empty dict on session resume → `_write_artifacts()` loops over nothing | blocker | verified | 21ec5e1 |
| [BUG-005](bugs/BUG-005-workflow-kb-name-mismatch.md) | `authorSkill` validation fails — `synthesize_workflow` writes `{persona}.{skill}_data` as KB ref; persona builder registers `{persona}.{skill}` (no suffix); validator can't resolve the reference | blocker | verified | 1b4d5d9 |
| [BUG-006](bugs/BUG-006-session-status-committed-constraint-violation.md) | PROMOTE/DONE save raises ORA-02290 — `status='committed'` not in CHK_ASS_STATUS constraint `('in_progress','completed','abandoned','expired')` | blocker | verified | (this commit) |
| [BUG-007](bugs/BUG-007-gold-seed-null-expected-extraction.md) | Gold set seeded with `null` `expected_extraction` values — `{f: None for f in gaps}` passed through literally when no example artifact was analysed | minor | verified | (this commit) |

> Test gap exposed by BUG-001, BUG-002, and BUG-003 (same root cause): `framework/tests/test_session_store.py` covers only stub mode (`pool=None`) for `AdbSessionStore`. The pool-attached path has zero coverage. **QA action**: add `TestAdbSessionStorePoolPath` using a thin oracledb cursor fake — exercise `save → load` round-trip with both `dict` and `str` shapes for `session_data`, and naive + aware `datetime` shapes for all timestamp columns.

## ✅ Done (Phases 0-3 + V3 + laptop mode)
- DECISIONs 001–004 filed and decided
- ADRs 001–020 authored (including amendments to 006/007/011)
- PDD V2 (~700 lines) + Executive Brief
- **PDD V3** (~1550 lines, 4 revisions) — 2 MCP tools, 6 REST endpoint groups, 15-state authorSkill session, REVIEW_SCHEMA quality gate, session persistence/resume, camelCase naming, client-agnostic
- **OpenAPI 3.1 spec** (framework/deploy/openapi.yaml, 1302 lines) — authoritative REST contract
- **OCI deployment runbook** (docs/wiki/engineering/oci-deployment-runbook.md, 1676 lines) — empty OCI tenancy → live framework
- PM wiki ingest (project-overview, personas, 6 module pages)
- Configuration plane (dev/staging/prod yamls, adapter configs, routing thresholds)
- Full adapter suite (Confluence native+MCP+codex_cli, Jira native+MCP+codex_cli, Git, UDAP/Fleet, code wiki builder)
- **Laptop mode**: bastion auto-reconnect (framework/core/adb_pool.py), Codex CLI MCP transport (codex_cli.py adapters), split-deployment topology support
- Parser pipeline (LLM parser with schema injection, markdown-aware chunker)
- Store layer (IncidentVectorStore, FilestoreContentStore, WikiMetadataStore)
- Retriever suite (vector_search, get_incident_summary, list_sources, query_fleet, text_to_sql, find_symbol, read_code_page)
- Orchestrator (shim_faaas, shim_workflows, shim_kb, 4-tier intent classifier, context builder with multi-persona fanout, synthesizer, cost telemetry)
- Persona skills (BasePersonaSkill + per-persona, Tier 1/2 dispatch)
- Ingestion pipeline (change_detection, webhook_router, scheduler, confluence_wiki_ingest)
- Eval harness (runner, recall+latency+cost+faithfulness metrics, markdown+JSON reports)
- FastAPI MCP server with all retrieval tools registered
- Skill-builder (intent_to_artifacts, conversation.py 15-state machine with REVIEW_SCHEMA + post-commit pipeline + session persistence, validate_links)
- Workflow runtime (executor, skill_registry, WorkflowMCPTool)
- 5 renderers (markdown, docx, pptx, email, slack) + 5 deliverers (email, filesystem, object_storage, slack, sync_return)
- 3 workflow skills (ops_eng.incident_summary, pm.release_brief, tpm.weekly_exec_review)
- 8 persona builders with provides_fields backfilled
- Workshop guide (pmo/workshops/persona-authoring-workshop.md)
- kb-cli with all subcommands (laptop-init, validate, ingest, eval, promote, migrate, skill-builder, skill-list, workflow-run, code-wiki-build)
- Fixture data for laptop demo (incidents, fleet, confluence pages, weekly ops, releases)

## 🚧 Blocked
- **Integration testing** — requires external provisioning (Oracle ADB, API keys). All dev work is complete against stubs.

## ⚠️ Risks / contradictions (from lint)
- **Stub-only testing** — all 176 Python files run against filestore + stub LLM. Real-world behavior (vector similarity, LLM synthesis quality, API rate limits) is untested until provisioning arrives. Mitigated: code is structured for easy swap via env vars. **Evidence this matters: BUG-001** — the pool-attached `AdbSessionStore` was a complete blocker on laptop mode, but `test_session_store.py` only exercised stub mode (`pool=None`), so the bug shipped. Any module that has a "real backend" + "stub backend" split needs at least one fake-backed test of the real path.

## 🔴 Production rollout gates (mandatory — do not skip)

The following steps are not automated and must be executed manually during the
first production deploy. Each has an ADR as source of truth.

| # | Gate | ADR | When | SQL / Command |
|---|------|-----|------|---------------|
| 1 | Confirm ADB In-Memory option enabled | ADR-025 | Before first ingestion | `SELECT value FROM v$option WHERE parameter = 'In-Memory Column Store';` |
| 2 | **Rebuild vector index INMEMORY** after first ingestion | ADR-025 | After `chunks` table is non-empty | `ALTER INDEX KB_INCIDENTS.ix_chunks_embedding_hnsw REBUILD ORGANIZATION INMEMORY NEIGHBOR GRAPH;` |
| 3 | Setup `OCI_VECTOR_CRED` DB credential for in-DB embedding | ADR-012 | Before any embedding runs | See `kb_incidents.sql` §ADR-012 comment + `bootstrap-vault.sh` |
| 4 | Run `kb-cli migrate --schema all --env prod` | — | First deploy | `bash framework/scripts/kbf-start.sh --migrate` |
| 5 | Smoke test: `POST /mcp tools/call askKnowledgeBase` returns non-empty results | — | After migration + ingestion + index rebuild | See OCI runbook §6 |

> Gate 2 is the most commonly forgotten. The disk-resident HNSW index (created
> by migration) is functional but 2-5× slower than the in-memory form. Run the
> REBUILD immediately after the first ingestion run confirms `chunks` is
> non-empty. See ADR-025 for fallback if the In-Memory option is not available
> on the chosen ADB tier.

## Next milestones
- Schedule persona authoring workshops (workshop guide ready at `pmo/workshops/persona-authoring-workshop.md`)
- Provide provisioning when ready for integration testing
- AIRA team exports 50 query/citation pairs for gold set

---

## How to read this

- 🌅 **Morning briefing** → start here when you wake up
- 📋 **Phase Kickoff** → external dependencies you need to handle in parallel
- 🔴 **Gates** → user approval steps that unblock the next phase
- 🔴 **Decisions** → things only you can answer. Reply: "DECISION-NNN: option X"
- 🟡 **In-flight** → who's doing what right now
- 📋 **Handoffs** → cross-agent transitions in progress
- 🚧 **Blocked** → stories waiting on something
- ⚠️ **Risks** → TPM lint findings (stale wiki, code/spec drift, etc.)
