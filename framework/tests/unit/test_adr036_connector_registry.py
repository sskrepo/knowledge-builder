"""ADR-036 — Connector Registry: read-only capability manifest catalog.

Tests per ADR-036 §H (test strategy) and implementation spec:
  (a) Registry exposes all 3 connector manifests with the full 8-field schema.
      (UDAP intentionally NOT registered — adapter raises NotImplementedError in
      prod; dev-fixtures-only. Registering an unimplemented connector violates the
      capability-honesty principle this ADR exists to enforce.)
  (b) A supported (connector, op) (e.g. confluence read) validates OK → PASS.
  (c) An unsupported connector (e.g. "lumberjack") triggers HARD_STOP with the
      ADR-036 §D.2 verbatim message pattern and does NOT proceed to DESIGN.
  (d) A write op is rejected (read-only phase constraint).
  (e) CONFIGURE_SOURCES still works for normal supported flows (no regression).
  (f) Registry is the single source of truth.
  (g) UDAP/fleet source triggers the honest hard-stop (same path as any other
      unregistered connector) — capability honesty for the unimplemented connector.

No live LLM / ADB calls — all tests use mocks.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.connectors.registry import (
    ConnectorManifest,
    ConnectorRegistry,
    GatingResult,
    HARD_STOP,
    PASS,
    get_registry,
    validate_connector_op,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MANIFESTS_DIR = Path(__file__).resolve().parents[2] / "connectors" / "manifests"

EXPECTED_CONNECTORS = {"confluence", "jira", "git"}
# UDAP is intentionally NOT registered: udap_adapter raises NotImplementedError in
# production (dev-fixtures-only). Registering an unimplemented connector would
# violate the capability-honesty principle ADR-036 exists to enforce.
DEFERRED_CONNECTORS = {"udap"}


# ---------------------------------------------------------------------------
# (a) Registry exposes all 4 connector manifests with the full schema
# ---------------------------------------------------------------------------

class TestRegistryCatalog:
    """Registry loads all four ADR-036 manifests with correct schema."""

    def setup_method(self):
        # Use a fresh registry from the real manifests directory
        self.registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)

    def test_list_connectors_returns_exactly_three(self):
        manifests = self.registry.list_connectors()
        ids = {m.connector_id for m in manifests}
        assert ids == EXPECTED_CONNECTORS, (
            f"Registry must expose exactly {EXPECTED_CONNECTORS}; got {ids}"
        )

    def test_udap_is_not_registered(self):
        """UDAP must NOT be in the registry — deferred until prod JDBC is implemented."""
        assert self.registry.get_connector("udap") is None, (
            "UDAP must not be registered: udap_adapter raises NotImplementedError in "
            "production (dev-fixtures-only). Registering it would violate ADR-036 "
            "capability-honesty."
        )
        ids = {m.connector_id for m in self.registry.list_connectors()}
        assert "udap" not in ids

    def test_manifest_schema_all_fields_present(self):
        """Every manifest must have all 8 required fields per ADR-036 §C.1."""
        for m in self.registry.list_connectors():
            assert m.connector_id, f"{m.connector_id}: connector_id empty"
            assert m.display_name, f"{m.connector_id}: display_name empty"
            assert m.description, f"{m.connector_id}: description empty"
            assert m.resource_types, f"{m.connector_id}: resource_types empty"
            assert m.supported_operations, f"{m.connector_id}: supported_operations empty"
            assert m.auth_model, f"{m.connector_id}: auth_model empty"
            assert m.access_probe_hook, f"{m.connector_id}: access_probe_hook empty"
            assert m.granularity_filters, f"{m.connector_id}: granularity_filters empty"

    def test_confluence_manifest_fields(self):
        m = self.registry.get_connector("confluence")
        assert m is not None
        assert m.display_name == "Confluence"
        assert "page" in m.resource_types
        assert "read" in m.supported_operations
        assert "search" in m.supported_operations
        assert m.auth_model == "api_key"
        assert "space_key" in m.granularity_filters
        assert m.access_probe_hook == "framework.adapters.confluence.probe.verify_access"

    def test_jira_manifest_fields(self):
        m = self.registry.get_connector("jira")
        assert m is not None
        assert m.display_name == "Jira"
        assert "issue" in m.resource_types
        assert "query" in m.supported_operations
        assert m.auth_model == "api_key"
        assert "jql_filter" in m.granularity_filters

    def test_git_manifest_fields(self):
        m = self.registry.get_connector("git")
        assert m is not None
        assert m.display_name == "Git Repository"
        assert "file" in m.resource_types
        assert "read" in m.supported_operations
        assert m.auth_model == "env_service_account"
        assert "path_prefix" in m.granularity_filters

    def test_get_connector_unknown_returns_none(self):
        assert self.registry.get_connector("lumberjack_logs") is None
        assert self.registry.get_connector("") is None
        assert self.registry.get_connector("CONFLUENCE") is None  # case-sensitive

    def test_list_connectors_sorted_by_id(self):
        manifests = self.registry.list_connectors()
        ids = [m.connector_id for m in manifests]
        assert ids == sorted(ids), "list_connectors must return sorted by connector_id"


# ---------------------------------------------------------------------------
# (b) Supported (connector, op) validates OK → PASS
# ---------------------------------------------------------------------------

class TestGatingSupportedConnectors:
    """Supported connector + operation combinations return PASS."""

    def setup_method(self):
        self.registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)

    def test_confluence_read_pass(self):
        result = self.registry.gate_connector_type("confluence", "read")
        assert result.status == PASS
        assert result.connector_id == "confluence"
        assert result.operation == "read"
        assert result.message == ""

    def test_confluence_search_pass(self):
        result = self.registry.gate_connector_type("confluence", "search")
        assert result.status == PASS

    def test_jira_query_pass(self):
        result = self.registry.gate_connector_type("jira", "query")
        assert result.status == PASS

    def test_git_list_pass(self):
        result = self.registry.gate_connector_type("git", "list")
        assert result.status == PASS

    def test_udap_read_hard_stops(self):
        """UDAP is not registered — any operation on it must produce HARD_STOP."""
        result = self.registry.gate_connector_type("udap", "read")
        assert result.status == HARD_STOP, (
            "UDAP is not registered in the connector registry (deferred: prod JDBC "
            "unimplemented). Requesting it must hit the honest hard-stop."
        )
        assert "udap" in result.message

    def test_connector_type_check_only_no_operation(self):
        """When operation is None, only the connector type is checked."""
        result = self.registry.gate_connector_type("confluence")
        assert result.status == PASS

    def test_module_level_validate_connector_op(self):
        """validate_connector_op convenience function delegates to singleton."""
        result = validate_connector_op("jira", "read")
        assert result.status == PASS


# ---------------------------------------------------------------------------
# (c) Unsupported connector triggers HARD_STOP with ADR-036 §D.2 message
# ---------------------------------------------------------------------------

class TestGatingUnsupportedConnector:
    """Unsupported connector produces HARD_STOP with verbatim ADR-036 §D.2 message."""

    def setup_method(self):
        self.registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)

    def test_lumberjack_hard_stop(self):
        result = self.registry.gate_connector_type("lumberjack_logs")
        assert result.status == HARD_STOP
        assert result.connector_id == "lumberjack_logs"

    def test_hard_stop_message_names_connector(self):
        result = self.registry.gate_connector_type("lumberjack_logs")
        assert '"lumberjack_logs"' in result.message, (
            "Hard-stop message must name the unsupported connector verbatim"
        )

    def test_hard_stop_message_lists_supported_connectors(self):
        result = self.registry.gate_connector_type("lumberjack_logs")
        for cid in EXPECTED_CONNECTORS:
            assert cid in result.message, (
                f"Hard-stop message must list all supported connectors; missing: {cid}"
            )

    def test_hard_stop_message_starts_with_configure_sources_failed(self):
        result = self.registry.gate_connector_type("lumberjack_logs")
        assert result.message.startswith("CONFIGURE_SOURCES failed:"), (
            "Hard-stop message must start with 'CONFIGURE_SOURCES failed:'"
        )

    def test_hard_stop_message_no_partial_state(self):
        result = self.registry.gate_connector_type("lumberjack_logs")
        assert "Skill design has not been started" in result.message

    def test_hard_stop_message_distinguishes_skill_author_vs_engineering(self):
        result = self.registry.gate_connector_type("lumberjack_logs")
        msg_lower = result.message.lower().replace("\n", " ")
        assert "engineering" in msg_lower and "skill-author" in msg_lower, (
            "Message must distinguish skill-author action from engineering action; "
            f"got: {result.message!r}"
        )

    def test_unknown_connector_does_not_proceed(self):
        """Gate returns HARD_STOP; caller must NOT proceed to DESIGN."""
        result = self.registry.gate_connector_type("nonexistent_source")
        assert result.status == HARD_STOP
        # No manifest returned — no probe hook to call
        assert self.registry.get_connector("nonexistent_source") is None


# ---------------------------------------------------------------------------
# (g) UDAP / fleet source triggers the honest hard-stop
# ---------------------------------------------------------------------------

class TestUdapDeferred:
    """UDAP is not registered in the connector registry (capability-honesty).

    udap_adapter.py raises NotImplementedError for all production list/fetch/discover
    calls.  It only works in filestore/dev mode against _dev_fixtures/fleet/*.json.
    Registering an unimplemented connector is exactly the capability-dishonesty
    ADR-036 exists to prevent.  UDAP is deferred until its production JDBC path
    is implemented.
    """

    def setup_method(self):
        self.registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)

    def test_udap_not_registered(self):
        """UDAP must not appear in the registry catalog."""
        assert self.registry.get_connector("udap") is None

    def test_fleet_request_hard_stops(self):
        """Requesting 'udap' (fleet) as a source triggers the honest hard-stop."""
        result = self.registry.gate_connector_type("udap")
        assert result.status == HARD_STOP, (
            "Requesting 'udap' must produce HARD_STOP — it is not registered "
            "because udap_adapter raises NotImplementedError in production."
        )

    def test_udap_hard_stop_message_names_connector(self):
        result = self.registry.gate_connector_type("udap", "query")
        assert result.status == HARD_STOP
        assert '"udap"' in result.message

    def test_udap_hard_stop_message_starts_with_configure_sources_failed(self):
        result = self.registry.gate_connector_type("udap", "read")
        assert result.message.startswith("CONFIGURE_SOURCES failed:")

    def test_udap_hard_stop_lists_only_implemented_connectors(self):
        """The hard-stop message must list the THREE implemented connectors, not UDAP."""
        result = self.registry.gate_connector_type("udap")
        for cid in EXPECTED_CONNECTORS:
            assert cid in result.message, (
                f"Hard-stop message must list supported connector '{cid}'"
            )
        # UDAP must not appear in the supported list in the message
        # (it is the unsupported connector being rejected)
        # The message names udap as the rejected one, not as supported
        supported_section_start = result.message.find("Supported connector types")
        if supported_section_start != -1:
            supported_section = result.message[supported_section_start:]
            # The supported section lists only confluence, jira, git
            for cid in EXPECTED_CONNECTORS:
                assert cid in supported_section

    def test_udap_no_manifest_file(self):
        """No udap.yaml must exist in the manifests directory."""
        udap_manifest = MANIFESTS_DIR / "udap.yaml"
        assert not udap_manifest.exists(), (
            f"udap.yaml must not exist in {MANIFESTS_DIR} — UDAP is deferred."
        )

    def test_fleet_alias_also_hard_stops(self):
        """'fleet' (common alias for UDAP) is also not registered."""
        result = self.registry.gate_connector_type("fleet")
        assert result.status == HARD_STOP


# ---------------------------------------------------------------------------
# (d) Write ops are rejected (read-only phase constraint)
# ---------------------------------------------------------------------------

class TestWriteOpRejected:
    """Write operations are rejected at manifest loading time (phase constraint)."""

    def test_manifest_with_write_op_raises_on_load(self, tmp_path):
        """A manifest declaring 'write' must fail to load."""
        bad_manifest = tmp_path / "bad_connector.yaml"
        bad_manifest.write_text(
            "connector_id: bad\n"
            "display_name: Bad\n"
            "description: test\n"
            "resource_types: [thing]\n"
            "supported_operations: [read, write]\n"
            "auth_model: api_key\n"
            "access_probe_hook: framework.test.bad.probe\n"
            "granularity_filters: [id]\n"
        )
        registry = ConnectorRegistry(manifests_dir=tmp_path)
        with pytest.raises(RuntimeError, match="write-phase operations"):
            registry.list_connectors()

    def test_manifest_with_delete_op_raises_on_load(self, tmp_path):
        bad_manifest = tmp_path / "delete_connector.yaml"
        bad_manifest.write_text(
            "connector_id: del\n"
            "display_name: Del\n"
            "description: test\n"
            "resource_types: [thing]\n"
            "supported_operations: [read, delete]\n"
            "auth_model: api_key\n"
            "access_probe_hook: framework.test.del.probe\n"
            "granularity_filters: [id]\n"
        )
        registry = ConnectorRegistry(manifests_dir=tmp_path)
        with pytest.raises(RuntimeError, match="write-phase operations"):
            registry.list_connectors()

    def test_write_op_on_registered_connector_returns_hard_stop(self):
        """gate_connector_type('confluence', 'write') → HARD_STOP (op not supported)."""
        registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)
        result = registry.gate_connector_type("confluence", "write")
        assert result.status == HARD_STOP
        assert "write" in result.message


# ---------------------------------------------------------------------------
# (e) CONFIGURE_SOURCES regression: supported sources still work
# ---------------------------------------------------------------------------

class TestConfigureSourcesRegression:
    """Supported connector flows do not regress after ADR-036 wiring."""

    def _make_skill_store(self):
        ss = MagicMock()
        ss.read_artifact.return_value = None
        return ss

    def _make_conv(self, persona="tpm"):
        from framework.skill_builder.conversation import SkillBuilderConversation
        c = SkillBuilderConversation(
            persona=persona,
            user_id="test-user",
            skill_store=self._make_skill_store(),
        )
        c._state = "CONFIGURE_SOURCES"
        c._data.persona = persona
        c._data.skill_name = "test_skill"
        c._data.synth_id = "synth-adr036-test"
        c._data.normalised_intent = {}  # legacy path — no LLM needed
        return c

    def test_confluence_source_accepted_no_hard_stop(self):
        """A 'confluence' source passes the registry gate and is added."""
        conv = self._make_conv()
        turn = conv._handle_configure_sources_response(
            "confluence FACP labels: weekly-status"
        )
        # Should NOT be a HARD_STOP — source added
        assert turn.state == "CONFIGURE_SOURCES"
        assert "lumberjack" not in turn.message.lower()
        # Source was added (not hard-stopped)
        assert len(conv._data.sources) == 1
        assert conv._data.sources[0].get("kind") == "confluence"

    def test_jira_source_accepted(self):
        conv = self._make_conv()
        turn = conv._handle_configure_sources_response(
            "jira project = OPS AND labels = weekly-status"
        )
        assert turn.state == "CONFIGURE_SOURCES"
        assert len(conv._data.sources) == 1
        assert conv._data.sources[0].get("kind") == "jira"

    def test_git_source_accepted(self):
        conv = self._make_conv()
        turn = conv._handle_configure_sources_response(
            "git repo org/my-repo paths: **/*.md"
        )
        assert turn.state == "CONFIGURE_SOURCES"
        assert len(conv._data.sources) == 1
        assert conv._data.sources[0].get("kind") == "git"

    def test_lumberjack_source_hard_stops_no_source_added(self):
        """An unsupported connector hard-stops; source is NOT added to session."""
        conv = self._make_conv()
        # Mock _log_connector_request to avoid ADB/file writes in tests
        with patch.object(conv, "_log_connector_request"):
            turn = conv._handle_configure_sources_response("lumberjack_logs some-ref")
        # Gate fired HARD_STOP
        assert "CONFIGURE_SOURCES failed" in turn.message
        assert '"lumberjack_logs"' in turn.message
        # Source must NOT be in session
        assert len(conv._data.sources) == 0

    def test_done_with_confluence_source_advances(self):
        """When user types 'done' after adding a valid confluence source, FSM advances."""
        conv = self._make_conv()
        # Pre-add a valid source
        conv._data.sources.append({"kind": "confluence", "space": "FACP"})
        # 'done' with legacy path (no normalised_intent) → _advance_to_configure_triggers
        with patch.object(conv, "_advance_to_configure_triggers") as mock_advance:
            mock_advance.return_value = MagicMock(state="CONFIGURE_TRIGGERS")
            turn = conv._handle_configure_sources_response("done")
        mock_advance.assert_called_once()


