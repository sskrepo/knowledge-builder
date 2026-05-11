"""sampler — fetch N real source samples for persona-team review.

Per ADR-015 §PREVIEW step. In filestore / laptop mode the sampler reads from
_dev_fixtures/. In production it delegates to the adapter registry. No external
services are required for local dev.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "framework" / "_dev_fixtures"


def fetch_samples(
    adapter_name: str,
    source_query: dict,
    n: int = 5,
) -> list[dict]:
    """Fetch up to N source samples for review.

    In filestore mode (KBF_STORE_BACKEND=filestore or no env set for production),
    this reads JSON/JSONL files from _dev_fixtures/{adapter_name}/.

    In production mode the adapter registry would be used; that path is a stub here.

    Args:
        adapter_name: e.g. "incidents", "fleet", "releases", "confluence", "jira".
        source_query: adapter-specific filter (e.g. {"jql": "...", "max": 5}).
        n: maximum number of samples to return.

    Returns:
        List of raw source item dicts, each with at minimum a "source_citation" key.
    """
    backend = os.environ.get("KBF_STORE_BACKEND", "filestore")

    if backend == "filestore":
        return _fetch_from_fixtures(adapter_name, source_query, n)

    return _fetch_from_adapter(adapter_name, source_query, n)


# ---------------------------------------------------------------------------
# filestore path
# ---------------------------------------------------------------------------

def _fetch_from_fixtures(adapter_name: str, source_query: dict, n: int) -> list[dict]:
    fixture_dir = FIXTURES_ROOT / adapter_name
    if not fixture_dir.exists():
        log.warning(
            "no fixture dir for adapter=%s at %s; returning empty sample",
            adapter_name, fixture_dir,
        )
        return _synthetic_samples(adapter_name, n)

    samples: list[dict] = []
    for path in sorted(fixture_dir.iterdir()):
        if len(samples) >= n:
            break
        if path.suffix == ".jsonl":
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and len(samples) < n:
                    try:
                        samples.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.debug("skipping malformed JSONL line in %s", path)
        elif path.suffix == ".json":
            try:
                data = json.loads(path.read_text())
                if isinstance(data, list):
                    samples.extend(data[:n - len(samples)])
                else:
                    samples.append(data)
            except json.JSONDecodeError:
                log.debug("skipping malformed JSON file %s", path)

    if not samples:
        return _synthetic_samples(adapter_name, n)

    for s in samples:
        if "source_citation" not in s:
            s["source_citation"] = f"fixture://{adapter_name}/{path.name}"

    return samples[:n]


def _synthetic_samples(adapter_name: str, n: int) -> list[dict]:
    return [
        {
            "source_citation": f"stub://{adapter_name}/sample-{i + 1}",
            "title": f"Sample {i + 1} from {adapter_name}",
            "content": f"[stub] replace with real {adapter_name} content",
            "_stub": True,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# production adapter path (stub — Phase 2 real impl uses adapter registry)
# ---------------------------------------------------------------------------

def _fetch_from_adapter(adapter_name: str, source_query: dict, n: int) -> list[dict]:
    log.info(
        "fetch_samples production path not yet implemented for adapter=%s; "
        "set KBF_STORE_BACKEND=filestore for local dev",
        adapter_name,
    )
    return _synthetic_samples(adapter_name, n)
