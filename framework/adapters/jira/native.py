"""Jira native (REST) adapter — full implementation.

Uses Jira REST API v2:
  - GET /rest/api/2/search?jql=... for listing
  - GET /rest/api/2/issue/{key}?expand=changelog,comments for fetch
  - Webhook receiver for change streaming

Per ADR-011. Untested against a live Jira instance — needs phase-1 integration
verification once auth tokens are populated.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

from .._base import (
    Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport,
)
from .shared import resolve_token, to_raw_item

log = logging.getLogger(__name__)


class JiraNativeAdapter:
    name = "jira:native"
    kind = "jira"
    mode = "native"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.page_size = cfg.get("pagination", {}).get("page_size", 100)
        self.rpm = cfg.get("rate_limit", {}).get("requests_per_minute", 200)
        self.issuetypes = cfg.get("issuetypes_supported", [])
        self._session = self._build_session()
        self._last_request = 0.0

    def _build_session(self):
        try:
            import requests
        except ImportError:
            log.warning("requests package not installed; adapter is stub-mode")
            return None
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        return s

    def _throttle(self) -> None:
        if not self.rpm:
            return
        min_interval = 60.0 / self.rpm
        elapsed = time.time() - self._last_request
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request = time.time()

    def healthcheck(self) -> HealthReport:
        if self._session is None:
            return HealthReport(False, self.mode, "requests not installed")
        try:
            r = self._session.get(f"{self.base_url}/rest/api/2/serverInfo", timeout=10)
            healthy = r.status_code == 200
            return HealthReport(healthy, self.mode,
                                f"{r.status_code} from /rest/api/2/serverInfo")
        except Exception as e:
            return HealthReport(False, self.mode, str(e))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if self._session is None:
            return
        if not q.jql:
            raise ValueError("Jira list requires SourceQuery.jql")

        start = 0
        while True:
            self._throttle()
            params = {
                "jql": q.jql,
                "fields": "key,updated,summary,issuetype",
                "startAt": start,
                "maxResults": self.page_size,
            }
            r = self._session.get(f"{self.base_url}/rest/api/2/search",
                                  params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            issues = data.get("issues", [])
            for issue in issues:
                yield RawItemRef(
                    kind="jira_issue",
                    source="jira",
                    source_id=issue["key"],
                    last_modified=_parse_iso(issue.get("fields", {}).get("updated")),
                )
            start += len(issues)
            if start >= data.get("total", 0) or not issues:
                break

    def fetch(self, ref: RawItemRef) -> RawItem:
        if self._session is None:
            raise RuntimeError("requests not installed; cannot fetch")
        self._throttle()
        params = {"expand": "changelog,comments"}
        r = self._session.get(
            f"{self.base_url}/rest/api/2/issue/{ref.source_id}",
            params=params, timeout=30,
        )
        r.raise_for_status()
        payload = r.json()

        fields = payload.get("fields", {})
        metadata = {
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "author": (fields.get("creator") or {}).get("displayName"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "labels": fields.get("labels", []),
            "components": [c.get("name") for c in fields.get("components", [])],
            "issuetype": (fields.get("issuetype") or {}).get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "status": (fields.get("status") or {}).get("name"),
            "project": (fields.get("project") or {}).get("key"),
        }
        return to_raw_item(payload=payload, metadata=metadata, source_id=ref.source_id)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        """Polls JQL `updated >= since` as fallback when webhooks aren't wired."""
        jql = f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
        for ref in self.list(SourceQuery(jql=jql)):
            yield ChangeEvent(
                kind="updated",
                source="jira",
                source_id=ref.source_id,
                timestamp=ref.last_modified or datetime.utcnow(),
            )


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
