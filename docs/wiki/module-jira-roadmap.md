---
title: Module — Jira roadmap (spec §4.6)
source: docs/raw/knowledge-builder-framework-spec.md (§4.6)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [module, jira, roadmap, v2]
status: open
---

# Module — Jira roadmap

> **Status: open / v2 candidate.** Approach undecided per spec §4.6. Defer until Phase 4+; treat as a research item.

## Why this is open
Roadmap data in Jira is heterogeneous: some teams use Epics → Stories cleanly; others use a mix of labels, components, and free-form fields. There is no obvious canonical "unit of aggregation."

## Candidate aggregations
- **Service-specific roadmaps** — likely the right unit. One ContentItem per service, summarizing the active and upcoming Epics with target releases and dependencies.
- **Persona-specific roadmaps** — e.g., the PM Knowledge Builder produces a "PM roadmap" view; the TPM produces a "delivery roadmap" view. Different schemas over the same Jira data.
- **Cross-team release rollups** — by FA release (24.05.x, 25.01, etc.).

## Plan
- v1: **do not ship**. Roadmap questions can be answered by the PM Knowledge Builder (Phase 3) using ad-hoc Jira filters.
- v2: file a DECISION at Phase 4 kickoff to pick the canonical aggregation; spec says "service-specific roadmaps may be the right unit."

## Open items
- **Owner**: PM persona team (since roadmap is product-shaped, not ops-shaped)
- **Schema**: TBD when DECISION is filed
- **Source overlap**: Jira PM-* roadmap epics are already covered by the PM builder's source list; this module would only add a new aggregation/extraction lens
