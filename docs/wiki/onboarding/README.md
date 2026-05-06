---
title: Persona Onboarding Workbooks
created: 2026-05-06
owner: pm
tags: [onboarding, persona]
status: current
---

# Persona Onboarding Workbooks

Self-contained workbooks for engaging persona team leads in parallel with engineering. Each one captures the framework's draft starter pack for that persona, lists the questions the persona team needs to answer, and outlines the workshop → schema iteration → dry-run → gold-set → eval → promote process.

## Available workbooks

| Persona group | Workbook | Audience |
|---|---|---|
| Product Manager + Technical Program Manager | [`pm-tpm.md`](pm-tpm.md) | PM team leads, TPM team leads |
| Ops Engineer (AIRA-equivalent) | [`ops-eng.md`](ops-eng.md) | AIRA team, Ops Engineering / SRE leads |

## Future workbooks (Phase 4+)

Will follow the same template once their starter packs are finalized:

- Architect (`architect.md`)
- Engineering Manager (`eng-mgr.md`)
- Developer (`developer.md`)
- Operations Manager (`ops-mgr.md`)
- Service Owner (`service-owner.md`)

## How to use these

1. Share the relevant workbook with the persona team lead
2. Schedule a 60–90 min workshop using the workbook's checklist
3. Persona team's "schema owner" iterates on the draft schemas in `framework/parsers/schemas/{persona}/`
4. Engineering runs dry-run + eval; persona team reviews
5. Promote to `status: production` once gold-set passes thresholds

## Cross-references

- Persona-builder contract: [`../adr/ADR-004-persona-builder-config.md`](../adr/ADR-004-persona-builder-config.md)
- Functional-area + resources dimensions: [`../adr/ADR-008-functional-area-and-resources.md`](../adr/ADR-008-functional-area-and-resources.md)
- AIRA comparison (relevant for ops-eng): [`../aira-comparison.md`](../aira-comparison.md)
- PDD: [`../pdd/PDD-Knowledge-Builder-Framework.md`](../pdd/PDD-Knowledge-Builder-Framework.md)
