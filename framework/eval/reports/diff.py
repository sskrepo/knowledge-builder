"""Compare current run against baseline. CI gate uses this."""
from __future__ import annotations

DEFAULT_TOLERANCE = 0.02  # 2 percentage points absolute

def diff_summaries(current: dict, baseline: dict, tolerance: float = DEFAULT_TOLERANCE) -> dict:
    rows = []
    regressed = False
    for k in ("avg_recall_at_5", "avg_recency_weighted_recall_at_5",
              "hit_rate_at_5", "avg_faithfulness"):
        c, b = current.get(k), baseline.get(k)
        if c is None or b is None:
            continue
        delta = c - b
        rows.append({"metric": k, "current": c, "baseline": b, "delta": delta})
        if delta < -tolerance:
            regressed = True
    return {"regressed": regressed, "rows": rows, "tolerance": tolerance}
