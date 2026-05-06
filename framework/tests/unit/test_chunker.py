"""Tests for the markdown-aware chunker."""
from framework.parsers.chunker import Chunker

def test_chunker_short_body():
    ch = Chunker(target_tokens=128)
    chunks = ch.chunk("c1", "Short body.", {"x": 1})
    assert len(chunks) == 1
    assert chunks[0].text == "Short body."
    assert chunks[0].id == "c1#chunk_0"

def test_chunker_with_headings():
    md = """## A\nLine A\n\n### A.1\nDeeper\n\n## B\nLine B\n"""
    ch = Chunker(target_tokens=128)
    chunks = ch.chunk("c2", md, {})
    assert any("Line A" in c.text for c in chunks)
    assert any("Line B" in c.text for c in chunks)
    # heading_path should reflect nesting
    deep = [c for c in chunks if "Deeper" in c.text]
    assert deep and deep[0].heading_path[-1] == "A.1"

def test_chunker_long_section_splits():
    long_text = "Sentence. " * 600  # > 2000 chars
    ch = Chunker(target_tokens=128)  # target ~512 chars
    chunks = ch.chunk("c3", long_text, {})
    assert len(chunks) > 1
