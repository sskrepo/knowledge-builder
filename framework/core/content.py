"""Content model — ContentItem, Chunk, Edge, ContentMetadata.

Per ADR-003 §6.1 and ADR-008 (multi-axis dimensions).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# Allowed kinds — must match shim_faaas.kinds_of_knowledge
KINDS = {
    "concept", "procedure", "runbook", "design", "decision",
    "incident_history", "postmortem", "known_issue", "feature_brief",
    "release_plan", "weekly_summary", "ecar", "sla", "catalog_entry",
}

CLASSIFICATIONS = {"public", "internal", "restricted"}


class MissingMetadataError(ValueError):
    """Raised when ContentItem.metadata lacks required fields."""


class VocabDriftWarning(UserWarning):
    """Raised when ContentItem fields contain values not in shim_faaas vocab."""


@dataclass
class ContentMetadata:
    # ACL placeholder (Phase 4 enforces; v1 metadata-only)
    persona_visibility: list[str]
    owner: str
    classification: str

    # Versioning (spec §10)
    source_sha: str
    parser_version: str
    schema_version: int

    # Time
    created_at: datetime
    updated_at: datetime
    last_reviewed: datetime | None = None

    # Provenance
    extracted_by: str = ""              # e.g. "jira:native"
    extraction_schema: str = ""         # path to JSON-Schema used
    metadata_drift: bool = False

    # Open extension
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise MissingMetadataError(
                f"classification must be one of {CLASSIFICATIONS}, got {self.classification!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "persona_visibility": list(self.persona_visibility),
            "owner": self.owner,
            "classification": self.classification,
            "source_sha": self.source_sha,
            "parser_version": self.parser_version,
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_reviewed": self.last_reviewed.isoformat() if self.last_reviewed else None,
            "extracted_by": self.extracted_by,
            "extraction_schema": self.extraction_schema,
            "metadata_drift": self.metadata_drift,
            "extra": dict(self.extra),
        }
        return d


@dataclass
class Edge:
    src: str             # URN
    dst: str             # URN
    rel: str             # "owns" | "depends_on" | "references" | "resolves" | ...
    metadata: dict = field(default_factory=dict)


@dataclass
class Chunk:
    id: str              # f"{content_id}#chunk_{ord}"
    content_id: str
    ord: int
    text: str
    heading_path: list[str] = field(default_factory=list)
    embedding: list[float] | None = None    # 3072 dims when populated
    metadata: dict = field(default_factory=dict)


@dataclass
class ContentItem:
    """One ingested unit, post-parse, pre-store."""
    # Identity
    id: str              # sha256(source : source_id : schema_version)
    source: str
    source_id: str
    path: str
    title: str
    body: str

    # Multi-axis (per ADR-008)
    persona: str
    primary_axis_kind: str       # "functional_area" | "service_id" | "feature_or_release" | "program"
    primary_axis_value: str = ""
    functional_area_all: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    kind: str = "concept"

    # Metadata
    metadata: ContentMetadata = field(default=None)  # type: ignore[assignment]

    # Children
    chunks: list[Chunk] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def validate(self) -> None:
        """Enforce ADR-008 + spec §10 invariants. Raise on hard misses."""
        if not isinstance(self.metadata, ContentMetadata):
            raise MissingMetadataError(
                f"ContentItem {self.id} has no ContentMetadata"
            )
        if self.kind not in KINDS:
            # Not hard-fail in v1; warn-and-mark per ADR-008
            self.metadata.metadata_drift = True
        if not self.id:
            raise MissingMetadataError("ContentItem.id is empty")
        if not self.title:
            raise MissingMetadataError(f"ContentItem {self.id} title is empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_id": self.source_id,
            "path": self.path,
            "title": self.title,
            "body": self.body,
            "persona": self.persona,
            "primary_axis_kind": self.primary_axis_kind,
            "primary_axis_value": self.primary_axis_value,
            "functional_area_all": list(self.functional_area_all),
            "resources": list(self.resources),
            "services": list(self.services),
            "kind": self.kind,
            "metadata": self.metadata.to_dict(),
            "chunks": [asdict(c) for c in self.chunks],
            "edges": [asdict(e) for e in self.edges],
        }
