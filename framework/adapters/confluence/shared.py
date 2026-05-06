"""Shared helpers for Confluence adapters."""
from __future__ import annotations

import logging
from .._base import RawItem
from ...core.vault import VaultClient

log = logging.getLogger(__name__)
_vault: VaultClient | None = None

def _get_vault() -> VaultClient:
    global _vault
    if _vault is None:
        _vault = VaultClient()
    return _vault

def resolve_token(secret_ref: str) -> str:
    return _get_vault().resolve(secret_ref)

def to_raw_item(payload: dict, metadata: dict, source_id: str) -> RawItem:
    return RawItem(
        kind="confluence_page",
        source="confluence",
        source_id=source_id,
        payload=payload,
        metadata=metadata,
    )
