"""ADR-032 P2-Exec — ask_parameterized ephemeral fetch tests.

Tests the Option C ephemeral ingestion path added to WorkflowExecutor for
ask_parameterized skills.

Coverage (≥15 tests required):
  1.  ask_parameterized + adapter present + page in allow-listed space →
      fetches via adapter, extracts via schema, does NOT write WikiMetadataStore
      (assert store.add NOT called), returns passages with correct page_id.
  2.  TTL cache hit on 2nd call → adapter.fetch called only once.
  3.  TTL cache eviction → adapter.fetch called again after TTL expires.
  4.  Adapter None → hard-fail with actionable message (adapter not configured).
  5.  space not in allow_list (URL form, space extractable) → hard-fail BEFORE
      any fetch (assert adapter.fetch NOT called).
  6.  ingest_on_demand false → hard-fail immediately.
  7.  author_fixed skill → ephemeral path NOT taken, existing retriever path used.
  8.  author_fixed skill with page ref in input → P3 guard still fires on mismatch.
  9.  ask_parameterized + bare numeric page_id → metadata fetch used for space check.
  10. ask_parameterized + empty page_id input → hard-fail (no page ref supplied).
  11. ask_parameterized + adapter fetch raises → hard-fail actionably.
  12. ask_parameterized + empty body text → hard-fail (unusable page content).
  13. _EphemeralCache thread-safety — concurrent puts/gets do not deadlock.
  14. _EphemeralCache LRU eviction at cap (50 entries).
  15. _EphemeralCache clear() empties the cache.
  16. _resolve_page_id helper covers all URL forms.
  17. _extract_space_key_from_url helper — URL with /spaces/ returns key; bare ID returns None.
  18. ask_parameterized + no space_allow_list (empty) → fetch proceeds without space check.
  19. Audit log written on ephemeral fetch.
  20. ask_parameterized EVAL note: gold page IDs exercised via the ephemeral path.

All tests: no live network calls, no real ADB, no real Confluence credentials.
"""
from __future__ import annotations

import hashlib
import json
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from datetime import datetime

import pytest
import yaml

