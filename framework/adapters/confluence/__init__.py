"""Confluence adapter factory — picks native or mcp based on config."""
from __future__ import annotations
from .native import ConfluenceNativeAdapter
from .mcp import ConfluenceMcpAdapter
from .._base import Adapter

def make_confluence_adapter(adapter_config: dict) -> Adapter:
    mode = adapter_config.get("mode", "native")
    if mode == "native":
        return ConfluenceNativeAdapter(adapter_config["native"])
    elif mode == "mcp":
        return ConfluenceMcpAdapter(adapter_config["mcp"])
    raise ValueError(f"unknown confluence mode: {mode}")
