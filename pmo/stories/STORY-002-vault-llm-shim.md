---
title: STORY-002 — Vault client + LLMClient shim
status: drafted
phase: 1
size: S
owner: pm
---
## User story
As an ingestion worker, I want vault:// references resolved at runtime so secrets never live in env files.

## Acceptance criteria
- [x] `VaultClient.resolve()` works against OCI Vault (60s cache)
- [x] `LLMClient.chat()` and `embed()` instrumented with cost telemetry
- [ ] Integration test against real Vault instance (needs provisioning)
