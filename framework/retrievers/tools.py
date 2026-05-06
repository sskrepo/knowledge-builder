"""MCP server tool registration — wires retrievers into FastAPI MCP endpoint."""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


def register_v1_tools(retrievers: dict):
    """Register Phase 1 tools by name. Returns a registry: {tool_name: callable}."""
    return {r.name: r for r in retrievers.values()}
