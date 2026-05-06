"""Token-aware text chunker.

Per ADR-008: chunks inherit ContentItem multi-axis metadata.
Per AIRA comparison: simple sentence-boundary heuristic is acceptable; we add
heading awareness for markdown bodies.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from ..core.content import Chunk
from ..core.ids import chunk_id

log = logging.getLogger(__name__)

# Approximate tokens-per-char for English text (gpt-4o tokenizer averages ~4)
CHARS_PER_TOKEN = 4


class Chunker:
    """Splits a body into chunks of approximately target_tokens.

    Strategy:
    1. Split body on markdown headings (## , ### ); each section is candidate chunk.
    2. Sections shorter than target merge with neighbors.
    3. Sections longer than target sentence-split.
    4. If a sentence > target, hard-split at target boundary.

    Each chunk inherits parent's multi-axis metadata + section heading_path.
    """

    HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

    def __init__(self, target_tokens: int = 512, overlap_tokens: int = 64):
        self.target_chars = target_tokens * CHARS_PER_TOKEN
        self.overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    def chunk(self, content_id: str, body: str, parent_metadata: dict) -> list[Chunk]:
        if not body:
            return []
        sections = self._split_on_headings(body)
        chunks_text: list[tuple[str, list[str]]] = []  # (text, heading_path)
        for heading_path, section_text in sections:
            for piece in self._split_section(section_text):
                chunks_text.append((piece, heading_path))

        result: list[Chunk] = []
        for ord_, (text, heading_path) in enumerate(chunks_text):
            cid = chunk_id(content_id, ord_)
            result.append(Chunk(
                id=cid,
                content_id=content_id,
                ord=ord_,
                text=text,
                heading_path=heading_path,
                metadata=dict(parent_metadata),
            ))
        return result

    def _split_on_headings(self, body: str) -> list[tuple[list[str], str]]:
        """Returns list of (heading_path, section_text)."""
        matches = list(self.HEADING_RE.finditer(body))
        if not matches:
            return [([], body)]

        sections: list[tuple[list[str], str]] = []
        stack: list[tuple[int, str]] = []  # (level, title)

        # Preamble before first heading
        if matches[0].start() > 0:
            preamble = body[:matches[0].start()].strip()
            if preamble:
                sections.append(([], preamble))

        for i, m in enumerate(matches):
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_path = [t for _, t in stack]

            section_start = m.end()
            section_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            section_text = body[section_start:section_end].strip()
            if section_text:
                sections.append((list(heading_path), section_text))

        return sections

    def _split_section(self, text: str) -> list[str]:
        if len(text) <= self.target_chars:
            return [text]
        sentences = self.SENTENCE_RE.split(text)
        out: list[str] = []
        buf = ""
        for s in sentences:
            if len(buf) + len(s) + 1 <= self.target_chars:
                buf = (buf + " " + s).strip()
            else:
                if buf:
                    out.append(buf)
                if len(s) > self.target_chars:
                    while s:
                        out.append(s[: self.target_chars])
                        s = s[self.target_chars :]
                    buf = ""
                else:
                    buf = s
        if buf:
            out.append(buf)
        return out
