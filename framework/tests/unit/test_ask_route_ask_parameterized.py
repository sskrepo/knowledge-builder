"""ADR-032 D1 fix + P2-API wiring — ask route maybe_render_artifact tests.

Tests for the D1 fix in framework/deploy/routes/ask.py:
  - ask_parameterized skill + question containing Confluence URL →
    inputs passed to executor include input_param=<page_id> (D1 fix).
  - ask_parameterized skill + explicit body page_id field → takes highest priority.
  - author_fixed skill → inputs == {"input": question} (unchanged).
  - ask_parameterized + no resolvable page ref → hard-fail (no execute call).
  - Response dict gets source_fetched_on_demand, source_fetched_page_id,
    latency_note wired when executor signals it (P2-API).

No live network, no real LLM, no ADB.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call
from typing import Any

import pytest
import yaml

# We import only the function under test; no FastAPI app needed.
from framework.deploy.routes.ask import maybe_render_artifact, _build_ask_response


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_ID = "18625350641"
SPACE_KEY = "FA"
SKILL_NAME = "project_tracking_test_email"
PERSONA = "tpm"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_skill_yaml(
    tmp_path: Path,
    *,
    skill_name: str = SKILL_NAME,
    persona: str = PERSONA,
    mode: str = "ask_parameterized",
    input_param: str = "page_id",
    ingest_on_demand: bool = True,
    space_allow_list: list | None = None,
    response_mode: str = "artifact_url",
) -> Path:
    """Write a minimal ask_parameterized skill YAML and return its path."""
    if space_allow_list is None:
        space_allow_list = [SPACE_KEY, "PROJ"]

    cfg: dict = {
        "workflow_skill": skill_name,
        "persona": persona,
        "status": "promoted",
        "source_binding": {
            "mode": mode,
            "input_param": input_param,
            "ingest_on_demand": ingest_on_demand,
            "source_type": "confluence_page",
            "space_allow_list": space_allow_list,
            "ephemeral_ttl_seconds": 300,
        },
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [
                    {"name": input_param, "type": "confluence_page_ref", "required": True}
                ],
                "output_format": "email",
                "response_mode": response_mode,
            },
        },
        "requires_extractions": [{"kb": f"tpm.{skill_name}"}],
        "synthesis": {"output_format": "email"},
        "delivery": {"kind": "filesystem", "path": f"/tmp/{skill_name}.eml"},
    }

    skill_dir = tmp_path / "workflow_skills" / persona
    skill_dir.mkdir(parents=True, exist_ok=True)
    p = skill_dir / f"{skill_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _write_author_fixed_skill_yaml(
    tmp_path: Path,
    *,
    skill_name: str = "fixed_skill",
    persona: str = PERSONA,
    response_mode: str = "artifact_url",
) -> Path:
    """Write a minimal author_fixed skill YAML (no source_binding)."""
    cfg = {
        "workflow_skill": skill_name,
        "persona": persona,
        "status": "promoted",
        "trigger": {
            "on_request": {
                "enabled": True,
                "inputs": [{"name": "input", "type": "string"}],
                "output_format": "email",
                "response_mode": response_mode,
            },
        },
        "requires_extractions": [{"kb": "tpm.fixed_kb"}],
        "synthesis": {"output_format": "email"},
        "delivery": {"kind": "filesystem", "path": "/tmp/fixed.eml"},
    }
    d = tmp_path / "workflow_skills" / persona
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{skill_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _make_app_state(
    tmp_path: Path,
    skill_yaml_path: Path,
    *,
    executor_result: dict | None = None,
    executor_raises: Exception | None = None,
) -> MagicMock:
    """Build a mock app_state with workflow_executor wired to the skill YAML dir.

    ADR-033: skill_store is explicitly set to None so maybe_render_artifact uses
    the disk-path (laptop/no-store mode).  These tests exercise the D1/P2 logic
    using _patch_skill_yaml to inject the skill cfg from disk, not from ADB.
    Tests that exercise the ADB artifact path are in test_shim_workflows_adb.py.
    """
    app_state = MagicMock()

    # ADR-033: set skill_store=None so maybe_render_artifact takes the disk path
    # (uses _patch_skill_yaml content) rather than calling read_artifact().
    app_state.skill_store = None

    # The executor mock captures inputs kwarg for assertion
    mock_executor = MagicMock()
    if executor_raises is not None:
        mock_executor.execute.side_effect = executor_raises
    else:
        default_result = {
            "skill": SKILL_NAME,
            "persona": PERSONA,
            "delivery": {"kind": "filesystem", "path": "/tmp/out.eml", "url": ""},
            "metrics": {"render_ms": 100},
        }
        if executor_result is not None:
            default_result.update(executor_result)
        mock_executor.execute.return_value = default_result

    app_state.workflow_executor = mock_executor

    return app_state


def _make_tier1_result(skill_name: str = SKILL_NAME, persona: str = PERSONA) -> dict:
    """Build a minimal tier-1 result dict as ContextBuilder.answer() would return."""
    return {
        "tier": 1,
        "intent": {
            "workflow_skill": skill_name,
            "persona": persona,
            "confidence": 0.92,
        },
        "answer": "Draft email",
        "passages": [],
        "cost": {"prompt": 100, "completion": 50},
        "latency_ms": 800,
    }


# ---------------------------------------------------------------------------
# Fixture: patch the skill YAML path resolution in maybe_render_artifact
# ---------------------------------------------------------------------------

def _patch_skill_yaml(skill_yaml_path: Path, persona: str = PERSONA, skill_name: str = SKILL_NAME):
    """Context manager that patches the skill YAML path lookup in maybe_render_artifact.

    Approach: patch os.path.exists (used by Path.exists internally) would cause
    recursion.  Instead we patch yaml.safe_load to detect the correct path and
    redirect reads.  The simplest approach: directly patch the skill_yaml_path
    attribute that maybe_render_artifact constructs by intercepting Path.__new__
    or by using monkeypatching of the yaml load step.

    We use the simplest reliable approach: patch the builtins open / Path.read_text
    at the module level in the ask module so that our tmp_path YAML content is served
    when the constructed path matches the workflow_skills pattern.
    """
    import contextlib
    import os as _os

    _skill_yaml_content = skill_yaml_path.read_text()

    @contextlib.contextmanager
    def _ctx():
        # We intercept at the yaml.safe_load call site in maybe_render_artifact
        # by patching ask._yaml.safe_load after patching Path.read_text.
        # Use os.path.exists to avoid recursive call.
        _skill_yaml_real_path = str(skill_yaml_path)

        original_exists = Path.exists
        original_read_text = Path.read_text

        def _safe_exists(self):
            s = str(self)
            if "workflow_skills" in s and skill_name in s:
                return _os.path.exists(_skill_yaml_real_path)
            # Call the unpatched version by going through os.path
            return _os.path.exists(s)

        def _safe_read_text(self, **kwargs):
            s = str(self)
            if "workflow_skills" in s and skill_name in s:
                return _skill_yaml_content
            # Call original for all other paths
            return original_read_text.__func__(self, **kwargs)

        with patch.object(Path, "exists", _safe_exists), \
             patch.object(Path, "read_text", _safe_read_text):
            yield

    return _ctx()


# ---------------------------------------------------------------------------
# Test D1-1: ask_parameterized + URL in question → inputs include page_id
# ---------------------------------------------------------------------------

class TestD1InputThreading:
    """D1 fix: maybe_render_artifact threads input_param into executor inputs."""

    def test_question_with_url_threads_page_id_into_inputs(self, tmp_path):
        """ask_parameterized skill + Confluence URL in question →
        executor.execute called with inputs including input_param=<page_id>."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result()

        question = (
            f"Please draft a status email based on https://conf.example.com"
            f"/spaces/{SPACE_KEY}/pages/{PAGE_ID}/My+Project+Page"
        )

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question)

        # executor.execute must have been called
        assert app_state.workflow_executor.execute.called, (
            "D1: executor.execute must be called for ask_parameterized skill"
        )
        _, kwargs = app_state.workflow_executor.execute.call_args
        inputs_passed = kwargs.get("inputs") or {}

        # Must include the page_id resolved from the question URL
        assert "page_id" in inputs_passed, (
            f"D1: inputs must include 'page_id'; got keys: {list(inputs_passed)}"
        )
        assert inputs_passed["page_id"] == PAGE_ID, (
            f"D1: inputs['page_id'] must be {PAGE_ID!r}; got {inputs_passed['page_id']!r}"
        )
        # Must also include "input": question (unchanged)
        assert inputs_passed.get("input") == question, (
            "D1: inputs['input'] must be the original question (unchanged)"
        )

    def test_explicit_body_page_id_takes_highest_priority(self, tmp_path):
        """When the request body explicitly includes page_id, it takes priority
        over anything extracted from the question string."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result()

        body_page_id = "99999999999"
        question_page_id = PAGE_ID  # different page_id embedded in question
        question = (
            f"Please draft based on https://conf.example.com/spaces/{SPACE_KEY}"
            f"/pages/{question_page_id}/SomePage"
        )
        body = {"page_id": body_page_id, "question": question, "persona": PERSONA}

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question, body=body)

        _, kwargs = app_state.workflow_executor.execute.call_args
        inputs_passed = kwargs.get("inputs") or {}
        assert inputs_passed.get("page_id") == body_page_id, (
            f"D1: explicit body page_id={body_page_id!r} must take priority over "
            f"question-extracted {question_page_id!r}; got {inputs_passed.get('page_id')!r}"
        )

    def test_pageid_querystring_in_question_extracted(self, tmp_path):
        """pageId=<id> querystring form in question → extracted and threaded."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result()

        question = f"Draft based on the page at https://conf.example.com/wiki/pages/viewpage.action?pageId={PAGE_ID}"

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question)

        _, kwargs = app_state.workflow_executor.execute.call_args
        inputs_passed = kwargs.get("inputs") or {}
        assert inputs_passed.get("page_id") == PAGE_ID, (
            f"D1: pageId querystring form must be extracted; got {inputs_passed.get('page_id')!r}"
        )

    def test_space_form_page_ref_in_question_extracted(self, tmp_path):
        """'pageId 18625350641' (space-separated, BUG-990fe form) in question →
        extracted and threaded into inputs."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result()

        question = f"Draft an email for Confluence pageId {PAGE_ID}"

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question)

        _, kwargs = app_state.workflow_executor.execute.call_args
        inputs_passed = kwargs.get("inputs") or {}
        assert inputs_passed.get("page_id") == PAGE_ID, (
            f"D1: space-form 'pageId {PAGE_ID}' must be extracted; got {inputs_passed.get('page_id')!r}"
        )


# ---------------------------------------------------------------------------
# Test D1-2: author_fixed skill → inputs unchanged
# ---------------------------------------------------------------------------

class TestAuthorFixedInputsUnchanged:
    """D1: author_fixed skills must pass inputs={"input": question} unchanged."""

    def test_author_fixed_inputs_only_has_input_key(self, tmp_path):
        """For author_fixed skills, inputs must only contain {"input": question}.
        No page_id injection must occur."""
        skill_yaml = _write_author_fixed_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result(skill_name="fixed_skill")

        question = "What are the key milestones for the FA DB upgrade?"

        with _patch_skill_yaml(skill_yaml, skill_name="fixed_skill"):
            maybe_render_artifact(app_state, result, question)

        assert app_state.workflow_executor.execute.called
        _, kwargs = app_state.workflow_executor.execute.call_args
        inputs_passed = kwargs.get("inputs") or {}
        assert set(inputs_passed.keys()) == {"input"}, (
            f"D1: author_fixed inputs must only have 'input' key; got {set(inputs_passed.keys())}"
        )
        assert inputs_passed["input"] == question

    def test_author_fixed_no_page_id_in_inputs(self, tmp_path):
        """Even if the question contains a Confluence URL, author_fixed skills
        must NOT inject page_id into inputs (guard does that separately)."""
        skill_yaml = _write_author_fixed_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result(skill_name="fixed_skill")

        question = f"Draft based on https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"

        with _patch_skill_yaml(skill_yaml, skill_name="fixed_skill"):
            maybe_render_artifact(app_state, result, question)

        _, kwargs = app_state.workflow_executor.execute.call_args
        inputs_passed = kwargs.get("inputs") or {}
        assert "page_id" not in inputs_passed, (
            "D1: author_fixed inputs must NOT include 'page_id' (that is ask_parameterized only)"
        )


# ---------------------------------------------------------------------------
# Test D1-3: ask_parameterized + no resolvable page ref → hard-fail, no execute
# ---------------------------------------------------------------------------

class TestAskParameterizedNoPageRef:
    """D1: when no page ref can be resolved, hard-fail without calling execute."""

    def test_no_page_ref_hard_fails_without_calling_executor(self, tmp_path):
        """ask_parameterized skill + question with no Confluence page reference →
        hard-fail with actionable message; executor.execute must NOT be called."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result()

        question = "What are the key project milestones?"  # no page ref

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question)

        # executor.execute must NOT have been called (no page ref → hard-fail before execute)
        app_state.workflow_executor.execute.assert_not_called(), (
            "D1: executor must NOT be called when no page ref is resolvable"
        )

        # result must signal source_not_available (hard-fail)
        assert result.get("tier") == 4, (
            f"D1: tier must be set to 4 (source_not_available); got {result.get('tier')}"
        )
        assert result.get("tier_description") == "source_not_available"
        assert "page_id" in str(result.get("source_not_available", {})).lower() or \
               "page" in str(result.get("answer", "")).lower(), (
            "D1: result must signal page ref required in answer or source_not_available"
        )

    def test_no_page_ref_result_tier_4(self, tmp_path):
        """Hard-fail mutates result to tier 4 so the consumer sees an actionable message."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)
        result = _make_tier1_result()

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, "generic query with no page", body={})

        assert result["tier"] == 4
        assert result["tier_description"] == "source_not_available"


# ---------------------------------------------------------------------------
# Test P2-API: response dict wired when executor signals ephemeral fetch
# ---------------------------------------------------------------------------

class TestP2ApiResponseWiring:
    """P2-API: maybe_render_artifact must wire P2-API fields from executor result."""

    def test_source_fetched_on_demand_wired_when_executor_signals(self, tmp_path):
        """When executor returns source_fetched_on_demand=True, result must get
        source_fetched_on_demand, source_fetched_page_id, latency_note populated."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(
            tmp_path, skill_yaml,
            executor_result={
                "source_fetched_on_demand": True,
                "source_fetched_page_id": PAGE_ID,
            },
        )
        result = _make_tier1_result()
        question = f"https://conf.example.com/spaces/{SPACE_KEY}/pages/{PAGE_ID}/"

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question)

        assert result.get("source_fetched_on_demand") is True, (
            f"P2-API: source_fetched_on_demand must be True; result={result!r}"
        )
        assert result.get("source_fetched_page_id") == PAGE_ID, (
            f"P2-API: source_fetched_page_id must be {PAGE_ID!r}"
        )
        assert result.get("latency_note"), (
            "P2-API: latency_note must be a non-empty string"
        )
        assert "on demand" in result["latency_note"].lower(), (
            f"P2-API: latency_note must mention 'on demand'; got {result['latency_note']!r}"
        )

    def test_source_fetched_absent_for_author_fixed(self, tmp_path):
        """For author_fixed skills, source_fetched_on_demand must be absent."""
        skill_yaml = _write_author_fixed_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml)  # executor does NOT signal ephemeral
        result = _make_tier1_result(skill_name="fixed_skill")

        with _patch_skill_yaml(skill_yaml, skill_name="fixed_skill"):
            maybe_render_artifact(app_state, result, "generic query")

        assert not result.get("source_fetched_on_demand"), (
            "P2-API: source_fetched_on_demand must be absent/False for author_fixed"
        )
        assert "source_fetched_page_id" not in result, (
            "P2-API: source_fetched_page_id must be absent for author_fixed"
        )
        assert "latency_note" not in result, (
            "P2-API: latency_note must be absent for author_fixed"
        )

    def test_build_ask_response_emits_camel_case_fields(self, tmp_path):
        """_build_ask_response must include sourceFetchedOnDemand etc. in response dict
        (snake_case keys — serializer converts to camelCase)."""
        # Simulate a result where maybe_render_artifact has already wired the fields
        result = {
            "answer": "Draft email produced.",
            "tier": 1,
            "tier_description": "workflow_skill",
            "intent": {"persona": PERSONA, "confidence": 0.9},
            "passages": [],
            "cost": {"prompt": 100, "completion": 50},
            "latency_ms": 1200,
            "delivery": {"kind": "filesystem", "path": "/tmp/out.eml", "url": ""},
            "source_fetched_on_demand": True,
            "source_fetched_page_id": PAGE_ID,
            "latency_note": "This request fetched a Confluence page on demand (+2–15s).",
        }
        consumer = MagicMock()
        consumer.token_budget_per_request = 4096

        response = _build_ask_response(result, consumer)

        assert response.get("source_fetched_on_demand") is True, (
            "P2-API: _build_ask_response must emit source_fetched_on_demand=True"
        )
        assert response.get("source_fetched_page_id") == PAGE_ID, (
            f"P2-API: source_fetched_page_id must be {PAGE_ID!r}"
        )
        assert "on demand" in (response.get("latency_note") or "").lower(), (
            "P2-API: latency_note must mention on-demand fetch"
        )

    def test_build_ask_response_no_ephemeral_fields_absent(self):
        """_build_ask_response must NOT emit P2-API fields when executor did not signal."""
        result = {
            "answer": "Fixed skill result.",
            "tier": 1,
            "tier_description": "workflow_skill",
            "intent": {"persona": PERSONA, "confidence": 0.9},
            "passages": [],
            "cost": {"prompt": 50, "completion": 20},
            "latency_ms": 500,
        }
        consumer = MagicMock()
        consumer.token_budget_per_request = 4096

        response = _build_ask_response(result, consumer)

        assert not response.get("source_fetched_on_demand"), (
            "P2-API: source_fetched_on_demand must be absent when no ephemeral fetch"
        )
        assert "source_fetched_page_id" not in response, (
            "P2-API: source_fetched_page_id must be absent"
        )
        assert "latency_note" not in response, (
            "P2-API: latency_note must be absent"
        )


