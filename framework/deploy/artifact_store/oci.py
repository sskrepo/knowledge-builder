"""OciArtifactStore — OCI Object Storage implementation (staging / production).

Auth strategy (per ADR-021 §OciArtifactStore design):
  KBF_ENV=staging|production  → OCI Python SDK, InstancePrincipalsSecurityTokenSigner
  KBF_ENV=laptop (forced OCI)  → OCI CLI subprocess with --auth security_token

Object key scheme:
  kbf-uploads/{synth_id}/{artifact_id}/{filename}

Temp area for resolve():
  {store_root}/uploads/tmp/{artifact_id}/{filename}
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ._base import ArtifactStore

log = logging.getLogger(__name__)

_SUPPORTED_ENVS_SDK = {"staging", "production"}


class OciArtifactStore(ArtifactStore):
    """Artifact storage backed by OCI Object Storage.

    Config dict keys:
      namespace   — OCI tenancy namespace
      bucket      — bucket name (e.g. 'kbf-uploads')
      region      — OCI region (e.g. 'eu-frankfurt-1')
      store_root  — local path for temp downloads (default ~/.kbf/store)
      oci_profile — OCI CLI profile name (used in CLI subprocess path only)
      kbf_env     — 'laptop', 'staging', or 'production'
    """

    def __init__(self, cfg: dict) -> None:
        # Namespace can come from (highest priority first):
        #   1. KBF_ARTIFACT_OCI_NAMESPACE env var
        #   2. cfg["namespace"] (from yaml config)
        #   3. auto-discovered via SDK get_namespace() (production/staging only)
        self._namespace_cfg = (
            os.environ.get("KBF_ARTIFACT_OCI_NAMESPACE")
            or cfg.get("namespace", "")
        )
        self._bucket = cfg["bucket"]
        self._region = cfg.get("region", "eu-frankfurt-1")
        self._store_root = Path(cfg.get("store_root", Path.home() / ".kbf" / "store"))
        self._oci_profile = (
            os.environ.get("KBF_ARTIFACT_OCI_PROFILE")
            or cfg.get("oci_profile", "adpcpprod")
        )
        self._kbf_env = cfg.get("kbf_env", os.environ.get("KBF_ENV", "laptop"))
        self._use_sdk = self._kbf_env in _SUPPORTED_ENVS_SDK

        # Temp dir for resolve() downloads
        self._tmp_root = self._store_root / "uploads" / "tmp"
        self._tmp_root.mkdir(parents=True, exist_ok=True)

        if self._use_sdk:
            self._client = self._build_sdk_client()
            # Auto-discover namespace from the tenancy if not explicitly configured.
            # On an OCI VM with InstancePrincipals this is always correct.
            if not self._namespace_cfg:
                self._namespace = self._client.get_namespace().data
                log.info(
                    "OciArtifactStore: auto-discovered namespace=%s",
                    self._namespace,
                )
            else:
                self._namespace = self._namespace_cfg
        else:
            self._client = None
            # CLI path — namespace must be known (either env var or config)
            if not self._namespace_cfg:
                raise ValueError(
                    "OciArtifactStore (CLI mode): namespace not set. "
                    "Set KBF_ARTIFACT_OCI_NAMESPACE or artifact_store.oci.namespace in config."
                )
            self._namespace = self._namespace_cfg
            log.info(
                "OciArtifactStore: using OCI CLI subprocess (kbf_env=%s profile=%s namespace=%s)",
                self._kbf_env, self._oci_profile, self._namespace,
            )

    # ------------------------------------------------------------------
    # ArtifactStore interface
    # ------------------------------------------------------------------

    def upload(
        self,
        synth_id: str,
        artifact_id: str,
        filename: str,
        data: bytes,
    ) -> None:
        key = self._key(synth_id, artifact_id, filename)
        if self._use_sdk:
            self._sdk_put(key, data, filename)
        else:
            self._cli_put(key, data)
        log.info(
            "OCI artifact upload: key=%s size=%d",
            key, len(data),
        )

    def resolve(self, artifact_id: str) -> Path | None:
        # We need the full key including synth_id — scan with prefix
        prefix = f"kbf-uploads/"
        objects = self._list_objects(prefix)
        # Filter to this artifact_id
        matching = [o for o in objects if f"/{artifact_id}/" in o]
        if not matching:
            log.warning("OCI artifact not found: artifact_id=%s", artifact_id)
            return None

        key = matching[0]
        filename = key.rstrip("/").rsplit("/", 1)[-1]

        dest_dir = self._tmp_root / artifact_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename

        if self._use_sdk:
            self._sdk_get(key, dest_path)
        else:
            self._cli_get(key, dest_path)

        return dest_path

    def cleanup(self, synth_id: str) -> None:
        prefix = f"kbf-uploads/{synth_id}/"
        keys = self._list_objects(prefix)
        for key in keys:
            if self._use_sdk:
                self._sdk_delete(key)
            else:
                self._cli_delete(key)
        log.info(
            "OCI artifact cleanup: synth_id=%s deleted %d objects",
            synth_id, len(keys),
        )
        # Also remove any local temp files
        for artifact_dir in self._tmp_root.glob("*"):
            if artifact_dir.is_dir():
                shutil.rmtree(artifact_dir, ignore_errors=True)

    def list_artifacts(self, synth_id: str) -> list[dict]:
        prefix = f"kbf-uploads/{synth_id}/"
        keys = self._list_objects(prefix)
        result = []
        for key in keys:
            parts = key.split("/")
            if len(parts) >= 4:
                result.append({
                    "artifact_id": parts[2],
                    "filename": parts[3],
                    "size_bytes": 0,   # OCI list doesn't return size by default
                    "uploaded_at": "",
                })
        return result

    # ------------------------------------------------------------------
    # OCI SDK helpers (staging / production — InstancePrincipals)
    # ------------------------------------------------------------------

    def _build_sdk_client(self):
        try:
            import oci
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            return oci.object_storage.ObjectStorageClient(
                config={}, signer=signer
            )
        except Exception as exc:
            log.error("Failed to build OCI SDK client: %s", exc)
            raise

    def _sdk_put(self, key: str, data: bytes, filename: str) -> None:
        import io
        self._client.put_object(
            namespace_name=self._namespace,
            bucket_name=self._bucket,
            object_name=key,
            put_object_body=io.BytesIO(data),
        )

    def _sdk_get(self, key: str, dest: Path) -> None:
        resp = self._client.get_object(
            namespace_name=self._namespace,
            bucket_name=self._bucket,
            object_name=key,
        )
        dest.write_bytes(resp.data.content)

    def _sdk_delete(self, key: str) -> None:
        try:
            self._client.delete_object(
                namespace_name=self._namespace,
                bucket_name=self._bucket,
                object_name=key,
            )
        except Exception as exc:
            log.warning("OCI delete failed for key=%s: %s", key, exc)

    def _sdk_list(self, prefix: str) -> list[str]:
        resp = self._client.list_objects(
            namespace_name=self._namespace,
            bucket_name=self._bucket,
            prefix=prefix,
        )
        return [o.name for o in resp.data.objects]

    # ------------------------------------------------------------------
    # OCI CLI subprocess helpers (laptop OCI override)
    # ------------------------------------------------------------------

    def _cli_base(self) -> list[str]:
        return [
            "oci", "os", "object",
            "--namespace", self._namespace,
            "--bucket-name", self._bucket,
            "--auth", "security_token",
            "--profile", self._oci_profile,
        ]

    def _cli_put(self, key: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".upload") as f:
            f.write(data)
            tmp_path = f.name
        try:
            cmd = self._cli_base() + ["put", "--name", key, "--file", tmp_path, "--force"]
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(
                    f"oci os object put failed: {result.stderr.decode()}"
                )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _cli_get(self, key: str, dest: Path) -> None:
        cmd = self._cli_base() + ["get", "--name", key, "--file", str(dest)]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(
                f"oci os object get failed: {result.stderr.decode()}"
            )

    def _cli_delete(self, key: str) -> None:
        cmd = self._cli_base() + ["delete", "--name", key, "--force"]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            log.warning("OCI CLI delete failed for key=%s: %s", key, result.stderr.decode())

    def _cli_list(self, prefix: str) -> list[str]:
        cmd = self._cli_base() + ["list", "--prefix", prefix, "--output", "json"]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            log.warning("OCI CLI list failed for prefix=%s: %s", prefix, result.stderr.decode())
            return []
        try:
            data = json.loads(result.stdout)
            return [o["name"] for o in data.get("data", [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Unified list (routes to SDK or CLI)
    # ------------------------------------------------------------------

    def _list_objects(self, prefix: str) -> list[str]:
        if self._use_sdk:
            return self._sdk_list(prefix)
        return self._cli_list(prefix)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key(synth_id: str, artifact_id: str, filename: str) -> str:
        return f"kbf-uploads/{synth_id}/{artifact_id}/{filename}"
