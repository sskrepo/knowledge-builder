"""Git adapter — single-mode (SSH or HTTPS clone). STUB.

ADR-039 (DECISION-020): canonical_identity() stub added per ABC contract.
Full Git canonical identity implementation is deferred (see ADR-039 §11).
The stub raises NotImplementedError so the ABC contract is enforced structurally
and Git cannot silently skip identity resolution.
"""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from ._base import (
    Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport,
    AdapterWithIdentity, CanonicalResult,
)


class GitAdapter(AdapterWithIdentity):
    name = "git"
    kind = "git"
    mode = "ssh"

    def __init__(self, cfg: dict):
        self.cache_path = cfg.get("clone_cache_path", "/var/lib/kb/git-cache")
        self.depth = cfg.get("clone_depth", 1)

    def healthcheck(self) -> HealthReport:
        return HealthReport(healthy=True, mode=self.mode, notes="stub")

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        raise NotImplementedError("Phase 1 implementation")

    def fetch(self, ref: RawItemRef) -> RawItem:
        raise NotImplementedError("Phase 1 implementation")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # Use git push hooks to emit ChangeEvents
        raise NotImplementedError("Phase 1 implementation")

    def canonical_identity(self, reference: str, resource_type: str) -> CanonicalResult:
        """Git canonical identity — deferred to follow-up ADR (ADR-039 §11).

        Conceptual canonical_id forms (deferred):
          resource_type=file:   "{normalized_repo_url}:{ref}:{path}"
          resource_type=commit: "{normalized_repo_url}:{sha40}"
          resource_type=ref:    "{normalized_repo_url}:refs/{branch_or_tag}"

        The stub raises NotImplementedError (explicit; per ADR-036 §M.2) so
        the ABC contract is enforced and Git cannot silently pass without
        implementing this method.
        """
        raise NotImplementedError(
            "Git canonical_identity: full implementation pending — "
            "deferred to follow-up ADR per ADR-039 §11. "
            "Git adapter cannot be registered in the Connector Registry "
            "until this method is fully implemented."
        )
