"""Shared helpers for Confluence adapters (auth, raw_item normalization)."""
from __future__ import annotations
from .._base import RawItem

def resolve_token(secret_ref: str) -> str:
    """Resolve a vault://kb/... reference via OCI Vault. STUB."""
    # TODO Phase 1: use core.vault_client.resolve(secret_ref); 60s cache
    return "<resolved-at-runtime>"

def to_raw_item(payload: dict, metadata: dict, source_id: str) -> RawItem:
    """Canonical RawItem builder. Both native and MCP must call this."""
    return RawItem(
        kind="confluence_page",
        source="confluence",
        source_id=source_id,
        payload=payload,
        metadata=metadata,
    )
