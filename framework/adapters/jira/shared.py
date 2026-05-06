"""Shared helpers for Jira adapters."""
from __future__ import annotations
from .._base import RawItem

def resolve_token(secret_ref: str) -> str:
    return "<resolved-at-runtime>"

def to_raw_item(payload: dict, metadata: dict, source_id: str) -> RawItem:
    return RawItem(
        kind="jira_issue",
        source="jira",
        source_id=source_id,
        payload=payload,
        metadata=metadata,
    )
