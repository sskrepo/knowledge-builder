"""EmailDeliverer — OCI Email Delivery / SMTP. Stub for laptop dev."""
from __future__ import annotations
import logging
from pathlib import Path
from ._base import BaseDeliverer

log = logging.getLogger(__name__)


class EmailDeliverer(BaseDeliverer):
    name = "email"

    def deliver(self, artifact: bytes, destination: dict) -> dict:
        # Laptop fallback: write to ~/.kbf/outbox/ as a "sent" simulation
        outbox = Path.home() / ".kbf" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        recipients = destination.get("to", ["nobody@nowhere"])
        subject = destination.get("subject", "Knowledge Builder Output")
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        path = outbox / f"{ts}-to-{','.join(recipients)[:40]}.html"
        path.write_bytes(artifact)
        log.info("EmailDeliverer: laptop-mode 'sent' to %s; archive=%s", recipients, path)
        return {
            "status": "delivered",
            "url": None,
            "error": None,
            "mode": "email_stub",
            "to": recipients,
            "subject": subject,
            "archive": str(path),
            "note": "laptop-mode: real send requires OCI Email Delivery or SMTP config",
        }
