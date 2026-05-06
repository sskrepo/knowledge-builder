---
title: STORY-015 — Webhook intake (Confluence + Jira)
status: drafted
phase: 1
size: M
owner: dev
---
## Acceptance criteria
- [x] `webhook_router.py` for both sources
- [x] HMAC signature verification
- [ ] Dedupe duplicate deliveries (idempotent on incident id)
- [ ] Failure replay queue
