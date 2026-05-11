"""SlackDeliverer — webhook POST. Stub for laptop dev."""
from __future__ import annotations
import logging
from pathlib import Path
from ._base import BaseDeliverer

log = logging.getLogger(__name__)


class SlackDeliverer(BaseDeliverer):
    name = "slack"

    def deliver(self, artifact: bytes, destination: dict) -> dict:
        webhook_url = destination.get("webhook_url")
        if not webhook_url:
            # Laptop fallback: write to ~/.kbf/slack-outbox/
            outbox = Path.home() / ".kbf" / "slack-outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            from datetime import datetime
            ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            path = outbox / f"{ts}-{destination.get('channel','default')}.json"
            path.write_bytes(artifact)
            return {
                "status": "delivered",
                "url": None,
                "error": None,
                "mode": "slack_stub",
                "archive": str(path),
                "note": "laptop-mode: real send requires webhook_url",
            }
        # Real send
        try:
            import requests
            r = requests.post(webhook_url, data=artifact, timeout=10,
                              headers={"Content-Type": "application/json"})
            return {"status": "delivered", "url": webhook_url, "error": None, "code": r.status_code}
        except Exception as e:
            log.exception("Slack send failed: %s", e)
            return {"status": "failed", "url": None, "error": str(e)}
