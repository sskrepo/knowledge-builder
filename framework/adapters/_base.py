"""Adapter Protocol — every source adapter implements this.

Per ADR-011. Both native and MCP modes of an adapter must satisfy this Protocol
and produce identical RawItem shapes so downstream parsers don't care which
mode produced an item.

ADR-039 (DECISION-020): adds CanonicalRef / Unresolvable types and the
AdapterWithIdentity ABC. Every adapter registered in the Connector Registry
MUST implement canonical_identity(reference, resource_type) -> CanonicalResult.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol, Union, runtime_checkable


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


# ---------------------------------------------------------------------------
# ADR-039 (DECISION-020): Canonical identity types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalRef:
    """Canonical identifier for a source resource, computed once at author/bind time.

    The ONLY type returned on success from canonical_identity(). Used for
    two-sided canonical==canonical comparison (INGEST stamps it; executor compares it).

    Attributes:
        connector_id:  registered connector (e.g. "confluence", "jira", "git").
        resource_type: per ADR-036 manifest resource_types (e.g. "page", "issue").
        canonical_id:  the stable, connector-defined primary key as a string.
                       For Confluence: numeric content/page ID (e.g. "18625350641").
                       For Jira issue/epic: numeric internal issue ID (e.g. "100042").
                       For Jira filter/sprint/project: numeric ID.
                       For Git: "{normalized_repo_url}:{ref}:{path}" (file/commit/ref).
                       For UDAP: SQL primary key string.
        display_hint:  optional human-readable label (title, issue key).
                       NEVER used for identity comparison — display only.
    """
    connector_id: str
    resource_type: str
    canonical_id: str
    display_hint: str = ""

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CanonicalRef):
            return False
        return (
            self.connector_id == other.connector_id
            and self.resource_type == other.resource_type
            and self.canonical_id == other.canonical_id
        )

    def __hash__(self) -> int:
        return hash((self.connector_id, self.resource_type, self.canonical_id))


# Unresolvable reason constants
UNRESOLVABLE_NOT_FOUND   = "ERROR_NOT_FOUND"
UNRESOLVABLE_NO_ACCESS   = "ERROR_NO_ACCESS"
UNRESOLVABLE_TRANSIENT   = "ERROR_TRANSIENT"
UNRESOLVABLE_INVALID_REF = "ERROR_INVALID_REF"


@dataclass(frozen=True)
class Unresolvable:
    """Returned when canonical_identity cannot resolve a reference.

    This is the ONLY typed failure return from canonical_identity().
    NEVER return a raw string or None — that is the exact bug (RC1/_resolve_page_id
    returning the original string unchanged on no-match) that ADR-039 eliminates.

    Attributes:
        connector_id:  the connector that attempted resolution.
        resource_type: the resource type requested.
        reference:     the original reference string.
        reason:        one of UNRESOLVABLE_* constants.
        detail:        human-readable detail for an actionable author-time message.
        retryable:     True = transient failure (retry when access is restored);
                       False = permanent failure (fix the reference or access).
    """
    connector_id: str
    resource_type: str
    reference: str
    reason: str
    detail: str = ""
    retryable: bool = False


# Union type for the canonical_identity return
CanonicalResult = Union[CanonicalRef, Unresolvable]


class AdapterWithIdentity(ABC):
    """ABC extension requiring canonical_identity.

    All adapters registered in the Connector Registry MUST subclass this
    (or implement canonical_identity) per DECISION-020 §1 and ADR-039.

    ADR-036 conformance harness verifies this contract at registration time.
    """

    @abstractmethod
    def canonical_identity(
        self,
        reference: str,
        resource_type: str,
    ) -> CanonicalResult:
        """Resolve a reference to its canonical identity for this connector.

        Called ONCE at author/bind time. The result is stamped onto every
        ContentItem via INGEST normalize(). Executor compares canonical_id == canonical_id;
        no heuristic reconciliation anywhere.

        Parameters
        ----------
        reference:
            Any reference form the author might supply.
            Confluence: full URL (?pageId=, /pages/N/, /display/SPACE/Title,
                        /rest/api/content/N), bare numeric string.
            Jira: issue key (PROJECT-123), full URL, bare numeric issue.id,
                  filter URL, sprint URL, project key.
        resource_type:
            Per ADR-036 manifest resource_types (e.g. "page", "issue", "filter").

        Returns
        -------
        CanonicalRef
            On success: the stable canonical identity. canonical_id is the
            connector-defined primary key (numeric string for Confluence/Jira).
        Unresolvable
            On any failure: typed failure with reason and retryable flag.
            MUST NEVER return a raw reference string unchanged (that is the
            RC1/_resolve_page_id silent-degradation bug this ADR eliminates).
        """
        raise NotImplementedError  # pragma: no cover


def canonical_ref_to_dict(cref: CanonicalRef) -> dict:
    """Serialize a CanonicalRef to a plain dict for metadata storage.

    Used by normalize() to stamp canonical_ref onto ContentItem.metadata.
    """
    return {
        "connector_id": cref.connector_id,
        "resource_type": cref.resource_type,
        "canonical_id": cref.canonical_id,
        "display_hint": cref.display_hint,
    }


def canonical_ref_from_dict(d: dict) -> CanonicalRef | None:
    """Deserialize a CanonicalRef from a metadata dict. Returns None on invalid input."""
    if not isinstance(d, dict):
        return None
    cid = d.get("canonical_id")
    conn = d.get("connector_id")
    rtype = d.get("resource_type")
    if not (cid and conn and rtype):
        return None
    return CanonicalRef(
        connector_id=conn,
        resource_type=rtype,
        canonical_id=cid,
        display_hint=d.get("display_hint", ""),
    )
