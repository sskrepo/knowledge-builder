"""find_symbol MCP tool — searches the code wiki index for a Python symbol.

Requires the code wiki to be built first (kb-cli code-wiki-build).
The index is stored in {store_root}/code_wiki_index.json.

Returns matching symbols with file path, line number (when known), kind, and
citation URL — no citation = bug per spec §10.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_VALID_KINDS = {"class", "function", "module"}


def _index_path(store_root: Path) -> Path:
    return store_root / "code_wiki_index.json"


def _load_index(store_root: Path) -> list[dict]:
    path = _index_path(store_root)
    if not path.exists():
        return []
    return json.loads(path.read_text())


class FindSymbolRetriever:
    """Search the code wiki structural index for a named symbol.

    Each hit is a dict:
        {
          "symbol":    str,     # matched name
          "file":      str,     # relative file path
          "line":      int,     # line number (0 if unknown at index time)
          "kind":      str,     # "class" | "function" | "module"
          "signature": str,     # full function signature or class header
          "citation_url": str,  # code://<file_path>#<symbol>
        }
    """

    name = "find_symbol"

    def __init__(self, store_root: Path | str | None = None):
        if store_root is None:
            store_root = os.environ.get(
                "KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store")
            )
        self.store_root = Path(store_root).expanduser()

    def __call__(
        self,
        symbol_name: str,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Find a symbol (class, function, or module) in the code wiki index.

        Args:
            symbol_name: name to search for (case-insensitive substring match)
            scope: optional module path prefix to restrict search
            kind: "class" | "function" | "module" (None = all)
            limit: max results

        Returns:
            list of symbol hit dicts
        """
        if kind is not None and kind not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")

        index = _load_index(self.store_root)
        if not index:
            log.warning(
                "find_symbol: code wiki index not found at %s — run `kb-cli code-wiki-build` first",
                _index_path(self.store_root),
            )
            return []

        query_lower = symbol_name.lower()
        results: list[dict] = []

        for entry in index:
            if scope and not entry["module_path"].startswith(scope):
                continue

            module_path = entry["module_path"]
            file_path = entry["file_path"]

            if kind in (None, "module") and query_lower in module_path.lower():
                results.append({
                    "symbol": module_path,
                    "file": file_path,
                    "line": 1,
                    "kind": "module",
                    "signature": module_path,
                    "citation_url": f"code://{file_path}",
                })

            if kind in (None, "class"):
                for cls_name in entry.get("class_names", []):
                    if query_lower in cls_name.lower():
                        results.append({
                            "symbol": cls_name,
                            "file": file_path,
                            "line": 0,
                            "kind": "class",
                            "signature": f"class {cls_name}",
                            "citation_url": f"code://{file_path}#{cls_name}",
                        })

            if kind in (None, "function"):
                for fn_name in entry.get("function_names", []):
                    if query_lower in fn_name.lower():
                        results.append({
                            "symbol": fn_name,
                            "file": file_path,
                            "line": 0,
                            "kind": "function",
                            "signature": f"def {fn_name}(...)",
                            "citation_url": f"code://{file_path}#{fn_name}",
                        })

            if len(results) >= limit:
                break

        log.debug(
            "find_symbol: query=%r scope=%r kind=%r → %d hits", symbol_name, scope, kind, len(results)
        )
        return results[:limit]
