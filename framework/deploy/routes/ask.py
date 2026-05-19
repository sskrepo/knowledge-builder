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
import uuid
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
    # Pass body so maybe_render_artifact can read explicit page_id field (D1).
    maybe_render_artifact(req.app.state, result, question, body=body)

    response = _build_ask_response(result, consumer)
    return to_camel_response(response)


def maybe_render_artifact(app_state, result: dict, question: str,
                          body: dict | None = None) -> None:
    """If tier-1 matched a workflow_skill with response_mode=artifact_url,
    run the WorkflowExecutor to render the artifact (PPT/DOCX/etc.) and
    mutate `result` in place with a `delivery` dict for the response builder.

    Mutates instead of returning so that callers in both the REST route and
    the MCP tool handler can use a single call without re-plumbing return
    values. Failures are logged and the result is left untouched — the
    text answer always reaches the user even if rendering fails.

    ADR-033: skill cfg is resolved from ADB artifact (not disk) for promoted
    skills when a skill_store is wired on app_state.  This ensures that
    source_binding, trigger, and delivery reflect the committed ADB artifact
    rather than a stale/absent disk byproduct.  Disk is only consulted when
    no skill_store is available (laptop/no-ADB path, explicitly INFO-logged).

    ADR-032 D1 fix: for ask_parameterized skills, the skill's
    source_binding.input_param is resolved and threaded into the executor
    inputs dict.  The page reference is resolved with this precedence:
      1. Explicit body field matching input_param (e.g. body["page_id"]).
      2. Extracted from the question string using _extract_numeric_id_fast (ADR-039).
    If no page ref can be resolved for an ask_parameterized skill, the call
    hard-fails with an actionable message (never executes with an empty page id).

    ADR-032 P2-API: when the executor signals source_fetched_on_demand=True,
    the response fields sourceFetchedOnDemand, sourceFetchedPageId, and
    latencyNote are populated in `result` for the response builder.

    Implementation notes:
      - WorkflowExecutor.execute_from_config() runs its own retrieve →
        synthesize → render → deliver chain. This duplicates the retrieve
        work the ContextBuilder already did, but the alternative (threading
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

    # -----------------------------------------------------------------------
    # ADR-033: resolve skill cfg from ADB artifact (preferred) or disk.
    #
    # Resolution order:
    #   1. ADB artifact via app_state.skill_store.read_artifact() — used when
    #      skill_store is available.  This is the authoritative source for
    #      promoted skills; guarantees source_binding is correct.
    #   2. Disk YAML fallback — used when skill_store is not available (laptop
    #      no-ADB path).  Explicitly INFO-logged — not silent.
    #
    # If both fail, skip rendering with a WARNING.
    # -----------------------------------------------------------------------
    cfg: dict | None = None
    skill_yaml_path: Path | None = None  # kept for legacy execute() compatibility

    skill_store = getattr(app_state, "skill_store", None)
    if skill_store is not None:
        # ADB path: read committed workflow_skill artifact.
        try:
            content = skill_store.read_artifact(persona, skill_name, "workflow_skill")
        except Exception as exc:
            log.warning(
                "render: skill_store.read_artifact failed for %s.%s: %s — "
                "will attempt disk fallback.",
                persona, skill_name, exc,
            )
            content = None

        if content is not None:
            try:
                cfg = _yaml.safe_load(content) or {}
                log.info(
                    "render: loaded skill cfg from ADB artifact for %s.%s (ADR-033)",
                    persona, skill_name,
                )
            except Exception as exc:
                log.warning(
                    "render: failed to parse ADB artifact for %s.%s: %s — "
                    "will attempt disk fallback.",
                    persona, skill_name, exc,
                )
                cfg = None
        else:
            log.warning(
                "render: ADB artifact not found for %s.%s — skill may not be "
                "committed.  Will attempt disk fallback.",
                persona, skill_name,
            )

    if cfg is None:
        # Disk fallback — laptop/no-store OR ADB read failed.
        candidate = (
            Path(__file__).resolve().parents[3]
            / "framework" / "workflow_skills" / persona / f"{skill_name}.yaml"
        )
        if not candidate.exists():
            log.warning(
                "render: skill cfg not found — ADB artifact absent AND disk "
                "file missing at %s — skipping render for %s.%s",
                candidate, persona, skill_name,
            )
            return
        if skill_store is not None:
            # ADB is wired but the artifact was absent/unreadable — disk file
            # may be a stale byproduct.  Log at WARNING so it's auditable.
            log.warning(
                "render: falling back to DISK for %s.%s (ADB artifact "
                "unavailable) — cfg may be stale. path=%s",
                persona, skill_name, candidate,
            )
        else:
            log.info(
                "render: loading skill cfg from disk for %s.%s (no skill_store "
                "wired — laptop mode).  path=%s",
                persona, skill_name, candidate,
            )
        try:
            cfg = _yaml.safe_load(candidate.read_text()) or {}
            skill_yaml_path = candidate  # for legacy execute() compat
        except Exception as exc:
            log.warning(
                "render: failed to parse disk YAML %s: %s — skipping",
                candidate, exc,
            )
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

    # -----------------------------------------------------------------------
    # ADR-032 D1 fix: build the inputs dict with the page ref threaded in
    # for ask_parameterized skills.
    #
    # author_fixed skills: inputs={"input": question} — unchanged behavior.
    # ask_parameterized skills: inputs={"input": question, input_param: page_ref}
    #   where page_ref is resolved with precedence:
    #     1. Explicit body field matching input_param (highest priority).
    #     2. Extracted from the question string via _extract_numeric_id_fast (ADR-039).
    #   If neither yields a page ref: hard-fail with actionable message.
    #   Never execute with an empty page id (no silent substitution).
    # -----------------------------------------------------------------------
    source_binding = cfg.get("source_binding") or {}
    sb_mode = source_binding.get("mode", "author_fixed")
    inputs: dict = {"input": question}

    if sb_mode == "ask_parameterized":
        from ...workflow_runtime.executor import ConfluencePageNotInKBError
        # ADR-039 (DECISION-020): _extract_confluence_page_ids deleted.
        # Use _extract_numeric_id_fast from the adapter's shared helpers to extract
        # a Confluence page ID from the question text. This is the fast-path numeric
        # extraction (no API call) — the full canonical resolution happens in the executor.
        from ...adapters.confluence.shared import _extract_numeric_id_fast
        input_param = source_binding.get("input_param", "page_id")

        # Priority 1: explicit body field (e.g. body["page_id"])
        page_ref: str = ""
        if body and input_param in body and body[input_param]:
            page_ref = str(body[input_param]).strip()
            log.debug(
                "render: ask_parameterized page_ref from body[%r]=%r",
                input_param, page_ref,
            )

        # Priority 2: extract from question string using fast-path numeric extraction.
        # ADR-039: _extract_numeric_id_fast handles all known URL forms (pageId=, /pages/NNN, etc).
        if not page_ref:
            extracted_id = _extract_numeric_id_fast(question)
            if extracted_id:
                page_ref = extracted_id
                log.debug(
                    "render: ask_parameterized page_ref extracted from question: %r",
                    page_ref,
                )

        if not page_ref:
            # No page ref resolvable — hard-fail, never execute with empty id.
            log.warning(
                "render: ask_parameterized skill %s.%s requires a page ref "
                "(input_param=%r) but none found in body or question — "
                "hard-failing (no silent substitution).",
                persona, skill_name, input_param,
            )
            result["answer"] = {
                "Answer": (
                    f"Skill '{skill_name}' requires a Confluence page reference "
                    f"(field: '{input_param}'). Include the page URL or pageId in "
                    "your request and retry."
                )
            }
            result["tier"] = 4
            result["tier_description"] = "source_not_available"
            result["source_not_available"] = {
                "page_id": "",
                "skill": skill_name,
                "resolution": (
                    f"Provide '{input_param}' in the request body or embed the "
                    "Confluence URL in your question."
                ),
            }
            return

        inputs[input_param] = page_ref
        log.info(
            "render: ask_parameterized skill %s.%s inputs[%r]=%r threaded",
            persona, skill_name, input_param, page_ref,
        )

    log.info(
        "render: invoking WorkflowExecutor for tier-1 skill %s.%s (response_mode=%s)",
        persona, skill_name, response_mode,
    )
    try:
        # ADR-033: use execute_from_config() when cfg was loaded from ADB (no path).
        # Fall back to execute() with path when cfg came from disk (laptop mode).
        if skill_yaml_path is None:
            exec_result = executor.execute_from_config(cfg, inputs=inputs)
        else:
            exec_result = executor.execute(skill_yaml_path, inputs=inputs)
    except Exception as exc:  # noqa: BLE001
        from ...workflow_runtime.executor import ConfluencePageNotInKBError
        if isinstance(exc, ConfluencePageNotInKBError):
            # ADR-032 P3/D1: source-not-available hard-fail — mutate result so the
            # consumer receives the actionable message rather than a silent empty
            # artifact. Override the tier-1 answer with the error text.
            log.warning(
                "render: source-not-available hard-fail for page %s (skill=%s) — "
                "surfacing source_not_available to consumer",
                exc.page_id, exc.skill_name,
            )
            result["answer"] = {"Answer": str(exc)}
            result["tier"] = 4
            result["tier_description"] = "source_not_available"
            result["source_not_available"] = {
                "page_id": exc.page_id,
                "skill": exc.skill_name,
                "resolution": "ingest then retry",
            }
            return
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

    # ADR-032 P2-API: wire ephemeral fetch disclosure fields into result.
    # The executor sets source_fetched_on_demand=True when an ask_parameterized
    # ephemeral fetch occurred.  The response builder (_build_ask_response) will
    # emit sourceFetchedOnDemand/sourceFetchedPageId/latencyNote to the caller.
    if exec_result.get("source_fetched_on_demand"):
        result["source_fetched_on_demand"] = True
        result["source_fetched_page_id"] = exec_result.get("source_fetched_page_id", "")
        result["latency_note"] = (
            "This request fetched a Confluence page on demand (+2–15s). "
            "The page was not written to the knowledge base."
        )
        log.info(
            "render: ephemeral fetch disclosed — sourceFetchedOnDemand=true "
            "page_id=%r skill=%s.%s",
            result["source_fetched_page_id"], persona, skill_name,
        )

    # ------------------------------------------------------------------
    # Backfill the inline answer/citations from the executor's ACTUAL
    # output.
    #
    # BUG: for artifact_url workflow skills the response's
    # answer/citations come from ContextBuilder's tier-1 passage
    # synthesis, which runs in ctx.answer() BEFORE this function. For
    # ask_parameterized / ephemeral skills that synthesis has NO
    # passages (the page is fetched HERE, in the executor's separate
    # retrieve->synthesize->render chain), so the synthesizer emits the
    # "(no relevant context found)" sentinel even though the executor
    # just produced a complete, correct artifact. Result: the response
    # lied — answer="(no relevant context found)" next to a valid
    # artifact_path.
    #
    # Fix (BUG-016 original): when the upstream answer is empty / the
    # no-answer sentinel, replace it with a truthful summary + the real
    # source citations from the executor's rendered_data.
    #
    # Fix (BUG-017): the empty/sentinel heuristic is insufficient. The
    # tier-1 synthesizer is handed the page text + a question like
    # "Generate a PPTX…" and, acting as a text Q&A synthesizer, emits a
    # confident REFUSAL ("I cannot generate a PPTX…") — which matches
    # neither "empty" nor the sentinel, so the misleading refusal was
    # surfaced next to a valid artifact_path (internally contradictory
    # response). When an artifact WAS delivered, a synthesizer refusal /
    # inability statement is definitively wrong (the artifact exists) and
    # must be replaced. A GENUINE synthesized summary, by contrast, is
    # still preserved (see test_real_upstream_answer_is_preserved) — we
    # only override the refusal class, not real content.
    _ans = result.get("answer")
    rendered = exec_result.get("rendered_data") or {}
    art_path = (result["delivery"].get("path")
                or result["delivery"].get("url") or "")
    _artifact_delivered = bool(art_path)
    _ans_text = (
        str(_ans.get("Answer", "")) if isinstance(_ans, dict)
        else str(_ans or "")
    ).strip().lower()
    # Refusal/inability lead-ins an LLM emits when asked to "generate" an
    # artifact it (as a text synthesizer) cannot itself produce. These do
    # not occur at the head of a genuine content summary.
    _REFUSAL_MARKERS = (
        "i cannot", "i can't", "i am unable", "i'm unable",
        "i am not able", "i'm not able", "i do not have the ability",
        "i don't have the ability", "unable to generate",
        "unable to create", "unable to provide", "cannot generate",
        "cannot create", "cannot provide",
    )
    _is_refusal = any(m in _ans_text[:200] for m in _REFUSAL_MARKERS)
    _needs_backfill = (
        (_artifact_delivered and _is_refusal)
        or not _ans
        or (isinstance(_ans, dict)
            and "no relevant context found"
                in str(_ans.get("Answer", "")).strip().lower())
        or (isinstance(_ans, dict)
            and str(_ans.get("Answer", "")).strip() == "")
        or (isinstance(_ans, str)
            and _ans.strip().lower() in ("", "(no relevant context found)"))
    )
    if _needs_backfill:
        _title = rendered.get("title") or skill_name
        _cites = [c for c in (rendered.get("citations") or []) if c]
        _pid = result.get("source_fetched_page_id", "")
        result["answer"] = {
            "Answer": (
                f"Generated '{_title}' via skill {persona}.{skill_name}"
                + (f" from Confluence page {_pid}" if _pid else "")
                + (f". Artifact: {art_path}" if art_path else ".")
            ),
            "Citations": ("; ".join(_cites) if _cites
                          else (art_path or "(see artifact)")),
        }
        if _cites:
            result["citations"] = list(dict.fromkeys(_cites))
            result["passages"] = [
                {"text": "", "citation": c, "score": 1.0}
                for c in result["citations"]
            ]
        log.info(
            "render: backfilled inline answer/citations from executor "
            "output (skill=%s.%s cites=%d artifact_delivered=%s) — "
            "executor outcome is authoritative for an artifact_url skill "
            "(upstream tier-1 text answer superseded)",
            persona, skill_name, len(_cites), _artifact_delivered,
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
    # maybe_render_artifact in this file.
    delivery = result.get("delivery") or {}
    if delivery.get("path") or delivery.get("url"):
        response["artifact_url"] = delivery.get("url") or ""
        response["artifact_path"] = delivery.get("path") or ""
        response["artifact_kind"] = delivery.get("kind") or ""

    # ADR-032 P2-API: surface ephemeral fetch disclosure fields.
    # Emitted only when maybe_render_artifact flagged an ask_parameterized
    # ephemeral fetch.  Absent/false for author_fixed skills.
    # Serializer (to_camel_response) converts these to camelCase:
    #   source_fetched_on_demand  → sourceFetchedOnDemand
    #   source_fetched_page_id    → sourceFetchedPageId
    #   latency_note              → latencyNote
    if result.get("source_fetched_on_demand"):
        response["source_fetched_on_demand"] = True
        response["source_fetched_page_id"] = result.get("source_fetched_page_id", "")
        response["latency_note"] = result.get(
            "latency_note",
            "This request fetched a Confluence page on demand (+2–15s).",
        )

    # Tier 4: attach request_id + skill suggestion.
    #
    # ADR-033 / BUG-queue: the tier-4 response must ALWAYS carry a server-side
    # request_id so consumers can correlate the response with server logs.
    # Previously, request_id was only set on the content-filter path
    # (_content_filtered=True).  The routing-miss tier-4 path had no request_id,
    # making log correlation impossible.  Fixed: generate a KBF request_id for
    # ALL tier-4 responses if one is not already present.
    if tier == 4:
        # Ensure every tier-4 response has a server-side request_id.
        request_id = result.get("request_id")
        if not request_id:
            request_id = f"KBF-{uuid.uuid4().hex[:12].upper()}"
        response["request_id"] = request_id

        if result.get("request_id"):
            # Content-filter path: suppress skill suggestion, surface clean message
            response["skill_suggestion"] = {
                "message": (
                    f"The query could not be processed. "
                    f"Request ID: {request_id}"
                ),
                "suggested_persona": intent.get("persona", ""),
            }
        else:
            # Routing-miss tier-4 path: surface skill suggestion
            response["skill_suggestion"] = result.get("skill_suggestion", {
                "message": (
                    "No grounded answer found. "
                    "Consider authoring a skill for this query type."
                ),
                "suggested_persona": intent.get("persona", ""),
            })

    return response
