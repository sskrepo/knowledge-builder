"""Git adapter — single-mode (SSH or HTTPS clone). STUB."""
from __future__ import annotations
from datetime import datetime
from typing import Iterable
from ._base import Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport

class GitAdapter:
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
