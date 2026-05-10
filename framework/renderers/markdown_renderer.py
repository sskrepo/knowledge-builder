"""Markdown renderer — render data dict via Jinja-style template.

Per ADR-016. Default-template-friendly: takes a `sections` dict and produces
clean Markdown without external deps.
"""
from __future__ import annotations
from pathlib import Path
from ._base import BaseRenderer


class MarkdownRenderer(BaseRenderer):
    name = "markdown"
    output_extension = ".md"

    def render(self, data: dict, template: str | Path | None = None) -> bytes:
        if template:
            t = Path(template).read_text() if Path(template).exists() else str(template)
            # Minimal templating: replace {{ key }} with data.get("key", "")
            out = t
            for k, v in self._flatten(data).items():
                out = out.replace("{{ " + k + " }}", str(v))
                out = out.replace("{{" + k + "}}", str(v))
            return out.encode("utf-8")
        # Default rendering
        lines = []
        if "title" in data:
            lines.append(f"# {data['title']}\n")
        for section_name, body in data.get("sections", {}).items():
            lines.append(f"## {section_name}\n")
            if isinstance(body, list):
                for item in body:
                    lines.append(f"- {item}")
            elif isinstance(body, dict):
                for k, v in body.items():
                    lines.append(f"- **{k}**: {v}")
            else:
                lines.append(str(body))
            lines.append("")
        return ("\n".join(lines)).encode("utf-8")

    @staticmethod
    def _flatten(d: dict, prefix="") -> dict:
        out = {}
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(MarkdownRenderer._flatten(v, key))
            else:
                out[key] = v
        return out
