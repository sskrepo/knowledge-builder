"""POST /api/v1/ask — Consumption flow entry point.

Routes the question through ContextBuilder's four-tier system and returns a
structured AskResponse with answer, citations, tier metadata, and cost telemetry.

Caller responsibilities:
  - Attach a valid Bearer token (validated by bearer_auth_middleware).
  - Include "question" in the JSON body (1-4096 chars).
  - Optional hints: persona, serviceId, functionalArea, maxResults.

This module is also importable by the MCP Track B tool so the core logic lives
in ``_start_ask()``, a plain async function both the route and the MCP tool call.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..auth.middleware import get_consumer, require_scope
from ..serialization import to_camel_response

log = logging.getLogger(__name__)

router = APIRouter()

_TIER_DESCRIPTIONS: dict[int, str] = {
    1: "workflow_skill",
    2: "kb_retrieval",
    3: "multi_persona_fanout",
    4: "no_answer",
}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/api/v1/ask")
async def ask_knowledge_base(req: Request):
    """POST /api/v1/ask — single entry point for all knowledge queries.

    Validates the Bearer token (via middleware), checks read scope, then
    delegates to the ContextBuilder attached to ``app.state.context_builder``.
    """
    consumer = get_consumer(req)
    require_scope(consumer, "read")

    body = await req.json()
    question = body.get("question", "")

    if not question or not isinstance(question, str) or len(question.strip()) == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "invalid_argument",
                    "message": "question must be 1-4096 characters",
                    "details": {},
                }
            },
        )

    if len(question) > 4096:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "invalid_argument",
                    "message": "question must be 1-4096 characters",
                    "details": {"max_length": 4096, "received": len(question)},
                }
            },
        )

    max_results = body.get("maxResults", 10)

    ctx = req.app.state.context_builder

    from ...orchestrator.budget import Budget  # relative from framework/deploy/routes/
    budget = Budget(
        max_tokens_in=consumer.token_budget_per_request,
        max_tokens_out=1500,
    )

    log.info(
        "ask: consumer=%s question_len=%d max_results=%d",
        consumer.name, len(question), max_results,
    )

    result = ctx.answer(
        query=question,
        budget=budget,
        persona_hint=body.get("persona", ""),
        service_id_hint=body.get("serviceId", ""),
        func_area_hint=body.get("functionalArea", ""),
        max_results=max_results,
    )

    # Single choke point — used by both REST + MCP handlers.
    maybe_render_artifact(req.app.state, result, question)

    response = _build_ask_response(result, consumer)
    return to_camel_response(response)


def maybe_render_artifact(app_state, result: dict, question: str) -> None:
    """If tier-1 matched a workflow_skill with response_mode=artifact_url,
    run the WorkflowExecutor to render the artifact (PPT/DOCX/etc.) and
    mutate `result` in place with a `delivery` dict for the response builder.

    Mutates instead of returning so that callers in both the REST route and
    the MCP tool handler can use a single call without re-plumbing return
    values. Failures are logged and the result is left untouched — the
    text answer always reaches the user even if rendering fails.

    Implementation notes:
      - WorkflowExecutor.execute() runs its own retrieve → synthesize →
        render → deliver chain. This duplicates the retrieve work the
        ContextBuilder already did, but the alternative (threading
        passages into the executor) is a bigger refactor.
      - We accept `app_state` (not `req`) so the MCP tool handler can call
        the same function — it has `app` but not a `Request`.
    """
    import yaml as _yaml
    if result.get("tier") != 1:
        return
    intent = result.get("intent") or {}
    skill_name = intent.get("workflow_skill")
    persona = intent.get("persona") or ""
    if not skill_name or not persona:
        log.debug(
            "render: tier 1 but no workflow_skill/persona in intent (skill=%r persona=%r) — skipping",
            skill_name, persona,
        )
        return

    skill_yaml_path = (
        Path(__file__).resolve().parents[3]
        / "framework" / "workflow_skills" / persona / f"{skill_name}.yaml"
    )
    if not skill_yaml_path.exists():
        log.warning("render: workflow_skill yaml not found at %s — skipping",
                    skill_yaml_path)
        return
    try:
        cfg = _yaml.safe_load(skill_yaml_path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("render: failed to parse %s: %s — skipping",
                    skill_yaml_path, exc)
        return

    response_mode = (
        (cfg.get("trigger", {}).get("on_request") or {}).get("response_mode")
    )
    if response_mode != "artifact_url":
        return  # text-only skill; nothing to render

    executor = getattr(app_state, "workflow_executor", None)
    if executor is None:
        log.warning("render: app.state.workflow_executor missing — cannot render")
        return

    log.info(
        "render: invoking WorkflowExecutor for tier-1 skill %s.%s (response_mode=%s)",
        persona, skill_name, response_mode,
    )
    try:
        exec_result = executor.execute(skill_yaml_path, inputs={"input": question})
    except Exception as exc:  # noqa: BLE001
        log.error("render: WorkflowExecutor.execute failed: %s", exc, exc_info=True)
        return

    delivery = exec_result.get("delivery") or {}
    result["delivery"] = {
        "kind": delivery.get("kind") or "filesystem",
        "path": delivery.get("path") or delivery.get("url") or "",
        "url": delivery.get("url") or "",
        "skill": exec_result.get("skill", skill_name),
        "render_ms": (exec_result.get("metrics") or {}).get("render_ms"),
    }
    log.info(
        "render: artifact ready → %s (skill=%s.%s render_ms=%s)",
        result["delivery"].get("path") or result["delivery"].get("url"),
        persona, skill_name, result["delivery"].get("render_ms"),
    )


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------


def _build_ask_response(result: dict, consumer) -> dict:
    """Map ContextBuilder result dict -> AskResponse (snake_case).

    The serializer (``to_camel_response``) converts all keys to camelCase
    before the response leaves this process.

    ContextBuilder.answer() returns:
        answer, schema, tier, intent (persona, personas, confidence, …),
        passages [{text, citation, score}], citations, used_kbs, used_tools,
        cost (arbitrary dict), latency_ms
    """
    passages = result.get("passages", [])
    citations_out: list[dict] = []
    for p in passages:
        citations_out.append({
            "citation_url": p.get("citation", ""),
            "text": p.get("text", ""),
            "relevance_score": p.get("score", 0.0),
            "content_id": p.get("content_id", ""),
            "chunk_id": p.get("chunk_id", ""),
            "metadata": p.get("metadata", {}),
        })

    intent = result.get("intent", {})
    tier = result.get("tier", 4)
    confidence = intent.get("confidence", 0.0)

    # cost dict from ContextPacket is free-form ({"tool_calls": N, "tier": N, …}).
    # We normalise it to the external contract (prompt/completion/total tokens).
    raw_cost = result.get("cost", {})
    cost_tokens = {
        "prompt": raw_cost.get("prompt", 0),
        "completion": raw_cost.get("completion", 0),
        "total": raw_cost.get("total",
                              raw_cost.get("prompt", 0) + raw_cost.get("completion", 0)),
    }

    response: dict = {
        "answer": result.get("answer", ""),
        "citations": citations_out,
        "confidence": confidence,
        "tier_used": tier,
        "tier_description": _TIER_DESCRIPTIONS.get(tier, "unknown"),
        "cost_tokens": cost_tokens,
        "latency_ms": result.get("latency_ms", 0),
    }

    # Surface the rendered artifact (PPT/DOCX/etc.) when WorkflowExecutor ran
    # for a tier-1 skill that declared response_mode=artifact_url. See
    # _maybe_render_artifact in this file.
    delivery = result.get("delivery") or {}
    if delivery.get("path") or delivery.get("url"):
        response["artifact_url"] = delivery.get("url") or ""
        response["artifact_path"] = delivery.get("path") or ""
        response["artifact_kind"] = delivery.get("kind") or ""

    # Tier 4: attach skill suggestion and/or requestId (content-filter path)
    if tier == 4:
        # Content-filter path: surface clean requestId, suppress skill suggestion
        if result.get("request_id"):
            response["request_id"] = result["request_id"]
            response["skill_suggestion"] = {
                "message": (
                    f"The query could not be processed. "
                    f"Request ID: {result['request_id']}"
                ),
                "suggested_persona": intent.get("persona", ""),
            }
        else:
            response["skill_suggestion"] = result.get("skill_suggestion", {
                "message": (
                    "No grounded answer found. "
                    "Consider authoring a skill for this query type."
                ),
                "suggested_persona": intent.get("persona", ""),
            })

    return response
