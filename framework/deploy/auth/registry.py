"""ConsumerRegistry — loads consumer manifests from consumer_manifests/*.yaml.

Token lookup is O(1), keyed by SHA-256 hash of the bearer token.

YAML field naming (PDD V3 §9.1):
  name               — human-readable consumer name
  token              — plaintext bearer token (dev/filestore mode only)
  tokenHash          — pre-hashed SHA-256 hex (production / OCI Vault mode)
  scopes             — list of allowed scopes: read | write | admin
  personaAllowlist   — list of allowed persona slugs; [] = all allowed
  rpmCap             — requests per minute cap (int)
  tokenBudgetPerRequest — max input tokens per request (int)
  userId             — stable user id (optional; defaults to SHA-1 prefix of filename stem)

If ``tokenHash`` is present in the YAML it is used directly (production path —
the real token never appears in the manifest file). Otherwise the plaintext
``token`` field is SHA-256 hashed on load (dev/filestore path).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import yaml

from .consumer import ConsumerManifest

log = logging.getLogger(__name__)


class ConsumerRegistry:
    """Loads and caches consumer manifests from a directory of *.yaml files.

    Manifests are loaded once at startup.  Token lookup is O(1) via the
    ``_by_token_hash`` dict, keyed by SHA-256(bearer_token).
    """

    def __init__(self, manifests_dir: Path) -> None:
        self._dir = Path(manifests_dir)
        self._by_token_hash: dict[str, ConsumerManifest] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, bearer_token: str) -> ConsumerManifest | None:
        """Return the ConsumerManifest for *bearer_token*, or ``None`` if not found.

        The incoming token is SHA-256 hashed before lookup so plaintext tokens
        are never held in the index.
        """
        token_hash = hashlib.sha256(bearer_token.encode()).hexdigest()
        return self._by_token_hash.get(token_hash)

    @property
    def consumer_count(self) -> int:
        """Number of successfully loaded consumer manifests."""
        return len(self._by_token_hash)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        if not self._dir.exists():
            log.warning("consumer_manifests dir not found: %s", self._dir)
            return

        for path in sorted(self._dir.glob("*.yaml")):
            try:
                with open(path) as fh:
                    cfg = yaml.safe_load(fh) or {}
                consumer = self._parse_manifest(path.stem, cfg)
                self._by_token_hash[consumer.token_hash] = consumer
                log.info(
                    "loaded consumer manifest: %s (scopes=%s rpm_cap=%d)",
                    consumer.name,
                    consumer.scopes,
                    consumer.rpm_cap,
                )
            except Exception as exc:
                log.error("failed to load manifest %s: %s", path, exc)

    def _parse_manifest(self, filename_stem: str, cfg: dict) -> ConsumerManifest:
        """Parse a raw YAML dict into a ConsumerManifest.

        Accepts camelCase YAML keys per PDD V3 §9.1.
        """
        # Token hashing: prefer pre-hashed tokenHash (production), else hash plaintext token
        token_hash: str
        if cfg.get("tokenHash"):
            token_hash = cfg["tokenHash"]
        else:
            token_raw: str = cfg.get("token", "")
            token_hash = hashlib.sha256(token_raw.encode()).hexdigest()

        # userId: explicit field wins; fall back to first 16 hex chars of SHA-1(stem)
        user_id: str = (
            cfg.get("userId")
            or hashlib.sha1(filename_stem.encode()).hexdigest()[:16]
        )

        return ConsumerManifest(
            name=cfg.get("name", filename_stem),
            token_hash=token_hash,
            scopes=list(cfg.get("scopes", ["read"])),
            persona_allowlist=list(cfg.get("personaAllowlist", [])),
            rpm_cap=int(cfg.get("rpmCap", 60)),
            token_budget_per_request=int(cfg.get("tokenBudgetPerRequest", 8000)),
            user_id=user_id,
        )
