"""OCI Functions / OCI Compute ingestion worker entrypoint.

Source of truth for KB entries is KBF_PERSONA_BUILDERS in ADB (Option B /
DECISION-006).  Disk persona_builders/*.yaml is a fallback used only when no
ADB pool can be built (pure fixture/laptop mode with KBF_STORE_BACKEND=filestore).

On laptop with ADB (KBF_ENV=laptop, pool available): reads production KB entries
from ADB and runs ConfluenceWikiIngestor in fixture mode (HTML from
framework/_dev_fixtures/confluence_pages/<SPACE>/).

In production (KBF_ENV=staging or prod): reads from ADB, uses real
ConfluenceNativeAdapter with API token from Vault/OCI Secrets.
"""
from __future__ import annotations
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Confluence adapter builder
# ---------------------------------------------------------------------------

def _build_confluence_adapter(cfg: dict, kbf_env: str):
    """Build the Confluence adapter from config.

    cfg is the already-loaded env YAML (laptop.yaml / staging.yaml / prod.yaml).
    Merges base framework/config/adapters/confluence.yaml with
    cfg["adapters_overrides"]["confluence"] so that laptop can set
    mode: codex_proxy and production can set mode: native / mcp.
    Returns None only when no Confluence config exists (fixture HTML fallback).
    """
    try:
        import yaml as _yaml

        # Load base adapter config
        base_path = REPO_ROOT / "framework" / "config" / "adapters" / "confluence.yaml"
        base_cfg: dict = {}
        if base_path.exists():
            base_cfg = _yaml.safe_load(base_path.read_text()) or {}

        # Env-specific overrides from the already-loaded cfg
        overrides = cfg.get("adapters_overrides", {}).get("confluence", {})

        # Merge: base first, env overrides on top
        merged = {**base_cfg, **overrides}
        mode = merged.get("mode", "")

        if not mode:
            log.info("Confluence mode not configured — using fixture mode")
            return None

        if mode == "codex_proxy":
            from ..adapters.confluence.codex_proxy import ConfluenceCodexProxyAdapter
            cp_cfg = {**merged.get("codex_proxy", {}), **overrides.get("codex_proxy", {})}
            log.info("ingestion_worker: Confluence codex_proxy server_name=%s", cp_cfg.get("server_name"))
            return ConfluenceCodexProxyAdapter(cp_cfg)

        if mode == "codex_cli":
            from ..adapters.confluence.codex_cli import ConfluenceCodexCLIAdapter
            cc_cfg = {**merged.get("codex_cli", {}), **overrides.get("codex_cli", {})}
            log.info("ingestion_worker: Confluence codex_cli server_name=%s", cc_cfg.get("server_name"))
            return ConfluenceCodexCLIAdapter(cc_cfg)

        if mode == "emcp_direct":
            from ..adapters.confluence.emcp_direct import ConfluenceEmcpDirectAdapter
            ed_cfg = {**merged.get("emcp_direct", {}), **overrides.get("emcp_direct", {})}
            log.info(
                "ingestion_worker: Confluence emcp_direct server_name=%s",
                ed_cfg.get("server_name"),
            )
            return ConfluenceEmcpDirectAdapter(ed_cfg)

        if mode == "mcp":
            from ..adapters.confluence.mcp import ConfluenceMcpAdapter
            log.info("ingestion_worker: Confluence mcp endpoint=%s", merged.get("mcp", {}).get("endpoint"))
            return ConfluenceMcpAdapter(merged.get("mcp", {}))

        if mode == "native":
            from ..adapters.confluence.native import ConfluenceNativeAdapter
            log.info("ingestion_worker: Confluence native base_url=%s", merged.get("native", {}).get("base_url"))
            return ConfluenceNativeAdapter(merged.get("native", {}))

        log.info("ingestion_worker: Confluence mode=%r not recognised — using fixture mode", mode)
        return None
    except Exception as exc:
        log.info("ingestion_worker: could not build Confluence adapter (%s) — using fixture mode", exc)
        return None


# ---------------------------------------------------------------------------
# KB entry loading — ADB primary, disk fallback
# ---------------------------------------------------------------------------

