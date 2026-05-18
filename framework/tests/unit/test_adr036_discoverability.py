"""ADR-036 — Connector Discoverability tests.

Tests per the brief:
  (a) listConnectors MCP tool returns exactly the 3 connectors with the 6
      user-facing fields and NO internal probe fields.
  (b) listConnectors appears in EXTERNAL_TOOLS_SCHEMA (tools/list) and is
      callable without write/admin scope.
  (c) UDAP/fleet NOT present in listConnectors output.
  (d) CONFIGURE_SOURCES entry prompt now contains the supported-connectors
      block (assert the 3 display names present).
  (e) The proactive block and the hard-stop block render from the same shared
      helper (change the registry test-double once, assert both surfaces
      reflect it — drift guard).
  (f) No regression in existing CONFIGURE_SOURCES supported/unsupported flows.

No live LLM / ADB calls — all tests use mocks and fresh registry instances.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.connectors.registry import (
    ConnectorManifest,
    ConnectorRegistry,
    format_supported_connectors_block,
    manifest_to_user_facing,
    get_registry,
)
from framework.deploy.mcp_tools import EXTERNAL_TOOLS_SCHEMA, build_external_tool_registry
from framework.deploy.auth.consumer import ConsumerManifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MANIFESTS_DIR = Path(__file__).resolve().parents[2] / "connectors" / "manifests"
EXPECTED_CONNECTOR_IDS = {"confluence", "jira", "git"}
USER_FACING_FIELDS = {
    "connector_id", "display_name", "description",
    "resource_types", "supported_operations", "auth_model",
}
INTERNAL_FIELDS = {"access_probe_hook", "granularity_filters"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _consumer(scopes=("read",)):
    return ConsumerManifest(
        name="test-consumer",
        token_hash="x",
        scopes=list(scopes),
        persona_allowlist=[],
        rpm_cap=60,
        token_budget_per_request=8000,
        user_id="test-user",
    )


def _app():
    app = MagicMock()
    app.state.skill_store = None
    app.state.session_store = None
    app.state.llm = None
    app.state.artifact_store = None
    app.state.error_store = None
    app.state.adb_pool = None
    app.state.bug_pool = None
    app.state.context_builder = None
    app.state.shim_kb = None
    app.state.kbf_ops_loader = None
    return app


# ---------------------------------------------------------------------------
# (a) listConnectors returns exactly 3 connectors with the 6 user-facing fields
# ---------------------------------------------------------------------------

class TestListConnectorsMcpTool:
    """listConnectors MCP tool returns the correct connector data."""

    def _handler(self):
        registry = build_external_tool_registry(_app())
        return registry["listConnectors"]

    def test_returns_exactly_three_connectors(self):
        result = _run(self._handler()(_consumer=_consumer()))
        assert result["total"] == 3
        ids = {c["connector_id"] for c in result["connectors"]}
        assert ids == EXPECTED_CONNECTOR_IDS

    def test_each_connector_has_exactly_six_user_facing_fields(self):
        result = _run(self._handler()(_consumer=_consumer()))
        for connector in result["connectors"]:
            assert set(connector.keys()) == USER_FACING_FIELDS, (
                f"connector {connector.get('connector_id')} has unexpected keys: "
                f"{set(connector.keys()) - USER_FACING_FIELDS}"
            )

    def test_no_internal_probe_fields_exposed(self):
        """access_probe_hook and granularity_filters must NOT appear in the output."""
        result = _run(self._handler()(_consumer=_consumer()))
        for connector in result["connectors"]:
            for internal_field in INTERNAL_FIELDS:
                assert internal_field not in connector, (
                    f"Internal field '{internal_field}' must not be exposed via "
                    f"listConnectors; found in connector {connector.get('connector_id')}"
                )

    def test_connector_fields_have_correct_types(self):
        result = _run(self._handler()(_consumer=_consumer()))
        for c in result["connectors"]:
            assert isinstance(c["connector_id"], str)
            assert isinstance(c["display_name"], str)
            assert isinstance(c["description"], str)
            assert isinstance(c["resource_types"], list)
            assert isinstance(c["supported_operations"], list)
            assert isinstance(c["auth_model"], str)
            assert len(c["resource_types"]) > 0
            assert len(c["supported_operations"]) > 0

    def test_total_matches_connectors_length(self):
        result = _run(self._handler()(_consumer=_consumer()))
        assert result["total"] == len(result["connectors"])

    def test_connectors_sorted_by_id(self):
        result = _run(self._handler()(_consumer=_consumer()))
        ids = [c["connector_id"] for c in result["connectors"]]
        assert ids == sorted(ids), "connectors must be sorted by connector_id"


# ---------------------------------------------------------------------------
# (b) listConnectors in tools/list and callable without write/admin scope
# ---------------------------------------------------------------------------

class TestListConnectorsAuth:
    """listConnectors requires no write/admin scope — it is read-only."""

    def _handler(self):
        registry = build_external_tool_registry(_app())
        return registry["listConnectors"]

    def test_callable_with_read_only_scope(self):
        """A consumer with only 'read' scope can call listConnectors."""
        result = _run(self._handler()(_consumer=_consumer(scopes=["read"])))
        assert "connectors" in result
        assert result.get("isError") is not True

    def test_callable_with_no_scope_anonymous(self):
        """Anonymous consumer (no scopes) can call listConnectors."""
        result = _run(self._handler()(_consumer=_consumer(scopes=[])))
        assert "connectors" in result
        assert result.get("isError") is not True

    def test_callable_without_consumer(self):
        """No _consumer kwarg (None) falls back to anonymous — must succeed."""
        result = _run(self._handler()(_consumer=None))
        assert "connectors" in result
        assert result.get("isError") is not True

    def test_appears_in_external_tools_schema(self):
        """listConnectors must appear in EXTERNAL_TOOLS_SCHEMA (tools/list surface)."""
        tool_names = {t["name"] for t in EXTERNAL_TOOLS_SCHEMA}
        assert "listConnectors" in tool_names, (
            f"listConnectors must be in EXTERNAL_TOOLS_SCHEMA; found: {sorted(tool_names)}"
        )

    def test_schema_entry_has_correct_shape(self):
        """Schema entry for listConnectors must have name, description, inputSchema."""
        schema_entry = next(
            (t for t in EXTERNAL_TOOLS_SCHEMA if t["name"] == "listConnectors"), None
        )
        assert schema_entry is not None
        assert "description" in schema_entry
        assert "inputSchema" in schema_entry
        assert schema_entry["inputSchema"]["type"] == "object"
        # No required fields — listConnectors takes no input
        assert "required" not in schema_entry["inputSchema"] or not schema_entry["inputSchema"].get("required")

    def test_registered_in_build_external_tool_registry(self):
        """build_external_tool_registry must include 'listConnectors' key."""
        registry = build_external_tool_registry(_app())
        assert "listConnectors" in registry, (
            "listConnectors must be returned by build_external_tool_registry"
        )
        assert callable(registry["listConnectors"])


# ---------------------------------------------------------------------------
# (c) UDAP/fleet NOT present in listConnectors output
# ---------------------------------------------------------------------------

class TestListConnectorsNoUdap:
    """UDAP/fleet must not appear in listConnectors output (capability-honesty)."""

    def _handler(self):
        registry = build_external_tool_registry(_app())
        return registry["listConnectors"]

    def test_udap_not_in_connectors(self):
        result = _run(self._handler()(_consumer=_consumer()))
        ids = {c["connector_id"] for c in result["connectors"]}
        assert "udap" not in ids, (
            "UDAP must not appear in listConnectors output — deferred until "
            "production JDBC is implemented (ADR-036 Amendment 4)."
        )

    def test_fleet_alias_not_in_connectors(self):
        result = _run(self._handler()(_consumer=_consumer()))
        ids = {c["connector_id"] for c in result["connectors"]}
        assert "fleet" not in ids, (
            "'fleet' (UDAP alias) must not appear in listConnectors output"
        )

    def test_only_implemented_connectors_returned(self):
        result = _run(self._handler()(_consumer=_consumer()))
        ids = {c["connector_id"] for c in result["connectors"]}
        assert ids == EXPECTED_CONNECTOR_IDS, (
            f"Expected exactly {EXPECTED_CONNECTOR_IDS}; got {ids}"
        )


# ---------------------------------------------------------------------------
# (d) CONFIGURE_SOURCES entry prompt contains the supported-connectors block
# ---------------------------------------------------------------------------

class TestConfigureSourcesProactiveBlock:
    """CONFIGURE_SOURCES entry prompt includes the supported-connectors block."""

    def _make_conv(self, persona="tpm"):
        from framework.skill_builder.conversation import SkillBuilderConversation
        ss = MagicMock()
        ss.read_artifact.return_value = None
        c = SkillBuilderConversation(
            persona=persona,
            user_id="test-user",
            skill_store=ss,
        )
        c._state = "CONFIGURE_SOURCES"
        c._data.persona = persona
        c._data.skill_name = "test_skill"
        c._data.synth_id = "synth-disco-test"
        c._data.normalised_intent = {}  # legacy path (no LLM needed)
        return c

    def test_legacy_path_contains_supported_connectors_header(self):
        """_advance_to_configure_sources (legacy) must include 'Supported source connectors:'."""
        conv = self._make_conv()
        turn = conv._advance_to_configure_sources()
        assert "Supported source connectors:" in turn.message, (
            "CONFIGURE_SOURCES prompt must proactively list supported connectors"
        )

    def test_legacy_path_contains_confluence_display_name(self):
        conv = self._make_conv()
        turn = conv._advance_to_configure_sources()
        assert "Confluence" in turn.message

    def test_legacy_path_contains_jira_display_name(self):
        conv = self._make_conv()
        turn = conv._advance_to_configure_sources()
        assert "Jira" in turn.message

    def test_legacy_path_contains_git_display_name(self):
        conv = self._make_conv()
        turn = conv._advance_to_configure_sources()
        assert "Git" in turn.message

    def test_v2_no_sources_path_contains_supported_connectors_header(self):
        """_advance_to_configure_sources_v2 (no sources found) must include the block."""
        conv = self._make_conv()
        conv._data.intent_description = "I want to build a skill"
        # v2 path requires LLM — mock it to return empty proposal
        conv._llm = MagicMock()
        conv._llm.chat.return_value = {"text": "[]"}
        turn = conv._advance_to_configure_sources_v2()
        assert "Supported source connectors:" in turn.message, (
            "CONFIGURE_SOURCES v2 no-sources prompt must proactively list connectors"
        )

    def test_legacy_path_contains_three_connector_ids(self):
        """All three connector_ids must be visible in the CONFIGURE_SOURCES prompt."""
        conv = self._make_conv()
        turn = conv._advance_to_configure_sources()
        for cid in EXPECTED_CONNECTOR_IDS:
            assert cid in turn.message, (
                f"connector_id '{cid}' must appear in CONFIGURE_SOURCES prompt"
            )


# ---------------------------------------------------------------------------
# (e) Drift guard: proactive block and hard-stop block share one helper
# ---------------------------------------------------------------------------

class TestSharedFormattingHelperDriftGuard:
    """Both the proactive block and the hard-stop block render from the same
    format_supported_connectors_block() helper.

    To verify: create a synthetic registry with a stub connector, assert that
    (a) the hard-stop rejection message contains the stub connector's line and
    (b) the proactive block also contains the stub connector's line.
    Since both call format_supported_connectors_block(), changing the registry
    test-double once propagates to both surfaces.
    """

    def _make_stub_registry(self, tmp_path):
        """Create a registry loaded from a tmp_path with two stub manifests."""
        stub1 = tmp_path / "alpha_source.yaml"
        stub1.write_text(
            "connector_id: alpha_source\n"
            "display_name: Alpha Source\n"
            "description: test connector A\n"
            "resource_types: [record, snapshot]\n"
            "supported_operations: [read, list]\n"
            "auth_model: api_key\n"
            "access_probe_hook: framework.test.alpha.probe\n"
            "granularity_filters: [tenant_id]\n"
        )
        stub2 = tmp_path / "beta_store.yaml"
        stub2.write_text(
            "connector_id: beta_store\n"
            "display_name: Beta Store\n"
            "description: test connector B\n"
            "resource_types: [document]\n"
            "supported_operations: [query, search]\n"
            "auth_model: env_service_account\n"
            "access_probe_hook: framework.test.beta.probe\n"
            "granularity_filters: [namespace]\n"
        )
        return ConnectorRegistry(manifests_dir=tmp_path)

    def test_format_block_contains_all_stub_connectors(self, tmp_path):
        """format_supported_connectors_block() renders all provided manifests."""
        reg = self._make_stub_registry(tmp_path)
        manifests = reg.list_connectors()
        block = format_supported_connectors_block(manifests)
        assert "alpha_source" in block
        assert "beta_store" in block
        assert "Alpha Source" in block
        assert "Beta Store" in block

    def test_hard_stop_message_uses_format_block(self, tmp_path):
        """The hard-stop rejection message contains the same block as format_supported_connectors_block."""
        reg = self._make_stub_registry(tmp_path)
        manifests = reg.list_connectors()
        expected_block = format_supported_connectors_block(manifests)
        result = reg.gate_connector_type("not_registered_xyz")
        # The rejection message must contain the same formatted lines
        for line in expected_block.splitlines():
            assert line in result.message, (
                f"Hard-stop message must contain formatted line: {line!r}"
            )

    def test_proactive_block_uses_same_helper(self, tmp_path):
        """The proactive CONFIGURE_SOURCES block renders via format_supported_connectors_block."""
        from framework.skill_builder.conversation import _build_proactive_connector_block
        reg = self._make_stub_registry(tmp_path)

        # Patch the module-level _get_connector_registry in conversation.py
        # to return our stub registry, then call _build_proactive_connector_block.
        with patch(
            "framework.skill_builder.conversation._get_connector_registry",
            return_value=reg,
        ):
            block = _build_proactive_connector_block()

        assert "Supported source connectors:" in block
        assert "alpha_source" in block
        assert "beta_store" in block
        assert "Alpha Source" in block
        assert "Beta Store" in block

    def test_both_surfaces_reflect_registry_change(self, tmp_path):
        """Change the registry once — both surfaces (hard-stop and proactive) reflect it.

        This is the key drift-prevention test: if format_supported_connectors_block()
        is the single code path, then a new connector added to the registry
        automatically appears in both surfaces without any further code change.
        """
        from framework.skill_builder.conversation import _build_proactive_connector_block
        reg = self._make_stub_registry(tmp_path)

        # Surface 1: hard-stop rejection message
        hard_stop_result = reg.gate_connector_type("nonexistent_connector_xyz")
        hard_stop_msg = hard_stop_result.message

        # Surface 2: proactive CONFIGURE_SOURCES block
        with patch(
            "framework.skill_builder.conversation._get_connector_registry",
            return_value=reg,
        ):
            proactive_block = _build_proactive_connector_block()

        # Both must contain both stub connector ids
        for cid in ("alpha_source", "beta_store"):
            assert cid in hard_stop_msg, (
                f"Hard-stop message missing connector '{cid}' after registry change"
            )
            assert cid in proactive_block, (
                f"Proactive block missing connector '{cid}' after registry change"
            )

    def test_manifest_to_user_facing_strips_internal_fields(self):
        """manifest_to_user_facing() must not include access_probe_hook or granularity_filters."""
        m = ConnectorManifest(
            connector_id="test",
            display_name="Test",
            description="A test connector",
            resource_types=["item"],
            supported_operations=["read"],
            auth_model="api_key",
            access_probe_hook="framework.test.probe.verify_access",
            granularity_filters=["id"],
        )
        d = manifest_to_user_facing(m)
        assert set(d.keys()) == USER_FACING_FIELDS
        assert "access_probe_hook" not in d
        assert "granularity_filters" not in d
        assert d["connector_id"] == "test"
        assert d["display_name"] == "Test"

    def test_list_connectors_user_facing_real_registry(self):
        """list_connectors_user_facing() returns user-facing dicts for real connectors."""
        reg = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)
        results = reg.list_connectors_user_facing()
        assert len(results) == 3
        for c in results:
            assert set(c.keys()) == USER_FACING_FIELDS
            assert "access_probe_hook" not in c


# ---------------------------------------------------------------------------
# (f) No regression in existing CONFIGURE_SOURCES supported/unsupported flows
# ---------------------------------------------------------------------------

class TestConfigureSourcesRegressionWithProactiveBlock:
    """Supported and unsupported flows continue to work after the proactive block was added."""

    def _make_conv(self, persona="tpm"):
        from framework.skill_builder.conversation import SkillBuilderConversation
        ss = MagicMock()
        ss.read_artifact.return_value = None
        c = SkillBuilderConversation(
            persona=persona,
            user_id="test-user",
            skill_store=ss,
        )
        c._state = "CONFIGURE_SOURCES"
        c._data.persona = persona
        c._data.skill_name = "test_skill"
        c._data.synth_id = "synth-regression-test"
        c._data.normalised_intent = {}  # legacy path
        return c

    def test_confluence_source_still_accepted(self):
        """Adding a supported confluence source must NOT be hard-stopped."""
        conv = self._make_conv()
        turn = conv._handle_configure_sources_response(
            "confluence FACP labels: weekly-status"
        )
        assert "CONFIGURE_SOURCES failed" not in turn.message
        assert len(conv._data.sources) == 1
        assert conv._data.sources[0].get("kind") == "confluence"

    def test_jira_source_still_accepted(self):
        conv = self._make_conv()
        turn = conv._handle_configure_sources_response(
            "jira project = OPS AND labels = weekly-status"
        )
        assert "CONFIGURE_SOURCES failed" not in turn.message
        assert len(conv._data.sources) == 1
        assert conv._data.sources[0].get("kind") == "jira"

    def test_git_source_still_accepted(self):
        conv = self._make_conv()
        turn = conv._handle_configure_sources_response(
            "git repo org/my-repo paths: **/*.md"
        )
        assert "CONFIGURE_SOURCES failed" not in turn.message
        assert len(conv._data.sources) == 1
        assert conv._data.sources[0].get("kind") == "git"

    def test_unsupported_source_still_hard_stops(self):
        """An unsupported connector still triggers the hard stop (no regression)."""
        conv = self._make_conv()
        with patch.object(conv, "_log_connector_request"):
            turn = conv._handle_configure_sources_response("lumberjack_logs some-ref")
        assert "CONFIGURE_SOURCES failed" in turn.message
        assert '"lumberjack_logs"' in turn.message
        assert len(conv._data.sources) == 0

    def test_done_still_advances_with_sources(self):
        """'done' with a valid source advances the FSM (no regression)."""
        conv = self._make_conv()
        conv._data.sources.append({"kind": "confluence", "space": "FACP"})
        with patch.object(conv, "_advance_to_configure_triggers") as mock:
            mock.return_value = MagicMock(state="CONFIGURE_TRIGGERS")
            turn = conv._handle_configure_sources_response("done")
        mock.assert_called_once()

    def test_proactive_block_present_in_legacy_entry_prompt(self):
        """The entry prompt from _advance_to_configure_sources includes the block."""
        conv = self._make_conv()
        turn = conv._advance_to_configure_sources()
        # All three display names must appear
        assert "Confluence" in turn.message
        assert "Jira" in turn.message
        assert "Git" in turn.message
        # The section header must appear
        assert "Supported source connectors:" in turn.message
        # The main instruction must still appear
        assert "Where does the source data live?" in turn.message
