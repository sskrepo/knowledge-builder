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
    CanonicalResult, AdapterWithIdentity, canonical_ref_to_dict,
)
from .shared import resolve_token, to_raw_item, resolve_to_numeric_id

log = logging.getLogger(__name__)


class ConfluenceNativeAdapter(AdapterWithIdentity):
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


    # ------------------------------------------------------------------
    # ADR-039 (DECISION-020): canonical_identity implementation
    # ------------------------------------------------------------------

    def canonical_identity(self, reference: str, resource_type: str) -> CanonicalResult:
        """Resolve any Confluence reference to a CanonicalRef with numeric page ID.

        Delegates to shared.resolve_to_numeric_id() which implements the full
        ADR-039 §4 three-step algorithm:
          Step 1: fast-path numeric extraction from URL patterns (no API call)
          Step 2: display-by-title URL title lookup (/display/SPACE/Title → id)
          Step 3: ID validation via /rest/api/content/{id} (verifies existence + access)

        Returns CanonicalRef on success or Unresolvable on any failure.
        NEVER returns a raw string (eliminates RC1/_resolve_page_id silent-degradation).
        """
        return resolve_to_numeric_id(
            reference=reference,
            resource_type=resource_type,
            session=self._session,
            base_url=self.base_url,
        )

    def normalize(self, raw_item: "RawItem") -> dict:
        """Produce a ContentItem dict from a RawItem.

        ADR-039: stamps canonical_ref onto metadata so the executor can compare
        canonical_id == canonical_id without any URL reconciliation.
        """
        meta = dict(raw_item.metadata or {})
        numeric_id = str(raw_item.source_id)
        # Stamp canonical_ref — the two-sided stamping required by ADR-039 §7.
        from .._base import canonical_ref_to_dict, CanonicalRef
        meta["canonical_ref"] = canonical_ref_to_dict(CanonicalRef(
            connector_id="confluence",
            resource_type="page",
            canonical_id=numeric_id,
            display_hint=meta.get("title", ""),
        ))
        return {
            "kind": raw_item.kind,
            "source": raw_item.source,
            "source_id": raw_item.source_id,
            "payload": raw_item.payload,
            "metadata": meta,
        }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
