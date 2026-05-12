"""build_artifact_store — factory function (per ADR-021 §factory.py).

Selection logic:
  KBF_ARTIFACT_STORE_BACKEND=oci     → OciArtifactStore  (explicit override)
  KBF_ARTIFACT_STORE_BACKEND=filestore → FilestoreArtifactStore (explicit override)
  KBF_ENV in (staging, production)   → OciArtifactStore  (auto)
  everything else                    → FilestoreArtifactStore (default)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def build_artifact_store(pool=None, env: str = ""):
    """Return an ArtifactStore appropriate for the current environment.

    Args:
        pool:  ADB connection pool (unused; kept for symmetry with build_session_store).
        env:   Environment name override.  Defaults to KBF_ENV env var.

    Returns:
        ArtifactStore instance (FilestoreArtifactStore or OciArtifactStore).
    """
    kbf_env = env or os.environ.get("KBF_ENV", "")
    backend = os.environ.get("KBF_ARTIFACT_STORE_BACKEND", "")

    use_oci = backend == "oci" or (
        not backend and kbf_env in ("staging", "production")
    )

    store_root = os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))

    if use_oci:
        cfg = _load_oci_cfg(store_root=store_root, kbf_env=kbf_env)
        from .oci import OciArtifactStore
        log.info(
            "ArtifactStore: OCI mode (env=%s bucket=%s namespace=%s)",
            kbf_env, cfg.get("bucket"), cfg.get("namespace"),
        )
        return OciArtifactStore(cfg)

    from .filestore import FilestoreArtifactStore
    log.info("ArtifactStore: filestore mode (root=%s)", store_root)
    return FilestoreArtifactStore(store_root=store_root)


def _load_oci_cfg(store_root: str, kbf_env: str) -> dict:
    """Load OCI artifact store config from env vars and env YAML.

    Priority: env vars > YAML values > defaults.
    """
    # Defaults
    cfg: dict = {
        "namespace": "",
        "bucket": "kbf-uploads",
        "region": "eu-frankfurt-1",
        "store_root": store_root,
        "kbf_env": kbf_env,
        "oci_profile": "adpcpprod",
    }

    # Try to load from env YAML (same mechanism as _init_laptop_adb_pool)
    if kbf_env:
        yaml_cfg = _read_yaml_artifact_cfg(kbf_env)
        if yaml_cfg:
            cfg.update({k: v for k, v in yaml_cfg.items() if v})

    # Env var overrides (highest priority)
    if os.environ.get("KBF_ARTIFACT_OCI_NAMESPACE"):
        cfg["namespace"] = os.environ["KBF_ARTIFACT_OCI_NAMESPACE"]
    if os.environ.get("KBF_ARTIFACT_OCI_BUCKET"):
        cfg["bucket"] = os.environ["KBF_ARTIFACT_OCI_BUCKET"]
    if os.environ.get("KBF_ARTIFACT_OCI_REGION"):
        cfg["region"] = os.environ["KBF_ARTIFACT_OCI_REGION"]
    if os.environ.get("KBF_ARTIFACT_OCI_PROFILE"):
        cfg["oci_profile"] = os.environ["KBF_ARTIFACT_OCI_PROFILE"]

    if not cfg["namespace"]:
        log.warning(
            "OciArtifactStore: namespace not configured. "
            "Set KBF_ARTIFACT_OCI_NAMESPACE or artifact_store.oci.namespace in %s.yaml",
            kbf_env,
        )

    return cfg


def _read_yaml_artifact_cfg(kbf_env: str) -> dict:
    """Read artifact_store.oci section from {kbf_env}.yaml."""
    try:
        import yaml
        config_dir = Path(__file__).resolve().parents[3] / "config"
        yaml_path = config_dir / f"{kbf_env}.yaml"
        if not yaml_path.exists():
            return {}
        with yaml_path.open() as f:
            data = yaml.safe_load(f) or {}
        artifact_cfg = data.get("artifact_store", {})
        oci_cfg = artifact_cfg.get("oci", {})
        return {
            "namespace": oci_cfg.get("namespace", ""),
            "bucket": oci_cfg.get("bucket", "kbf-uploads"),
            "region": oci_cfg.get("region", "eu-frankfurt-1"),
        }
    except Exception as exc:
        log.debug("Could not read artifact_store config from YAML: %s", exc)
        return {}