def _load_kb_entries_from_adb(skill_store, persona_filter: str | None = None) -> list[dict]:
    """Return production KB entries from KBF_PERSONA_BUILDERS (Option B source of truth).

    Each returned dict has keys: persona, kb_name, kb (parsed content_yaml dict).
    """
    import yaml as _yaml

    try:
        rows = skill_store.list_persona_builder_kbs(status="production")
    except Exception as exc:
        log.error("list_persona_builder_kbs failed: %s", exc)
        return []

    if persona_filter:
        rows = [r for r in rows if r.get("persona") == persona_filter]

    entries: list[dict] = []
    for row in rows:
        raw = row.get("content_yaml", "") or ""
        # content_yaml may be a LOB object
        if hasattr(raw, "read"):
            raw = raw.read()
        try:
            kb_dict = _yaml.safe_load(raw) or {}
        except Exception as exc:
            log.warning(
                "could not parse content_yaml for %s.%s: %s",
                row.get("persona"), row.get("kb_name"), exc,
            )
            continue
        entries.append({
            "persona":  row.get("persona", ""),
            "kb_name":  row.get("kb_name", ""),
            "kb":       kb_dict,
        })

    log.info(
        "loaded %d production KB entries from ADB%s",
        len(entries),
        f" (persona={persona_filter})" if persona_filter else "",
    )
    return entries


def _load_kb_entries_from_disk(persona_filter: str | None = None) -> list[dict]:
    """Fallback: load production KB entries from framework/persona_builders/*.yaml.

    Used only when ADB is unavailable (pure filestore/fixture mode).
    """
    import yaml as _yaml

    builders_dir = REPO_ROOT / "framework" / "persona_builders"
    builder_files = sorted(builders_dir.glob("*.yaml"))
    if persona_filter:
        builder_files = [b for b in builder_files if b.stem == persona_filter]

    entries: list[dict] = []
    for bf in builder_files:
        if bf.name.startswith("_"):
            continue
        try:
            cfg = _yaml.safe_load(bf.read_text()) or {}
        except Exception as exc:
            log.warning("could not parse %s: %s", bf, exc)
            continue

        if cfg.get("status") != "production":
            log.info("disk fallback: skipping %s (status=%s)", bf.stem, cfg.get("status"))
            continue

        persona = cfg.get("persona", bf.stem)
        for kb in cfg.get("knowledge_bases", []):
            entries.append({
                "persona":  persona,
                "kb_name":  kb.get("name", "?"),
                "kb":       kb,
            })

    log.info("disk fallback: loaded %d production KB entries", len(entries))
    return entries