# ---------------------------------------------------------------------------
# Test ConfluencePageNotInKBError surfacing
# ---------------------------------------------------------------------------

class TestConfluencePageNotInKBErrorSurfacing:
    """When executor raises ConfluencePageNotInKBError, result must be mutated
    to tier 4 / source_not_available — no silent failures."""

    def test_executor_raises_cnike_mutates_result(self, tmp_path):
        """When executor.execute raises ConfluencePageNotInKBError, result is
        mutated to tier=4 / source_not_available with the actionable message."""
        from framework.workflow_runtime.executor import ConfluencePageNotInKBError
        skill_yaml = _write_skill_yaml(tmp_path)
        exc = ConfluencePageNotInKBError(PAGE_ID, SKILL_NAME)
        app_state = _make_app_state(tmp_path, skill_yaml, executor_raises=exc)
        result = _make_tier1_result()

        question = f"pageId={PAGE_ID}"

        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, question)

        assert result["tier"] == 4
        assert result["tier_description"] == "source_not_available"
        assert result.get("source_not_available", {}).get("page_id") == PAGE_ID


# ---------------------------------------------------------------------------
# Regression: inline answer/citations backfill from executor output
# (fixes "(no relevant context found)" next to a valid artifact_path)
# ---------------------------------------------------------------------------

