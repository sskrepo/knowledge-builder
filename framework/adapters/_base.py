"""Adapter Protocol — every source adapter implements this.

Per ADR-011. Both native and MCP modes of an adapter must satisfy this Protocol
and produce identical RawItem shapes so downstream parsers don't care which
mode produced an item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol, runtime_checkable


@dataclass
class RawItem:
    """Output of every adapter; input to every parser."""
    kind: str            # "confluence_page" | "jira_issue" | "git_file" | "udap_row" | ...
    source: str          # "confluence" | "jira" | "git" | "udap"
    source_id: str       # vendor-canonical id (page id, issue key, file path)
    payload: dict        # raw API response (or MCP tool result, normalized)
    metadata: dict = field(default_factory=dict)  # created_at, author, labels, …


@dataclass
class RawItemRef:
    """Light reference returned by list(); fetch() materializes it."""
    kind: str
    source: str
    source_id: str
    last_modified: datetime | None = None


@dataclass
class SourceQuery:
    """Adapter-agnostic source narrowing (e.g., Confluence space, Jira JQL)."""
    space: str | None = None
    jql: str | None = None
    paths: list[str] = field(default_factory=list)
    labels_include: list[str] = field(default_factory=list)
    labels_exclude: list[str] = field(default_factory=list)
    since: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ChangeEvent:
    kind: str            # "created" | "updated" | "deleted"
    source: str
    source_id: str
    timestamp: datetime


@dataclass
class HealthReport:
    healthy: bool
    mode: str
    notes: str = ""
    capabilities: list[str] = field(default_factory=list)


@runtime_checkable
class Adapter(Protocol):
    """Source-adapter Protocol. Every adapter implements this."""
    name: str
    kind: str
    mode: str

    def healthcheck(self) -> HealthReport: ...
    def list(self, source_query: SourceQuery) -> Iterable[RawItemRef]: ...
    def fetch(self, ref: RawItemRef) -> RawItem: ...
    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]: ...
    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]: ...
