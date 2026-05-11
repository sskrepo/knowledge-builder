"""Som-style code wiki builder — structural index regenerated on commit.

Scans a Python codebase, extracts:
  - Module-level docstrings
  - Class names + method signatures
  - Top-level function signatures
  - Import list

Each module becomes one ContentItem (kind="catalog_entry") with a single
chunk containing the full structural summary. The index is stored in the
active content store (filestore on laptop, ADB in production).

This is NOT a semantic search over code. It's a deterministic structural map
— the right retrieval pattern for code navigation per ADR spec §11.

Usage (CLI):
  kb-cli code-wiki-build [--repo-path .] [--store-root ~/.kbf/store]

Usage (library):
  builder = CodeWikiBuilder(repo_path=Path("."))
  items = builder.build()           # list[ContentItem]
  builder.write_to_store(items)     # persists to filestore
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..core.content import ContentItem, ContentMetadata, Chunk
from ..core.ids import content_item_id, chunk_id, source_sha

log = logging.getLogger(__name__)

_PARSER_VERSION = "code_wiki_builder:1.0"
_SCHEMA_VERSION = 1


def _module_docstring(tree: ast.Module) -> str:
    return ast.get_docstring(tree) or ""


def _extract_classes(tree: ast.Module) -> list[dict]:
    classes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append({
                    "name": item.name,
                    "signature": _func_signature(item),
                    "docstring": ast.get_docstring(item) or "",
                    "line": item.lineno,
                    "is_async": isinstance(item, ast.AsyncFunctionDef),
                })
        classes.append({
            "name": node.name,
            "line": node.lineno,
            "docstring": ast.get_docstring(node) or "",
            "bases": [ast.unparse(b) for b in node.bases],
            "methods": methods,
        })
    return classes


def _extract_functions(tree: ast.Module) -> list[dict]:
    funcs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append({
                "name": node.name,
                "signature": _func_signature(node),
                "docstring": ast.get_docstring(node) or "",
                "line": node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })
    return funcs


def _extract_imports(tree: ast.Module) -> list[str]:
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {module} import {names}")
    return imports


def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        return ast.unparse(node).split("\n")[0].lstrip("async def ").lstrip("def ")
    except Exception:
        return f"{node.name}(...)"


def _build_summary(
    module_path: str,
    docstring: str,
    classes: list[dict],
    functions: list[dict],
    imports: list[str],
) -> str:
    """Build the human-readable structural page text used for chunk body."""
    lines = [f"# Module: {module_path}"]
    if docstring:
        lines.append(f"\n{docstring}")
    if imports:
        lines.append("\n## Imports")
        for imp in imports[:20]:
            lines.append(f"  - {imp}")
    if classes:
        lines.append("\n## Classes")
        for cls in classes:
            bases = f"({', '.join(cls['bases'])})" if cls["bases"] else ""
            lines.append(f"  ### {cls['name']}{bases} (line {cls['line']})")
            if cls["docstring"]:
                lines.append(f"  {cls['docstring'][:200]}")
            for m in cls["methods"]:
                prefix = "async " if m["is_async"] else ""
                lines.append(f"    - {prefix}{m['signature']} (line {m['line']})")
    if functions:
        lines.append("\n## Functions")
        for fn in functions:
            prefix = "async " if fn["is_async"] else ""
            lines.append(f"  - {prefix}{fn['signature']} (line {fn['line']})")
            if fn["docstring"]:
                lines.append(f"    {fn['docstring'][:120]}")
    return "\n".join(lines)


def _parse_python_file(file_path: Path, repo_root: Path) -> dict | None:
    """Parse one Python file into a structured record. Returns None on syntax error."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        log.warning("syntax error in %s: %s", file_path, exc)
        return None

    rel_path = str(file_path.relative_to(repo_root))
    module_path = rel_path.replace(os.sep, ".").removesuffix(".py")

    docstring = _module_docstring(tree)
    classes = _extract_classes(tree)
    functions = _extract_functions(tree)
    imports = _extract_imports(tree)
    summary = _build_summary(module_path, docstring, classes, functions, imports)

    return {
        "module_path": module_path,
        "file_path": rel_path,
        "docstring": docstring,
        "classes": classes,
        "functions": functions,
        "imports": imports,
        "summary": summary,
        "source_sha": source_sha(source),
    }


