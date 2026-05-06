"""Latency percentiles."""
from __future__ import annotations

def percentiles(values: list[float], ps: list[float] = [50, 95, 99]) -> dict:
    if not values:
        return {f"p{int(p)}": 0 for p in ps}
    s = sorted(values)
    out = {}
    for p in ps:
        idx = int((p / 100.0) * (len(s) - 1))
        out[f"p{int(p)}"] = s[idx]
    return out