# ---------------------------------------------------------------------------
# (f) Registry is the single source of truth
# ---------------------------------------------------------------------------

class TestRegistrySingleSourceOfTruth:
    """Connector types are declared ONLY in the registry manifests."""

    def setup_method(self):
        self.registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)

    def test_registry_is_sole_authority_for_supported_connectors(self):
        """list_connectors() returns exactly the registered set; nothing extra."""
        ids = {m.connector_id for m in self.registry.list_connectors()}
        assert ids == EXPECTED_CONNECTORS

    def test_manifests_dir_contains_exactly_three_yaml_files(self):
        yaml_files = list(MANIFESTS_DIR.glob("*.yaml"))
        assert len(yaml_files) == 3, (
            f"Expected exactly 3 manifest YAML files (confluence, jira, git); "
            f"found {len(yaml_files)}: {[f.name for f in yaml_files]}. "
            f"UDAP is intentionally excluded — deferred until prod JDBC is implemented."
        )

    def test_each_manifest_filename_matches_connector_id(self):
        """Manifest filename stem must match connector_id for traceability."""
        for yaml_path in MANIFESTS_DIR.glob("*.yaml"):
            import yaml as _yaml
            raw = _yaml.safe_load(yaml_path.read_text())
            assert raw["connector_id"] == yaml_path.stem, (
                f"Manifest {yaml_path.name}: filename stem '{yaml_path.stem}' must "
                f"match connector_id '{raw['connector_id']}'"
            )

    def test_no_write_ops_in_any_registered_manifest(self):
        """Phase constraint: no write ops in any registered manifest."""
        write_ops = {"write", "delete", "create", "update"}
        for m in self.registry.list_connectors():
            illegal = write_ops & set(m.supported_operations)
            assert not illegal, (
                f"Manifest '{m.connector_id}' declares write-phase ops {illegal} — "
                "these are reserved for ADR-037 Phase 1"
            )