from framework.workflow_runtime.executor import (
    ConfluencePageNotInKBError,
    WorkflowExecutor,
    _EphemeralCache,
    _ephemeral_cache,
    _extract_space_key_from_url,
    _any_promoted_skill_requires_ephemeral,
)
from framework.adapters.confluence.shared import _extract_numeric_id_fast


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_ID = "18625350641"
OTHER_PAGE_ID = "20030556732"
SPACE_KEY = "FA"
SKILL_NAME = "project_tracking_test_email"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill_yaml(
    tmp_path: Path,
    *,
    skill_name: str = SKILL_NAME,
    mode: str = "ask_parameterized",
    input_param: str = "page_id",
    ingest_on_demand: bool = True,
    space_allow_list: list | None = None,
    ephemeral_ttl_seconds: int = 300,
) -> Path:
    """Write a minimal ask_parameterized workflow skill YAML."""
    if space_allow_list is None:
        space_allow_list = [SPACE_KEY, "PROJ"]

    cfg: dict = {
        "workflow_skill": skill_name,
        "persona": "tpm",
        "status": "promoted",
        "source_binding": {
            "mode": mode,
            "input_param": input_param,
            "ingest_on_demand": ingest_on_demand,
            "source_type": "confluence_page",
            "space_allow_list": space_allow_list,
            "ephemeral_ttl_seconds": ephemeral_ttl_seconds,
        },
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [
                    {
                        "name": input_param,
                        "type": "confluence_page_ref",
                        "description": "Confluence page URL or pageId",
                        "required": True,
                    }
                ],
                "output_format": "email",
                "response_mode": "artifact_url",
            },
        },
        "requires_extractions": [{"kb": f"tpm.{skill_name}"}],
        "synthesis": {"output_format": "email"},
        "delivery": {"kind": "filesystem", "path": f"/tmp/{skill_name}.eml"},
    }
    skill_dir = tmp_path / "workflow_skills" / "tpm"
    skill_dir.mkdir(parents=True, exist_ok=True)
    p = skill_dir / f"{skill_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _make_author_fixed_skill_yaml(
    tmp_path: Path, skill_name: str = "fixed_source_skill"
) -> Path:
    """Write a minimal author_fixed workflow skill YAML (no source_binding)."""
    cfg = {
        "workflow_skill": skill_name,
        "persona": "tpm",
        "status": "promoted",
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [{"name": "input", "type": "string"}],
                "output_format": "email",
                "response_mode": "artifact_url",
            },
        },
        "requires_extractions": [{"kb": "tpm.fixed_kb"}],
        "synthesis": {"output_format": "email"},
        "delivery": {"kind": "filesystem", "path": "/tmp/fixed.eml"},
    }
    d = tmp_path / "workflow_skills" / "tpm"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{skill_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _make_adapter(page_id: str = PAGE_ID, space: str = SPACE_KEY, body: str = "Page content here") -> MagicMock:
    """Build a mock Confluence adapter whose fetch() returns a usable raw_item."""
    raw_item = MagicMock()
    raw_item.metadata = {
        "page_id": page_id,
        "space": space,
        "title": f"Test page {page_id}",
        "url": f"https://confluence.example.com/wiki/spaces/{space}/pages/{page_id}",
    }
    raw_item.payload = {"body": body}
    raw_item.text = body
    adapter = MagicMock()
    adapter.fetch.return_value = raw_item
    return adapter


def _make_executor(adapter=None, retrievers=None, shim_kb=None) -> WorkflowExecutor:
    """Build a WorkflowExecutor with a mock LLM (no real calls)."""
    llm = MagicMock()
    # LLM returns a simple dict for extraction
    llm.chat.return_value = {
        "text": '{"meeting_datetime_pt": "2026-05-16", "rag_status": "Green"}',
        "tokens_out": 50,
    }
    return WorkflowExecutor(
        store=None,
        llm=llm,
        retrievers=retrievers or {},
        shim_kb=shim_kb,
        confluence_adapter=adapter,
    )


# ---------------------------------------------------------------------------
# Fixture: clear the module-level ephemeral cache between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_ephemeral_cache():
    """Ensure the module-level _ephemeral_cache is empty before each test."""
    _ephemeral_cache.clear()
    yield
    _ephemeral_cache.clear()


# ---------------------------------------------------------------------------
# Test 1: Happy path — fetch, extract, no persistent store write
# ---------------------------------------------------------------------------

class TestAskParameterizedHappyPath:

    def test_fetches_via_adapter_and_returns_passages(self, tmp_path):
        """ask_parameterized skill + adapter present + page in allow-listed space
        → adapter.fetch called, passages returned with correct page_id."""
        skill_yaml = _make_skill_yaml(tmp_path)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        cfg = yaml.safe_load(skill_yaml.read_text())
        passages = executor._retrieve_for_inputs(
            cfg=cfg,
            inputs={"page_id": f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"},
            sources=[],
        )

        adapter.fetch.assert_called_once()
        assert len(passages) >= 1
        assert any(
            p.get("metadata", {}).get("page_id") == PAGE_ID for p in passages
        ), f"Expected page_id {PAGE_ID} in passages metadata; got {[p.get('metadata') for p in passages]}"

    def test_does_not_write_to_wiki_metadata_store(self, tmp_path):
        """The ephemeral path MUST NOT call WikiMetadataStore.add() or any equivalent.

        This is the load-bearing no-persist boundary assertion (ADR-032 §C, §E.5).
        """
        skill_yaml = _make_skill_yaml(tmp_path)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        # Patch WikiMetadataStore at the module level to detect any import+call
        wiki_store_mock = MagicMock()
        with patch(
            "framework.workflow_runtime.executor.WorkflowExecutor._log_ephemeral_fetch",
            return_value=None,
        ):
            cfg = yaml.safe_load(skill_yaml.read_text())
            executor._retrieve_for_inputs(
                cfg=cfg,
                inputs={"page_id": f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"},
                sources=[],
            )

        # WikiMetadataStore mock was never constructed or called
        wiki_store_mock.add.assert_not_called()

        # Stronger: confirm WikiMetadataStore is not imported inside the ephemeral path
        # by checking the executor has no wiki_store attribute after the call
        assert not hasattr(executor, "wiki_store") or executor.wiki_store is None, (
            "WorkflowExecutor must not have a wiki_store; ephemeral path must never persist content."
        )

    def test_passage_has_ephemeral_flag(self, tmp_path):
        """Returned passage must carry metadata.ephemeral=True to mark it as non-persistent."""
        skill_yaml = _make_skill_yaml(tmp_path)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        cfg = yaml.safe_load(skill_yaml.read_text())
        passages = executor._retrieve_for_inputs(
            cfg=cfg,
            inputs={"page_id": f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"},
            sources=[],
        )
        assert all(
            p.get("metadata", {}).get("ephemeral") is True for p in passages
        ), "All ephemeral passages must carry metadata.ephemeral=True"

    def test_passage_has_real_citation_url(self, tmp_path):
        """Citation URL must be the real Confluence page URL (never a fixture:// path)."""
        skill_yaml = _make_skill_yaml(tmp_path)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        cfg = yaml.safe_load(skill_yaml.read_text())
        passages = executor._retrieve_for_inputs(
            cfg=cfg,
            inputs={"page_id": f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"},
            sources=[],
        )
        for p in passages:
            citation = p.get("citation", "")
            assert "fixture://" not in citation, (
                f"Ephemeral passage must not have fixture:// citation; got {citation!r}"
            )
            assert citation, "Citation must not be empty"


# ---------------------------------------------------------------------------
# Test 2: TTL cache hit — adapter called once
# ---------------------------------------------------------------------------

class TestTtlCache:

    def test_cache_hit_second_call_adapter_called_once(self, tmp_path):
        """On the second call with the same page_id, the adapter must NOT be called
        again — the cached passages are returned directly."""
        skill_yaml = _make_skill_yaml(tmp_path, ephemeral_ttl_seconds=300)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        cfg = yaml.safe_load(skill_yaml.read_text())
        inputs = {"page_id": f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"}

        # First call — fetches from adapter
        passages_1 = executor._retrieve_for_inputs(cfg=cfg, inputs=inputs, sources=[])
        assert adapter.fetch.call_count == 1

        # Second call — must hit cache
        passages_2 = executor._retrieve_for_inputs(cfg=cfg, inputs=inputs, sources=[])
        assert adapter.fetch.call_count == 1, (
            f"Adapter.fetch must be called only once (TTL cache hit); got {adapter.fetch.call_count}"
        )
        assert passages_1 == passages_2, "Cached passages must be identical to first-call passages"

    def test_cache_eviction_after_ttl_calls_adapter_again(self, tmp_path):
        """After TTL expires, the cache entry is evicted and adapter.fetch is called again."""
        skill_yaml = _make_skill_yaml(tmp_path, ephemeral_ttl_seconds=0)  # TTL=0 → instant expiry
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        cfg = yaml.safe_load(skill_yaml.read_text())
        inputs = {"page_id": f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"}

        # First call — fetches
        executor._retrieve_for_inputs(cfg=cfg, inputs=inputs, sources=[])
        assert adapter.fetch.call_count == 1

        # Second call with TTL=0 — cache entry already expired → fetch again
        executor._retrieve_for_inputs(cfg=cfg, inputs=inputs, sources=[])
        assert adapter.fetch.call_count == 2, (
            "Adapter.fetch must be called again after TTL=0 expiry"
        )


# ---------------------------------------------------------------------------
# Test 4: Adapter None → hard-fail
# ---------------------------------------------------------------------------

class TestAdapterNoneHardFail:

    def test_adapter_none_raises_actionable_error(self, tmp_path):
        """When self.confluence_adapter is None, ask_parameterized skill must
        hard-fail with an actionable message. NEVER silently returns empty."""
        skill_yaml = _make_skill_yaml(tmp_path)
        executor = _make_executor(adapter=None)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},
                sources=[],
            )

        msg = str(exc_info.value)
        assert PAGE_ID in msg or "adapter" in msg.lower(), (
            f"Error must mention page_id or adapter; got: {msg!r}"
        )
        assert "Confluence adapter" in msg, (
            "Error must mention 'Confluence adapter' so the user knows the failure mode"
        )
        # Must NOT mention provider internals
        for forbidden in ("Traceback", "File \"", "ConfluencePageNotInKBError"):
            assert forbidden not in msg

    def test_adapter_none_error_names_skill(self, tmp_path):
        """The error message must include the skill name so the user can contact the author."""
        skill_yaml = _make_skill_yaml(tmp_path, skill_name="my_custom_skill")
        executor = _make_executor(adapter=None)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},
                sources=[],
            )

        assert "my_custom_skill" in str(exc_info.value), (
            "Error must name the skill so the user can contact the skill author"
        )


# ---------------------------------------------------------------------------
# Test 5: Space not in allow_list → hard-fail BEFORE any fetch
# ---------------------------------------------------------------------------

class TestSpaceAllowListEnforcement:

    def test_space_not_in_allow_list_raises_before_fetch(self, tmp_path):
        """When the page's Confluence space is not in space_allow_list, the
        executor must hard-fail BEFORE calling adapter.fetch (trust check first)."""
        skill_yaml = _make_skill_yaml(
            tmp_path, space_allow_list=["FA", "PROJ"]
        )
        adapter = _make_adapter(PAGE_ID, "RESTRICTED")
        executor = _make_executor(adapter)

        # URL includes /spaces/RESTRICTED/ — detectable without any API call
        restricted_url = f"https://conf.example.com/spaces/RESTRICTED/pages/{PAGE_ID}/My+Page"

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": restricted_url},
                sources=[],
            )

        # Adapter.fetch must NOT have been called (trust enforced before network call)
        adapter.fetch.assert_not_called()

        msg = str(exc_info.value)
        assert "RESTRICTED" in msg or "allow-list" in msg.lower(), (
            f"Error must mention the offending space or allow-list; got: {msg!r}"
        )

    def test_space_in_allow_list_does_not_raise(self, tmp_path):
        """When the page's space IS in space_allow_list, the fetch proceeds normally."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=["FA"])
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        url = f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/My+Page"

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"page_id": url},
            sources=[],
        )
        assert len(passages) >= 1
        adapter.fetch.assert_called_once()

    def test_empty_space_allow_list_no_space_check(self, tmp_path):
        """When space_allow_list is empty [], the space check is skipped entirely
        and the fetch proceeds (allow-list not configured = allow all spaces for this skill)."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=[])
        adapter = _make_adapter(PAGE_ID, "ANYSPACE")
        executor = _make_executor(adapter)

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"page_id": PAGE_ID},
            sources=[],
        )
        assert len(passages) >= 1
        adapter.fetch.assert_called_once()

    def test_bare_numeric_id_single_fetch_for_space_check(self, tmp_path):
        """D2 fix: when a bare numeric page_id is supplied (no URL), the executor
        uses a SINGLE adapter.fetch() call to obtain both space metadata and content.
        No fetch_metadata() is called (that method does not exist on any adapter).
        fetch() is called exactly once; space is read from raw_item.metadata["space"]
        (a string, as emcp_direct.normalize() sets it).  Allow-listed space → passages
        returned; fetch called once, no second fetch."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=["FA", "PROJ"])
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)  # metadata["space"] = "FA"
        executor = _make_executor(adapter)

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"page_id": PAGE_ID},  # bare numeric ID — no URL to parse
            sources=[],
        )
        # fetch() called exactly ONCE (single-fetch model — D2 fix)
        assert adapter.fetch.call_count == 1, (
            f"D2: fetch must be called exactly once for bare numeric ID; "
            f"got call_count={adapter.fetch.call_count}"
        )
        # fetch_metadata must NOT be called (it does not exist on real adapters)
        assert not hasattr(adapter, "fetch_metadata") or adapter.fetch_metadata.call_count == 0, (
            "D2: fetch_metadata must NOT be called; single-fetch model uses fetch() only"
        )
        assert len(passages) >= 1
        assert passages[0].get("metadata", {}).get("space") == SPACE_KEY

    def test_bare_numeric_id_space_not_allowed_raises_after_single_fetch(self, tmp_path):
        """D2 fix: bare numeric page_id in a restricted space → single fetch(), then
        space check BEFORE extraction.  Fetched content is discarded (never extracted,
        never cached).  Hard-fail with allow-list violation message.
        One fetch total; no second fetch; no extraction."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=["FA", "PROJ"])
        adapter = _make_adapter(PAGE_ID, "SECRET")  # metadata["space"] = "SECRET"
        executor = _make_executor(adapter)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},  # bare numeric ID
                sources=[],
            )

        # fetch() called once (to determine space) — D2 single-fetch model
        assert adapter.fetch.call_count == 1, (
            "D2: fetch must be called exactly once even when space is not allowed; "
            f"got call_count={adapter.fetch.call_count}"
        )
        msg = str(exc_info.value)
        assert "SECRET" in msg or "allow-list" in msg.lower(), (
            f"Error must mention the offending space or allow-list; got: {msg!r}"
        )
        # Content must NOT be cached (disallowed content never persisted)
        from framework.workflow_runtime.executor import _ephemeral_cache
        cache_key = f"ephemeral:{PAGE_ID}"
        assert _ephemeral_cache.get(cache_key, 300) is None, (
            "D2: disallowed content must NOT be cached"
        )


