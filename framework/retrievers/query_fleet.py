"""query_fleet MCP tool — fleet read-through via UDAP adapter.

Queries fleet resources by type with optional field filters.
Every result carries a citation_url (no citation = bug per spec §10).

In filestore mode: reads from _dev_fixtures/fleet/ JSON files via UdapAdapter.
In production: reads through UDAP JDBC views.
"""
from __future__ import annotations

import logging
from typing import Any

from ..adapters.udap_adapter import UdapAdapter, _load_fixtures
from ..adapters._base import SourceQuery

log = logging.getLogger(__name__)

_VALID_RESOURCE_TYPES = {"pod", "node", "tenant"}


class QueryFleetRetriever:
    name = "query_fleet"

    def __init__(self, udap_adapter: UdapAdapter):
        self.adapter = udap_adapter

    def __call__(
        self,
        resource_type: str,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query fleet resources by type.

        Args:
            resource_type: "pod" | "node" | "tenant"
            filters: dict of field -> value to match (all must match, AND semantics)
            limit: max results

        Returns:
            list of dicts, each with resource data plus citation_url.
        """
        if resource_type not in _VALID_RESOURCE_TYPES:
            raise ValueError(
                f"resource_type must be one of {sorted(_VALID_RESOURCE_TYPES)}, got {resource_type!r}"
            )

        q = SourceQuery(extra={"resource_type": resource_type})
        refs = list(self.adapter.list(q))
        results: list[dict] = []

        for ref in refs:
            if len(results) >= limit:
                break
            raw = self.adapter.fetch(ref)
            record = dict(raw.payload)
            citation_url = raw.metadata.get("citation_url", f"udap://fleet/{resource_type}/{ref.source_id}")
            record["citation_url"] = citation_url

            if filters:
                if not all(_matches(record, field, value) for field, value in filters.items()):
                    continue

            results.append(record)

        log.debug("query_fleet: resource_type=%s filters=%s → %d results", resource_type, filters, len(results))
        return results


def _matches(record: dict, field: str, value: Any) -> bool:
    """Return True if record[field] matches value.

    Handles list fields (any-member match) and scalar equality.
    """
    actual = record.get(field)
    if isinstance(actual, list):
        return value in actual
    return actual == value
