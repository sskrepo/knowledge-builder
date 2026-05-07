---
title: Agentic Code Access — Read & Write Story
created: 2026-05-06
owner: architect
tags: [agents, code, aira, phase-2, phase-3, decision-005]
status: current
related: [PDD, ADR-002, ADR-003, module-code, persona-onboarding-ops-eng]
---

# Agentic Code Access — Story

How remote agents (Aira today; coding assistants and per-persona agents tomorrow) read and modify code through the Knowledge Builder Framework. This is **spec §8.2**, the second of the three explicit open problems. The read path is designed and lands in Phase 2; the write path becomes formal in **DECISION-005** at Phase 2 kickoff.

---

## Two paths — separate them cleanly

```
                                   AGENT NEEDS …
                                        │
                          ┌─────────────┴─────────────┐
                          │                           │
                  READ — "where is X?"          WRITE — "fix X"
                  "what does Y do?"             "open a PR for Z"
                          │                           │
                          ▼                           ▼
          ┌───────────────────────────┐     ┌─────────────────────┐
          │  MCP tools over kb_code   │     │  Sandbox (Container │
          │  (fast, cheap, central)   │     │  Instance) + PR flow│
          │  Phase 2 deliverable      │     │  Phase 3 deliverable│
          └───────────────────────────┘     └─────────────────────┘
```

90%+ of agentic code questions are reads. The write path is rare-but-expensive; we keep them separate so cost stays bounded.

---

## Read path — designed; ships Phase 2

### Storage: `kb_code` (per ADR-002)

A **structural index, NOT raw code embeddings** — per spec §4.3, raw code embeddings underperform structural lookup for navigation.

```
kb_code schema in Oracle 23ai ADB:
├── code_pages          — markdown body per module summary + path index
├── symbols             — name, kind, path, line range, signature
├── symbol_refs         — call/import graph (when language adapter produces it)
└── openapi_index       — endpoint + schema lookup for OpenAPI specs
```

### Build pipeline (CI on every commit)

```
git push to org/fa-code
        │
        ▼
CI runs: kb-cli code-rebuild --repo org/fa-code --branch main
        │
        ▼
For each module:
  ├── AST-walk the source                   (rule-based)
  ├── Generate per-module summary           (LLM-assisted; schema-bounded;
  │                                          Som-style wiki pattern)
  ├── Extract symbol entries                 (rule-based: name/kind/loc/sig)
  ├── Build symbol_refs graph                (language-adapter-specific)
  └── Index OpenAPI specs                    (yaml/json schema parse)
        │
        ▼
upsert into kb_code (Oracle 23ai ADB) — idempotent on git_sha
```

### Retrieval — agent-facing MCP tools

| Tool | Returns | Use when |
|---|---|---|
| `read_code_page(path)` | Markdown summary of a module + dependencies | "what does this module do?" |
| `find_symbol(name, kind?, repo?)` | Path + line + signature | "where is `verify_token` defined?" |
| `vector_search(corpus="dev_decisions", query)` | Cited passages from developer decisions | "why was this designed this way?" |
| `search_wiki` over OpenAPI index | Endpoint definition + request/response schema | "what's the request shape for `/v1/customers`?" |

All cited; all served from a single converged DB; no codebase materialization in the agent's context.

### Cost profile

- **~$0.005 per query** (one embedding for vector_search; cheap structural lookup for find_symbol)
- **Token cost in agent's context**: ~500 tokens per `read_code_page` (a summary, not the full module)
- **Wiki regen cost**: ~$0.50 per repo per day (LLM-assisted summary generation; cached on `git_sha`)

### Why structural index over raw embeddings

Per spec §4.3 + AIRA comparison §1.4 — raw code embeddings have known weaknesses:

- Variable names get over-weighted vs. semantics
- Cross-language searching is poor
- "Find me where auth happens" returns 50 near-identical token-validation snippets, not the orchestrator
- Symbol-level granularity is hard to reconstruct

Structural indexes solve these:
- AST-derived names + signatures preserve typed identity
- Module summaries (LLM-written) capture intent in natural language
- Vector search applied only to *summaries* (not raw code), giving semantic recall without the noise

---

## Write path — open; DECISION-005 in Phase 2 close

When the agent needs to actually modify code, the read path isn't enough. We need a substrate where the agent can edit, test, and open a PR.

### Three candidates (per spec §8.2)

