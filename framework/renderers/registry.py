"""Renderer registry — looks up renderer by name."""
from __future__ import annotations
from .markdown_renderer import MarkdownRenderer
from .pptx_renderer import PptxRenderer
from .docx_renderer import DocxRenderer
from .email_renderer import EmailRenderer
from .slack_renderer import SlackRenderer

_RENDERERS = {
    "markdown": MarkdownRenderer,
    "pptx":     PptxRenderer,
    "docx":     DocxRenderer,
    "email":    EmailRenderer,
    "slack":    SlackRenderer,
}

def get_renderer(name: str):
    if name not in _RENDERERS:
        raise ValueError(f"unknown renderer: {name}; available: {list(_RENDERERS)}")
    return _RENDERERS[name]()
