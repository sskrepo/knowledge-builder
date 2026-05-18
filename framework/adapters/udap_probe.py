"""UDAP/Sentinel adapter — ADR-036 connectivity probe.

``verify_access`` is the ``access_probe_hook`` registered in the UDAP
connector manifest (framework/connectors/manifests/udap.yaml).

The hook path ``framework.adapters.udap_probe.verify_access`` is used
instead of the ADR-036 §C.2 example path because ``udap_adapter.py`` is a
flat module (not a package) and cannot host a ``probe`` sub-module without
restructuring.  The manifest uses this corrected path.

Implementation note (migration phase):
  Checks whether UDAP/Sentinel is configured via KBF_STORE_BACKEND or a
  dedicated UDAP config.  In filestore mode (``KBF_STORE_BACKEND=filestore``),
  always returns reachable=True using dev fixtures.  In production mode,
  checks for the required UDAP JDBC connection config.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def verify_access(reference: str = "", env: str = "", **kwargs) -> dict:
    """Lightweight UDAP/Sentinel connectivity check.

    Args:
        reference: Tenancy ID, region, or service name being probed.
        env:       KBF environment name.  Falls back to KBF_ENV, then "laptop".
        **kwargs:  Additional context (ignored; future compatibility).

    Returns:
        dict with keys:
          - ``reachable`` (bool)
          - ``connector_id`` (str): always ``"udap"``.
          - ``reference`` (str)
          - ``mode`` (str): ``"filestore"`` | ``"jdbc"`` | ``"unconfigured"``.
          - ``notes`` (str)
    """
    resolved_env = env or os.environ.get("KBF_ENV", "laptop")
    store_backend = os.environ.get("KBF_STORE_BACKEND", "").lower()

    if store_backend == "filestore":
        fixtures_dir = Path(__file__).resolve().parents[1] / "_dev_fixtures" / "fleet"
        return {
            "reachable": True,
            "connector_id": "udap",
            "reference": reference,
            "mode": "filestore",
            "notes": (
                f"UDAP in filestore mode — reading from dev fixtures at "
                f"{fixtures_dir} ({'exists' if fixtures_dir.exists() else 'missing'})."
            ),
        }

    repo_root = _find_repo_root()
    try:
        import yaml as _yaml
        env_path = repo_root / "framework" / "config" / f"{resolved_env}.yaml"
        env_cfg: dict = {}
        if env_path.exists():
            env_cfg = _yaml.safe_load(env_path.read_text()) or {}
        udap_cfg = env_cfg.get("udap", env_cfg.get("fleet", {}))
        jdbc_url = udap_cfg.get("jdbc_url", "")
        if not jdbc_url:
            return {
                "reachable": False,
                "connector_id": "udap",
                "reference": reference,
                "mode": "unconfigured",
                "notes": (
                    f"No UDAP JDBC URL configured for env={resolved_env!r}. "
                    "Set udap.jdbc_url in the environment config, or run with "
                    "KBF_STORE_BACKEND=filestore for dev/laptop mode."
                ),
            }
        return {
            "reachable": True,
            "connector_id": "udap",
            "reference": reference,
            "mode": "jdbc",
            "notes": f"UDAP JDBC configured for env={resolved_env!r}.",
        }
    except Exception as exc:
        log.warning("udap_probe.verify_access: config read failed: %s", exc)
        return {
            "reachable": False,
            "connector_id": "udap",
            "reference": reference,
            "mode": "error",
            "notes": f"Config read error: {exc}",
        }


def _find_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