def _build_skill_store(cfg: dict):
    """Build an AdbSkillStore from config dict, or return None on failure."""
    try:
        from ..core.adb_pool import create_adb_pool
        from .skill_store.adb import AdbSkillStore
        import os as _os
        from pathlib import Path as _Path

        adb_cfg = cfg.get("adb", {})

        def _resolve(ref: str) -> str:
            if ref and ref.startswith("env://"):
                val = _os.environ.get(ref[6:], "")
                if not val:
                    raise RuntimeError(f"Secret env var not set: {ref[6:]}")
                return val
            return ref or ""

        pool_dict = {
            "deployment_mode": cfg.get("deployment_mode", "laptop"),
            "adb": {
                "service_name":    adb_cfg.get("dsn") or adb_cfg.get("service_name", ""),
                "wallet_path":     str(_Path(adb_cfg.get("wallet_path", "")).expanduser()),
                "user":            adb_cfg.get("admin_user", "Admin"),
                "password":        _resolve(adb_cfg.get("admin_password_secret", "")),
                "wallet_password": _resolve(adb_cfg.get("wallet_password_secret", "")),
            },
            "bastion": cfg.get("bastion", {}),
        }
        pool = create_adb_pool(pool_dict)
        return AdbSkillStore(pool)
    except Exception as exc:
        log.info("ADB not available (%s) — will fall back to disk persona builders", exc)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(persona_builder: str | None = None, skill_store=None):
    """Run ingestion for all production KB entries (or one persona if specified).

    KB entries are read from KBF_PERSONA_BUILDERS in ADB (Option B source of
    truth).  Falls back to framework/persona_builders/*.yaml when ADB is
    unavailable.

    Args:
        persona_builder: Optional persona slug to restrict ingestion to one persona.
        skill_store:     Injected AdbSkillStore (for testing). Built from env config
                         if None.
    """
    import yaml as _yaml
    from ..ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor

    kbf_env = os.environ.get("KBF_ENV", "laptop")

    # ── Load env config ────────────────────────────────────────────────────
    config_path = REPO_ROOT / "framework" / "config" / f"{kbf_env}.yaml"
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = _yaml.safe_load(config_path.read_text()) or {}
        except Exception as exc:
            log.warning("could not load config %s: %s", config_path, exc)

    # ── Resolve skill_store ────────────────────────────────────────────────
    if skill_store is None:
        store_backend = os.environ.get("KBF_STORE_BACKEND", "")
        if store_backend == "filestore":
            log.info("KBF_STORE_BACKEND=filestore — skipping ADB, using disk fallback")
        else:
            skill_store = _build_skill_store(cfg)

    # ── Load KB entries ────────────────────────────────────────────────────
    if skill_store is not None:
        kb_entries = _load_kb_entries_from_adb(skill_store, persona_filter=persona_builder)
    else:
        kb_entries = _load_kb_entries_from_disk(persona_filter=persona_builder)

    if not kb_entries:
        log.info("no production KB entries found — nothing to ingest")
        return {"pages_new": 0, "pages_updated": 0, "pages_unchanged": 0, "skipped_builders": 0}

    # ── Ingest each KB entry ───────────────────────────────────────────────
    confluence_adapter = _build_confluence_adapter(cfg, kbf_env)
    ingestor = ConfluenceWikiIngestor(adapter=confluence_adapter)
    log.info(
        "ingestion_worker: Confluence adapter mode=%s",
        "live" if confluence_adapter is not None else "fixture",
    )

    total_stats = {"pages_new": 0, "pages_updated": 0, "pages_unchanged": 0, "skipped_builders": 0}

    for entry in kb_entries:
        persona  = entry["persona"]
        kb_name  = entry["kb_name"]
        kb       = entry["kb"]

        for src in kb.get("sources", []):
            kind = src.get("kind")

            if kind == "confluence":
                space  = src.get("space", "")
                labels = src.get("include_labels") or src.get("labels") or []
                try:
                    stats = ingestor.ingest_space(space, labels or None)
                    pages_total = (
                        stats["pages_new"]
                        + stats["pages_updated"]
                        + stats["pages_unchanged"]
                    )
                    log.info(
                        "  %s.%s ← Confluence %s: new=%d updated=%d unchanged=%d total=%d",
                        persona, kb_name, space,
                        stats["pages_new"], stats["pages_updated"],
                        stats["pages_unchanged"], pages_total,
                    )
                    # 0 pages back from the adapter is a failed extraction, not a
                    # silent success — log as ERROR (visible in operator console)
                    # so the source is surfaced for investigation. Worker keeps
                    # processing the remaining KBs so one bad source doesn't kill
                    # the whole run.
                    if pages_total == 0:
                        log.error(
                            "  %s.%s ← Confluence %s: 0 pages returned (labels=%s). "
                            "KB extraction yielded nothing — check space key, label "
                            "filters, and codex/Confluence access.",
                            persona, kb_name, space, labels or "(none)",
                        )
                    total_stats["pages_new"]       += stats["pages_new"]
                    total_stats["pages_updated"]    += stats["pages_updated"]
                    total_stats["pages_unchanged"]  += stats["pages_unchanged"]
                except Exception as exc:
                    log.error("  %s.%s Confluence ingest failed: %s", persona, kb_name, exc)

            elif kind == "adb":
                # ADB sources are read at query time — no separate ingest step.
                log.info(
                    "  %s.%s ← ADB table %s (query-time retrieval, no ingest needed)",
                    persona, kb_name, src.get("table", "?"),
                )

            elif kind == "jira":
                log.info("  %s.%s ← Jira (Phase 2 — not yet wired)", persona, kb_name)

            elif kind == "git":
                log.info("  %s.%s ← Git (Phase 2 — not yet wired)", persona, kb_name)

            else:
                log.warning(
                    "  %s.%s unknown source kind %r — skipping", persona, kb_name, kind
                )

    log.info(
        "ingestion_worker done: new=%d updated=%d unchanged=%d",
        total_stats["pages_new"], total_stats["pages_updated"], total_stats["pages_unchanged"],
    )
    return total_stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    persona = sys.argv[1] if len(sys.argv) > 1 else None
    stats = main(persona)
    print(
        f"Done: {stats['pages_new']} new, {stats['pages_updated']} updated, "
        f"{stats['pages_unchanged']} unchanged"
    )
