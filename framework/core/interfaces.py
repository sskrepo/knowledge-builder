"""Protocols every concrete adapter, parser, store, retriever implements.

Per ADR-003 §6.2 / §6.3. Single source of truth for contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol, runtime_checkable

from .content import ContentItem


@dataclass
class RawItem:
    kind: str
    source: str
    source_id: str
    payload: dict
    metadata: dict = field(default_factory=dict)


@dataclass
class ParseContext:
    schema_id: str
    parser_version: str
    persona: str | None = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class Parser(Protocol):
    name: str
    input_kinds: set[str]
    def parse(self, raw: RawItem, ctx: ParseContext) -> ContentItem: ...


@dataclass
class Query:
    kind: str
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
    kind: str
    schema_name: str
    def migrate(self) -> None: ...
    def upsert(self, items: list[ContentItem]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def query(self, q: Query) -> list[Result]: ...


@runtime_checkable
class Retriever(Protocol):
    name: str
    def __call__(self, **kwargs) -> list[Result]: ...
