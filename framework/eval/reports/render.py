"""Render eval results to markdown + JSON."""
from __future__ import annotations
import json
from pathlib import Path

def render_markdown(results: dict) -> str:
    s = results["summary"]
    lines = [
        "# Eval Report",
        "",
        f"- **Total questions:** {s['total']}",
        f"- **OK:** {s['ok']} (errors: {s['errors']})",
        f"- **Avg recall@5:** {s['avg_recall_at_5']:.3f}",
        f"- **Avg recency-weighted recall@5:** {s['avg_recency_weighted_recall_at_5']:.3f}",
        f"- **Avg MRR:** {s['avg_mrr']:.3f}",
        f"- **Hit rate @ 5:** {s['hit_rate_at_5']:.3f}",
        f"- **Latency p50/p95/p99:** {s['latency']['p50']:.0f} / {s['latency']['p95']:.0f} / {s['latency']['p99']:.0f} ms",
    ]
    if "avg_faithfulness" in s:
        lines.append(f"- **Avg faithfulness (sampled):** {s['avg_faithfulness']:.3f}")
    lines += ["", "## Per-question", "", "| ID | hit@5 | recall@5 | mrr | latency (ms) | status |",
              "|---|---|---|---|---|---|"]
    for q in results["per_question"]:
        if q.get("status") == "error":
            lines.append(f"| {q['id']} | — | — | — | — | error: {q.get('error', '')[:50]} |")
        else:
            lines.append(
                f"| {q['id']} | {q['hit_at_5']} | {q['recall_at_5']:.2f} | "
                f"{q['mrr']:.2f} | {q['latency_ms']} | ok |"
            )
    return "\n".join(lines)


def write_report(results: dict, out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{run_id}.md"
    json_path = out_dir / f"{run_id}.json"
    md_path.write_text(render_markdown(results))
    json_path.write_text(json.dumps(results, indent=2, default=str))
    return md_path
