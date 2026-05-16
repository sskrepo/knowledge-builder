"""Smoke test: framework/deploy/openapi.yaml parses and contains required fields.

ADR-032 P2-API — verifies the spec is well-formed YAML and that the three
on-demand fetch response fields are present in the AskResponse schema with
the correct types.

Run with:
    python3 -m pytest framework/tests/unit/test_openapi_spec.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPEC_PATH = Path(__file__).parents[3] / "framework" / "deploy" / "openapi.yaml"


def _load_spec() -> dict:
    with _SPEC_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenApiSpecParses:
    """The spec file must parse as valid YAML and be a recognisable OpenAPI document."""

    def test_file_exists(self):
        assert _SPEC_PATH.exists(), f"openapi.yaml not found at {_SPEC_PATH}"

    def test_yaml_parses_without_error(self):
        doc = _load_spec()
        assert isinstance(doc, dict), "Top-level YAML must be a mapping"

    def test_openapi_version_field_present(self):
        doc = _load_spec()
        assert "openapi" in doc, "Missing 'openapi' key"
        assert doc["openapi"].startswith("3."), (
            f"Expected OpenAPI 3.x, got {doc['openapi']!r}"
        )

    def test_components_schemas_present(self):
        doc = _load_spec()
        assert "components" in doc
        assert "schemas" in doc["components"]

    def test_ask_response_schema_present(self):
        doc = _load_spec()
        schemas = doc["components"]["schemas"]
        assert "AskResponse" in schemas, "AskResponse schema missing"

    def test_no_broken_internal_refs(self):
        """Every $ref to #/components/schemas/X must resolve within the same document."""
        doc = _load_spec()
        schemas = doc["components"].get("schemas", {})
        responses = doc["components"].get("responses", {})

        import json
        raw = json.dumps(doc)
        import re
        refs = re.findall(r'"#/components/schemas/([^"]+)"', raw)
        for ref in refs:
            assert ref in schemas, (
                f"Broken $ref: #/components/schemas/{ref} — schema not defined"
            )


class TestAskResponseOnDemandFields:
    """ADR-032 P2-API — three on-demand fetch fields must be present in AskResponse."""

    def _ask_response_props(self) -> dict:
        doc = _load_spec()
        return doc["components"]["schemas"]["AskResponse"]["properties"]

    def test_source_fetched_on_demand_present(self):
        props = self._ask_response_props()
        assert "sourceFetchedOnDemand" in props, (
            "sourceFetchedOnDemand missing from AskResponse"
        )

    def test_source_fetched_on_demand_is_boolean(self):
        props = self._ask_response_props()
        assert props["sourceFetchedOnDemand"]["type"] == "boolean", (
            "sourceFetchedOnDemand must have type: boolean"
        )

    def test_source_fetched_on_demand_has_description(self):
        props = self._ask_response_props()
        desc = props["sourceFetchedOnDemand"].get("description", "")
        assert desc.strip(), "sourceFetchedOnDemand must have a non-empty description"

    def test_source_fetched_page_id_present(self):
        props = self._ask_response_props()
        assert "sourceFetchedPageId" in props, (
            "sourceFetchedPageId missing from AskResponse"
        )

    def test_source_fetched_page_id_is_string(self):
        props = self._ask_response_props()
        assert props["sourceFetchedPageId"]["type"] == "string", (
            "sourceFetchedPageId must have type: string"
        )

    def test_latency_note_present(self):
        props = self._ask_response_props()
        assert "latencyNote" in props, "latencyNote missing from AskResponse"

    def test_latency_note_is_string(self):
        props = self._ask_response_props()
        assert props["latencyNote"]["type"] == "string", (
            "latencyNote must have type: string"
        )

    def test_new_fields_are_optional(self):
        """All three on-demand fields must be optional (not in the required array)."""
        doc = _load_spec()
        ask_response = doc["components"]["schemas"]["AskResponse"]
        required_arr = ask_response.get("required", [])
        for field in ("sourceFetchedOnDemand", "sourceFetchedPageId", "latencyNote"):
            assert field not in required_arr, (
                f"{field} must be optional — it should not appear in AskResponse.required"
            )

    def test_existing_required_fields_unchanged(self):
        """The existing required fields must still be present after the P2-API change."""
        doc = _load_spec()
        ask_response = doc["components"]["schemas"]["AskResponse"]
        required_arr = ask_response.get("required", [])
        for field in (
            "answer", "citations", "confidence", "tierUsed",
            "tierDescription", "costTokens", "skillSuggestion",
        ):
            assert field in required_arr, (
                f"Pre-existing required field '{field}' is missing from AskResponse.required"
            )
