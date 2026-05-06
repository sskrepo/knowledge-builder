from __future__ import annotations
from .native import JiraNativeAdapter
from .mcp import JiraMcpAdapter
from .._base import Adapter

def make_jira_adapter(adapter_config: dict) -> Adapter:
    mode = adapter_config.get("mode", "native")
    if mode == "native":
        return JiraNativeAdapter(adapter_config["native"])
    elif mode == "mcp":
        return JiraMcpAdapter(adapter_config["mcp"])
    raise ValueError(f"unknown jira mode: {mode}")
