"""Change detection — webhook receiver + scheduled poll fallback."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


class ChangeDetector:
    def __init__(self, adapter, pipeline):
        self.adapter = adapter
        self.pipeline = pipeline

    def poll_since(self, since: datetime) -> int:
        """Polls adapter for changes since `since`. Returns count processed."""
        n = 0
        for evt in self.adapter.stream_changes(since):
            try:
                from ..core.interfaces import RawItem
                ref = type("R", (), {"source": evt.source, "source_id": evt.source_id,
                                      "kind": evt.kind, "last_modified": evt.timestamp})()
                raw = self.adapter.fetch(ref)
                self.pipeline.ingest_one(raw)
                n += 1
            except Exception as e:
                log.exception("change ingest failed: %s", e)
        return n

    def poll_recent(self, minutes: int = 5) -> int:
        return self.poll_since(datetime.utcnow() - timedelta(minutes=minutes))
