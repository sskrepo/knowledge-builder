"""ObjectStorageDeliverer — OCI Object Storage. Stub for laptop dev."""
from __future__ import annotations
import logging
from pathlib import Path
from ._base import BaseDeliverer

log = logging.getLogger(__name__)


class ObjectStorageDeliverer(BaseDeliverer):
    name = "oci_object_storage"

    def deliver(self, artifact: bytes, destination: dict) -> dict:
        try:
            import oci
            # Real OCI Object Storage path would go here.
            # For laptop dev, fall back to filesystem if no OCI auth.
        except ImportError:
            pass
        # Laptop fallback: write to ~/.kbf/outputs/
        local_root = Path.home() / ".kbf" / "outputs"
        path_in_bucket = destination.get("path", "untitled")
        local_path = local_root / path_in_bucket
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(artifact)
        log.info("ObjectStorageDeliverer: laptop-mode write to %s", local_path)
        return {
            "status": "delivered",
            "url": f"file://{local_path}",
            "error": None,
            "mode": "oci_object_storage_stub",
            "path": str(local_path),
            "note": "laptop-mode: real OCI Object Storage upload requires OCI SDK + creds",
        }
