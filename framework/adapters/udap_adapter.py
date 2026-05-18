"""UDAP / Sentinel adapter — read-through (no ingest) per ADR-001.

Fleet data is never ingested into a content store. Every retrieval call goes
direct to the source (or, in filestore/dev mode, reads from fixture files).

In production: connects via JDBC to UDAP/Sentinel views.
In filestore mode: reads from framework/_dev_fixtures/fleet/*.json.

`discover()` supports procedural source resolution (ADR-011 Amendment 1).
Supported ops: list_pods, list_nodes, list_tenants, filter_by.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ._base import (
    Adapter, ChangeEvent, HealthReport, RawItem, RawItemRef, SourceQuery,
    AdapterWithIdentity, CanonicalResult,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "_dev_fixtures" / "fleet"

_RESOURCE_TYPES = {"pod", "node", "tenant"}


def _is_filestore_mode() -> bool:
    return os.environ.get("KBF_STORE_BACKEND", "").lower() == "filestore"


def _load_fixtures(resource_type: str | None = None) -> list[dict]:
    """Load all fixture JSON files, optionally filtered by resource_type."""
    records: list[dict] = []
    if not _FIXTURES_DIR.exists():
        return records
    for p in sorted(_FIXTURES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if resource_type is None or data.get("resource_type") == resource_type:
            data.setdefault("_fixture_path", str(p))
            records.append(data)
    return records


def _record_to_ref(record: dict) -> RawItemRef:
    rt = record.get("resource_type", "unknown")
    rid = record.get(f"{rt}_id") or record.get("pod_id") or record.get("node_id") or record.get("tenant_id") or "?"
    last_check = record.get("last_check_at")
    last_modified: datetime | None = None
    if last_check:
        try:
            last_modified = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
        except ValueError:
            pass
    return RawItemRef(
        kind=f"fleet_{rt}",
        source="udap",
        source_id=rid,
        last_modified=last_modified,
    )


def _record_to_raw_item(record: dict) -> RawItem:
    rt = record.get("resource_type", "unknown")
    rid = record.get(f"{rt}_id") or record.get("pod_id") or record.get("node_id") or record.get("tenant_id") or "?"
    fixture_path = record.pop("_fixture_path", f"fixture://fleet/{rid}")
    return RawItem(
        kind=f"fleet_{rt}",
        source="udap",
        source_id=rid,
        payload=dict(record),
        metadata={
            "resource_type": rt,
            "citation_url": f"udap://fleet/{rt}/{rid}",
            "fixture_path": fixture_path,
            "last_check_at": record.get("last_check_at", ""),
        },
    )


class UdapAdapter(AdapterWithIdentity):
    """Fleet read-through adapter.

    In filestore/dev mode: reads from _dev_fixtures/fleet/ JSON files.
    In production: JDBC connection to UDAP/Sentinel views is configured via cfg.
    """

    name = "udap"
    kind = "udap"
    mode = "read_through"

    def __init__(self, cfg: dict):
        self._connection_cfg = cfg.get("connection", {})
        self.allowlist_file = cfg.get("allowlisted_views_file")
        self.guardrails = cfg.get("text_to_sql", {}).get("guardrails", {})
        self._fixture_mode = _is_filestore_mode()

    def healthcheck(self) -> HealthReport:
        if self._fixture_mode:
            fixture_count = len(list(_FIXTURES_DIR.glob("*.json"))) if _FIXTURES_DIR.exists() else 0
            return HealthReport(
                healthy=True,
                mode=self.mode,
                notes=f"filestore mode; {fixture_count} fixture files in {_FIXTURES_DIR}",
                capabilities=["list_pods", "list_nodes", "list_tenants", "filter_by"],
            )
        return HealthReport(
            healthy=True,
            mode=self.mode,
            notes="production stub — JDBC connection not validated at startup",
            capabilities=["pod_health", "restart_counts", "refresh_progress",
                          "fleet_inventory", "patching_status"],
        )

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        """Return RawItemRefs for all fleet resources matching the query.

        In filestore mode: all fixture files filtered by resource_type in q.extra.
        In production: queries the UDAP allowlisted view.
        """
        if not self._fixture_mode:
            raise NotImplementedError(
                "UDAP production JDBC query not implemented; set KBF_STORE_BACKEND=filestore for dev"
            )
        resource_type = q.extra.get("resource_type") if q.extra else None
        records = _load_fixtures(resource_type)
        for record in records:
            record_copy = dict(record)
            yield _record_to_ref(record_copy)

    def fetch(self, ref: RawItemRef) -> RawItem:
        """Return raw fleet record by source_id (resource ID).

        In filestore mode: scans fixture files for matching resource ID.
        """
        if not self._fixture_mode:
            raise NotImplementedError(
                "UDAP production JDBC fetch not implemented; set KBF_STORE_BACKEND=filestore for dev"
            )
        resource_type = ref.kind.removeprefix("fleet_") if ref.kind.startswith("fleet_") else None
        records = _load_fixtures(resource_type)
        for record in records:
            rt = record.get("resource_type", "")
            rid = record.get(f"{rt}_id") or record.get("pod_id") or record.get("node_id") or record.get("tenant_id")
            if rid == ref.source_id:
                return _record_to_raw_item(record)
        raise KeyError(f"fleet resource not found: {ref.source_id} (kind={ref.kind})")

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        """Fleet is queried live; no change streaming (UDAP has no change feed)."""
        return iter([])

    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]:
        """Procedural source discovery (ADR-011 Amendment 1).

        Supported ops (filestore mode):
          - list_pods:    yields refs for all pod fixtures
          - list_nodes:   yields refs for all node fixtures
          - list_tenants: yields refs for all tenant fixtures
          - filter_by:    narrows the accumulated refs by field=value
          - for_each:     iterates; fleet adapter treats this as a passthrough
        """
        if not self._fixture_mode:
            raise NotImplementedError(
                "UDAP production discover not implemented; set KBF_STORE_BACKEND=filestore for dev"
            )

        accumulated: list[dict] = []

        for step in recipe:
            op = step.get("op", "")
            if op == "list_pods":
                accumulated = _load_fixtures("pod")
            elif op == "list_nodes":
                accumulated = _load_fixtures("node")
            elif op == "list_tenants":
                accumulated = _load_fixtures("tenant")
            elif op == "filter_by":
                field = step.get("field")
                value = step.get("value")
                if field and value is not None:
                    accumulated = [r for r in accumulated if r.get(field) == value]
            elif op == "for_each":
                pass
            else:
                raise ValueError(f"UdapAdapter.discover: unknown op {op!r}")

        for record in accumulated:
            record_copy = dict(record)
            yield _record_to_ref(record_copy)

    # ------------------------------------------------------------------
    # ADR-039 (DECISION-020): canonical_identity stub
    # ------------------------------------------------------------------

    def canonical_identity(self, reference: str, resource_type: str) -> CanonicalResult:
        """UDAP canonical identity — deferred until production JDBC path implemented.

        Per ADR-036 Amendment 4 / Section O: UDAP is not registered in the
        Connector Registry because its production JDBC path is not implemented.
        canonical_identity raises NotImplementedError explicitly (ADR-036 §M.2)
        so the ABC contract is enforced and UDAP cannot silently pass.
        """
        raise NotImplementedError(
            "UDAP canonical_identity: deferred until production JDBC path is implemented. "
            "UDAP is not registered in the Connector Registry per ADR-036 Amendment 4."
        )
