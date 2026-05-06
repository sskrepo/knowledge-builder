"""Eval runner — runs gold set against the orchestrator and reports metrics."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .metrics.recall import recall_at_k, hit_at_k, recency_weighted_recall_at_k, mrr
from .metrics.latency import percentiles
from .metrics.faithfulness import faithfulness_score

log = logging.getLogger(__name__)


def load_gold_set(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def run_gold_set(
    gold_set_path: Path,
    context_builder,
    sample_faithfulness: int = 5,
) -> dict:
    """Runs every question in the gold set; returns aggregated metrics."""
    questions = load_gold_set(gold_set_path)
    per_q: list[dict] = []
    latencies: list[float] = []

    for q in questions:
        t0 = time.time()
        try:
            response = context_builder.answer(q["question"])
        except Exception as e:
            log.exception("eval fail on %s: %s", q["id"], e)
            per_q.append({"id": q["id"], "status": "error", "error": str(e)})
            continue
        latency_ms = int((time.time() - t0) * 1000)
        latencies.append(latency_ms)

        retrieved = response.get("passages", [])
        retrieved_citations = [p["citation"] for p in retrieved]
        expected = q.get("expected_citations", [])

        hit = hit_at_k(retrieved_citations, expected, k=5)
        rec5 = recall_at_k(retrieved_citations, expected, k=5)
        rwr5 = recency_weighted_recall_at_k(
            [{"citation": p["citation"], "age_days": None} for p in retrieved],
            expected, k=5,
        )
        mrr_score = mrr(retrieved_citations, expected)

        # Optional faithfulness sample
        faith = None
        if len(per_q) < sample_faithfulness:
            answer_text = json.dumps(response.get("answer", {}))
            contexts = [p["text"] for p in retrieved[:5]]
            faith = faithfulness_score(q["question"], answer_text, contexts)

        per_q.append({
            "id": q["id"],
            "hit_at_5": hit,
            "recall_at_5": rec5,
            "recency_weighted_recall_at_5": rwr5,
            "mrr": mrr_score,
            "latency_ms": latency_ms,
            "faithfulness": faith,
            "status": "ok",
        })

    ok = [q for q in per_q if q.get("status") == "ok"]
    summary = {
        "total": len(questions),
        "ok": len(ok),
        "errors": len(questions) - len(ok),
        "avg_recall_at_5": sum(q["recall_at_5"] for q in ok) / max(len(ok), 1),
        "avg_recency_weighted_recall_at_5": sum(q["recency_weighted_recall_at_5"] for q in ok) / max(len(ok), 1),
        "avg_mrr": sum(q["mrr"] for q in ok) / max(len(ok), 1),
        "hit_rate_at_5": sum(q["hit_at_5"] for q in ok) / max(len(ok), 1),
        "latency": percentiles(latencies),
    }
    faith_values = [q["faithfulness"] for q in ok if q.get("faithfulness") is not None]
    if faith_values:
        summary["avg_faithfulness"] = sum(faith_values) / len(faith_values)

    return {"summary": summary, "per_question": per_q}