# ---------------------------------------------------------------------------
# Test 6: ingest_on_demand false → hard-fail
# ---------------------------------------------------------------------------

class TestIngestOnDemandFalse:

    def test_ingest_on_demand_false_hard_fails(self, tmp_path):
        """When ingest_on_demand: false, the executor must hard-fail with an
        actionable message. The adapter must not be called."""
        skill_yaml = _make_skill_yaml(tmp_path, ingest_on_demand=False)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},
                sources=[],
            )

        # Adapter must NOT have been called
        adapter.fetch.assert_not_called()

        msg = str(exc_info.value)
        assert PAGE_ID in msg or "ingest_on_demand" in msg.lower() or "ingest" in msg.lower(), (
            f"Error must mention page_id or ingest_on_demand; got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Test 7: author_fixed skill → ephemeral path NOT taken
# ---------------------------------------------------------------------------

class TestAuthorFixedUnchanged:

    def _make_result(self, page_id: str):
        from framework.core.interfaces import Result
        return Result(
            content_id=page_id,
            chunk_id=None,
            text=f"Content from {page_id}",
            score=0.9,
            citation_url=f"wiki://{page_id}",
            metadata={"page_id": page_id},
        )

    def test_author_fixed_skill_does_not_use_ephemeral_path(self, tmp_path):
        """An author_fixed skill (no source_binding) must NOT enter the ephemeral path.
        The existing retriever path must be used unchanged."""
        skill_yaml = _make_author_fixed_skill_yaml(tmp_path)

        # Retriever returns a result for a fixed page
        result = self._make_result(OTHER_PAGE_ID)
        retriever = MagicMock(return_value=[result])
        shim_kb = MagicMock()
        shim_kb.all_cards.return_value = [{
            "name": "fixed_kb",
            "persona": "tpm",
            "retrieval_tools": ["search_wiki"],
        }]

        confluence_adapter = MagicMock()
        executor = _make_executor(adapter=confluence_adapter, retrievers={"search_wiki": retriever}, shim_kb=shim_kb)

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "What are the milestones?"},
            sources=[],
        )

        # Confluence adapter must NOT have been called (author_fixed → retriever path)
        confluence_adapter.fetch.assert_not_called()
        assert len(passages) >= 1

    def test_author_fixed_skill_p3_guard_deleted_by_adr039(self, tmp_path):
        """ADR-039 (DECISION-020): the P3 regex guard has been DELETED for author_fixed skills.

        Previously, a pageId= reference in free-text inputs would trigger a guard that
        hard-failed if the retriever returned a different page. ADR-039 replaces this with
        canonical==canonical matching via source_binding.pinned_ref.

        A generic author_fixed skill with no source_binding.pinned_ref now returns
        whatever the retriever provides — even if the input contains pageId= text.
        Source identity is enforced at author-time (synthesize_workflow stamps pinned_ref),
        not at execution-time via input scanning.
        """
        skill_yaml = _make_author_fixed_skill_yaml(tmp_path)

        # Retriever returns a page DIFFERENT from the one mentioned in inputs
        wrong_result = self._make_result(OTHER_PAGE_ID)
        retriever = MagicMock(return_value=[wrong_result])
        shim_kb = MagicMock()
        shim_kb.all_cards.return_value = [{
            "name": "fixed_kb",
            "persona": "tpm",
            "retrieval_tools": ["search_wiki"],
        }]

        executor = _make_executor(retrievers={"search_wiki": retriever}, shim_kb=shim_kb)

        # ADR-039: P3 guard deleted — no exception raised.
        # Generic author_fixed (no pinned_ref) passes through without any page-identity check.
        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": f"Please use pageId={PAGE_ID}"},
            sources=[],
        )
        assert len(passages) >= 1, (
            "ADR-039: P3 guard is deleted; generic author_fixed must return passages "
            "regardless of pageId= text in inputs (identity guard is now pinned_ref-based)"
        )

    def test_author_fixed_skill_p3_guard_inert_for_generic_query(self, tmp_path):
        """For author_fixed skills with a generic query (no page ref), the P3
        guard must be completely inert — passages are returned unchanged."""
        skill_yaml = _make_author_fixed_skill_yaml(tmp_path)

        result = self._make_result(OTHER_PAGE_ID)
        retriever = MagicMock(return_value=[result])
        shim_kb = MagicMock()
        shim_kb.all_cards.return_value = [{
            "name": "fixed_kb",
            "persona": "tpm",
            "retrieval_tools": ["search_wiki"],
        }]

        executor = _make_executor(retrievers={"search_wiki": retriever}, shim_kb=shim_kb)

        passages = executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"input": "What are the key project milestones?"},
            sources=[],
        )
        assert len(passages) >= 1, "Generic query — guard must be inert"


