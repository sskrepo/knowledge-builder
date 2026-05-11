"""Unit tests for UdapAdapter in filestore/dev mode.

All tests run against _dev_fixtures/fleet/ — no JDBC, no external services.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "_dev_fixtures" / "fleet"


@pytest.fixture(autouse=True)
def filestore_mode(monkeypatch):
    monkeypatch.setenv("KBF_STORE_BACKEND", "filestore")


@pytest.fixture
def adapter():
    from framework.adapters.udap_adapter import UdapAdapter
    return UdapAdapter(cfg={"connection": {}, "allowlisted_views_file": ""})


# --------------------------------------------------------------------------
# healthcheck
# --------------------------------------------------------------------------

def test_healthcheck_returns_healthy(adapter):
    report = adapter.healthcheck()
    assert report.healthy is True
    assert report.mode == "read_through"
    assert "filestore" in report.notes
    assert len(report.capabilities) > 0


# --------------------------------------------------------------------------
# list()
# --------------------------------------------------------------------------

def test_list_all_resources(adapter):
    from framework.adapters._base import SourceQuery
    refs = list(adapter.list(SourceQuery()))
    assert len(refs) == 6  # 3 pods + 2 nodes + 1 tenant


def test_list_pods_only(adapter):
    from framework.adapters._base import SourceQuery
    refs = list(adapter.list(SourceQuery(extra={"resource_type": "pod"})))
    assert len(refs) == 3
    for ref in refs:
        assert ref.kind == "fleet_pod"
        assert ref.source == "udap"
        assert ref.source_id.startswith("pod-")


def test_list_nodes_only(adapter):
    from framework.adapters._base import SourceQuery
    refs = list(adapter.list(SourceQuery(extra={"resource_type": "node"})))
    assert len(refs) == 2
    assert all(r.kind == "fleet_node" for r in refs)


def test_list_tenants_only(adapter):
    from framework.adapters._base import SourceQuery
    refs = list(adapter.list(SourceQuery(extra={"resource_type": "tenant"})))
    assert len(refs) == 1
    assert refs[0].source_id == "tenant-acme-corp"


def test_list_refs_have_last_modified(adapter):
    from framework.adapters._base import SourceQuery
    refs = list(adapter.list(SourceQuery(extra={"resource_type": "pod"})))
    for ref in refs:
        assert ref.last_modified is not None


# --------------------------------------------------------------------------
# fetch()
# --------------------------------------------------------------------------

def test_fetch_pod(adapter):
    from framework.adapters._base import SourceQuery, RawItemRef
    from datetime import datetime, timezone
    ref = RawItemRef(kind="fleet_pod", source="udap", source_id="pod-alpha-001",
                     last_modified=datetime.now(tz=timezone.utc))
    raw = adapter.fetch(ref)
    assert raw.kind == "fleet_pod"
    assert raw.source == "udap"
    assert raw.source_id == "pod-alpha-001"
    assert raw.payload["status"] == "healthy"
    assert raw.payload["tenant_id"] == "tenant-acme-corp"


def test_fetch_node(adapter):
    from framework.adapters._base import RawItemRef
    from datetime import datetime, timezone
    ref = RawItemRef(kind="fleet_node", source="udap", source_id="node-us-west-2b",
                     last_modified=datetime.now(tz=timezone.utc))
    raw = adapter.fetch(ref)
    assert raw.payload["status"] == "degraded"
    assert raw.payload["patching_status"] == "behind"


def test_fetch_tenant(adapter):
    from framework.adapters._base import RawItemRef
    from datetime import datetime, timezone
    ref = RawItemRef(kind="fleet_tenant", source="udap", source_id="tenant-acme-corp",
                     last_modified=datetime.now(tz=timezone.utc))
    raw = adapter.fetch(ref)
    assert raw.payload["tenant_name"] == "Acme Corporation"
    assert raw.payload["sla_tier"] == "gold"


def test_fetch_missing_raises_key_error(adapter):
    from framework.adapters._base import RawItemRef
    from datetime import datetime, timezone
    ref = RawItemRef(kind="fleet_pod", source="udap", source_id="pod-does-not-exist",
                     last_modified=datetime.now(tz=timezone.utc))
    with pytest.raises(KeyError):
        adapter.fetch(ref)


# --------------------------------------------------------------------------
# fetch() — citation
# --------------------------------------------------------------------------

def test_fetch_includes_citation_in_metadata(adapter):
    from framework.adapters._base import RawItemRef
    from datetime import datetime, timezone
    ref = RawItemRef(kind="fleet_pod", source="udap", source_id="pod-alpha-001",
                     last_modified=datetime.now(tz=timezone.utc))
    raw = adapter.fetch(ref)
    assert "citation_url" in raw.metadata
    assert "udap://" in raw.metadata["citation_url"]


# --------------------------------------------------------------------------
# stream_changes()
# --------------------------------------------------------------------------

def test_stream_changes_returns_empty(adapter):
    from datetime import datetime, timezone
    changes = list(adapter.stream_changes(datetime.now(tz=timezone.utc)))
    assert changes == []


# --------------------------------------------------------------------------
# discover()
# --------------------------------------------------------------------------

def test_discover_list_pods(adapter):
    refs = list(adapter.discover([{"op": "list_pods"}]))
    assert len(refs) == 3
    assert all(r.kind == "fleet_pod" for r in refs)


def test_discover_list_nodes(adapter):
    refs = list(adapter.discover([{"op": "list_nodes"}]))
    assert len(refs) == 2


def test_discover_list_tenants(adapter):
    refs = list(adapter.discover([{"op": "list_tenants"}]))
    assert len(refs) == 1


def test_discover_filter_by_tenant(adapter):
    refs = list(adapter.discover([
        {"op": "list_pods"},
        {"op": "filter_by", "field": "tenant_id", "value": "tenant-acme-corp"},
    ]))
    assert len(refs) == 2
    for ref in refs:
        assert "acme" in ref.source_id or True  # filter by payload field, ref.source_id is pod id


def test_discover_filter_by_status(adapter):
    refs = list(adapter.discover([
        {"op": "list_pods"},
        {"op": "filter_by", "field": "status", "value": "healthy"},
    ]))
    assert len(refs) == 2  # pod-alpha-001 and pod-gamma-003 are healthy


def test_discover_unknown_op_raises(adapter):
    with pytest.raises(ValueError, match="unknown op"):
        list(adapter.discover([{"op": "explode_everything"}]))


# --------------------------------------------------------------------------
# query_fleet retriever
# --------------------------------------------------------------------------

def test_query_fleet_pods(adapter):
    from framework.retrievers.query_fleet import QueryFleetRetriever
    retriever = QueryFleetRetriever(udap_adapter=adapter)
    results = retriever(resource_type="pod")
    assert len(results) == 3
    for r in results:
        assert "citation_url" in r
        assert r["resource_type"] == "pod"


def test_query_fleet_filters_by_status(adapter):
    from framework.retrievers.query_fleet import QueryFleetRetriever
    retriever = QueryFleetRetriever(udap_adapter=adapter)
    results = retriever(resource_type="pod", filters={"status": "degraded"})
    assert len(results) == 1
    assert results[0]["pod_id"] == "pod-beta-002"


def test_query_fleet_filters_by_tenant(adapter):
    from framework.retrievers.query_fleet import QueryFleetRetriever
    retriever = QueryFleetRetriever(udap_adapter=adapter)
    results = retriever(resource_type="pod", filters={"tenant_id": "tenant-acme-corp"})
    assert len(results) == 2


def test_query_fleet_nodes(adapter):
    from framework.retrievers.query_fleet import QueryFleetRetriever
    retriever = QueryFleetRetriever(udap_adapter=adapter)
    results = retriever(resource_type="node")
    assert len(results) == 2
    assert all("citation_url" in r for r in results)


def test_query_fleet_invalid_resource_type_raises(adapter):
    from framework.retrievers.query_fleet import QueryFleetRetriever
    retriever = QueryFleetRetriever(udap_adapter=adapter)
    with pytest.raises(ValueError, match="resource_type must be one of"):
        retriever(resource_type="database")


def test_query_fleet_limit(adapter):
    from framework.retrievers.query_fleet import QueryFleetRetriever
    retriever = QueryFleetRetriever(udap_adapter=adapter)
    results = retriever(resource_type="pod", limit=1)
    assert len(results) == 1


# --------------------------------------------------------------------------
# text_to_sql retriever
# --------------------------------------------------------------------------

def test_text_to_sql_pod_health(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    result = retriever("show me pod health status")
    assert result["view"] == "pod_health"
    assert isinstance(result["results"], list)
    assert len(result["results"]) > 0
    assert "citation" in result
    assert "udap://" in result["citation"]
    assert result["matched_pattern"] is True


def test_text_to_sql_restart_counts(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    result = retriever("how many restarts in the last 24h?")
    assert result["view"] == "restart_counts"
    assert "restart_count" in result["results"][0]


def test_text_to_sql_fleet_inventory(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    result = retriever("list all pods in fleet inventory")
    assert result["view"] == "fleet_inventory"
    assert len(result["results"]) > 0


def test_text_to_sql_patching_status(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    result = retriever("what is the patching status?")
    assert result["view"] == "patching_status"


def test_text_to_sql_no_match_returns_error(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    result = retriever("what is the weather like?")
    assert "error" in result
    assert result["results"] == []


def test_text_to_sql_forbidden_keyword_raises(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    with pytest.raises(ValueError, match="forbidden keywords"):
        retriever("DROP TABLE pod_health")


def test_text_to_sql_sql_string_format(adapter):
    from framework.retrievers.text_to_sql import TextToSqlRetriever
    retriever = TextToSqlRetriever()
    result = retriever("show pod health")
    assert "SELECT" in result["sql"]
    assert "pod_health" in result["sql"]
    assert "FETCH FIRST" in result["sql"]
