---
title: STORY-004 — Confluence native adapter (REST)
status: drafted
phase: 1
size: L
owner: dev
---
## User story
As an ingestion worker, I want to pull Confluence pages by space + label via REST so we can ingest design-doc supplements to incidents.

## Acceptance criteria
- [x] `list()` paginates with `start` + `limit`
- [x] `fetch()` returns `body.storage` + labels + version
- [x] `stream_changes()` polls via CQL `lastmodified >= since`
- [x] Healthcheck against `/wiki/rest/api/space`
- [x] Rate limiting (RPM)
- [ ] Verified against live Confluence (post-provisioning)
