"""ConsumerManifest dataclass — parsed representation of a consumer_manifests/*.yaml file."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ConsumerManifest:
    """Parsed consumer manifest from consumer_manifests/{consumer}.yaml.

    Fields match the camelCase YAML keys defined in PDD V3 §9.1 but are stored
    here in snake_case (Python convention). The registry translates on load.
    """

    name: str
    """Human-readable consumer name, e.g. 'dev-local' or 'sravan-laptop'."""

    token_hash: str
    """SHA-256 hex digest of the bearer token.  Never stored plaintext at runtime."""

    scopes: list[str]
    """Allowed operation scopes: 'read', 'write', 'admin'."""

    persona_allowlist: list[str]
    """Personas this consumer may query.  Empty list = all personas allowed."""

    rpm_cap: int
    """Maximum requests per minute (sliding window, per-process in v1)."""

    token_budget_per_request: int
    """Maximum input tokens per single /ask or authorSkill call."""

    user_id: str
    """Stable user identifier.  Derived from manifest name (SHA-1 prefix) if not
    supplied explicitly in the YAML via the ``userId`` field."""

    def has_scope(self, scope: str) -> bool:
        """Return True if *scope* is in this consumer's allowed scopes."""
        return scope in self.scopes

    def allows_persona(self, persona: str) -> bool:
        """Return True if this consumer may query *persona*.

        An empty ``persona_allowlist`` means all personas are permitted.
        """
        if not self.persona_allowlist:
            return True
        return persona in self.persona_allowlist
