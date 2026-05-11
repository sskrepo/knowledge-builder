"""Tests for framework/deploy/serialization.py.

Coverage:
- snake_to_camel for all known openapi.yaml field names
- camel_to_snake for the reverse of each
- convert_keys with nested dicts
- convert_keys with lists of dicts
- convert_keys with mixed nesting (list inside dict inside dict)
- Round-trip: snake -> camel -> snake returns original
- to_camel_response returns a JSONResponse with correct content-type header
- to_camel_response converts keys recursively
- to_camel_response accepts a custom status_code
- from_camel_request converts camelCase request body to snake_case
- Edge cases: empty dict, single-word key, None values, non-string values preserved
"""
import json

import pytest
from fastapi.responses import JSONResponse

from framework.deploy.serialization import (
    camel_to_snake,
    convert_keys,
    from_camel_request,
    snake_to_camel,
    to_camel_response,
)


# ---------------------------------------------------------------------------
# snake_to_camel — known field names from openapi.yaml schemas
# ---------------------------------------------------------------------------

class TestSnakeToCamel:
    """Unit tests for snake_to_camel()."""

    def test_synth_id(self):
        assert snake_to_camel("synth_id") == "synthId"

    def test_created_at(self):
        assert snake_to_camel("created_at") == "createdAt"

    def test_skill_name(self):
        assert snake_to_camel("skill_name") == "skillName"

    def test_tier_used(self):
        assert snake_to_camel("tier_used") == "tierUsed"

    def test_citation_url(self):
        assert snake_to_camel("citation_url") == "citationUrl"

    def test_source_sha(self):
        assert snake_to_camel("source_sha") == "sourceSha"

    def test_persona_allowlist(self):
        assert snake_to_camel("persona_allowlist") == "personaAllowlist"

    def test_token_budget_per_request(self):
        assert snake_to_camel("token_budget_per_request") == "tokenBudgetPerRequest"

    # Additional field names used throughout the implementation plan

    def test_updated_at(self):
        assert snake_to_camel("updated_at") == "updatedAt"

    def test_expires_at(self):
        assert snake_to_camel("expires_at") == "expiresAt"

    def test_uptime_seconds(self):
        assert snake_to_camel("uptime_seconds") == "uptimeSeconds"

    def test_api_version(self):
        assert snake_to_camel("api_version") == "apiVersion"

    def test_schema_version(self):
        assert snake_to_camel("schema_version") == "schemaVersion"

    def test_build_sha(self):
        assert snake_to_camel("build_sha") == "buildSha"

    def test_total_tokens(self):
        assert snake_to_camel("total_tokens") == "totalTokens"

    def test_by_persona(self):
        assert snake_to_camel("by_persona") == "byPersona"

    def test_by_operation(self):
        assert snake_to_camel("by_operation") == "byOperation"

    def test_session_summary(self):
        assert snake_to_camel("session_summary") == "sessionSummary"

    def test_artifact_path(self):
        assert snake_to_camel("artifact_path") == "artifactPath"

    def test_fields_confirmed(self):
        assert snake_to_camel("fields_confirmed") == "fieldsConfirmed"

    def test_sources_configured(self):
        assert snake_to_camel("sources_configured") == "sourcesConfigured"

    def test_triggers_configured(self):
        assert snake_to_camel("triggers_configured") == "triggersConfigured"

    def test_intent_description(self):
        assert snake_to_camel("intent_description") == "intentDescription"

    def test_committed_before_abandon(self):
        assert snake_to_camel("committed_before_abandon") == "committedBeforeAbandon"

    # Single-word key — no transformation expected
    def test_single_word_unchanged(self):
        assert snake_to_camel("persona") == "persona"
        assert snake_to_camel("status") == "status"
        assert snake_to_camel("message") == "message"
        assert snake_to_camel("state") == "state"
        assert snake_to_camel("done") == "done"

    # Empty string edge case
    def test_empty_string(self):
        assert snake_to_camel("") == ""


# ---------------------------------------------------------------------------
# camel_to_snake — reverse of each snake_to_camel case above
# ---------------------------------------------------------------------------