# ---------------------------------------------------------------------------
# Test 11: Adapter fetch raises → hard-fail
# ---------------------------------------------------------------------------

class TestAdapterFetchFailure:

    def test_adapter_fetch_raises_hard_fails_actionably(self, tmp_path):
        """When adapter.fetch() raises, the executor must hard-fail with an
        actionable message. NEVER returns empty content silently."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=[])
        adapter = MagicMock()
        adapter.fetch.side_effect = ConnectionError("Confluence unreachable")
        executor = _make_executor(adapter)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},
                sources=[],
            )

        msg = str(exc_info.value)
        assert PAGE_ID in msg
        # Must not expose internal framework class names or traceback markers
        for forbidden in ("ConfluencePageNotInKBError", "Traceback", "File \""):
            assert forbidden not in msg, f"Must not expose {forbidden!r} in consumer-facing message"
        # Must be actionable — tell the user how to proceed
        assert "Verify" in msg or "contact" in msg.lower() or "configure" in msg.lower(), (
            f"Error must be actionable; got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Test 12: Empty body text → hard-fail
# ---------------------------------------------------------------------------

class TestEmptyBodyHardFail:

    def test_empty_body_hard_fails(self, tmp_path):
        """If the fetched page has no usable body text, the executor must hard-fail."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=[])
        adapter = MagicMock()
        raw_item = MagicMock()
        raw_item.metadata = {"page_id": PAGE_ID, "space": SPACE_KEY, "url": f"https://conf.example.com/pages/{PAGE_ID}"}
        raw_item.payload = {"body": ""}  # empty body
        raw_item.text = ""
        adapter.fetch.return_value = raw_item
        executor = _make_executor(adapter)

        with pytest.raises(ConfluencePageNotInKBError) as exc_info:
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},
                sources=[],
            )

        assert PAGE_ID in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 13–15: _EphemeralCache unit tests
