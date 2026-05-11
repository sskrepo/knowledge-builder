"""FilesystemDeliverer — writes artifact to a local path. Laptop-mode default."""
from __future__ import annotations
from pathlib import Path
from ._base import BaseDeliverer


class FilesystemDeliverer(BaseDeliverer):
    name = "filesystem"

    def deliver(self, artifact: bytes, destination: dict) -> dict:
        path = Path(destination["path"]).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(artifact)
        return {"status": "delivered", "url": f"file://{path}", "error": None, "path": str(path)}
