# Pending Decisions

User-facing surface for "what's waiting on me?" — TPM-owned. See `dev-agent-team/shared/pending-decisions-protocol.md` for the canonical protocol.

## Files
- [index.md](index.md) — counts across all phases
- `PHASE-N.md` — one per phase, with severity buckets (🚨 / 🟡 / 📝 / 🔮 / ✅)

## When you deliver an item
Reply in chat. Any agent that picks up the next session will:
1. Move the row to ✅ Done
2. Reconcile `pmo/dashboard.md` and `docs/wiki/current-status.md`
3. Append to `docs/wiki/log.md`
4. Surface what just unblocked
