"""Deterministic ID derivation for ContentItem and Chunk.

Per ADR-002: ContentItem `id = sha256(source : source_id : schema_version)`.
Re-running ingestion with no source change is a no-op.
"""
from __future__ import annotations

import hashlib


def content_item_id(source: str, source_id: str, schema_version: int) -> str:
    """Deterministic ContentItem id. 16-char hex prefix is enough collision-wise
    for our scale (<10^9 items); we keep the full 64-char to be safe in DB."""
    raw = f"{source}:{source_id}:{schema_version}"
    return hashlib.sha256(raw.encode()).hexdigest()


def chunk_id(content_id: str, ord_: int) -> str:
    """Chunk id is content_id#chunk_{N}."""
    return f"{content_id}#chunk_{ord_}"


def source_sha(text: str) -> str:
    """Hash of raw source content; used to detect change for idempotency."""
    return hashlib.sha256(text.encode()).hexdigest()
