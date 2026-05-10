"""Email renderer — HTML body for emailing artifacts/answers."""
from __future__ import annotations
from pathlib import Path
from ._base import BaseRenderer


class EmailRenderer(BaseRenderer):
    name = "email"
    output_extension = ".html"

    def render(self, data: dict, template: str | Path | None = None) -> bytes:
        if template and Path(template).exists():
            html = Path(template).read_text()
            # Minimal substitution
            for k, v in self._flatten(data).items():
                html = html.replace("{{" + k + "}}", str(v)).replace("{{ " + k + " }}", str(v))
            return html.encode("utf-8")

        # Default email template
        title = data.get("title", "Knowledge Builder Framework")
        subtitle = data.get("subtitle", "")
        sections_html = []
        for name, body in data.get("sections", {}).items():
            sections_html.append(f"<h2>{name}</h2>")
            if isinstance(body, list):
                items = "".join(f"<li>{i}</li>" for i in body)
                sections_html.append(f"<ul>{items}</ul>")
            elif isinstance(body, dict):
                rows = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in body.items())
                sections_html.append(f"<table>{rows}</table>")
            else:
                sections_html.append(f"<p>{body}</p>")
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:Arial,sans-serif;max-width:720px;margin:24px auto;color:#333}}
h1{{color:#1F3864}}h2{{color:#2E75B6;margin-top:18px}}table{{border-collapse:collapse}}
td{{padding:6px 12px;border-bottom:1px solid #eee}}</style></head>
<body><h1>{title}</h1><p>{subtitle}</p>{''.join(sections_html)}</body></html>"""
        return html.encode("utf-8")

    @staticmethod
    def _flatten(d: dict, prefix="") -> dict:
        out = {}
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(EmailRenderer._flatten(v, key))
            else:
                out[key] = v
        return out
