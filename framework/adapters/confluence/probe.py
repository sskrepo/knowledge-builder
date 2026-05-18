"""Confluence adapter — ADR-036 connectivity probe.

``verify_access`` is the ``access_probe_hook`` registered in the Confluence
connector manifest (framework/connectors/manifests/confluence.yaml).

ADR-035 (CONFIGURE/INSPECT instance access-verify) calls this function
after ADR-036's registry type-check has already confirmed that "confluence"
is a supported connector type.  This probe checks whether the specific
Confluence instance / credentials are reachable.

Implementation note (migration phase):
  This is the ADR-036 migration stub.  The function performs a lightweight
  config-only availability check (no live HTTP call) identical to the
  existing ``_check_confluence_adapter_available`` helper in conversation.py.
  A live HTTP probe (calling the Confluence ``/rest/api/content`` health
  endpoint) should replace this in a follow-on task once ADR-035 instance
  access-verify is fully wired.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def verify_access(reference: str = "", env: str = "", **kwargs) -> dict:
    """Lightweight Confluence connectivity check.

    Called by ADR-035 INSPECT_SOURCES instance access-verify.

    Args:
        reference: Confluence space key, page ID, or URL being probed.
        env:       KBF environment name (e.g. "laptop", "staging").
                   Falls back to KBF_ENV env var, then "laptop".
        **kwargs:  Additional context (ignored; future compatibility).

    Returns:
        dict with keys:
          - ``reachable`` (bool): True when a Confluence adapter mode is
            configured for this env and credentials appear present.
          - ``connector_id`` (str): always ``"confluence"``.
          - ``reference`` (str): the reference that was probed.
          - ``mode`` (str): the configured adapter mode, or ``"unconfigured"``.
          - ``notes`` (str): human-readable status detail.
    """
    resolved_env = env or os.environ.get("KBF_ENV", "laptop")
    repo_root = _find_repo_root()

    try:
        import yaml as _yaml
        base_path = repo_root / "framework" / "config" / "adapters" / "confluence.yaml"
        base_cfg: dict = {}
        if base_path.exists():
            base_cfg = _yaml.safe_load(base_path.read_text()) or {}
        env_path = repo_root / "framework" / "config" / f"{resolved_env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        overrides = env_cfg.get("adapters_overrides", {}).get("confluence", {})
        merged = {**base_cfg, **overrides}
        mode = merged.get("mode", "")
        if not mode:
            return {
                "reachable": False,
                "connector_id": "confluence",
                "reference": reference,
                "mode": "unconfigured",
                "notes": (
                    f"No Confluence adapter mode configured for env={resolved_env!r}. "
                    "Set mode in framework/config/adapters/confluence.yaml."
                ),
            }
        return {
            "reachable": True,
            "connector_id": "confluence",
            "reference": reference,
            "mode": mode,
            "notes": f"Confluence adapter mode={mode!r} configured for env={resolved_env!r}.",
        }
    except Exception as exc:
        log.warning("confluence probe.verify_access: config read failed: %s", exc)
        return {
            "reachable": False,
            "connector_id": "confluence",
            "reference": reference,
            "mode": "error",
            "notes": f"Config read error: {exc}",
        }


def _find_repo_root() -> Path:
    """Return the repository root (the directory containing ``framework/``)."""
    return Path(__file__).resolve().parents[3]
