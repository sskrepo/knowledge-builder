"""recall@k and recency-weighted recall (ADR-005 amend 2)."""
from __future__ import annotations

import math
from typing import Iterable

DEFAULT_TAU_DAYS = 180.0


def recall_at_k(retrieved_citations: list[str], expected_citations: list[str], k: int = 5) -> float:
    """Binary hit/miss recall@k."""
    top_k = retrieved_citations[:k]
    hits = sum(1 for c in expected_citations if c in top_k)
    return hits / max(len(expected_citations), 1)


def hit_at_k(retrieved_citations: list[str], expected_citations: list[str], k: int = 5) -> int:
    """1 if any expected is in top-k else 0."""
    return 1 if any(c in retrieved_citations[:k] for c in expected_citations) else 0


def recency_weighted_recall_at_k(
    retrieved: list[dict],            # [{citation: str, age_days: float|None}]
    expected_citations: list[str],
    k: int = 5,
    tau_days: float = DEFAULT_TAU_DAYS,
) -> float:
    """Per ADR-005 amendment 2 — exp(-age/TAU) weighting."""
    top_k = retrieved[:k]
    score = 0.0
    for exp_c in expected_citations:
        for r in top_k:
            if r.get("citation") == exp_c:
                age = r.get("age_days") or 0
                w = math.exp(-age / tau_days)
                score += w
                break
    return score / max(len(expected_citations), 1)


def mrr(retrieved_citations: list[str], expected_citations: list[str]) -> float:
    """Mean reciprocal rank (top-1-ish)."""
    for i, c in enumerate(retrieved_citations, start=1):
        if c in expected_citations:
            return 1.0 / i
    return 0.0
