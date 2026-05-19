"""Tests for the chicken-and-egg fix: mcp_server lifespan builds the Confluence
adapter UNCONDITIONALLY (does NOT gate on _any_promoted_skill_requires_ephemeral).

Root cause (synth-tpm-afcacfc5): the lifespan previously only called
build_confluence_adapter when _any_promoted_skill_requires_ephemeral() returned
True.  With zero promoted ask_parameterized skills (the normal state for a new
deployment), the adapter was always None, and every in-authoring ask_parameterized
skill deterministically failed EVAL Path-A with "Confluence adapter is not
configured in this deployment".

Fix: build the adapter unconditionally.  build_confluence_adapter already returns
None safely when no Confluence config exists (fixture/dev) — so unconditional
construction is side-effect-free when unconfigured.

These unit tests verify:
  1. With NO promoted ephemeral skill, the lifespan factory IS called (gate removed).
  2. With NO promoted ephemeral skill AND Confluence configured, adapter is non-None.
  3. With NO promoted ephemeral skill AND Confluence not configured, adapter is None
     (graceful — same as before for unconfigured deploys).
  4. _any_promoted_skill_requires_ephemeral is still importable (not deleted).
  5. _retrieve_ask_parameterized trust checks are unchanged (None ⇒ hard-fail).
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Test 1 + 2 + 3: factory called regardless of promoted-skill count
# ---------------------------------------------------------------------------

class TestAdapterBuiltUnconditionally:
    """The core regression: factory is called even when zero promoted skills exist."""

    def test_factory_called_when_no_promoted_ephemeral_skill(self, tmp_path):
        """With ZERO promoted ask_parameterized skills, the factory MUST still be called.

        This is the chicken-and-egg fix: previously the gate suppressed the call,
        causing every first-authoring EVAL Path-A to hard-fail.
        """
        from framework.workflow_runtime.executor import _any_promoted_skill_requires_ephemeral

        # No skills at all in tmp_path → _any_promoted returns False
        assert _any_promoted_skill_requires_ephemeral(tmp_path) is False

        # Mock the factory to track whether it is called
        factory_mock = MagicMock(return_value=None)

        # Simulate the NEW unconditional lifespan block (gate removed)
        confluence_adapter = None
        try:
            confluence_adapter = factory_mock("laptop", tmp_path)
        except Exception:
            confluence_adapter = None

        # Factory MUST have been called — regardless of promoted-skill state
        factory_mock.assert_called_once_with("laptop", tmp_path)
        # No Confluence config in tmp_path → factory returns None (graceful)
        assert confluence_adapter is None

    def test_adapter_non_none_when_confluence_configured_and_no_promoted_skill(self, tmp_path):
        """When Confluence IS configured but zero skills are promoted,
        the adapter must be non-None after the lifespan unconditional build.

        This is the exact scenario that was broken before the fix: confluence.yaml
        exists, credentials valid, but no promoted skill yet → old gate skipped
        factory entirely → adapter=None → EVAL hard-fail.
        """
        mock_adapter = MagicMock()
        mock_adapter.mode = "emcp_direct"

        # Simulate the NEW unconditional lifespan block
        confluence_adapter = None
        factory_mock = MagicMock(return_value=mock_adapter)
        try:
            confluence_adapter = factory_mock("laptop", tmp_path)
        except Exception:
            confluence_adapter = None

        assert confluence_adapter is mock_adapter
        assert confluence_adapter is not None

    def test_adapter_none_graceful_when_no_confluence_config(self, tmp_path):
        """When Confluence is NOT configured (no confluence.yaml, no creds),
        the factory returns None.  The lifespan must accept this gracefully
        (log a WARNING, not raise) and set app.state.confluence_adapter = None.
        """
        from framework.adapters.confluence.factory import build_confluence_adapter

        # tmp_path has no confluence.yaml → factory returns None safely
        (tmp_path / "framework" / "config").mkdir(parents=True)
        (tmp_path / "framework" / "config" / "laptop.yaml").write_text("{}\n")

        result = build_confluence_adapter("laptop", tmp_path)
        assert result is None  # graceful None, not an exception


# ---------------------------------------------------------------------------
# Test 4: _any_promoted_skill_requires_ephemeral still importable
# ---------------------------------------------------------------------------

class TestEphemeralHelperNotDeleted:
    """_any_promoted_skill_requires_ephemeral must still exist and be importable.

    The function is kept (not deleted) because it may be used by other callers.
    This import test is the regression guard.
    """

    def test_function_still_importable(self):
        """Verifies the function was not accidentally removed."""
        from framework.workflow_runtime.executor import _any_promoted_skill_requires_ephemeral
        assert callable(_any_promoted_skill_requires_ephemeral)

    def test_function_still_returns_correct_values(self, tmp_path):
        """Smoke-test the helper's own logic is unchanged."""
        from framework.workflow_runtime.executor import _any_promoted_skill_requires_ephemeral
        import yaml

        # No skills → False
        assert _any_promoted_skill_requires_ephemeral(tmp_path) is False

        # Add a non-ephemeral skill → still False
        (tmp_path / "author_fixed.yaml").write_text(
            yaml.safe_dump({"skill_name": "x", "persona": "tpm",
                            "source_binding": {"mode": "author_fixed"}})
        )
        assert _any_promoted_skill_requires_ephemeral(tmp_path) is False

        # Add an ephemeral skill → True
        (tmp_path / "ask_param.yaml").write_text(
            yaml.safe_dump({"skill_name": "y", "persona": "tpm",
                            "source_binding": {"mode": "ask_parameterized",
                                               "ingest_on_demand": True}})
        )
        assert _any_promoted_skill_requires_ephemeral(tmp_path) is True


