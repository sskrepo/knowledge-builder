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

    response = _build_ask_response(result, consumer)
    return to_camel_response(response)


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

    # Tier 4: attach skill suggestion
    if tier == 4:
        response["skill_suggestion"] = result.get("skill_suggestion", {
            "message": (
                "No grounded answer found. "
                "Consider authoring a skill for this query type."
            ),
            "suggested_persona": intent.get("persona", ""),
        })

    return response