class TestCamelToSnake:
    """Unit tests for camel_to_snake()."""

    def test_synth_id(self):
        assert camel_to_snake("synthId") == "synth_id"

    def test_created_at(self):
        assert camel_to_snake("createdAt") == "created_at"

    def test_skill_name(self):
        assert camel_to_snake("skillName") == "skill_name"

    def test_tier_used(self):
        assert camel_to_snake("tierUsed") == "tier_used"

    def test_citation_url(self):
        assert camel_to_snake("citationUrl") == "citation_url"

    def test_source_sha(self):
        assert camel_to_snake("sourceSha") == "source_sha"

    def test_persona_allowlist(self):
        assert camel_to_snake("personaAllowlist") == "persona_allowlist"

    def test_token_budget_per_request(self):
        assert camel_to_snake("tokenBudgetPerRequest") == "token_budget_per_request"

    def test_updated_at(self):
        assert camel_to_snake("updatedAt") == "updated_at"

    def test_expires_at(self):
        assert camel_to_snake("expiresAt") == "expires_at"

    def test_uptime_seconds(self):
        assert camel_to_snake("uptimeSeconds") == "uptime_seconds"

    def test_api_version(self):
        assert camel_to_snake("apiVersion") == "api_version"

    def test_schema_version(self):
        assert camel_to_snake("schemaVersion") == "schema_version"

    def test_total_tokens(self):
        assert camel_to_snake("totalTokens") == "total_tokens"

    # Single-word key — no transformation expected
    def test_single_word_unchanged(self):
        assert camel_to_snake("persona") == "persona"
        assert camel_to_snake("status") == "status"
        assert camel_to_snake("message") == "message"

    # Uppercase acronym handling (regex: ([A-Z]+)([A-Z][a-z]))
    def test_acronym_boundary(self):
        # e.g. "XMLParser" -> "xml_parser"
        assert camel_to_snake("XMLParser") == "xml_parser"

    def test_empty_string(self):
        assert camel_to_snake("") == ""


# ---------------------------------------------------------------------------
# convert_keys — structural traversal
# ---------------------------------------------------------------------------

class TestConvertKeys:
    """Unit tests for convert_keys() structural traversal."""

    def test_flat_dict_snake_to_camel(self):
        result = convert_keys({"synth_id": "abc", "created_at": "2026-01-01"}, snake_to_camel)
        assert result == {"synthId": "abc", "createdAt": "2026-01-01"}

    def test_flat_dict_camel_to_snake(self):
        result = convert_keys({"synthId": "abc", "createdAt": "2026-01-01"}, camel_to_snake)
        assert result == {"synth_id": "abc", "created_at": "2026-01-01"}

    def test_nested_dict(self):
        data = {
            "session_summary": {
                "skill_name": "incident_summary",
                "artifact_path": "/path/to/skill.yaml",
            }
        }
        result = convert_keys(data, snake_to_camel)
        assert result == {
            "sessionSummary": {
                "skillName": "incident_summary",
                "artifactPath": "/path/to/skill.yaml",
            }
        }

    def test_list_of_dicts(self):
        data = [
            {"synth_id": "s1", "skill_name": "alpha"},
            {"synth_id": "s2", "skill_name": "beta"},
        ]
        result = convert_keys(data, snake_to_camel)
        assert result == [
            {"synthId": "s1", "skillName": "alpha"},
            {"synthId": "s2", "skillName": "beta"},
        ]

    def test_dict_containing_list_of_dicts(self):
        data = {
            "sessions": [
                {"synth_id": "s1", "created_at": "2026-01-01"},
                {"synth_id": "s2", "created_at": "2026-01-02"},
            ]
        }
        result = convert_keys(data, snake_to_camel)
        assert result == {
            "sessions": [
                {"synthId": "s1", "createdAt": "2026-01-01"},
                {"synthId": "s2", "createdAt": "2026-01-02"},
            ]
        }

    def test_deeply_nested(self):
        data = {
            "cost_tokens": {
                "by_persona": {
                    "ops_eng": {"prompt_tokens": 100, "completion_tokens": 50}
                }
            }
        }
        result = convert_keys(data, snake_to_camel)
        assert result == {
            "costTokens": {
                "byPersona": {
                    "opsEng": {"promptTokens": 100, "completionTokens": 50}
                }
            }
        }

    def test_non_dict_non_list_passthrough(self):
        # Scalar types returned unchanged
        assert convert_keys("hello", snake_to_camel) == "hello"
        assert convert_keys(42, snake_to_camel) == 42
        assert convert_keys(3.14, snake_to_camel) == 3.14
        assert convert_keys(True, snake_to_camel) is True
        assert convert_keys(None, snake_to_camel) is None

    def test_none_values_preserved_in_dict(self):
        data = {"expires_at": None, "synth_id": "abc"}
        result = convert_keys(data, snake_to_camel)
        assert result == {"expiresAt": None, "synthId": "abc"}

    def test_non_string_values_preserved(self):
        data = {
            "total_tokens": 1024,
            "done": True,
            "score": 0.87,
            "tags": ["a", "b"],
        }
        result = convert_keys(data, snake_to_camel)
        assert result == {
            "totalTokens": 1024,
            "done": True,
            "score": 0.87,
            "tags": ["a", "b"],
        }

    def test_empty_dict(self):
        assert convert_keys({}, snake_to_camel) == {}

    def test_empty_list(self):
        assert convert_keys([], snake_to_camel) == []

    def test_list_of_non_dict_scalars(self):
        # Lists of scalars are not modified (no keys to convert)
        assert convert_keys([1, 2, 3], snake_to_camel) == [1, 2, 3]
        assert convert_keys(["a", "b"], snake_to_camel) == ["a", "b"]

    def test_values_are_only_keys_converted_not_values(self):
        # Value strings that look like snake_case must NOT be converted
        data = {"skill_name": "incident_summary"}
        result = convert_keys(data, snake_to_camel)
        # Key converted, value left alone
        assert result == {"skillName": "incident_summary"}


