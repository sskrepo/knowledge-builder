"""Text chunker — splits long bodies for embedding.

Per ADR-008: chunks inherit ContentItem multi-axis metadata.
Status: STUB. Phase 1 STORY-005.
"""
from __future__ import annotations

import hashlib
from ..core.content import Chunk

class Chunker:
    def __init__(self, target_tokens: int = 512, overlap_tokens: int = 64):
        self.target = target_tokens
        self.overlap = overlap_tokens

    def chunk(self, content_id: str, body: str, parent_metadata: dict) -> list[Chunk]:
        # TODO Phase 1:
        # 1. Token-aware split (use tiktoken for OpenAI-compatible counts)
        # 2. Respect heading boundaries from markdown body
        # 3. Maintain overlap_tokens at chunk boundaries
        # 4. Build heading_path for each chunk
        # 5. id = f"{content_id}#chunk_{ord}"
        raise NotImplementedError("STORY-005 wk2")