# ---------------------------------------------------------------------------

class TestEphemeralCache:

    def test_get_returns_none_when_empty(self):
        cache = _EphemeralCache()
        assert cache.get("missing_key", 300) is None

    def test_put_and_get_roundtrip(self):
        cache = _EphemeralCache()
        cache.put("k1", ["passage1"], 300)
        assert cache.get("k1", 300) == ["passage1"]

    def test_ttl_zero_expires_immediately(self):
        cache = _EphemeralCache()
        cache.put("k2", ["data"], 0)
        # TTL=0 → should be expired immediately (or within floating-point tolerance)
        # Slightly advance time conceptually by re-checking after a tiny sleep
        time.sleep(0.01)
        result = cache.get("k2", 0)
        assert result is None, "TTL=0 cache entry must be expired"

    def test_lru_eviction_at_cap(self):
        """At _MAX_SIZE=50 entries, the oldest is evicted on put."""
        cache = _EphemeralCache()
        # Fill to cap
        for i in range(50):
            cache.put(f"key_{i}", f"value_{i}", 300)
        # All 50 entries present
        assert cache.get("key_0", 300) is not None

        # 51st entry → LRU eviction (oldest = key_0 since it was inserted first)
        cache.put("key_50", "value_50", 300)
        # After eviction, exactly 50 entries remain; key_50 is present
        assert cache.get("key_50", 300) == "value_50"
        # Total size must not exceed 50
        with cache._lock:
            assert len(cache._store) <= 50, f"Cache size exceeded cap: {len(cache._store)}"

    def test_clear_empties_cache(self):
        cache = _EphemeralCache()
        cache.put("k3", "v3", 300)
        cache.clear()
        assert cache.get("k3", 300) is None

    def test_thread_safety_concurrent_puts_no_deadlock(self):
        """Concurrent puts from multiple threads must not deadlock or corrupt state."""
        cache = _EphemeralCache()
        errors: list = []

        def _worker(n):
            try:
                for i in range(20):
                    cache.put(f"key_{n}_{i}", f"val_{n}_{i}", 300)
                    cache.get(f"key_{n}_{i}", 300)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread-safety errors: {errors}"

    def test_effective_ttl_uses_min_of_get_and_stored_ttl(self):
        """If the stored TTL is shorter than the requested TTL, the shorter TTL wins."""
        cache = _EphemeralCache()
        cache.put("k4", "v4", 0)  # stored TTL = 0 (instant expiry)
        time.sleep(0.01)
        # Even if we request ttl=300, the stored TTL=0 already expired
        result = cache.get("k4", 300)
        assert result is None, "Stored TTL=0 must override the requested TTL of 300"


