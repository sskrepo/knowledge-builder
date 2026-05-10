"""analyze_artifact — parse PPT/DOCX/email and infer required fields.

Per ADR-015 + ADR-007 amend 4. Phase 3 polish for full PPT/DOCX structural analysis;
v1 is a heuristic that handles markdown and structured text, with stubs for PPT/DOCX.
"""
from __future__ import annotations
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def analyze_artifact(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        log.warning("artifact path does not exist: %s; falling back to heuristic", path)
        return ["title", "summary", "details"]

    if p.suffix == ".pptx":
        return _analyze_pptx(p)
    if p.suffix == ".docx":
        return _analyze_docx(p)
    if p.suffix in (".md", ".txt"):
        return _analyze_markdown(p)
    return ["title", "summary", "details"]


def _analyze_pptx(p: Path) -> list[str]:
    try:
        from pptx import Presentation
    except ImportError:
        log.warning("python-pptx not installed; using fallback fields")
        return ["title", "section_a", "section_b", "section_c"]
    prs = Presentation(str(p))
    fields = ["title"]
    for i, slide in enumerate(prs.slides):
        if slide.shapes.title and slide.shapes.title.text:
            t = slide.shapes.title.text.strip().lower().replace(" ", "_").replace("-", "_")
            t = "".join(c for c in t if c.isalnum() or c == "_")
            if t and t not in fields:
                fields.append(t)
    return fields


def _analyze_docx(p: Path) -> list[str]:
    try:
        from docx import Document
    except ImportError:
        return ["title", "introduction", "details", "conclusion"]
    doc = Document(str(p))
    fields = ["title"]
    for para in doc.paragraphs:
        if para.style.name.startswith("Heading"):
            t = para.text.strip().lower().replace(" ", "_").replace("-", "_")
            t = "".join(c for c in t if c.isalnum() or c == "_")
            if t and t not in fields:
                fields.append(t)
    return fields


def _analyze_markdown(p: Path) -> list[str]:
    import re
    fields = []
    for line in p.read_text().splitlines():
        m = re.match(r"^#{1,6}\s+(.+)$", line)
        if m:
            t = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            t = "".join(c for c in t if c.isalnum() or c == "_")
            if t and t not in fields:
                fields.append(t)
    return fields or ["title", "summary"]
