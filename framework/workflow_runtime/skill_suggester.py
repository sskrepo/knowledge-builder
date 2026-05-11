"""Skill suggester — ADR-018 implementation.

Logs Tier-4 misses to a JSONL file; groups them by query pattern;
emits weekly digest per persona. Works without Oracle ADB (filestore mode).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "for", "in", "on",
    "at", "to", "from", "with", "by", "of", "and", "or", "but", "not",
    "that", "this", "it", "its", "all", "what", "which", "who", "how",
    "me", "my", "we", "our", "you", "your", "they", "them", "their", "get",
}


def _query_fingerprint(query: str) -> str:
    """A normalized, stopword-stripped token set for grouping similar queries."""
    tokens = sorted(
        t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in _STOPWORDS
    )
    return " ".join(tokens)


def _jaccard(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / max(len(ta | tb), 1)


class SkillSuggester:
    """Logs Tier-4 misses, clusters by query pattern, emits weekly digest.

    File layout:
      {log_dir}/skill_suggestions.jsonl   — append-only miss log
      {log_dir}/candidates.json           — grouped candidates (written on get_candidates())
    """

    def __init__(self, log_dir: str = "~/.kbf/telemetry/skill_suggestions"):
        self._log_dir = Path(log_dir).expanduser()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_dir / "skill_suggestions.jsonl"
        self._candidates_file = self._log_dir / "candidates.json"

    def log_miss(self, query: str, persona: str, context: dict | None = None) -> None:
        """Append a Tier-4 miss record to skill_suggestions.jsonl."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "persona": persona,
            "query": query,
            "fingerprint": _query_fingerprint(query),
            "context": context or {},
        }
        try:
            with self._log_file.open("a") as f:
                f.write(json.dumps(record) + "\n")
            log.info(
                "skill_suggester: logged miss persona=%s query=%r", persona, query[:80]
            )
        except Exception as e:
            log.warning("skill_suggester: failed to write miss log: %s", e)

    def get_candidates(
        self,
        persona: str | None = None,
        min_frequency: int = 3,
        similarity_threshold: float = 0.50,
    ) -> list[dict]:
        """Group logged misses by query pattern; return candidates above min_frequency.

        Grouping algorithm:
        1. Compute fingerprint (stopword-stripped sorted tokens) for each miss.
        2. Cluster fingerprints by Jaccard similarity >= similarity_threshold.
        3. Filter clusters to those with >= min_frequency occurrences.
        4. Return list of candidate dicts with id, persona, pattern, count, examples.
        """
        records = self._load_records(persona)
        if not records:
            return []

        clusters: list[dict] = []

        for record in records:
            fp = record["fingerprint"]
            matched = False
            for cluster in clusters:
                if _jaccard(cluster["fingerprint"], fp) >= similarity_threshold:
                    cluster["count"] += 1
                    cluster["last_seen"] = record["ts"]
                    if len(cluster["example_queries"]) < 5:
                        cluster["example_queries"].append(record["query"])
                    matched = True
                    break
            if not matched:
                clusters.append({
                    "id": _cluster_id(fp, record["persona"]),
                    "persona": record["persona"],
                    "fingerprint": fp,
                    "query_pattern": _to_pattern(fp),
                    "count": 1,
                    "first_seen": record["ts"],
                    "last_seen": record["ts"],
                    "example_queries": [record["query"]],
                    "status": "pending",
                })

        candidates = [c for c in clusters if c["count"] >= min_frequency]
        candidates.sort(key=lambda x: -x["count"])

        # Persist for CLI introspection
        try:
            self._candidates_file.write_text(
                json.dumps(candidates, indent=2, default=str)
            )
        except Exception as e:
            log.warning("skill_suggester: failed to write candidates.json: %s", e)

        return candidates

    def generate_weekly_digest(self, persona: str) -> str:
        """Generate a markdown weekly digest of missed queries for a persona team."""
        candidates = self.get_candidates(persona=persona, min_frequency=1)
        if not candidates:
            return f"## {persona.upper()} persona — skill suggestion digest\n\nNo missed queries recorded this week.\n"

        lines = [
            f"## {persona.upper()} persona — skill suggestion digest",
            "",
            f"Top queries you couldn't answer well recently:",
            "",
        ]
        for i, c in enumerate(candidates[:5], 1):
            lines.append(
                f"{i}. \"{c['example_queries'][0]}\""
                f"  ({c['count']} queries)"
            )
            lines.append(
                f"   Pattern: `{c['query_pattern']}`"
            )
            lines.append(
                f"   → Looks like a candidate for a {persona} workflow skill."
            )
            lines.append(
                f"   → `kb-cli skill-builder --resume {c['id']}`"
            )
            lines.append("")

        lines.append(
            "Run `kb-cli skill-builder --resume <id>` to scaffold a skill from a candidate."
        )
        return "\n".join(lines)

    def cluster_nightly(self, min_frequency: int = 3) -> list[dict]:
        """Run the nightly clustering pass. Returns all candidates above min_frequency."""
        log.info("skill_suggester: running nightly cluster pass")
        return self.get_candidates(min_frequency=min_frequency)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    def _load_records(self, persona: str | None = None) -> list[dict]:
        if not self._log_file.exists():
            return []
        records = []
        with self._log_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if persona and record.get("persona") != persona:
                        continue
                    records.append(record)
                except json.JSONDecodeError:
                    log.debug("skill_suggester: skipped malformed record")
        return records


def _cluster_id(fingerprint: str, persona: str) -> str:
    raw = f"{persona}:{fingerprint}"
    return "sc-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _to_pattern(fingerprint: str) -> str:
    """Convert a sorted token fingerprint to a readable pattern phrase."""
    tokens = fingerprint.split()
    if not tokens:
        return "(empty query)"
    if len(tokens) <= 3:
        return " ".join(tokens)
    return " ".join(tokens[:4]) + "..."
