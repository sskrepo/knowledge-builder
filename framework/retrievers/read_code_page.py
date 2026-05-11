"""read_code_page MCP tool — returns the structural wiki page for a Python module.

Requires the code wiki to have been built (kb-cli code-wiki-build).
Looks up the module by path or module dotted path in {store_root}/code_wiki_index.json,
then loads the full ContentItem from {store_root}/content_items/.

Returns:
    {
      "module": str,           # dotted module path
      "file": str,             # relative file path
      "summary": str,          # full structural summary text
      "docstring": str,        # module-level docstring
      "classes": [...],        # class names
      "functions": [...],      # function names
      "imports": [...],        # import statements (from index)
      "citation_url": str,     # code://<file_path>
    }
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _index_path(store_root: Path) -> Path:
    return store_root / "code_wiki_index.json"


def _load_index(store_root: Path) -> list[dict]:
    p = _index_path(store_root)
    return json.loads(p.read_text()) if p.exists() else []


def _load_content_item(store_root: Path, content_id: str) -> dict | None:
    ci_path = store_root / "content_items" / f"{content_id}.json"
    if not ci_path.exists():
        return None
    return json.loads(ci_path.read_text())


class ReadCodePageRetriever:
    """Return the full structural wiki page for a module.

    Accepts either:
      - A dotted module path: "framework.adapters.udap_adapter"
      - A relative file path: "framework/adapters/udap_adapter.py"
    """

    name = "read_code_page"

    def __init__(self, store_root: Path | str | None = None):
        if store_root is None:
            store_root = os.environ.get(
                "KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store")
            )
        self.store_root = Path(store_root).expanduser()

    def __call__(self, module_path: str) -> dict:
        """Return structural wiki page for module_path.

        Args:
            module_path: dotted module name or relative file path

        Returns:
            Structured page dict (see module docstring).
        """
        index = _load_index(self.store_root)
        if not index:
            log.warning(
                "read_code_page: code wiki index not found at %s — run `kb-cli code-wiki-build` first",
                _index_path(self.store_root),
            )
            return _not_found(module_path)

        entry = _find_entry(index, module_path)
        if entry is None:
            log.debug("read_code_page: module %r not in index", module_path)
            return _not_found(module_path)

        ci = _load_content_item(self.store_root, entry["content_id"])
        summary = ci["body"] if ci else entry.get("docstring", "")

        return {
            "module": entry["module_path"],
            "file": entry["file_path"],
            "summary": summary,
            "docstring": entry.get("docstring", ""),
            "classes": entry.get("class_names", []),
            "functions": entry.get("function_names", []),
            "citation_url": entry.get("citation_url", f"code://{entry['file_path']}"),
        }


def _find_entry(index: list[dict], query: str) -> dict | None:
    normalized_query = query.replace("/", ".").removesuffix(".py")
    for entry in index:
        if entry["module_path"] == normalized_query:
            return entry
        if entry["file_path"] == query:
            return entry
    query_lower = normalized_query.lower()
    for entry in index:
        if entry["module_path"].lower() == query_lower:
            return entry
    return None


def _not_found(module_path: str) -> dict:
    return {
        "module": module_path,
        "file": "",
        "summary": f"Module '{module_path}' not found in code wiki. Run `kb-cli code-wiki-build` to index.",
        "docstring": "",
        "classes": [],
        "functions": [],
        "citation_url": f"code://{module_path}",
    }
