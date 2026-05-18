"""Connector Registry — ADR-036: read-only capability manifest catalog.

Single source of truth for supported connector types and their capability
manifests.  CONFIGURE_SOURCES consults this registry before proceeding to
DESIGN_SKILL or ADR-035 instance access-verify.

Phase constraint (ADR-036 / DECISION-016): read-only operations only.
Operations "write", "delete", "create", "update" are reserved for ADR-037
Phase 1 and MUST NOT appear in registered manifests.

Registry location: framework/connectors/manifests/*.yaml
Each file is one connector manifest (connector_id must match filename stem).

Usage:
    from framework.connectors.registry import get_registry, validate_connector_op
    registry = get_registry()
    result = validate_connector_op("confluence", "read")
    if result.status == HARD_STOP:
        print(result.message)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase-constraint: operations permitted in this ADR-036 phase
# ---------------------------------------------------------------------------
_READ_ONLY_OPS: frozenset[str] = frozenset({"read", "query", "list", "search"})

# Operations reserved for ADR-037 — must NOT be registered in manifests yet
_WRITE_OPS: frozenset[str] = frozenset({"write", "delete", "create", "update"})

# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------

@dataclass
class ConnectorManifest:
    """Capability manifest for a registered connector type.

    Per ADR-036 §C.1 schema.  All nine fields are present; `notes` is
    optional (None when the manifest sets ``notes: ~`` or omits the key).
    """
    connector_id: str
    display_name: str
    description: str
    resource_types: list[str]
    supported_operations: list[str]
    auth_model: str
    access_probe_hook: str
    granularity_filters: list[str]
    notes: Optional[str] = None

    def supports_operation(self, operation: str) -> bool:
        """Return True when the connector supports the requested operation."""
        return operation in self.supported_operations


# ---------------------------------------------------------------------------
# Gating result
# ---------------------------------------------------------------------------

HARD_STOP = "HARD_STOP"
PASS = "PASS"


@dataclass
class GatingResult:
    """Result of a CONFIGURE_SOURCES registry gate check.

    Attributes:
        status:  ``HARD_STOP`` or ``PASS``.
        message: Verbatim user-facing message (per ADR-036 §D.2) when
                 status == HARD_STOP; empty string when PASS.
        connector_id: The connector_id that was checked.
        operation:    The operation that was checked (may be None for
                      connector-not-found checks).
    """
    status: str
    message: str = ""
    connector_id: str = ""
    operation: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_MANIFESTS_DIR = Path(__file__).parent / "manifests"


class ConnectorRegistry:
    """Declarative catalog of supported connector types.

    Loaded at first access from YAML manifests in
    ``framework/connectors/manifests/``.

    The registry is the ONLY place where a connector type is declared as
    supported.  An adapter in ``framework/adapters/`` that is not registered
    here is treated as internal-only and is not available to skill authors.

    Per ADR-036: immutable once loaded; no dynamic registration at runtime.
    """

    def __init__(self, manifests_dir: Path | None = None) -> None:
        self._dir = manifests_dir or _MANIFESTS_DIR
        self._catalog: dict[str, ConnectorManifest] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Internal loader
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        errors: list[str] = []
        for yaml_path in sorted(self._dir.glob("*.yaml")):
            try:
                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                manifest = self._parse_manifest(raw, yaml_path)
                self._validate_manifest(manifest, yaml_path)
                self._catalog[manifest.connector_id] = manifest
            except Exception as exc:
                errors.append(f"  {yaml_path.name}: {exc}")
        if errors:
            raise RuntimeError(
                "ConnectorRegistry: failed to load one or more manifests:\n"
                + "\n".join(errors)
            )
        self._loaded = True
        log.debug(
            "ConnectorRegistry loaded %d connector(s): %s",
            len(self._catalog),
            sorted(self._catalog),
        )

    @staticmethod
    def _parse_manifest(raw: dict, path: Path) -> ConnectorManifest:
        required = {
            "connector_id", "display_name", "description",
            "resource_types", "supported_operations",
            "auth_model", "access_probe_hook", "granularity_filters",
        }
        missing = required - set(raw or {})
        if missing:
            raise ValueError(
                f"Missing required field(s): {sorted(missing)}"
            )
        return ConnectorManifest(
            connector_id=str(raw["connector_id"]),
            display_name=str(raw["display_name"]),
            description=str(raw["description"]).strip(),
            resource_types=[str(r) for r in raw["resource_types"]],
            supported_operations=[str(op) for op in raw["supported_operations"]],
            auth_model=str(raw["auth_model"]),
            access_probe_hook=str(raw["access_probe_hook"]),
            granularity_filters=[str(f) for f in raw["granularity_filters"]],
            notes=str(raw["notes"]).strip() if raw.get("notes") else None,
        )

    @staticmethod
    def _validate_manifest(manifest: ConnectorManifest, path: Path) -> None:
        """Phase constraint: no write operations in this phase (ADR-036 §C.1)."""
        illegal = _WRITE_OPS & set(manifest.supported_operations)
        if illegal:
            raise ValueError(
                f"Manifest '{manifest.connector_id}' declares write-phase operations "
                f"{sorted(illegal)} — these are reserved for ADR-037 Phase 1 and "
                f"MUST NOT appear in registered manifests in the ADR-036 phase."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_connector(self, connector_id: str) -> Optional[ConnectorManifest]:
        """Return the manifest for the given connector_id, or None if not found."""
        self._ensure_loaded()
        return self._catalog.get(connector_id)

    def list_connectors(self) -> list[ConnectorManifest]:
        """Return all registered manifests, sorted by connector_id."""
        self._ensure_loaded()
        return [self._catalog[k] for k in sorted(self._catalog)]

    # ------------------------------------------------------------------
    # CONFIGURE_SOURCES gating (ADR-036 §D.1)
    # ------------------------------------------------------------------

    def gate_connector_type(
        self,
        connector_id: str,
        operation: Optional[str] = None,
    ) -> GatingResult:
        """Check the connector type + optional operation against the registry.

        This is Step 2 of the CONFIGURE_SOURCES gate sequence
        (ADR-036 §D.1):

        a. Look up connector_id in the registry.
        b. If NOT FOUND → HARD_STOP with the verbatim ADR-036 §D.2 message.
        c. If FOUND and operation provided → verify operation is in
           supported_operations.
        d. If operation not supported → HARD_STOP.
        e. Otherwise → PASS.

        Args:
            connector_id: The ``source_type`` / ``kind`` string from the
                          skill author's input (e.g. ``"confluence"``).
            operation:    Optional operation to validate (e.g. ``"read"``).
                          When None, only the connector type is checked.

        Returns:
            GatingResult with status PASS or HARD_STOP.
        """
        self._ensure_loaded()
        manifest = self._catalog.get(connector_id)

        if manifest is None:
            return GatingResult(
                status=HARD_STOP,
                connector_id=connector_id,
                operation=operation,
                message=self._build_unsupported_connector_message(connector_id),
            )

        if operation is not None and not manifest.supports_operation(operation):
            return GatingResult(
                status=HARD_STOP,
                connector_id=connector_id,
                operation=operation,
                message=self._build_unsupported_operation_message(
                    connector_id, operation, manifest
                ),
            )

        return GatingResult(status=PASS, connector_id=connector_id, operation=operation)

    # ------------------------------------------------------------------
    # Message builders (ADR-036 §D.2 — verbatim pattern)
    # ------------------------------------------------------------------

    def _build_unsupported_connector_message(self, connector_id: str) -> str:
        supported_lines = "\n".join(
            f"  - {m.connector_id:<16}({m.display_name} — "
            f"{', '.join(m.resource_types[:3])}{'...' if len(m.resource_types) > 3 else ''})"
            for m in self.list_connectors()
        )
        return (
            f'CONFIGURE_SOURCES failed: unsupported connector type "{connector_id}".\n'
            "\n"
            "This connector type is not registered in the Connector Registry and cannot\n"
            "be used as a skill source.\n"
            "\n"
            "Supported connector types in this framework installation:\n"
            f"{supported_lines}\n"
            "\n"
            f'To use "{connector_id}" as a source, the connector must first be registered\n'
            "in the Connector Registry with a capability manifest. This is an engineering\n"
            "task, not a skill-author task.\n"
            "\n"
            "Skill design has not been started. No partial state has been saved."
        )

    def _build_unsupported_operation_message(
        self,
        connector_id: str,
        operation: str,
        manifest: ConnectorManifest,
    ) -> str:
        ops_str = ", ".join(f'"{op}"' for op in manifest.supported_operations)
        return (
            f'CONFIGURE_SOURCES failed: connector "{connector_id}" does not support '
            f'operation "{operation}".\n'
            "\n"
            f"Supported operations for {manifest.display_name}: {ops_str}.\n"
            "\n"
            f'The operation "{operation}" is not available for this connector in the '
            f"current framework phase (ADR-036 read-only phase).\n"
            "\n"
            "Skill design has not been started. No partial state has been saved."
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Optional[ConnectorRegistry] = None


def get_registry() -> ConnectorRegistry:
    """Return the module-level ConnectorRegistry singleton (lazy-load)."""
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry()
    return _registry


def validate_connector_op(
    connector_id: str,
    operation: Optional[str] = None,
) -> GatingResult:
    """Convenience: validate a (connector_id, operation) pair.

    Delegates to the module-level registry singleton.
    """
    return get_registry().gate_connector_type(connector_id, operation)
