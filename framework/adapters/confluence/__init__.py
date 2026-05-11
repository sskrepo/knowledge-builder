"""Confluence adapter factory — picks native, mcp, or codex_cli based on config."""
from __future__ import annotations

import os

from .native import ConfluenceNativeAdapter
from .mcp import ConfluenceMcpAdapter
from .._base import Adapter


def make_confluence_adapter(adapter_config: dict) -> Adapter:
    mode = adapter_config.get("mode", "native")
    if mode == "native":
        return ConfluenceNativeAdapter(adapter_config["native"])
    elif mode == "mcp":
        return ConfluenceMcpAdapter(adapter_config["mcp"])
    elif mode == "codex_cli":
        if os.getenv("KBF_ENV", "dev") not in ("dev", "laptop"):
            raise RuntimeError(
                "mode: codex_cli is laptop-only. "
                "Set mode: mcp with a service token for staging/prod."
            )
        from .codex_cli import ConfluenceCodexCliAdapter
        return ConfluenceCodexCliAdapter(adapter_config["codex_cli"])
    raise ValueError(f"unknown confluence mode: {mode}")
