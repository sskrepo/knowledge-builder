"""Connector Registry — ADR-036.

Single source of truth listing supported connector types and their
capability manifests.  CONFIGURE_SOURCES consults this registry before
allowing skill design to proceed.

Phase: read-only (ADR-036).  Write/orchestration is ADR-037 roadmap.
"""
from .registry import (
    ConnectorManifest,
    ConnectorRegistry,
    get_registry,
    validate_connector_op,
    GatingResult,
    HARD_STOP,
    PASS,
)

__all__ = [
    "ConnectorManifest",
    "ConnectorRegistry",
    "get_registry",
    "validate_connector_op",
    "GatingResult",
    "HARD_STOP",
    "PASS",
]