| Option | What | Pros | Cons |
|---|---|---|---|
| **VM spin-up** (Rajeev's original) | Per-task ephemeral VM, full clone, regen wiki, edit, test, PR | Isolated, reproducible, full toolchain | Heavy: ~1-2min spin-up, ~$0.10+/run, OCI orchestration |
| **Pre-built code wiki only** | Read-only MCP path ONLY | Cheapest possible | Defeats use case — no actual code work |
| **Hybrid** ⭐ | MCP for reads; sandbox per write task | Best $/value; 90% reads are cheap; expensive path only when needed | Two paths to maintain |

**Spec recommended: hybrid.** **Architect lean (DECISION-005 default): hybrid with OCI Container Instances** as the sandbox substrate (faster than VMs, ephemeral, no orchestration overhead).

### Recommended write-path flow

```
Agent decides "I want to change auth-service"
        │
        ▼
Calls: kb-cli code-spawn --repo org/auth-service --task <agent-session-id>
        │
        ▼
Framework provisions OCI Container Instance:
  ├── pull base image with toolchain (lint, fmt, test runners, gh CLI)
  ├── clone repo @ HEAD
  ├── regenerate Som wiki locally  (so agent has fresh structural view)
  ├── inject short-lived deploy key from OCI Vault
  └── return SSH-style endpoint + auth token (TTL 1h default)
        │
        ▼
Agent works in sandbox via SSH/exec MCP tools (claude-code style):
  ├── reads files
  ├── makes edits
  ├── runs pytest / lint
  └── gh pr create
        │
        ▼
Sandbox auto-tears-down at TTL or on PR creation
        │
        ▼
Human reviews the PR (agent-authored PRs marked clearly)
```

### Why ephemeral container per task (not long-lived VM)

- **Reproducibility**: no state pollution between tasks
- **Security**: compromise blast radius is one task
- **Cost**: idle VMs are pure waste; per-task spin-up is consumption-based
- **Auditability**: full lifecycle log per task (every command, every test result, every diff)

### OCI primitives that fit

| Primitive | Role |
|---|---|
| **OCI Container Instances** | Fast (~30s) ephemeral compute; no orchestration overhead |
| **OCI Object Storage** | Per-task artifacts (test logs, diff snapshots, eval outputs) |
| **OCI Vault** | Short-lived deploy keys (rotated per task); kept out of the container's environment |
| **OCI Functions** | Sandbox-spawn dispatcher (when triggered by `kb-cli code-spawn`) |
| **OCI Streaming** | Audit-event fan-out for compliance log retention |

---

## Concrete walk-through — Aira investigating an auth-service incident

```
1. INC-2026-001234 fires:
   "auth-service hitting NPE on verify_token in prod"

2. Aira agent receives the incident and starts investigation:

   READ PATH (cheap, fast):
   ────────────────────────
   a) get_incident_summary(INC-2026-001234)
      → returns: "Pod DB state was null during resource validation,
                  resolution: updated state to ACTIVE/None"
                  citation: jira://INC-2026-001234

   b) vector_search(corpus="ops_incidents", "NPE token verify")
      → returns: 5 similar past incidents

   c) find_symbol("verify_token", kind="function", repo="auth-service")
      → returns: kb-code/services/auth/auth/verify_token.py:142
                 signature: def verify_token(token: str) -> User | None

   d) read_code_page("kb-code/services/auth/overview.md")
      → returns: module summary noting null-token-cache eviction logic

   Aira synthesizes: "NPE matches pattern from INC-2025-009987;
   root cause is null-cache eviction during state rotation;
   fix at line 142 needs null-check on cache.get(token)."

   Total cost: ~$0.04 in MCP tool calls. No code materialization.

3. Aira decides to attempt a fix automatically:

   WRITE PATH (heavier; explicit opt-in):
   ───────────────────────────────────────
   e) kb-cli code-spawn --repo org/auth-service --task aira-2026-05-06-001
      → spins up OCI Container Instance (~30s)
      → returns: sandbox endpoint + SSH key (TTL 1h)

   f) Aira via sandbox MCP tools:
      - cat services/auth/auth/verify_token.py
      - edit_file: add `if token not in cache: return None` at line 141
      - run pytest tests/auth/test_verify_token.py
        → all pass
      - gh pr create --title "fix: null-token-cache NPE in verify_token"
                     --body "linked: jira://INC-2026-001234, ..."

   Total write-path cost: ~$0.15 (sandbox compute + LLM tool calls)

4. Human on-call engineer reviews the PR
   → if accepted, normal CI/CD takes over
   → if rejected, Aira learns from the review comment
```

The 90% case (read-only Q&A) costs $0.04. The rare 10% case (actual code change) costs $0.15. **Bounded.**

---

## Open questions DECISION-005 will settle

Filed at Phase 2 kickoff:

| # | Question | Default |
|---|----------|---------|
| 1 | Substrate: OCI Container Instances vs full VM vs existing dev-environments | OCI Container Instances |
| 2 | Per-task TTL | 1 hour (configurable) |
| 3 | Cross-repo support | v1 single-repo; v2 multi-repo |
| 4 | Test runners required | pytest, JUnit, Go test, npm test |
| 5 | PR auto-merge policy on green CI | No — always human review for v1 |
| 6 | Deploy keys: Vault short-lived vs. GitHub App tokens | OCI Vault short-lived |
| 7 | Per-task audit log retention | 90 days, OCI Object Storage |
| 8 | Sandbox concurrency cap per agent | 5 simultaneous tasks |
| 9 | Network egress policy from sandbox | Allow GitHub + npm/pypi mirrors; deny arbitrary |
| 10 | Compliance / SOC2 implications | Architect + InfoSec to confirm |

---

## Phase timing

| Phase | What ships for code access |
|---|---|
| **Phase 1 (now)** | Stubs only — `read_code_page` and `find_symbol` raise NotImplementedError |
| **Phase 2** | Full read path: code-wiki CI builder, `kb_code` schema migrations, MCP tools live, OpenAPI specs indexed. **Aira can ask code questions.** |
| **Phase 2 close** | DECISION-005 filed (write-path substrate chosen) |
| **Phase 3** | Write path: `kb-cli code-spawn`, sandbox lifecycle, agent-mediated PR workflow |
| **Phase 4** | Hardening: audit log, multi-repo support, advanced test runners, network egress hardening |

---

## Why this design

### Why hybrid wins
- **Cost** — 90% of code interactions are reads; making them cheap is decisive
- **Latency** — read path is sub-second; write path takes minutes; users tolerate seconds for "investigate" but not for "ask a question"
- **Security** — write path is the security risk; isolating it lets us be permissive on reads and strict on writes
- **Auditability** — sandbox lifecycle gives us a perfect audit boundary; reads through MCP are just retrieval logs

### Why structural index over raw embeddings (read path)
- AST-derived names + signatures preserve typed identity that embeddings lose
- Summary-level vector recall avoids noise from variable-name token weighting
- Symbol-level granularity is built-in
- Cross-language searching works (summaries are natural language)

### Why ephemeral containers over long-lived VMs (write path)
- Reproducibility (no state pollution)
- Security (compromise blast radius = 1 task)
- Cost (consumption-based; no idle waste)
- Auditability (clean lifecycle = clean audit log)

---

## What persona teams need to know

### For Aira / ops_eng team
- **Phase 2**: code reads light up. Aira can answer "where is auth implemented?" via `find_symbol`.
- **Phase 3**: write path lights up. Aira can attempt fixes automatically (PRs go to human review).
- **Effort from your team**: zero — this is framework infrastructure. You just gain new MCP tools.

### For Developer persona team
- The Developer Knowledge Builder ([`framework/persona_builders/developer.yaml`](../../framework/persona_builders/developer.yaml)) drives the code-wiki CI.
- Refining what gets extracted per module is the developer team's domain (mostly auto via AST; LLM summary takes some tuning per language).
- Will need its own onboarding workbook in Phase 2 — see [`onboarding/README.md`](onboarding/README.md) reserved-filename list.

### For platform / DevOps team
- The write path needs your input on OCI Container Instances setup, deploy-key policy, network egress, audit retention.
- DECISION-005 is the formal asking point — workshop will be at Phase 2 kickoff (~Week 12).

---

## References

- Spec §4.3 (code as a data type), §8.2 (the open problem)
- [PDD §4 — five-layer architecture](pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-002 §kb_code](adr/ADR-002-storage-shape.md) — code structural index schema
- [ADR-003 §retrievers](adr/ADR-003-core-interfaces.md) — `read_code_page`, `find_symbol` MCP contracts
- [module-code](module-code.md) — data-type module page
- [persona-onboarding workbooks](onboarding/) — Developer workbook is reserved for Phase 2
- DECISION-005 — to be filed at Phase 2 kickoff (placeholder in [pending-decisions/PHASE-2.md](../../pmo/pending-decisions/PHASE-2.md))