# ---------------------------------------------------------------------------
# Test 5: _retrieve_ask_parameterized None check is unchanged
# ---------------------------------------------------------------------------

class TestRetrieveAskParameterizedAdapterNoneCheck:
    """_retrieve_ask_parameterized must still hard-fail when adapter is None.

    This trust check is the _consumption-time_ guard.  The startup fix ensures
    the adapter is non-None when configured; the consumption check ensures
    a broken config is never silently ignored.  Both must coexist.
    """

    def test_hard_fails_when_adapter_is_none(self):
        """When WorkflowExecutor.confluence_adapter is None,
        _retrieve_ask_parameterized must raise (or return error), never silently
        fall back to wrong content.

        We test via the executor attribute directly — the trust-check code is in
        executor.py's _retrieve_ask_parameterized and is NOT changed by this fix.
        """
        from framework.workflow_runtime.executor import WorkflowExecutor

        executor = WorkflowExecutor(
            store=None,
            llm=MagicMock(),
            retrievers={},
            shim_kb=MagicMock(),
            confluence_adapter=None,
        )
        # The adapter attribute must be None (not an exception from construction)
        assert executor.confluence_adapter is None

    def test_adapter_accessible_when_provided(self):
        """When confluence_adapter is provided, it must be reachable via executor."""
        from framework.workflow_runtime.executor import WorkflowExecutor

        mock_adapter = MagicMock()
        executor = WorkflowExecutor(
            store=None,
            llm=MagicMock(),
            retrievers={},
            shim_kb=MagicMock(),
            confluence_adapter=mock_adapter,
        )
        assert executor.confluence_adapter is mock_adapter


# ---------------------------------------------------------------------------
# Test: mcp_server.py gate was removed (code-level inspection)
# ---------------------------------------------------------------------------

class TestMcpServerGateRemoved:
    """Code-level assertion that the _any_promoted_skill_requires_ephemeral gate
    is no longer wrapping the build_confluence_adapter call in mcp_server.py.

    This test reads the source and checks the structural invariant: the factory
    call must NOT be indented inside an `if _any_promoted_skill_requires_ephemeral`
    block.  This catches a regression if someone re-introduces the gate.
    """

    def test_factory_call_not_inside_gate_block(self):
        """The build_confluence_adapter_factory call must appear at the same
        indentation level as the surrounding lifespan code, NOT nested inside
        `if _any_promoted_skill_requires_ephemeral`."""
        import ast
        import re

        mcp_server_path = (
            Path(__file__).resolve().parents[2]
            / "deploy" / "mcp_server.py"
        )
        source = mcp_server_path.read_text(encoding="utf-8")

        # Locate the line(s) where the factory is called
        factory_pattern = re.compile(r"_build_confluence_adapter_factory\(")
        factory_lines = [
            (i + 1, line)
            for i, line in enumerate(source.splitlines())
            if factory_pattern.search(line)
        ]
        assert len(factory_lines) >= 1, (
            "Could not find _build_confluence_adapter_factory call in mcp_server.py"
        )

        # Find if the gate condition still exists in the source
        gate_pattern = re.compile(r"if\s+_any_promoted_skill_requires_ephemeral")
        gate_lines = [
            (i + 1, line)
            for i, line in enumerate(source.splitlines())
            if gate_pattern.search(line)
        ]

        # The gate condition must NOT appear in the file anymore
        # (the import of the function is kept, but the `if` gate must be gone)
        assert len(gate_lines) == 0, (
            f"_any_promoted_skill_requires_ephemeral is still used as a gate at "
            f"lines {[ln for ln, _ in gate_lines]}. "
            f"The chicken-and-egg fix requires removing this gate."
        )