class TestAnswerBackfill:
    """maybe_render_artifact must replace an empty/no-answer upstream tier-1
    answer with a truthful summary + real citations from the executor's
    rendered_data — but must NOT clobber a real upstream answer."""

    _RENDERED = {
        "rendered_data": {
            "title": "RODS Support for Dynamic Tables Replication",
            "citations": ["wiki://18625350641",
                          "https://confluence.oraclecorp.com/.../pageId=18625350641"],
        },
        "source_fetched_on_demand": True,
        "source_fetched_page_id": "18625350641",
    }

    def test_no_answer_sentinel_is_backfilled(self, tmp_path):
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml,
                                    executor_result=self._RENDERED)
        result = _make_tier1_result()
        result["answer"] = {"Answer": "(no relevant context found)",
                            "Citations": "(no relevant context found)"}
        result["passages"] = []
        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, f"pageId={PAGE_ID}")
        ans = result["answer"]["Answer"].lower()
        assert "no relevant context found" not in ans
        assert "rods support for dynamic tables replication" in ans
        assert "18625350641" in ans
        # citations now reflect the real fetched source
        assert result["citations"] == ["wiki://18625350641",
            "https://confluence.oraclecorp.com/.../pageId=18625350641"]
        assert result["passages"] and result["passages"][0]["citation"] == "wiki://18625350641"

    def test_empty_string_answer_is_backfilled(self, tmp_path):
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml,
                                    executor_result=self._RENDERED)
        result = _make_tier1_result()
        result["answer"] = ""
        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, f"pageId={PAGE_ID}")
        assert "RODS Support" in result["answer"]["Answer"]

    def test_real_upstream_answer_is_preserved(self, tmp_path):
        """author_fixed-style skill whose tier-1 synthesis produced a real
        answer must NOT be overwritten by the artifact summary."""
        skill_yaml = _write_skill_yaml(tmp_path)
        app_state = _make_app_state(tmp_path, skill_yaml,
                                    executor_result=self._RENDERED)
        result = _make_tier1_result()
        result["answer"] = {"Answer": "Here is the real synthesized exec summary..."}
        with _patch_skill_yaml(skill_yaml):
            maybe_render_artifact(app_state, result, f"pageId={PAGE_ID}")
        assert result["answer"]["Answer"] == "Here is the real synthesized exec summary..."