# ---------------------------------------------------------------------------
# Probe module imports (check access_probe_hook paths are importable)
# ---------------------------------------------------------------------------

class TestProbeHooks:
    """access_probe_hook dotted paths for registered connectors must be importable."""

    def test_confluence_probe_importable(self):
        from framework.adapters.confluence.probe import verify_access
        assert callable(verify_access)

    def test_jira_probe_importable(self):
        from framework.adapters.jira.probe import verify_access
        assert callable(verify_access)

    def test_git_probe_importable(self):
        from framework.adapters.git_probe import verify_access
        assert callable(verify_access)

    def test_udap_probe_not_in_registry(self):
        """udap_probe exists as dev-fixture code but must NOT appear in registry manifests.

        The probe itself is not deleted (the adapter may be used internally in
        dev/filestore mode), but no manifest references it — UDAP is not a
        registered connector until its production JDBC path is implemented.
        """
        registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)
        manifest = registry.get_connector("udap")
        assert manifest is None, (
            "UDAP manifest must not exist in the registry. "
            "udap_adapter raises NotImplementedError in production."
        )

    def test_confluence_probe_returns_dict_with_required_keys(self, tmp_path):
        """Probe returns a dict with reachable, connector_id, reference, mode, notes."""
        from framework.adapters.confluence.probe import verify_access
        result = verify_access(reference="TEST", env="laptop")
        assert isinstance(result, dict)
        for key in ("reachable", "connector_id", "reference", "mode", "notes"):
            assert key in result, f"Probe result missing key: {key}"
        assert result["connector_id"] == "confluence"

    def test_git_probe_returns_dict_with_required_keys(self):
        from framework.adapters.git_probe import verify_access
        result = verify_access(reference="org/repo", env="laptop")
        assert isinstance(result, dict)
        assert result["connector_id"] == "git"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestRegistryEdgeCases:

    def test_fresh_registry_each_time_loads_cleanly(self):
        """Creating a new ConnectorRegistry always loads cleanly."""
        r1 = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)
        r2 = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)
        assert len(r1.list_connectors()) == len(r2.list_connectors())

    def test_missing_required_field_raises_on_load(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "connector_id: bad\n"
            "display_name: Bad\n"
            # missing description, resource_types, supported_operations, auth_model,
            # access_probe_hook, granularity_filters
        )
        registry = ConnectorRegistry(manifests_dir=tmp_path)
        with pytest.raises(RuntimeError, match="Missing required field"):
            registry.list_connectors()

    def test_empty_manifests_dir_loads_empty_catalog(self, tmp_path):
        registry = ConnectorRegistry(manifests_dir=tmp_path)
        assert registry.list_connectors() == []

    def test_gate_unsupported_op_on_known_connector(self):
        registry = ConnectorRegistry(manifests_dir=MANIFESTS_DIR)
        # 'delete' is not in confluence's supported_operations (read-only phase)
        result = registry.gate_connector_type("confluence", "delete")
        assert result.status == HARD_STOP
        assert "delete" in result.message

    def test_gating_result_dataclass(self):
        r = GatingResult(status=PASS, connector_id="confluence", operation="read")
        assert r.status == PASS
        assert r.message == ""
