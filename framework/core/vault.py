"""Secrets resolution — `vault://kb/<slug>` references.

Three backends:
  - `vault` (default; prod) — OCI Vault via oci-python-sdk
  - `local` — a YAML/JSON file on the machine (laptop dev)
  - `env`   — environment variables (CI / local test fallback)

Picked by env var `KBF_SECRETS_BACKEND`. Same `VaultClient.resolve(ref)` API
across all three.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VAULT_PREFIX = "vault://kb/"
CACHE_TTL_SEC = 60


class _LocalFileBackend:
    """Reads secrets from a local file (YAML or JSON).

    File path: $KBF_SECRETS_FILE (default: ~/.kbf/secrets.yaml).
    Format:
        secrets:
          openai-api-key: sk-proj-...
          adb-admin-dev: <password>
          confluence-readonly: <token>
          ...
    """
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get(
            "KBF_SECRETS_FILE",
            str(Path.home() / ".kbf" / "secrets.yaml"),
        ))
        self._loaded: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.warning("secrets file not found: %s — secrets will be unresolved",
                        self.path)
            return
        text = self.path.read_text()
        if self.path.suffix in {".json"}:
            data = json.loads(text)
        else:
            try:
                import yaml
                data = yaml.safe_load(text)
            except ImportError:
                log.error("PyYAML not installed; cannot read %s", self.path)
                return
        secrets = (data or {}).get("secrets", data) or {}
        if not isinstance(secrets, dict):
            log.error("secrets file %s must be a mapping or have a 'secrets' key", self.path)
            return
        # Normalize keys to slug form
        self._loaded = {str(k): str(v) for k, v in secrets.items()}
        log.info("local secrets backend loaded %d entries from %s",
                 len(self._loaded), self.path)

    def resolve(self, slug: str) -> str:
        if slug in self._loaded:
            return self._loaded[slug]
        # Fallback: env var KBF_SECRET_<SLUG_UPPER_SNAKE>
        envvar = "KBF_SECRET_" + slug.upper().replace("-", "_")
        v = os.environ.get(envvar)
        if v is not None:
            return v
        raise KeyError(f"secret not found in local file or env: {slug}")


class _EnvBackend:
    """Reads secrets purely from environment variables.

    Convention: `vault://kb/<slug>` → env var `KBF_SECRET_<SLUG_UPPER_SNAKE>`.
    Useful for CI.
    """
    def resolve(self, slug: str) -> str:
        envvar = "KBF_SECRET_" + slug.upper().replace("-", "_")
        v = os.environ.get(envvar)
        if v is None:
            raise KeyError(f"secret not found in env: {slug} ({envvar})")
        return v


class _VaultBackend:
    """OCI Vault — production default."""
    def __init__(self, vault_ocid: str | None = None,
                 region: str | None = None,
                 compartment_ocid: str | None = None):
        self.vault_ocid = vault_ocid or os.environ.get("KBF_VAULT_OCID")
        self.region = region or os.environ.get("OCI_REGION", "us-ashburn-1")
        self.compartment_ocid = compartment_ocid or os.environ.get("KBF_COMPARTMENT_OCID")
        self._client = self._build_client()

    def _build_client(self):
        try:
            import oci  # type: ignore
            from oci.secrets import SecretsClient  # type: ignore
        except ImportError:
            log.warning("oci SDK not installed; vault backend is stub-mode")
            return None

        auth_method = os.environ.get("OCI_AUTH_METHOD", "instance_principal")
        if auth_method == "config_file":
            config = oci.config.from_file(
                profile_name=os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT"))
            return SecretsClient(config)
        if auth_method == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            return SecretsClient(config={}, signer=signer)
        # default: instance principal
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return SecretsClient(config={}, signer=signer)

    def resolve(self, slug: str) -> str:
        if not self._client:
            raise RuntimeError("oci SDK not available; cannot resolve from vault")
        if not self.compartment_ocid:
            raise RuntimeError("KBF_COMPARTMENT_OCID not set; cannot resolve from vault")
        bundles = self._client.list_secrets(
            compartment_id=self.compartment_ocid,
            name=slug,
        ).data
        if not bundles:
            raise KeyError(f"vault secret not found: {slug}")
        secret_id = bundles[0].id
        bundle = self._client.get_secret_bundle(secret_id=secret_id).data
        import base64
        return base64.b64decode(bundle.secret_bundle_content.content).decode()


class VaultClient:
    """Façade. Picks backend via KBF_SECRETS_BACKEND env var.

    Backends:
      vault (default) | local | env
    """
    def __init__(self, backend: str | None = None, **backend_kwargs):
        self.backend_name = (
            backend
            or os.environ.get("KBF_SECRETS_BACKEND", "vault")
        )
        self._cache: dict[str, tuple[float, str]] = {}
        self._backend = self._build_backend(self.backend_name, backend_kwargs)
        log.info("VaultClient using backend: %s", self.backend_name)

    @staticmethod
    def _build_backend(name: str, kw: dict):
        if name == "local":
            return _LocalFileBackend(path=kw.get("path"))
        if name == "env":
            return _EnvBackend()
        return _VaultBackend(**kw)

    def resolve(self, ref: str) -> str:
        """Resolve `vault://kb/<slug>` → secret value. Cached 60s."""
        if not ref.startswith(VAULT_PREFIX):
            raise ValueError(f"not a vault reference: {ref!r}")
        slug = ref[len(VAULT_PREFIX):]

        now = time.time()
        cached = self._cache.get(slug)
        if cached and now - cached[0] < CACHE_TTL_SEC:
            return cached[1]

        try:
            val = self._backend.resolve(slug)
        except KeyError:
            # Final fallback: env var even when backend != env
            envvar = "KBF_SECRET_" + slug.upper().replace("-", "_")
            val = os.environ.get(envvar, f"<unresolved:{slug}>")
            log.warning("secret %s unresolved by %s backend; using env fallback",
                        slug, self.backend_name)

        self._cache[slug] = (now, val)
        return val

    def required_secrets_manifest(self, configs: list[dict]) -> list[str]:
        """Walk config dicts and return all distinct vault:// refs."""
        refs: set[str] = set()

        def walk(o: Any) -> None:
            if isinstance(o, str) and o.startswith(VAULT_PREFIX):
                refs.add(o)
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for c in configs:
            walk(c)
        return sorted(refs)
