"""FilestoreArtifactStore — local filesystem implementation (laptop mode).

Layout under store_root:
  uploads/
    {synth_id}/
      {artifact_id}/
        {filename}      — raw file bytes
        _meta.json      — {artifact_id, filename, size_bytes, uploaded_at}
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ._base import ArtifactStore

log = logging.getLogger(__name__)


class FilestoreArtifactStore(ArtifactStore):
    """Artifact storage backed by the local filesystem.

    Used in KBF_ENV=laptop (or when KBF_ARTIFACT_STORE_BACKEND=filestore).
    No external services required — uploads land under ``store_root/uploads/``.
    """

    def __init__(self, store_root: str) -> None:
        self._uploads_root = Path(store_root) / "uploads"
        self._uploads_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # ArtifactStore interface
    # ------------------------------------------------------------------

    def upload(
        self,
        synth_id: str,
        artifact_id: str,
        filename: str,
        data: bytes,
    ) -> None:
        dest_dir = self._uploads_root / synth_id / artifact_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        file_path = dest_dir / filename
        file_path.write_bytes(data)

        meta = {
            "artifact_id": artifact_id,
            "filename": filename,
            "size_bytes": len(data),
            "uploaded_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        (dest_dir / "_meta.json").write_text(json.dumps(meta))

        log.info(
            "artifact upload: synth_id=%s artifact_id=%s filename=%s size=%d",
            synth_id, artifact_id, filename, len(data),
        )

    def resolve(self, artifact_id: str) -> Path | None:
        # artifact_id values are globally unique (art-{uuid8}); one glob is enough
        matches = list(self._uploads_root.glob(f"*/{artifact_id}"))
        if not matches:
            log.warning("artifact not found: artifact_id=%s", artifact_id)
            return None

        artifact_dir = matches[0]
        # Return the first non-meta file in the directory
        for p in artifact_dir.iterdir():
            if p.name != "_meta.json":
                return p

        log.warning("artifact dir empty: %s", artifact_dir)
        return None

    def cleanup(self, synth_id: str) -> None:
        synth_dir = self._uploads_root / synth_id
        if synth_dir.exists():
            shutil.rmtree(synth_dir)
            log.info("artifact cleanup: synth_id=%s removed %s", synth_id, synth_dir)
        else:
            log.debug("artifact cleanup: nothing to remove for synth_id=%s", synth_id)

    def list_artifacts(self, synth_id: str) -> list[dict]:
        synth_dir = self._uploads_root / synth_id
        if not synth_dir.exists():
            return []

        result = []
        for artifact_dir in synth_dir.iterdir():
            if not artifact_dir.is_dir():
                continue
            meta_path = artifact_dir / "_meta.json"
            if meta_path.exists():
                try:
                    result.append(json.loads(meta_path.read_text()))
                except Exception:
                    pass
        return result
