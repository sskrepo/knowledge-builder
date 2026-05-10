"""SyncReturnDeliverer — returns the artifact as base64 in the response."""
from __future__ import annotations
import base64
from ._base import BaseDeliverer


class SyncReturnDeliverer(BaseDeliverer):
    name = "sync_return"

    def deliver(self, artifact: bytes, destination: dict) -> dict:
        return {
            "status": "delivered",
            "mode": "sync_return",
            "artifact_base64": base64.b64encode(artifact).decode("ascii"),
            "size_bytes": len(artifact),
        }
