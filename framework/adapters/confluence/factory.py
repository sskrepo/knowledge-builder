"""Confluence adapter factory — shared utility for building the Confluence adapter.

Extracted from framework/skill_builder/conversation.py (ADR-032 P2-Infra).

The factory is consumed from two call sites:
  1. framework/skill_builder/conversation.py — INGEST state of authorSkill sessions
     (unchanged behavior; conversation.py imports build_confluence_adapter and
     re-exports it as _build_confluence_adapter for backward compat).
  2. framework/deploy/mcp_server.py — lifespan startup, optional dependency for
     ask_parameterized skill ephemeral fetch (ADR-032 P2).

Public API
----------
build_confluence_adapter(kbf_env, repo_root) -> adapter | None

    Merges base framework/config/adapters/confluence.yaml with env-specific
    adapters_overrides.confluence from {kbf_env}.yaml (e.g. laptop.yaml sets
    mode: codex_proxy for eMCP via Codex CLI).

    Returns the adapter instance on success, or None when:
      - no Confluence mode is configured (fixture/dev mode)
      - an exception occurs during adapter construction (logged as WARNING)

    This None-means-unavailable contract is load-bearing: callers that require
    a live adapter (ask_parameterized skill ephemeral fetch) must hard-fail with
    an actionable message when None is returned; they MUST NOT silently substitute
    content from a different source.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # adapter types are imported dynamically inside the function

log = logging.getLogger(__name__)


def build_confluence_adapter(kbf_env: str, repo_root: "Path"):
    """Build the Confluence adapter from config.

    Merges base framework/config/adapters/confluence.yaml with env-specific
    adapters_overrides.confluence from {kbf_env}.yaml (e.g. laptop.yaml sets
    mode: codex_proxy for eMCP via Codex CLI).  Returns None only when no
    Confluence config exists at all (falls back to fixture HTML).

    Behavior is identical to the original _build_confluence_adapter in
    framework/skill_builder/conversation.py — this is a relocation, not a rewrite.

    Parameters
    ----------
    kbf_env:
        Active environment name — "laptop", "staging", or "production".
        Used to find the env-specific YAML for adapter_overrides.
    repo_root:
        Absolute path to the repository root (parent of framework/).

    Returns
    -------
    Confluence adapter instance (ConfluenceCodexProxyAdapter,
    ConfluenceEmcpDirectAdapter, ConfluenceNativeAdapter, etc.) on success,
    or None if no adapter is configured or construction fails.
    """
    try:
        import yaml as _yaml

        # Load base adapter config
        base_path = repo_root / "framework" / "config" / "adapters" / "confluence.yaml"
        base_cfg: dict = {}
        if base_path.exists():
            base_cfg = _yaml.safe_load(base_path.read_text()) or {}

        # Load env-specific overrides (laptop.yaml, staging.yaml, prod.yaml)
        env_path = repo_root / "framework" / "config" / f"{kbf_env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        overrides = env_cfg.get("adapters_overrides", {}).get("confluence", {})

        # Merge: base first, env overrides on top
        merged = {**base_cfg, **overrides}
        mode = merged.get("mode", "")

        if not mode:
            log.info("Confluence mode not configured — using fixture mode")
            return None

        if mode == "codex_proxy":
            from ..confluence.codex_proxy import ConfluenceCodexProxyAdapter
            cp_cfg = {**merged.get("codex_proxy", {}), **overrides.get("codex_proxy", {})}
            log.info("Confluence adapter: codex_proxy server_name=%s", cp_cfg.get("server_name"))
            return ConfluenceCodexProxyAdapter(cp_cfg)

        if mode == "codex_cli":
            from ..confluence.codex_cli import ConfluenceCodexCLIAdapter
            cc_cfg = {**merged.get("codex_cli", {}), **overrides.get("codex_cli", {})}
            log.info("Confluence adapter: codex_cli server_name=%s", cc_cfg.get("server_name"))
            return ConfluenceCodexCLIAdapter(cc_cfg)

        if mode == "emcp_direct":
            # Direct HTTPS+OAuth to the emcp.oracle.com Confluence MCP server.
            # Uses the bearer token codex stored in the macOS Keychain (after
            # `codex mcp login central_confluence`). ~10s/page versus the 180s
            # timeout we saw with codex_proxy (BUG-queue-d3ec0).
            from ..confluence.emcp_direct import ConfluenceEmcpDirectAdapter
            ed_cfg = {**merged.get("emcp_direct", {}), **overrides.get("emcp_direct", {})}
            log.info(
                "Confluence adapter: emcp_direct server_name=%s",
                ed_cfg.get("server_name"),
            )
            return ConfluenceEmcpDirectAdapter(ed_cfg)

        if mode == "mcp":
            from ..confluence.mcp import ConfluenceMcpAdapter
            log.info("Confluence adapter: mcp endpoint=%s", merged.get("mcp", {}).get("endpoint"))
            return ConfluenceMcpAdapter(merged.get("mcp", {}))

        if mode == "native":
            from ..confluence.native import ConfluenceNativeAdapter
            log.info("Confluence adapter: native base_url=%s", merged.get("native", {}).get("base_url"))
            return ConfluenceNativeAdapter(merged.get("native", {}))

        log.info("Confluence mode=%r not recognised — using fixture mode", mode)
        return None
    except Exception as exc:
        log.warning("could not build Confluence adapter (%s) — using fixture mode", exc)
        return None
