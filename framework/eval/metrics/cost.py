"""Cost roll-up."""
from __future__ import annotations

def total_dollars(events: list) -> float:
    return sum(getattr(e, "dollars", 0.0) or 0.0 for e in events)

def tokens_summary(events: list) -> dict:
    tin = sum(getattr(e, "tokens_in", 0) for e in events)
    tout = sum(getattr(e, "tokens_out", 0) for e in events)
    return {"tokens_in": tin, "tokens_out": tout, "total": tin + tout}