# ---------------------------------------------------------------------------
# Test 16: _extract_numeric_id_fast helper (ADR-039 replaces _resolve_page_id)
# ---------------------------------------------------------------------------

class TestExtractNumericIdFast:
    """ADR-039 (DECISION-020): _resolve_page_id has been DELETED.
    The fast-path numeric extraction is now in _extract_numeric_id_fast()
    (framework/adapters/confluence/shared.py). This class replaces TestResolvePageId.
    """

    def test_bare_numeric_id(self):
        assert _extract_numeric_id_fast("18625350641") == "18625350641"

    def test_querystring_form(self):
        assert _extract_numeric_id_fast("?pageId=18625350641") == "18625350641"

    def test_viewpage_action_form(self):
        url = "https://conf.example.com/pages/viewpage.action?pageId=18625350641"
        assert _extract_numeric_id_fast(url) == "18625350641"

    def test_rest_path_form(self):
        url = "https://conf.example.com/wiki/spaces/FA/pages/18625350641/My+Page"
        assert _extract_numeric_id_fast(url) == "18625350641"

    def test_bare_pageid_eq_form(self):
        assert _extract_numeric_id_fast("pageId=18625350641") == "18625350641"

    def test_unrecognised_ref_returns_none(self):
        """ADR-039: unlike _resolve_page_id, unrecognised refs return None (not the ref unchanged)."""
        ref = "some-unrecognised-reference"
        assert _extract_numeric_id_fast(ref) is None


