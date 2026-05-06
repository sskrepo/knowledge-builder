---
title: STORY-009 — CI eval gate
status: drafted
phase: 1
size: M
owner: qa
---
## Acceptance criteria
- [x] `framework/deploy/ci/eval-gate.yml` runs on parser/store/retriever PRs
- [x] Compares run vs baseline; blocks merge on regression > 2pp
- [ ] Per-PR cost cap ($5 hard cap)
- [ ] Wired in actual GitHub/OCI DevOps once repo lives there
