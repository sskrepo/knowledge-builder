"""Git adapter — ADR-036 connectivity probe.

``verify_access`` is the ``access_probe_hook`` registered in the Git
connector manifest (framework/connectors/manifests/git.yaml).

The hook path ``framework.adapters.git_probe.verify_access`` is used
instead of the ADR-036 §C.2 example path because ``git_adapter.py`` is a
flat module (not a package) and cannot host a ``probe`` sub-module without
restructuring.  The manifest uses this corrected path.

Implementation note (migration phase):
  Config-only check — does NOT perform a live git clone or SSH connection.
  Checks whether a git clone cache path is configured and accessible.
  A live connectivity probe (e.g. ``git ls-remote``) should replace this
  once ADR-035 is fully wired.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def verify_access(reference: str = "", env: str = "", **kwargs) -> dict:
    """Lightweight Git connectivity check.

    Args:
        reference: Repository URL or local path being probed.
        env:       KBF environment name.  Falls back to KBF_ENV, then "laptop".
        **kwargs:  Additional context (ignored; future compatibility).

    Returns:
        dict with keys:
          - ``reachable`` (bool)
          - ``connector_id`` (str): always ``"git"``.
          - ``reference`` (str)
          - ``mode`` (str)
          - ``notes`` (str)
    """
    resolved_env = env or os.environ.get("KBF_ENV", "laptop")
    repo_root = _find_repo_root()

    try:
        import yaml as _yaml
        env_path = repo_root / "framework" / "config" / f"{resolved_env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        git_cfg = env_cfg.get("adapters_overrides", {}).get("git", {})
        clone_cache = git_cfg.get("clone_cache_path", "/var/lib/kb/git-cache")
        cache_path = Path(clone_cache).expanduser()
        accessible = cache_path.exists() or str(cache_path).startswith("/var/lib/kb")
        return {
            "reachable": accessible,
            "connector_id": "git",
            "reference": reference,
            "mode": "ssh",
            "notes": (
                f"Git clone cache path={clone_cache!r} "
                f"({'exists' if cache_path.exists() else 'not yet created'})."
            ),
        }
    except Exception as exc:
        log.warning("git_probe.verify_access: config read failed: %s", exc)
        return {
            "reachable": False,
            "connector_id": "git",
            "reference": reference,
            "mode": "error",
            "notes": f"Config read error: {exc}",
        }


def _find_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