class CodeWikiBuilder:
    """Builds a structural code wiki from a Python repository.

    Each .py file → one ContentItem of kind 'catalog_entry'.
    In filestore mode the items are persisted to {store_root}/content_items/.
    The code wiki index (list of module records) is also written to
    {store_root}/code_wiki_index.json for fast retrieval by find_symbol and
    read_code_page without scanning every content item.
    """

    def __init__(
        self,
        repo_path: Path | str = ".",
        store_root: Path | str | None = None,
        exclude_dirs: set[str] | None = None,
    ):
        self.repo_path = Path(repo_path).resolve()
        if store_root is None:
            store_root = os.environ.get(
                "KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store")
            )
        self.store_root = Path(store_root).expanduser()
        self.exclude_dirs = exclude_dirs or {
            "__pycache__", ".git", ".venv", "venv", "node_modules",
            ".mypy_cache", ".pytest_cache", ".ruff_cache",
        }

    def _iter_python_files(self) -> Iterator[Path]:
        for p in sorted(self.repo_path.rglob("*.py")):
            if any(part in self.exclude_dirs for part in p.parts):
                continue
            yield p

    def build(self) -> list[ContentItem]:
        """Parse all Python files in repo_path; return list of ContentItems."""
        now = datetime.now(tz=timezone.utc)
        items: list[ContentItem] = []

        for py_file in self._iter_python_files():
            record = _parse_python_file(py_file, self.repo_path)
            if record is None:
                continue

            ci_id = content_item_id("code_wiki", record["module_path"], _SCHEMA_VERSION)
            body_text = record["summary"]

            chunk = Chunk(
                id=chunk_id(ci_id, 0),
                content_id=ci_id,
                ord=0,
                text=body_text,
                heading_path=["module", record["module_path"]],
                metadata={
                    "module_path": record["module_path"],
                    "file_path": record["file_path"],
                    "class_names": [c["name"] for c in record["classes"]],
                    "function_names": [f["name"] for f in record["functions"]]
                        + [m["name"] for c in record["classes"] for m in c["methods"]],
                    "citation_url": f"code://{record['file_path']}",
                },
            )

            meta = ContentMetadata(
                persona_visibility=["dev", "architect", "dev_manager"],
                owner="code_wiki_builder",
                classification="internal",
                source_sha=record["source_sha"],
                parser_version=_PARSER_VERSION,
                schema_version=_SCHEMA_VERSION,
                created_at=now,
                updated_at=now,
                extracted_by="code_wiki_builder",
                extraction_schema="code_wiki_builder:structural_index",
            )

            ci = ContentItem(
                id=ci_id,
                source="code_wiki",
                source_id=record["module_path"],
                path=record["file_path"],
                title=record["module_path"],
                body=body_text,
                persona="dev",
                primary_axis_kind="service_id",
                primary_axis_value=record["module_path"].split(".")[0],
                kind="catalog_entry",
                metadata=meta,
                chunks=[chunk],
            )
            ci.validate()
            items.append(ci)

        log.info("code_wiki_builder: indexed %d Python modules from %s", len(items), self.repo_path)
        return items

    def write_to_store(self, items: list[ContentItem]) -> Path:
        """Persist ContentItems to filestore and write the fast-lookup index.

        Returns path to the written code_wiki_index.json.
        """
        from ..stores.filestore_content_store import FilestoreContentStore
        store = FilestoreContentStore(root=self.store_root)
        store.upsert(items)

        index_records = []
        for item in items:
            chunk_meta = item.chunks[0].metadata if item.chunks else {}
            index_records.append({
                "module_path": item.source_id,
                "file_path": item.path,
                "content_id": item.id,
                "citation_url": f"code://{item.path}",
                "class_names": chunk_meta.get("class_names", []),
                "function_names": chunk_meta.get("function_names", []),
                "docstring": item.body.split("\n")[2] if len(item.body.split("\n")) > 2 else "",
            })

        index_path = self.store_root / "code_wiki_index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index_records, indent=2, default=str))
        log.info("code_wiki_builder: wrote index to %s (%d entries)", index_path, len(index_records))
        return index_path

    def run(self) -> Path:
        """Full build-and-persist cycle. Returns index path."""
        items = self.build()
        return self.write_to_store(items)
