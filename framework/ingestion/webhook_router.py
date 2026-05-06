"""Webhook router — receives Confluence/Jira webhooks and ingests via pipeline.

Phase 1: scaffolding only. Real verification of webhook signatures + dedupe
of duplicate deliveries lands in Phase 2.
"""
from __future__ import annotations
import logging
import hmac
import hashlib

log = logging.getLogger(__name__)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """HMAC-SHA256 verification (Jira/Confluence pattern)."""
    if not header or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class WebhookRouter:
    """Maps incoming webhook payloads to ingestion pipelines."""

    def __init__(self, pipelines_by_source: dict, secrets: dict | None = None):
        self.pipelines = pipelines_by_source     # "jira" -> IngestionPipeline
        self.secrets = secrets or {}

    def handle_jira(self, body: dict, signature: str | None = None) -> dict:
        # Jira webhook payloads: webhookEvent + issue + changelog
        issue = body.get("issue") or {}
        key = issue.get("key")
        if not key:
            return {"status": "ignored", "reason": "no issue key"}
        pipeline = self.pipelines.get("jira")
        if not pipeline:
            return {"status": "ignored", "reason": "no jira pipeline"}
        try:
            from ..core.interfaces import RawItem
            ref = type("R", (), {"source": "jira", "source_id": key,
                                  "kind": "jira_issue", "last_modified": None})()
            raw = pipeline.adapter.fetch(ref)
            pipeline.ingest_one(raw)
            return {"status": "ok", "key": key}
        except Exception as e:
            log.exception("jira webhook ingest failed: %s", e)
            return {"status": "error", "error": str(e)}

    def handle_confluence(self, body: dict, signature: str | None = None) -> dict:
        page = body.get("page") or body.get("content") or {}
        page_id = str(page.get("id") or "")
        if not page_id:
            return {"status": "ignored", "reason": "no page id"}
        pipeline = self.pipelines.get("confluence")
        if not pipeline:
            return {"status": "ignored", "reason": "no confluence pipeline"}
        try:
            ref = type("R", (), {"source": "confluence", "source_id": page_id,
                                  "kind": "confluence_page", "last_modified": None})()
            raw = pipeline.adapter.fetch(ref)
            pipeline.ingest_one(raw)
            return {"status": "ok", "page_id": page_id}
        except Exception as e:
            log.exception("confluence webhook ingest failed: %s", e)
            return {"status": "error", "error": str(e)}
