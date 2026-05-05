---
title: Personas
source: docs/raw/knowledge-builder-framework-spec.md (§1)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [personas, framework]
status: current
---

# Personas

Two distinct persona populations interact with the framework: **knowledge producers** (humans working in roles like PM, TPM, Architect — they author content and own a Knowledge Builder config) and **use-case agents** (LLM-driven consumers like Aira, internal portals).

## Knowledge producers (humans behind a Knowledge Builder)

### Product Manager (PM)
- **Core jobs**: define product scope, write feature briefs, plan releases, prioritize roadmap.
- **Primary docs**: Confluence "PRODUCT" space (PRDs, feature briefs), Jira PM-* epics, design-doc git repos.
- **Pain**: every new agent or coworker re-asks the same product context; PRDs go stale; release plans live across N tools.
- **Knowledge Builder shipped in v1**: yes (PM Knowledge Builder).

### Technical Program Manager (TPM)
- **Core jobs**: cross-team coordination, weekly ops summaries, ECAR (compliance/risk) authoring, dependency tracking.
- **Primary docs**: Confluence "TPM" space (weekly ops, ECARs), Jira OPS-* issues, status decks.
- **Pain**: weekly ops content is ephemeral; consumers (execs, on-call) need it summarized; cross-team dependency state is tribal.
- **Knowledge Builder shipped in v1**: yes (TPM Knowledge Builder).

### Architect
- **Core jobs**: tech decisions, ADRs, design docs, system maps, integration playbooks.
- **Primary docs**: Confluence design-docs, ADR repos, OpenAPI specs in code.
- **Pain**: design rationale lives in meeting notes; current system shape requires reading 20 places.
- **Knowledge Builder shipped in v1**: deferred to Phase 4+ (will follow ADR-004 contract).

### Development Manager
- **Core jobs**: engineering execution, story breakdown, code-quality enforcement, on-call rotations.
- **Primary docs**: engineering wiki, Jira project boards, runbooks.
- **Knowledge Builder shipped in v1**: deferred.

### Developer
- **Core jobs**: implementation, code review, debugging.
- **Primary "docs"**: code itself (the structural index per spec §4.3).
- **Knowledge Builder shipped in v1**: covered indirectly by the code-wiki module (Phase 2); a Dev-specific persona builder is deferred.

### DevOps / SRE
- **Core jobs**: deploys, incident response, fleet operations, runbooks.
- **Primary docs**: runbooks (Confluence), Jira incidents, fleet UDAP data.
- **Knowledge Builder shipped in v1**: deferred (Aira covers incident path today; DevOps-specific KB later).

### Executive
- **Core jobs**: strategy, OKR tracking, board updates.
- **Primary docs**: strategy decks, weekly ops summaries, exec dashboards.
- **Knowledge Builder shipped in v1**: deferred.

## Use-case agents (knowledge consumers)

### Aira (incident agent)
- **What it does**: triages incidents, finds blast radius, surfaces resolutions for known errors, links incidents to services/owners/tenants.
- **Reads**: incident KB (vector + graph), fleet (read-through), eventually code wiki for fix lookups.
- **Status**: production today; spec §4.1 path is already proven. v1 framework target is to match or beat Aira's current KB on the gold set.

### Internal portals (search, Q&A, onboarding)
- **What it does**: employees ask questions; portal answers with citations.
- **Reads**: any persona KB they're authorized for.
- **Status**: future v2 consumer.

### Coding assistants (Codex-style, internal)
- **What it does**: code-aware Q&A and code generation.
- **Reads**: code wiki, OpenAPI index, design docs.
- **Status**: future v2 consumer.

### Future per-persona agents
Each persona above (PM, TPM, Architect, etc.) will eventually have a *use-case agent counterpart* that queries the corresponding Knowledge Builder's output. These agents are downstream of the framework — they don't ship as part of v1.

## Persona visibility map (ACL placeholder per spec §10)
Even though enforcement is v2 (spec §2.8), every ContentItem carries `persona_visibility` from day one. Default mappings:

| Producing builder | Default `persona_visibility` |
|---|---|
| PM Knowledge Builder | `[pm, tpm, architect, dev_mgr, dev, exec]` |
| TPM Knowledge Builder | `[tpm, pm, architect, dev_mgr, exec]` |
| Aira / incident KB | `[devops, dev_mgr, dev, tpm, architect, aira]` |
| FA semantic graph | `[architect, dev, dev_mgr]` |
| Code wiki | `[dev, architect, dev_mgr, aira]` |

Persona teams can override per-corpus in their builder config (`metadata_defaults.persona_visibility`).

## How personas drive scope decisions
- **DECISION-004** (initial v1 personas) limited Knowledge Builders to PM + TPM + Aira — enough to validate the per-persona contract and ship the proven incident path. See the decision file for rationale.
- New personas added to the framework follow ADR-004 (config-only, no framework change).