# ---------------------------------------------------------------------------
# Round-trip: snake -> camel -> snake
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Verify that snake -> camel -> snake returns the original for all known field names."""

    KNOWN_SNAKE_FIELDS = [
        "synth_id",
        "created_at",
        "skill_name",
        "tier_used",
        "citation_url",
        "source_sha",
        "persona_allowlist",
        "token_budget_per_request",
        "updated_at",
        "expires_at",
        "uptime_seconds",
        "api_version",
        "schema_version",
        "build_sha",
        "total_tokens",
        "by_persona",
        "by_operation",
        "session_summary",
        "artifact_path",
        "fields_confirmed",
        "sources_configured",
        "triggers_configured",
        "intent_description",
        "committed_before_abandon",
        "prompt_tokens",
        "completion_tokens",
        "cost_tokens",
        "skill_suggestion",
        "persona",
        "status",
        "message",
        "state",
        "done",
    ]

    @pytest.mark.parametrize("snake_field", KNOWN_SNAKE_FIELDS)
    def test_round_trip_field(self, snake_field: str):
        camel = snake_to_camel(snake_field)
        recovered = camel_to_snake(camel)
        assert recovered == snake_field, (
            f"Round-trip failed: {snake_field!r} -> {camel!r} -> {recovered!r}"
        )

    def test_round_trip_dict(self):
        original = {
            "synth_id": "abc",
            "created_at": "2026-01-01",
            "token_budget_per_request": 8000,
        }
        as_camel = convert_keys(original, snake_to_camel)
        recovered = convert_keys(as_camel, camel_to_snake)
        assert recovered == original

    def test_round_trip_nested_dict(self):
        original = {
            "session_summary": {
                "skill_name": "incident_summary",
                "artifact_path": "/skills/ops_eng/incident_summary.yaml",
                "fields_confirmed": ["persona", "intent"],
            }
        }
        as_camel = convert_keys(original, snake_to_camel)
        recovered = convert_keys(as_camel, camel_to_snake)
        assert recovered == original


# ---------------------------------------------------------------------------
# to_camel_response
# ---------------------------------------------------------------------------

class TestToCamelResponse:
    """Tests for to_camel_response()."""

    def test_returns_json_response(self):
        result = to_camel_response({"synth_id": "abc"})
        assert isinstance(result, JSONResponse)

    def test_content_type_header(self):
        result = to_camel_response({"synth_id": "abc"})
        content_type = result.headers.get("content-type", "")
        assert "application/json" in content_type

    def test_default_status_code_200(self):
        result = to_camel_response({"synth_id": "abc"})
        assert result.status_code == 200

    def test_custom_status_code(self):
        result = to_camel_response({"error": {"code": "not_found"}}, status_code=404)
        assert result.status_code == 404

    def test_keys_converted_to_camel_case(self):
        result = to_camel_response({
            "synth_id": "s1",
            "created_at": "2026-01-01",
            "skill_name": "ops",
        })
        body = json.loads(result.body)
        assert "synthId" in body
        assert "createdAt" in body
        assert "skillName" in body
        assert "synth_id" not in body

    def test_nested_keys_converted(self):
        result = to_camel_response({
            "session_summary": {
                "skill_name": "incident_summary",
                "artifact_path": "/path",
            }
        })
        body = json.loads(result.body)
        assert "sessionSummary" in body
        assert "skillName" in body["sessionSummary"]
        assert "artifactPath" in body["sessionSummary"]

    def test_list_of_dicts_converted(self):
        result = to_camel_response({
            "sessions": [
                {"synth_id": "s1", "skill_name": "alpha"},
            ]
        })
        body = json.loads(result.body)
        assert body["sessions"][0]["synthId"] == "s1"
        assert body["sessions"][0]["skillName"] == "alpha"

    def test_empty_dict(self):
        result = to_camel_response({})
        body = json.loads(result.body)
        assert body == {}

    def test_none_values_preserved(self):
        result = to_camel_response({"expires_at": None, "synth_id": "abc"})
        body = json.loads(result.body)
        assert body["expiresAt"] is None
        assert body["synthId"] == "abc"

    def test_non_string_values_preserved(self):
        result = to_camel_response({
            "total_tokens": 500,
            "done": True,
            "score": 0.95,
        })
        body = json.loads(result.body)
        assert body["totalTokens"] == 500
        assert body["done"] is True
        assert body["score"] == pytest.approx(0.95)

    def test_status_201_for_created(self):
        result = to_camel_response({"synth_id": "new"}, status_code=201)
        assert result.status_code == 201

    def test_status_503_for_degraded_health(self):
        result = to_camel_response({"status": "degraded", "checks": {}}, status_code=503)
        assert result.status_code == 503


# ---------------------------------------------------------------------------
# from_camel_request
# ---------------------------------------------------------------------------

class TestFromCamelRequest:
    """Tests for from_camel_request()."""

    def test_flat_conversion(self):
        body = {"synthId": "abc", "skillName": "incident_summary"}
        result = from_camel_request(body)
        assert result == {"synth_id": "abc", "skill_name": "incident_summary"}

    def test_nested_conversion(self):
        body = {
            "sessionSummary": {
                "skillName": "incident_summary",
                "artifactPath": "/path",
            }
        }
        result = from_camel_request(body)
        assert result == {
            "session_summary": {
                "skill_name": "incident_summary",
                "artifact_path": "/path",
            }
        }

    def test_list_of_dicts(self):
        body = [{"synthId": "s1"}, {"synthId": "s2"}]
        result = from_camel_request(body)
        assert result == [{"synth_id": "s1"}, {"synth_id": "s2"}]

    def test_empty_dict(self):
        assert from_camel_request({}) == {}

    def test_none_values_preserved(self):
        body = {"expiresAt": None, "synthId": "abc"}
        result = from_camel_request(body)
        assert result["expires_at"] is None
        assert result["synth_id"] == "abc"

    def test_non_string_values_preserved(self):
        body = {"totalTokens": 1024, "done": False, "score": 0.7}
        result = from_camel_request(body)
        assert result["total_tokens"] == 1024
        assert result["done"] is False

    def test_ask_request_body(self):
        """Simulate a real POST /api/v1/ask body."""
        body = {
            "question": "What is the p99 latency for service X?",
            "persona": "ops_eng",
            "serviceId": "payments-svc",
            "functionalArea": "observability",
            "maxResults": 5,
        }
        result = from_camel_request(body)
        assert result["question"] == body["question"]
        assert result["persona"] == body["persona"]
        assert result["service_id"] == "payments-svc"
        assert result["functional_area"] == "observability"
        assert result["max_results"] == 5

    def test_author_skill_start_body(self):
        """Simulate a real POST /api/v1/kb/authorSkill body (new session)."""
        body = {"input": "I want to build an incident summary skill"}
        result = from_camel_request(body)
        assert result == {"input": "I want to build an incident summary skill"}

    def test_author_skill_continue_body(self):
        """Simulate a real POST /api/v1/kb/authorSkill body (resume with synthId)."""
        body = {"input": "ops_eng", "synthId": "synth-ops_eng-20260510-a1b2"}
        result = from_camel_request(body)
        assert result["input"] == "ops_eng"
        assert result["synth_id"] == "synth-ops_eng-20260510-a1b2"
