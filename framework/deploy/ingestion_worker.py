"""OCI Functions / OCI Compute ingestion worker entrypoint.

On laptop (KBF_ENV=laptop or no env): runs in fixture mode — reads HTML from
framework/_dev_fixtures/confluence_pages/<SPACE>/ and writes markdown to ~/.kbf/wiki/.

In production (KBF_ENV=staging or prod): uses ConfluenceNativeAdapter with a real
API token from Vault/OCI Secrets.  ADB sources use direct oracledb queries.
"""
from __future__ import annotations
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def main(persona_builder: str | None = None):
    """Run ingestion for one persona builder (or all if None).

    Reads framework/persona_builders/*.yaml, skips non-production entries, and
    for each KB with a Confluence source calls ConfluenceWikiIngestor.ingest_space().
    On laptop this uses dev fixture HTML; in production it calls the real REST API.
    """
    import yaml
    from ..ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor
    from .mcp_server import _load_env_llm_overrides

    REPO_ROOT = Path(__file__).resolve().parents[2]
    builders_dir = REPO_ROOT / "framework" / "persona_builders"

    kbf_env = os.environ.get("KBF_ENV", "laptop")
    is_laptop = kbf_env == "laptop"

    builder_files = sorted(builders_dir.glob("*.yaml"))
    if persona_builder:
        builder_files = [b for b in builder_files if b.stem == persona_builder]

    # Shared ingestor — fixture mode on laptop, real adapter in production
    # In production, pass adapter=ConfluenceNativeAdapter(cfg) here.
    ingestor = ConfluenceWikiIngestor(adapter=None)

    total_stats = {"pages_new": 0, "pages_updated": 0, "pages_unchanged": 0, "skipped_builders": 0}

    for bf in builder_files:
        if bf.name.startswith("_"):
            continue
        try:
            with open(bf) as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            log.warning("could not parse %s: %s", bf, e)
            continue

        if cfg.get("status") != "production":
            log.info("skipping %s (status=%s)", bf.stem, cfg.get("status"))
            total_stats["skipped_builders"] += 1
            continue

        persona = cfg.get("persona", bf.stem)
        log.info("ingesting persona %s — %d KBs", persona, len(cfg.get("knowledge_bases", [])))

        for kb in cfg.get("knowledge_bases", []):
            kb_name = kb.get("name", "?")
            for src in kb.get("sources", []):
                kind = src.get("kind")
                if kind == "confluence":
                    space = src.get("space", "")
                    labels = src.get("include_labels") or src.get("labels") or []
                    try:
                        stats = ingestor.ingest_space(space, labels or None)
                        log.info(
                            "  %s.%s ← Confluence %s: new=%d updated=%d unchanged=%d",
                            persona, kb_name, space,
                            stats["pages_new"], stats["pages_updated"], stats["pages_unchanged"],
                        )
                        total_stats["pages_new"] += stats["pages_new"]
                        total_stats["pages_updated"] += stats["pages_updated"]
                        total_stats["pages_unchanged"] += stats["pages_unchanged"]
                    except Exception as e:
                        log.error("  %s.%s Confluence ingest failed: %s", persona, kb_name, e)
                elif kind == "adb":
                    # ADB sources are read at query time via retrieval tools (text_to_sql etc.)
                    # — no separate ingest step needed.
                    log.info("  %s.%s ← ADB table %s (query-time retrieval, no ingest needed)",
                             persona, kb_name, src.get("table", "?"))
                elif kind == "jira":
                    # Phase 2: Jira adapter wiring
                    log.info("  %s.%s ← Jira (Phase 2 — not yet wired)", persona, kb_name)
                elif kind == "git":
                    # Phase 2: Git adapter wiring
                    log.info("  %s.%s ← Git (Phase 2 — not yet wired)", persona, kb_name)
                else:
                    log.warning("  %s.%s unknown source kind %r — skipping", persona, kb_name, kind)

    log.info(
        "ingestion_worker done: new=%d updated=%d unchanged=%d skipped_builders=%d",
        total_stats["pages_new"], total_stats["pages_updated"],
        total_stats["pages_unchanged"], total_stats["skipped_builders"],
    )
    return total_stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    persona = sys.argv[1] if len(sys.argv) > 1 else None
    stats = main(persona)
    print(
        f"Done: {stats['pages_new']} new, {stats['pages_updated']} updated, "
        f"{stats['pages_unchanged']} unchanged, "
        f"{stats['skipped_builders']} builders skipped (non-production)"
    )
