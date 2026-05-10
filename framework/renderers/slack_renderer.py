"""Slack renderer — produces block-kit JSON for Slack webhooks."""
from __future__ import annotations
import json
from pathlib import Path
from ._base import BaseRenderer


class SlackRenderer(BaseRenderer):
    name = "slack"
    output_extension = ".json"

    def render(self, data: dict, template: str | Path | None = None) -> bytes:
        blocks = []
        if "title" in data:
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": data["title"]}})
        if "subtitle" in data:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": data["subtitle"]}})
        for name, body in data.get("sections", {}).items():
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": name}})
            if isinstance(body, list):
                text = "\n".join(f"• {i}" for i in body)
            elif isinstance(body, dict):
                text = "\n".join(f"*{k}:* {v}" for k, v in body.items())
            else:
                text = str(body)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

        payload = {"blocks": blocks}
        return json.dumps(payload, indent=2).encode("utf-8")
