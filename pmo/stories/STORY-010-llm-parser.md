---
title: STORY-010 — LLM parser with incident schema
status: drafted
phase: 1
size: L
owner: dev
---
## Acceptance criteria
- [x] gpt-4o with `response_format: json_object`
- [x] Schema-injected system prompt (descriptions + max-lengths + enums)
- [x] Schema validation on output
- [x] Multi-axis dimension extraction → `ContentItem.functional_area_all`, `resources`, etc.
- [x] Edge generation: incident → service / resource / tenant / owner
- [ ] Quality eval on real Jira samples (post-provisioning)
