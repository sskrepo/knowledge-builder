"""Renderer Protocol — per ADR-016.

A Renderer takes (data: dict, template: str|Path) → bytes (the rendered artifact).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    name: str             # "pptx" | "docx" | "email" | "slack" | "markdown"
    output_extension: str # ".pptx" | ".docx" | ".html" | ".md"

    def render(self, data: dict, template: str | Path | None = None) -> bytes:
        """Render data into bytes. `template` may be None for default-template renderers."""
        ...


class BaseRenderer:
    name: str = ""
    output_extension: str = ""

    def render(self, data: dict, template: str | Path | None = None) -> bytes:
        raise NotImplementedError
