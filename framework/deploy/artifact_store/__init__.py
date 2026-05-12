"""Artifact store package — dual-mode upload storage (ADR-021).

Exports:
  ArtifactStore           — ABC defining the upload/resolve/cleanup contract
  build_artifact_store    — factory: filestore for laptop, OCI for staging/prod
"""
from ._base import ArtifactStore
from .factory import build_artifact_store

__all__ = ["ArtifactStore", "build_artifact_store"]
