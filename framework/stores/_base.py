"""Base Store contract — every concrete store extends this.

Per ADR-003 (Store Protocol). Concrete impls: IncidentVectorStore,
WikiMetadataStore, CodeStructuralStore, FaSemanticGraphStore,
FleetReadThroughStore, ShimIndexStore.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..core.content import ContentItem


@dataclass
class Query:
    kind: str           # "vector_knn" | "wiki_search" | "graph_traverse" | "fleet_view" | ...
    payload: dict
    persona: str | None = None
    limit: int = 10


@dataclass
class Result:
    content_id: str
    chunk_id: str | None
    text: str
    score: float
    citation_url: str
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class Store(Protocol):
    kind: str           # "vector" | "wiki" | "graph" | "sql_passthrough" | "code_index" | "shim"
    schema_name: str    # e.g. "kb_incidents"

    def migrate(self) -> None: ...
    def upsert(self, items: list[ContentItem]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def query(self, q: Query) -> list[Result]: ...


class BaseStore:
    """Optional base class with shared helpers. Concrete stores subclass this."""
    kind: str = ""
    schema_name: str = ""

    def migrate(self) -> None: ...
    def upsert(self, items: list[ContentItem]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def query(self, q: Query) -> list[Result]:
        raise NotImplementedError
