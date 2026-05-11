"""Unit tests for CodeWikiBuilder and the code-wiki MCP retrievers.

All tests run against the framework/ directory itself as demo content.
No external services. No ADB.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest


FRAMEWORK_DIR = Path(__file__).resolve().parents[3]  # repo root / framework/..


@pytest.fixture(autouse=True)
def filestore_mode(monkeypatch):
    monkeypatch.setenv("KBF_STORE_BACKEND", "filestore")


# --------------------------------------------------------------------------
# CodeWikiBuilder — parsing helpers (pure unit tests, no disk I/O)
# --------------------------------------------------------------------------

def _parse_source(source: str) -> dict:
    """Helper: parse a source string and return structured record."""
    import ast as _ast
    import tempfile, os
    from framework.adapters.code_wiki_builder import _parse_python_file
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir="/tmp") as f:
        f.write(source)
        tmp_path = Path(f.name)
    try:
        return _parse_python_file(tmp_path, tmp_path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def test_parse_module_docstring():
    record = _parse_source('"""This module does things."""\n\nx = 1\n')
    assert record is not None
    assert record["docstring"] == "This module does things."


def test_parse_class_extraction():
    source = textwrap.dedent("""\
        class Foo:
            def bar(self) -> None:
                pass
            def baz(self, x: int) -> str:
                return str(x)
    """)
    record = _parse_source(source)
    assert record is not None
    assert len(record["classes"]) == 1
    cls = record["classes"][0]
    assert cls["name"] == "Foo"
    assert len(cls["methods"]) == 2
    method_names = {m["name"] for m in cls["methods"]}
    assert method_names == {"bar", "baz"}


def test_parse_function_extraction():
    source = textwrap.dedent("""\
        def my_func(x: int, y: str = "hello") -> bool:
            return True

        async def my_async_func() -> None:
            pass
    """)
    record = _parse_source(source)
    assert record is not None
    assert len(record["functions"]) == 2
    fn_names = {f["name"] for f in record["functions"]}
    assert fn_names == {"my_func", "my_async_func"}
    async_fn = next(f for f in record["functions"] if f["name"] == "my_async_func")
    assert async_fn["is_async"] is True


def test_parse_imports():
    source = textwrap.dedent("""\
        import os
        import sys
        from pathlib import Path
        from typing import Any, Optional
    """)
    record = _parse_source(source)
    assert record is not None
    assert "os" in record["imports"]
    assert "sys" in record["imports"]
    assert any("pathlib" in imp for imp in record["imports"])


def test_parse_syntax_error_returns_none():
    from framework.adapters.code_wiki_builder import _parse_python_file
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir="/tmp") as f:
        f.write("def broken(:\n    pass\n")
        tmp_path = Path(f.name)
    try:
        result = _parse_python_file(tmp_path, tmp_path.parent)
        assert result is None
    finally:
        tmp_path.unlink(missing_ok=True)


def test_summary_contains_module_header():
    record = _parse_source('"""Docstring."""\nclass X:\n    pass\n')
    assert "# Module:" in record["summary"]
    assert "## Classes" in record["summary"]
    assert "X" in record["summary"]


# --------------------------------------------------------------------------
# CodeWikiBuilder — full build (uses tmp store)
# --------------------------------------------------------------------------

@pytest.fixture
def tmp_store(tmp_path):
    return tmp_path / "store"


@pytest.fixture
def builder(tmp_store):
    from framework.adapters.code_wiki_builder import CodeWikiBuilder
    return CodeWikiBuilder(
        repo_path=FRAMEWORK_DIR / "framework" / "adapters",
        store_root=tmp_store,
    )


def test_build_returns_content_items(builder):
    items = builder.build()
    assert len(items) > 0
    for item in items:
        assert item.source == "code_wiki"
        assert item.kind == "catalog_entry"
        assert item.metadata is not None
        assert item.body  # non-empty summary


def test_build_items_have_chunks(builder):
    items = builder.build()
    for item in items:
        assert len(item.chunks) == 1
        chunk = item.chunks[0]
        assert chunk.content_id == item.id
        assert chunk.text == item.body


def test_build_items_have_citations(builder):
    items = builder.build()
    for item in items:
        chunk = item.chunks[0]
        assert "citation_url" in chunk.metadata
        assert chunk.metadata["citation_url"].startswith("code://")


def test_write_to_store_creates_index(builder, tmp_store):
    items = builder.build()
    index_path = builder.write_to_store(items)
    assert Path(index_path).exists()
    index = json.loads(Path(index_path).read_text())
    assert len(index) == len(items)
    for entry in index:
        assert "module_path" in entry
        assert "file_path" in entry
        assert "content_id" in entry
        assert "citation_url" in entry


def test_write_to_store_persists_content_items(builder, tmp_store):
    items = builder.build()
    builder.write_to_store(items)
    content_dir = tmp_store / "content_items"
    assert content_dir.exists()
    assert len(list(content_dir.glob("*.json"))) == len(items)


def test_build_excludes_pycache(tmp_store):
    """__pycache__ directories are excluded."""
    from framework.adapters.code_wiki_builder import CodeWikiBuilder
    b = CodeWikiBuilder(
        repo_path=FRAMEWORK_DIR / "framework",
        store_root=tmp_store,
    )
    items = b.build()
    for item in items:
        assert "__pycache__" not in item.path


def test_run_returns_index_path(builder, tmp_store):
    index_path = builder.run()
    assert Path(index_path).exists()
    assert index_path.name == "code_wiki_index.json"


def test_idempotent_rebuild(builder, tmp_store):
    """Running build twice produces same content_ids (idempotent hashing)."""
    items1 = builder.build()
    items2 = builder.build()
    ids1 = {i.id for i in items1}
    ids2 = {i.id for i in items2}
    assert ids1 == ids2


# --------------------------------------------------------------------------
# find_symbol retriever
# --------------------------------------------------------------------------

@pytest.fixture
def store_with_index(tmp_store):
    from framework.adapters.code_wiki_builder import CodeWikiBuilder
    b = CodeWikiBuilder(
        repo_path=FRAMEWORK_DIR / "framework" / "adapters",
        store_root=tmp_store,
    )
    b.run()
    return tmp_store


def test_find_symbol_class(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    hits = retriever("UdapAdapter")
    assert len(hits) > 0
    hit = hits[0]
    assert hit["kind"] == "class"
    assert hit["symbol"] == "UdapAdapter"
    assert "citation_url" in hit
    assert hit["citation_url"].startswith("code://")


def test_find_symbol_function(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    hits = retriever("healthcheck")
    assert len(hits) > 0
    for hit in hits:
        assert hit["kind"] == "function"
        assert "citation_url" in hit


def test_find_symbol_module(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    hits = retriever("udap_adapter", kind="module")
    assert len(hits) > 0
    assert hits[0]["kind"] == "module"


def test_find_symbol_kind_filter(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    hits = retriever("adapter", kind="class")
    for hit in hits:
        assert hit["kind"] == "class"


def test_find_symbol_invalid_kind_raises(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    with pytest.raises(ValueError, match="kind must be one of"):
        retriever("foo", kind="database_table")


def test_find_symbol_no_match_returns_empty(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    hits = retriever("xyzzy_nonexistent_symbol_abc123")
    assert hits == []


def test_find_symbol_missing_index_returns_empty(tmp_store):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=tmp_store)
    hits = retriever("anything")
    assert hits == []


def test_find_symbol_limit(store_with_index):
    from framework.retrievers.find_symbol import FindSymbolRetriever
    retriever = FindSymbolRetriever(store_root=store_with_index)
    hits = retriever("a", limit=2)
    assert len(hits) <= 2


# --------------------------------------------------------------------------
# read_code_page retriever
# --------------------------------------------------------------------------

def test_read_code_page_by_module_path(store_with_index):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=store_with_index)
    page = retriever("framework.adapters.udap_adapter")
    assert page["module"] == "framework.adapters.udap_adapter"
    assert page["file"].endswith("udap_adapter.py")
    assert "UdapAdapter" in page["classes"]
    assert "citation_url" in page
    assert page["citation_url"].startswith("code://")


def test_read_code_page_by_file_path(store_with_index):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=store_with_index)
    page = retriever("framework/adapters/udap_adapter.py")
    assert page["module"] == "framework.adapters.udap_adapter"


def test_read_code_page_not_found(store_with_index):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=store_with_index)
    page = retriever("this.module.does.not.exist")
    assert "not found" in page["summary"].lower() or page["file"] == ""


def test_read_code_page_missing_index(tmp_store):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=tmp_store)
    page = retriever("any.module")
    assert page["classes"] == []
    assert page["functions"] == []
    assert "citation_url" in page


def test_read_code_page_citation_always_present(store_with_index):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=store_with_index)
    page = retriever("framework.adapters.udap_adapter")
    assert page["citation_url"]
    assert page["citation_url"].startswith("code://")


def test_read_code_page_summary_not_empty(store_with_index):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=store_with_index)
    page = retriever("framework.adapters.udap_adapter")
    assert len(page["summary"]) > 50


def test_read_code_page_code_wiki_builder(store_with_index):
    from framework.retrievers.read_code_page import ReadCodePageRetriever
    retriever = ReadCodePageRetriever(store_root=store_with_index)
    page = retriever("framework.adapters.code_wiki_builder")
    assert "CodeWikiBuilder" in page["classes"]