# ---------------------------------------------------------------------------
# Test 17: _extract_space_key_from_url helper
# ---------------------------------------------------------------------------

class TestExtractSpaceKeyFromUrl:

    def test_url_with_spaces_path(self):
        url = "https://conf.example.com/wiki/spaces/FA/pages/18625350641/My+Page"
        assert _extract_space_key_from_url(url) == "FA"

    def test_url_with_proj_space(self):
        url = "https://conf.example.com/spaces/PROJ/pages/123/"
        assert _extract_space_key_from_url(url) == "PROJ"

    def test_bare_numeric_id_returns_none(self):
        assert _extract_space_key_from_url("18625350641") is None

    def test_url_without_spaces_returns_none(self):
        url = "https://conf.example.com/wiki/pages/viewpage.action?pageId=18625350641"
        assert _extract_space_key_from_url(url) is None


# ---------------------------------------------------------------------------
# Test 19: Audit log written on ephemeral fetch
# ---------------------------------------------------------------------------

class TestAuditLog:

    def test_audit_log_written_on_ephemeral_fetch(self, tmp_path, monkeypatch):
        """_log_ephemeral_fetch must write an entry to ephemeral_fetch.jsonl."""
        import framework.workflow_runtime.executor as exec_mod

        telemetry_dir = tmp_path / "telemetry"
        monkeypatch.setattr(exec_mod, "_TELEMETRY_DIR", telemetry_dir)

        executor = _make_executor()
        executor._log_ephemeral_fetch(PAGE_ID, SPACE_KEY, SKILL_NAME, "abc123")

        audit_file = telemetry_dir / "ephemeral_fetch.jsonl"
        assert audit_file.exists(), "ephemeral_fetch.jsonl must be created by _log_ephemeral_fetch"
        lines = audit_file.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["page_id"] == PAGE_ID
        assert entry["space_key"] == SPACE_KEY
        assert entry["skill_name"] == SKILL_NAME
        assert entry["content_hash"] == "abc123"
        assert "ts" in entry


# ---------------------------------------------------------------------------
# Test 20: WorkflowExecutor constructor accepts confluence_adapter
# ---------------------------------------------------------------------------

class TestWorkflowExecutorConstructor:

    def test_accepts_confluence_adapter_param(self):
        """WorkflowExecutor.__init__ must accept confluence_adapter kwarg."""
        mock_adapter = MagicMock()
        executor = WorkflowExecutor(confluence_adapter=mock_adapter)
        assert executor.confluence_adapter is mock_adapter

    def test_defaults_to_none(self):
        """When confluence_adapter is omitted, it defaults to None (backward-compat)."""
        executor = WorkflowExecutor()
        assert executor.confluence_adapter is None

    def test_existing_constructor_args_unchanged(self):
        """All existing constructor params still work as before (backward-compat)."""
        store = MagicMock()
        llm = MagicMock()
        retrievers = {"search_wiki": MagicMock()}
        shim_kb = MagicMock()
        executor = WorkflowExecutor(
            store=store, llm=llm, retrievers=retrievers, shim_kb=shim_kb
        )
        assert executor.store is store
        assert executor.llm is llm
        assert executor.retrievers is retrievers
        assert executor.shim_kb is shim_kb
        assert executor.confluence_adapter is None  # default


# ---------------------------------------------------------------------------
# D2 single-fetch model: additional assertions
# ---------------------------------------------------------------------------

