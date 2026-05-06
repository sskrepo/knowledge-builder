---
title: STORY-001 — Core types + Protocols (ADR-003)
status: drafted
created: 2026-05-06
owner: pm
phase: 1
size: M
---
## User story
As a Phase-1 backend dev, I want the core type system in place so all subsequent code can build on stable Protocols.

## Acceptance criteria
- [x] `framework/core/{content,interfaces,ids,urns,llm,vault}.py` exist
- [x] Multi-axis fields on `ContentItem` per ADR-008
- [x] `mypy --strict framework/core/` passes
- [x] Unit tests for ids and urns

## Notes
Already landed in commit (Architect kickoff cycle).
