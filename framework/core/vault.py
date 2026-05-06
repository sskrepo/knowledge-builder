"""OCI Vault client — resolves vault://kb/{slug} references at runtime.

Per ADR-010. 60s in-memory cache; rotation on signal.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

VAULT_PREFIX = "vault://kb/"
CACHE_TTL_SEC = 60


class VaultClient:
    def __init__(self, vault_ocid: str | None = None, region: str | None = None):
        self.vault_ocid = vault_ocid or os.environ.get("KBF_VAULT_OCID")
        self.region = region or os.environ.get("OCI_REGION", "us-ashburn-1")
        self._cache: dict[str, tuple[float, str]] = {}
        try:
            import oci  # type: ignore
            from oci.secrets import SecretsClient  # type: ignore
            self._oci = oci
            cfg = oci.config.from_file() if os.environ.get("OCI_AUTH_METHOD") == "config_file" \
                  else oci.auth.signers.get_resource_principals_signer()
            if isinstance(cfg, dict):
                self._client = SecretsClient(cfg)
            else:
                self._client = SecretsClient(config={}, signer=cfg)
        except ImportError:
            log.warning("oci SDK not installed; VaultClient is stub-mode")
            self._oci = None
            self._client = None

    def resolve(self, ref: str) -> str:
        """Resolve `vault://kb/<slug>` → secret value. Cached 60s."""
        if not ref.startswith(VAULT_PREFIX):
            raise ValueError(f"not a vault reference: {ref!r}")
        slug = ref[len(VAULT_PREFIX):]

        now = time.time()
        cached = self._cache.get(slug)
        if cached and now - cached[0] < CACHE_TTL_SEC:
            return cached[1]

        if self._client is None:
            # Stub mode — environment fallback for local dev
            envvar = "KBF_SECRET_" + slug.upper().replace("-", "_")
            val = os.environ.get(envvar, f"<unresolved:{slug}>")
            self._cache[slug] = (now, val)
            return val

        # Real OCI Vault read
        # 1. List secrets in compartment matching name=slug
        # 2. Fetch latest version contents (base64-decoded)
        bundles = self._client.list_secrets(
            compartment_id=self._compartment_ocid(),
            name=slug,
        ).data
        if not bundles:
            raise KeyError(f"vault secret not found: {slug}")
        secret_id = bundles[0].id
        from oci.secrets import models  # type: ignore
        bundle = self._client.get_secret_bundle(secret_id=secret_id).data
        import base64
        val = base64.b64decode(bundle.secret_bundle_content.content).decode()
        self._cache[slug] = (now, val)
        return val

    def _compartment_ocid(self) -> str:
        return os.environ.get("KBF_COMPARTMENT_OCID", "")

    def required_secrets_manifest(self, configs: list[dict]) -> list[str]:
        """Walk a list of config dicts and return all distinct vault:// refs."""
        import re
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
