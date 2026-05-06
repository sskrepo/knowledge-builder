---
title: STORY-021 — Performance pass (p95 <500ms)
status: drafted
phase: 1
size: L
owner: dev
---
## Acceptance criteria
- [ ] vector_search p95 <500ms on 50K-chunk corpus
- [ ] HNSW parameters tuned (M, EFCONSTRUCTION)
- [ ] Connection pool sized correctly
- [ ] Caching of shim_faaas + shim_kb (TTL 60s)
