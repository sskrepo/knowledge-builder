"""ArtifactStore ABC — per ADR-021 §ArtifactStore ABC."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ArtifactStore(ABC):
    """Abstract base class for artifact upload storage.

    Implementations:
      FilestoreArtifactStore — writes under {store_root}/uploads/ (laptop mode)
      OciArtifactStore       — writes to OCI Object Storage (staging / production)

    All methods are synchronous (blocking I/O).  The MCP handler runs them in a
    thread pool via ``asyncio.to_thread`` to avoid blocking the event loop.
    """

    @abstractmethod
    def upload(
        self,
        synth_id: str,
        artifact_id: str,
        filename: str,
        data: bytes,
    ) -> None:
        """Persist raw file bytes.

        Idempotent: re-uploading the same artifact_id overwrites.

        Args:
            synth_id:    Session ID — used to scope uploads for cleanup.
            artifact_id: Opaque ``art-{uuid8}`` key.
            filename:    Original filename including extension.
            data:        Decoded file bytes (not base64).
        """

    @abstractmethod
    def resolve(self, artifact_id: str) -> Path | None:
        """Return a local Path to the uploaded file, or None if not found.

        For FilestoreArtifactStore the stored path is returned directly.
        For OciArtifactStore the object is downloaded to a local temp area
        before the path is returned.

        Args:
            artifact_id: The ``art-{uuid8}`` key returned by ``upload()``.

        Returns:
            Local filesystem Path to the file, or None if the artifact is
            not found.
        """

    @abstractmethod
    def cleanup(self, synth_id: str) -> None:
        """Delete all uploads scoped to ``synth_id``.

        Called when an authorSkill session reaches DONE or is abandoned.

        Args:
            synth_id: Session ID whose uploads should be deleted.
        """

    @abstractmethod
    def list_artifacts(self, synth_id: str) -> list[dict]:
        """Return metadata for all uploads under ``synth_id``.

        Each entry: {artifact_id, filename, size_bytes, uploaded_at}

        Args:
            synth_id: Session ID to list uploads for.

        Returns:
            List of metadata dicts (may be empty).
        """
