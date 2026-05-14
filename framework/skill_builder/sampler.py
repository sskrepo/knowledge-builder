"""sampler — fetch N real source samples for persona-team review.

Per ADR-015 §PREVIEW step and ADR-026 §Fix 2.

Live Confluence fetch (ADR-026):
  When source_query contains a page_id or page_url the sampler fetches the real
  page via the Confluence adapter (ConfluenceEmcpDirectAdapter or whichever mode
  is configured in the env YAML), regardless of KBF_STORE_BACKEND.  This gives
  the source-grounded schema review real content to compare against the candidate
  schema.

Filestore fallback:
  Retained for sources that have no page-id (label-only Confluence queries, Jira,
  Git) and for local dev without Confluence access.  A hard-fail is thrown only
  when require_live=True is passed explicitly and the live fetch fails.

No external services are required for local dev — set KBF_STORE_BACKEND=filestore
and omit page_id/page_url from source_query.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "framework" / "_dev_fixtures"


def fetch_samples(
    adapter_name: str,
    source_query: dict,
    n: int = 3,
    require_live: bool = False,
    kbf_env: str | None = None,
    repo_root: Path | None = None,
) -> list[dict]:
    """Fetch up to N source samples for review.

    Priority order:
      1. Live Confluence fetch — when source_query has page_id or page_url AND
         a Confluence adapter is configured (emcp_direct, codex_proxy, etc.).
         Returns real page content with source_citation set to the page URL.
      2. Filestore fixtures — when no live adapter is available or the adapter
         fails.  Reads JSON/JSONL files from _dev_fixtures/{adapter_name}/.
      3. Synthetic stubs — when no fixtures exist.

    Args:
        adapter_name: e.g. "confluence", "incidents", "fleet", "releases".
        source_query: adapter-specific filter, may include:
            - page_id: "20030556732" or page_url: "https://..."
            - space: "FAAAS"
            - labels: ["weekly-status"]
        n: maximum number of samples to return.
        require_live: if True and live fetch fails, raise RuntimeError.
        kbf_env: KBF_ENV value (default: read from environment).
        repo_root: framework root directory (default: auto-detected).

    Returns:
        List of raw source item dicts, each with at minimum a "source_citation" key
        and a "content" key with page markdown/text.
    """
    root = repo_root or REPO_ROOT
    env = kbf_env or os.environ.get("KBF_ENV", "laptop")

    # Live Confluence fetch when page-id or URL is given
    if adapter_name == "confluence":
        page_id = source_query.get("page_id") or source_query.get("pageId")
        page_url = source_query.get("page_url") or source_query.get("url")

        if page_id or page_url:
            try:
                return _fetch_confluence_live(
                    page_id=page_id,
                    page_url=page_url,
                    n=n,
                    kbf_env=env,
                    repo_root=root,
                )
            except Exception as exc:
                log.warning(
                    "fetch_samples: live Confluence fetch failed (%s) — falling back to fixtures",
                    exc,
                )
                if require_live:
                    raise RuntimeError(
                        f"fetch_samples: live fetch required but failed: {exc}"
                    ) from exc

    backend = os.environ.get("KBF_STORE_BACKEND", "filestore")
    if backend == "filestore":
        return _fetch_from_fixtures(adapter_name, source_query, n)

    return _fetch_from_adapter(adapter_name, source_query, n)


# ---------------------------------------------------------------------------
# Live Confluence fetch (ADR-026 Fix 2)
# ---------------------------------------------------------------------------

def _fetch_confluence_live(
    page_id: str | None,
    page_url: str | None,
    n: int,
    kbf_env: str,
    repo_root: Path,
) -> list[dict]:
    """Fetch up to n Confluence pages directly via the configured adapter.

    Resolves the adapter from config using the same factory as
    conversation.py::_build_confluence_adapter.  Returns a list of dicts
    compatible with review_extractions and the source-grounded review prompt.
    """
    from .conversation import _build_confluence_adapter
    from ..adapters._base import RawItemRef

    adapter = _build_confluence_adapter(kbf_env, repo_root)
    if adapter is None:
        raise RuntimeError(
            "fetch_samples: no Confluence adapter configured "
            f"(kbf_env={kbf_env!r}). Check framework/config/adapters/confluence.yaml "
            "and the env-specific overrides in framework/config/{kbf_env}.yaml."
        )

    # Collect source identifiers to fetch
    ids_to_fetch: list[str] = []
    if page_url:
        ids_to_fetch.append(page_url)
    if page_id and page_id not in ids_to_fetch:
        ids_to_fetch.append(str(page_id))

    samples: list[dict] = []
    for source_id in ids_to_fetch[:n]:
        try:
            ref = RawItemRef(kind="confluence_page", source="confluence", source_id=source_id)
            raw_item = adapter.fetch(ref)
            # RawItem has: payload (dict), metadata (dict), source_id, source, kind, content_hash
            meta = raw_item.metadata if hasattr(raw_item, "metadata") else {}
            payload = raw_item.payload if hasattr(raw_item, "payload") else {}

            # Extract body text — prefer the markdown body from emcp_direct normalize()
            body_text = (
                (payload.get("body") or {}).get("storage", {}).get("value", "")
                if isinstance(payload.get("body"), dict)
                else str(payload.get("body", ""))
            )
            if not body_text:
                body_text = str(payload)[:8000]

            title = meta.get("title") or payload.get("title") or source_id
            page_url_out = (
                meta.get("url")
                or payload.get("url")
                or (
                    f"https://confluence.oraclecorp.com/confluence/pages/viewpage.action"
                    f"?pageId={meta.get('id', source_id)}"
                    if not source_id.startswith("http")
                    else source_id
                )
            )

            samples.append({
                "source_citation": page_url_out,
                "title": title,
                "content": body_text[:8000],  # cap at 8k chars for prompt budget
                "space": meta.get("space") or (payload.get("space") or {}).get("key", ""),
                "labels": meta.get("labels", []),
                "version": meta.get("version"),
                "_live": True,
            })
            log.info(
                "fetch_samples: fetched live Confluence page id=%s title=%r len=%d",
                source_id, title, len(body_text),
            )
        except Exception as exc:
            log.warning(
                "fetch_samples: failed to fetch page %s: %s", source_id, exc
            )
            raise

    if not samples:
        raise RuntimeError(
            f"fetch_samples: live Confluence fetch returned no samples "
            f"for page_id={page_id!r} page_url={page_url!r}"
        )

    return samples


# ---------------------------------------------------------------------------
# filestore path
# ---------------------------------------------------------------------------

def _fetch_from_fixtures(adapter_name: str, source_query: dict, n: int) -> list[dict]:
    fixture_dir = FIXTURES_ROOT / adapter_name
    if not fixture_dir.exists():
        log.warning(
            "no fixture dir for adapter=%s at %s; returning synthetic sample",
            adapter_name, fixture_dir,
        )
        return _synthetic_samples(adapter_name, n)

    samples: list[dict] = []
    last_path = fixture_dir
    for path in sorted(fixture_dir.iterdir()):
        if len(samples) >= n:
            break
        last_path = path
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
            s["source_citation"] = f"fixture://{adapter_name}/{last_path.name}"

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
# production adapter path (non-Confluence; Confluence is handled above)
# ---------------------------------------------------------------------------

def _fetch_from_adapter(adapter_name: str, source_query: dict, n: int) -> list[dict]:
    log.info(
        "fetch_samples production path not yet implemented for adapter=%s; "
        "set KBF_STORE_BACKEND=filestore for local dev",
        adapter_name,
    )
    return _synthetic_samples(adapter_name, n)