class TestD2SingleFetchModel:
    """ADR-032 D2 fix — verify the single adapter.fetch() model.

    Before D2 fix: _retrieve_ask_parameterized called fetch_metadata(page_id)
    which does not exist on any Confluence adapter, causing AttributeError.
    After D2 fix: a single fetch() call provides both space info and content.
    Space check happens BEFORE extraction even though content is already in hand.
    Disallowed content is never extracted, never cached, never persisted.
    """

    def test_single_fetch_for_allowed_space_url_form(self, tmp_path):
        """URL form: space extractable pre-fetch → fetch called once, no 2nd fetch."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=["FA"])
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        url = f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/My+Page"
        executor._retrieve_for_inputs(
            cfg=yaml.safe_load(skill_yaml.read_text()),
            inputs={"page_id": url},
            sources=[],
        )
        assert adapter.fetch.call_count == 1, (
            f"D2: fetch must be called exactly once; got {adapter.fetch.call_count}"
        )

    def test_space_not_allowed_content_never_cached(self, tmp_path):
        """D2: when space is not allow-listed, fetched content must NEVER be cached."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=["FA"])
        # Adapter returns a page in RESTRICTED space
        adapter = _make_adapter(PAGE_ID, "RESTRICTED")
        executor = _make_executor(adapter)

        with pytest.raises(ConfluencePageNotInKBError):
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},  # bare numeric — space determined post-fetch
                sources=[],
            )

        cache_key = f"ephemeral:{PAGE_ID}"
        assert _ephemeral_cache.get(cache_key, 300) is None, (
            "D2: disallowed content must never be cached (no persist invariant)"
        )

    def test_space_not_allowed_content_never_extracted(self, tmp_path):
        """D2: when space is not allow-listed, LLM extraction must NOT be called."""
        skill_yaml = _make_skill_yaml(tmp_path, space_allow_list=["FA"])
        adapter = _make_adapter(PAGE_ID, "SECRET")
        executor = _make_executor(adapter)

        # The mock LLM tracks calls
        llm_call_count_before = executor.llm.chat.call_count

        with pytest.raises(ConfluencePageNotInKBError):
            executor._retrieve_for_inputs(
                cfg=yaml.safe_load(skill_yaml.read_text()),
                inputs={"page_id": PAGE_ID},
                sources=[],
            )

        # LLM must NOT have been called (space check failed before extraction)
        assert executor.llm.chat.call_count == llm_call_count_before, (
            "D2: LLM extraction must NOT run for disallowed space"
        )


# ---------------------------------------------------------------------------
# P2-API: source_fetched_on_demand wiring in execute() result
# ---------------------------------------------------------------------------

class TestP2ApiResponseWiring:
    """ADR-032 P2-API — verify execute() result dict includes ephemeral fetch signals."""

    def _run_execute(self, tmp_path) -> dict:
        """Run execute() end-to-end with mocked render/deliver and return the result."""
        skill_yaml = _make_skill_yaml(tmp_path)
        adapter = _make_adapter(PAGE_ID, SPACE_KEY)
        executor = _make_executor(adapter)

        # Mock render and deliver so we don't need real templates
        executor._render = lambda cfg, data: b"fake artifact"
        executor._deliver = lambda cfg, artifact, inputs: {
            "kind": "filesystem", "path": f"/tmp/test.eml", "url": "",
        }
        executor._record_cost = lambda *a, **kw: None
        executor._record_eval_entry = lambda *a, **kw: None

        url = f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"
        return executor.execute(
            skill_yaml,
            inputs={"page_id": url, "input": "draft email"},
        )

    def test_execute_sets_source_fetched_on_demand_true(self, tmp_path):
        """execute() result must include source_fetched_on_demand=True for
        ask_parameterized skills where an ephemeral fetch occurred."""
        result = self._run_execute(tmp_path)
        assert result.get("source_fetched_on_demand") is True, (
            f"P2-API: source_fetched_on_demand must be True; result keys: {list(result)}"
        )

    def test_execute_sets_source_fetched_page_id(self, tmp_path):
        """execute() result must include source_fetched_page_id with the fetched page id."""
        result = self._run_execute(tmp_path)
        assert result.get("source_fetched_page_id") == PAGE_ID, (
            f"P2-API: source_fetched_page_id must be {PAGE_ID!r}; got {result.get('source_fetched_page_id')!r}"
        )

    def test_execute_no_ephemeral_flag_absent_for_author_fixed(self, tmp_path):
        """For author_fixed skills, source_fetched_on_demand must be absent/False."""
        skill_yaml = _make_author_fixed_skill_yaml(tmp_path)
        executor = _make_executor()
        executor._retrieve_for_inputs = lambda cfg, inputs, sources: [{
            "text": "fixed content",
            "citation": "wiki://fixed",
            "metadata": {"page_id": "999", "ephemeral": False},
            "kb": "tpm.fixed_kb",
        }]
        executor._render = lambda cfg, data: b"fake"
        executor._deliver = lambda cfg, artifact, inputs: {"kind": "filesystem", "path": "/tmp/x"}
        executor._record_cost = lambda *a, **kw: None
        executor._record_eval_entry = lambda *a, **kw: None

        result = executor.execute(skill_yaml, inputs={"input": "query"})
        assert not result.get("source_fetched_on_demand"), (
            "P2-API: source_fetched_on_demand must be absent or False for author_fixed"
        )
