"""Ingestion pipeline — orchestrates adapter → parser → store."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from ..core.interfaces import RawItem, ParseContext

log = logging.getLogger(__name__)


class IngestionPipeline:
    """Single source-to-store pipeline. One per persona × kb_name."""

    def __init__(self, adapter, parser, store, schema_id: str, parser_version: str = "v1"):
        self.adapter = adapter
        self.parser = parser
        self.store = store
        self.schema_id = schema_id
        self.parser_version = parser_version

    def ingest_one(self, raw: RawItem) -> None:
        ctx = ParseContext(
            schema_id=self.schema_id,
            parser_version=self.parser_version,
            persona=getattr(self.parser, "persona", None),
        )
        item = self.parser.parse(raw, ctx)
        self.store.upsert([item])

    def ingest_batch(self, raws: Iterable[RawItem], batch_size: int = 32) -> int:
        """Returns number of items processed."""
        from ..core.interfaces import ParseContext
        ctx = ParseContext(
            schema_id=self.schema_id,
            parser_version=self.parser_version,
            persona=getattr(self.parser, "persona", None),
        )
        n = 0
        buf: list = []
        for raw in raws:
            try:
                item = self.parser.parse(raw, ctx)
                buf.append(item)
                n += 1
            except Exception as e:
                log.exception("parse failed for %s/%s: %s",
                              raw.source, raw.source_id, e)
            if len(buf) >= batch_size:
                self.store.upsert(buf)
                buf = []
        if buf:
            self.store.upsert(buf)
        return n

    def ingest_from_query(self, source_query) -> int:
        """List → fetch → parse → upsert pipeline driven by a SourceQuery."""
        def _gen():
            for ref in self.adapter.list(source_query):
                try:
                    yield self.adapter.fetch(ref)
                except Exception as e:
                    log.exception("fetch failed for %s: %s", ref.source_id, e)
        return self.ingest_batch(_gen())
