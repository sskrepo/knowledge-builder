---
title: STORY-020 — Historical backfill script
status: drafted
phase: 1
size: M
owner: dev
---
## Acceptance criteria
- [ ] CLI: `kb-cli backfill --source jira --since 2023-01-01 --persona ops-eng`
- [ ] Resumable (writes progress checkpoints)
- [ ] Token-cost projection before start (require explicit confirmation)
- [ ] ~50K Jira issues processable in <12 hours
