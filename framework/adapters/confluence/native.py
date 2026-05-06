"""Confluence native (REST) adapter — full implementation.

Uses Confluence Cloud REST API:
  - GET /wiki/rest/api/content?spaceKey=...&label=... for listing
  - GET /wiki/rest/api/content/{id}?expand=body.storage,metadata.labels for fetch
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


class ConfluenceNativeAdapter:
    name = "confluence:native"
    kind = "confluence"
    mode = "native"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.page_size = cfg.get("pagination", {}).get("page_size", 50)
        self.rpm = cfg.get("rate_limit", {}).get("requests_per_minute", 120)
        self._session = self._build_session()
        self._last_request = 0.0

    def _build_session(self):
        try:
            import requests
        except ImportError:
            log.warning("requests not installed; adapter is stub-mode")
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
            r = self._session.get(f"{self.base_url}/wiki/rest/api/space",
                                  params={"limit": 1}, timeout=10)
            return HealthReport(r.status_code == 200, self.mode,
                                f"{r.status_code} from /space")
        except Exception as e:
            return HealthReport(False, self.mode, str(e))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if self._session is None:
            return
        if not q.space:
            raise ValueError("Confluence list requires SourceQuery.space")

        start = 0
        while True:
            self._throttle()
            params = {
                "spaceKey": q.space,
                "limit": self.page_size,
                "start": start,
                "expand": "version,metadata.labels",
            }
            if q.labels_include:
                params["label"] = ",".join(q.labels_include)
            r = self._session.get(f"{self.base_url}/wiki/rest/api/content",
                                  params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            for page in results:
                # Honor exclude_labels in app filter
                labels = [lbl.get("name") for lbl in
                         (page.get("metadata", {}).get("labels", {}).get("results", []))]
                if any(x in labels for x in q.labels_exclude):
                    continue
                yield RawItemRef(
                    kind="confluence_page",
                    source="confluence",
                    source_id=str(page["id"]),
                    last_modified=_parse_iso(page.get("version", {}).get("when")),
                )
            start += len(results)
            if start >= data.get("size", 0) or not results:
                break

    def fetch(self, ref: RawItemRef) -> RawItem:
        if self._session is None:
            raise RuntimeError("requests not installed; cannot fetch")
        self._throttle()
        params = {"expand": "body.storage,metadata.labels,version,space,history"}
        r = self._session.get(
            f"{self.base_url}/wiki/rest/api/content/{ref.source_id}",
            params=params, timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        metadata = {
            "title": payload.get("title"),
            "space": (payload.get("space") or {}).get("key"),
            "version": (payload.get("version") or {}).get("number"),
            "updated_at": (payload.get("version") or {}).get("when"),
            "labels": [lbl.get("name") for lbl in
                       (payload.get("metadata", {}).get("labels", {}).get("results", []))],
            "url": (payload.get("_links") or {}).get("self"),
        }
        return to_raw_item(payload=payload, metadata=metadata, source_id=ref.source_id)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        # Confluence doesn't support since-filter natively in /content endpoint;
        # the webhook receiver in framework/ingestion/webhook_router.py is the
        # primary path. Polling fallback uses CQL search.
        if self._session is None:
            return
        cql = f'lastmodified >= "{since.strftime("%Y-%m-%d")}"'
        self._throttle()
        r = self._session.get(
            f"{self.base_url}/wiki/rest/api/content/search",
            params={"cql": cql, "limit": self.page_size}, timeout=30,
        )
        r.raise_for_status()
        for page in r.json().get("results", []):
            yield ChangeEvent(
                kind="updated",
                source="confluence",
                source_id=str(page["id"]),
                timestamp=_parse_iso(page.get("version", {}).get("when")) or datetime.utcnow(),
            )


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
