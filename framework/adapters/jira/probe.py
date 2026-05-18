"""Jira adapter — ADR-036 connectivity probe.

``verify_access`` is the ``access_probe_hook`` registered in the Jira
connector manifest (framework/connectors/manifests/jira.yaml).

ADR-035 (CONFIGURE/INSPECT instance access-verify) calls this function
after ADR-036's registry type-check has confirmed that "jira" is a
supported connector type.

Implementation note (migration phase):
  Config-only check — does NOT make a live HTTP call.  Checks whether a
  Jira adapter mode is configured for the requested env.  A live HTTP
  probe should replace this in a follow-on task once ADR-035 is fully wired.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def verify_access(reference: str = "", env: str = "", **kwargs) -> dict:
    """Lightweight Jira connectivity check.

    Args:
        reference: Jira project key, JQL filter, or issue key being probed.
        env:       KBF environment name (e.g. "laptop", "staging").
                   Falls back to KBF_ENV env var, then "laptop".
        **kwargs:  Additional context (ignored; future compatibility).

    Returns:
        dict with keys:
          - ``reachable`` (bool)
          - ``connector_id`` (str): always ``"jira"``.
          - ``reference`` (str)
          - ``mode`` (str)
          - ``notes`` (str)
    """
    resolved_env = env or os.environ.get("KBF_ENV", "laptop")
    repo_root = _find_repo_root()

    try:
        import yaml as _yaml
        base_path = repo_root / "framework" / "config" / "adapters" / "jira.yaml"
        base_cfg: dict = {}
        if base_path.exists():
            base_cfg = _yaml.safe_load(base_path.read_text()) or {}
        env_path = repo_root / "framework" / "config" / f"{resolved_env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        overrides = env_cfg.get("adapters_overrides", {}).get("jira", {})
        merged = {**base_cfg, **overrides}
        mode = merged.get("mode", "")
        if not mode:
            return {
                "reachable": False,
                "connector_id": "jira",
                "reference": reference,
                "mode": "unconfigured",
                "notes": (
                    f"No Jira adapter mode configured for env={resolved_env!r}. "
                    "Set mode in framework/config/adapters/jira.yaml."
                ),
            }
        return {
            "reachable": True,
            "connector_id": "jira",
            "reference": reference,
            "mode": mode,
            "notes": f"Jira adapter mode={mode!r} configured for env={resolved_env!r}.",
        }
    except Exception as exc:
        log.warning("jira probe.verify_access: config read failed: %s", exc)
        return {
            "reachable": False,
            "connector_id": "jira",
            "reference": reference,
            "mode": "error",
            "notes": f"Config read error: {exc}",
        }


def _find_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
