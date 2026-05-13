"""author_skill routes — POST/GET/DELETE /api/v1/kb/authorSkill.

Implements the Knowledge Builder Flow: a stateful, server-side conversation that
guides the caller through the 14-state skill authoring machine.

Route map:
  POST   /api/v1/kb/authorSkill              — start new session OR continue (synthId in body)
  POST   /api/v1/kb/authorSkill/{synth_id}   — continue existing session by path param
  GET    /api/v1/kb/authorSkill              — list all sessions for the authenticated user
  GET    /api/v1/kb/authorSkill/{synth_id}   — get session state / last turn
  DELETE /api/v1/kb/authorSkill/{synth_id}   — abandon session

Session lifecycle:
  1. POST (no synthId) → SkillBuilderConversation created, start() called → saved with
     status=in_progress, last_turn stored in session dict.
  2. POST (synthId present) → session loaded, conversation restored from dict,
     respond() called → session saved with updated state + last_turn.
  3. GET {synthId} → load session, return last_turn envelope (no state mutation).
  4. DELETE {synthId} → abandon session (status=abandoned, artifacts in git preserved).

The core logic lives in ``_start_or_continue_session()`` so that the MCP Track B
tool can call the same function without going through HTTP.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth.middleware import get_consumer, require_scope
from ..serialization import to_camel_response

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post("/api/v1/kb/authorSkill")
async def author_skill_start_or_continue(req: Request):
    """Start a new authoring session or continue one by passing synthId in the body.

    Body (all optional):
      synthId           — if present, resumes that session
      userInput / message  — text to pass to respond() if resuming
      persona           — persona hint for new sessions
      intentDescription — task description for new sessions

    Requires 'write' scope.
    """
    consumer = get_consumer(req)
    require_scope(consumer, "write")

    body = await req.json()
    synth_id = body.get("synthId") or body.get("synth_id")

    if synth_id:
        # Resume path — delegate to continue handler logic
        user_input = body.get("userInput") or body.get("user_input") or body.get("message", "")
        return await _handle_continue(req, consumer, synth_id, user_input)

    # New session path
    persona = body.get("persona", "")
    intent = body.get("intentDescription") or body.get("intent_description", "")

    # _start_or_continue_session is fully sync and may take 60-180s when the
    # state machine reaches INGEST (codex subprocess). Off-load to a worker
    # thread so the FastAPI event loop stays responsive. See BUG-queue-d3ec0.
    result = await asyncio.to_thread(
        _start_or_continue_session,
        session_store=req.app.state.session_store,
        llm=getattr(req.app.state, "llm", None),
        artifact_store=getattr(req.app.state, "artifact_store", None),
        skill_store=getattr(req.app.state, "skill_store", None),
        user_id=consumer.user_id,
        synth_id=None,
        user_input=None,
        persona=persona,
        intent_description=intent,
    )
    return _envelope_response(result)


@router.post("/api/v1/kb/authorSkill/{synth_id}")
async def author_skill_continue(synth_id: str, req: Request):
    """Continue an existing authoring session.

    Body:
      userInput / message — the user's response to the last turn

    Requires 'write' scope.
    """
    consumer = get_consumer(req)
    require_scope(consumer, "write")

    body = await req.json()
    user_input = body.get("userInput") or body.get("user_input") or body.get("message", "")

    return await _handle_continue(req, consumer, synth_id, user_input)


@router.get("/api/v1/kb/authorSkill")
async def author_skill_list(req: Request):
    """List all authoring sessions for the authenticated user.

    Returns a list of session summaries ordered by updated_at descending.
    Requires 'read' scope.
    """
    consumer = get_consumer(req)
    require_scope(consumer, "read")

    sessions = req.app.state.session_store.list_for_user(consumer.user_id)

    summaries = [_session_to_summary(s) for s in sessions]
    return to_camel_response({"sessions": summaries, "count": len(summaries)})


@router.get("/api/v1/kb/authorSkill/{synth_id}")
async def author_skill_get(synth_id: str, req: Request):
    """Get the state and last turn for a specific authoring session.

    Returns the last ConversationTurn envelope without advancing the state machine.
    Requires 'read' scope.
    """
    consumer = get_consumer(req)
    require_scope(consumer, "read")

    session = req.app.state.session_store.load(synth_id, user_id=consumer.user_id)
    if session is None:
        return _not_found(synth_id)

    # Return last_turn if available, otherwise synthesise a minimal state envelope
    last_turn = session.get("last_turn")
    if last_turn:
        return to_camel_response(last_turn)

    # Fallback: reconstruct a state envelope from the session (no last_turn stored yet)
    envelope = {
        "synth_id": session.get("synth_id", synth_id),
        "state": session.get("state", "IDENTIFY_PERSONA"),
        "status": session.get("status", "in_progress"),
        "message": "",
        "data": None,
        "options": None,
        "artifacts_preview": None,
        "progress": session.get("progress"),
        "done": session.get("done", False),
    }
    return to_camel_response(envelope)


@router.delete("/api/v1/kb/authorSkill/{synth_id}")
async def author_skill_delete(synth_id: str, req: Request):
    """Abandon an authoring session.

    Sets status=abandoned. Committed artifacts in git are preserved.
    Requires 'write' scope.
    """
    consumer = get_consumer(req)
    require_scope(consumer, "write")

    session = req.app.state.session_store.load(synth_id, user_id=consumer.user_id)
    if session is None:
        return _not_found(synth_id)

    req.app.state.session_store.abandon(synth_id, user_id=consumer.user_id)

    log.info("session abandoned: synth_id=%s user=%s", synth_id, consumer.user_id)
    return to_camel_response({
        "synth_id": synth_id,
        "status": "abandoned",
        "message": "Session abandoned. Committed artifacts in git are preserved.",
    })


# ---------------------------------------------------------------------------
# Core session logic — importable by MCP Track B tool
# ---------------------------------------------------------------------------


def _start_or_continue_session(
    *,
    session_store,
    llm,
    user_id: str,
    synth_id: str | None,
    user_input: str | None,
    skill_store,
    persona: str = "",
    intent_description: str = "",
    artifact_store=None,
) -> dict:
    """Start a new authoring session or advance an existing one.

    This function is the single choke-point for all session mutations.
    Both REST routes and the MCP Track B tool call this function.

    Args:
        session_store:      A SessionStore instance.
        llm:                LLMClient or None (stub mode).
        user_id:            Stable user identifier from ConsumerManifest.
        synth_id:           If provided, load and continue this session. If None, create new.
        user_input:         Text to pass to ``conversation.respond()``. Ignored when starting.
        persona:            Persona hint for new sessions.
        intent_description: Task description for new sessions.
        artifact_store:     ArtifactStore or None. Threaded into SkillBuilderConversation
                            so the ANALYZE_ARTIFACT state can resolve uploaded artifacts.
        skill_store:        SkillStore or None. Threaded into SkillBuilderConversation
                            so _write_artifacts writes to ADB in addition to filesystem.

    Returns:
        A dict envelope with synth_id, state, message, data, options,
        artifacts_preview, progress, done, status, last_turn.
    """
    from ...skill_builder.conversation import SkillBuilderConversation  # relative import

    if skill_store is None:
        # Loud guard at the choke-point: every entry point (REST route and MCP
        # tool handler) MUST pass app.state.skill_store. Forgetting it was the
        # silent root cause of synth-tpm-14a54555.
        raise RuntimeError(
            "_start_or_continue_session: skill_store is required. "
            "Caller must pass app.state.skill_store. ADB is the source of "
            "truth — there is no filesystem-only / stub mode."
        )

    if synth_id:
        # Resume existing session
        session = session_store.load(synth_id, user_id=user_id)
        if session is None:
            return {
                "_error": {
                    "code": "not_found",
                    "message": f"Session '{synth_id}' not found or access denied.",
                    "details": {},
                }
            }

        conversation = SkillBuilderConversation.from_dict(
            session, llm=llm, artifact_store=artifact_store, skill_store=skill_store
        )
        turn = conversation.respond(user_input or "")
    else:
        # Start new session
        conversation = SkillBuilderConversation(
            persona=persona,
            user_id=user_id,
            llm=llm,
            artifact_store=artifact_store,
            skill_store=skill_store,
        )
        turn = conversation.start(intent_description=intent_description)
        synth_id = turn.synth_id  # always stamped by _turn()

    # Build the turn envelope (snake_case — serializer converts to camelCase)
    envelope = _turn_to_envelope(turn)

    # Determine session status.
    # DB constraint CHK_ASS_STATUS allows: in_progress | completed | abandoned | expired.
    # "committed" is NOT a valid value — use "completed" for finished sessions (BUG-006).
    status = "completed" if turn.done else "in_progress"

    # Clean up uploaded artifacts when the session completes (ADR-021)
    if turn.done and artifact_store is not None:
        try:
            resolved_synth_id = turn.synth_id or (synth_id or "")
            if resolved_synth_id:
                artifact_store.cleanup(resolved_synth_id)
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("artifact cleanup failed for synth_id=%s: %s", synth_id, exc)

    # Persist session dict with last_turn so GET can re-serve it
    session_dict = conversation.to_dict()
    session_dict["status"] = status
    session_dict["last_turn"] = envelope
    session_dict["done"] = turn.done

    session_store.save(session_dict, user_id=user_id)

    log.info(
        "session %s: state=%s done=%s user=%s",
        synth_id, turn.state, turn.done, user_id,
    )

    return envelope


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _handle_continue(req: Request, consumer, synth_id: str, user_input: str):
    """Shared implementation for the two POST-continue routes."""
    # See BUG-queue-d3ec0: _start_or_continue_session is sync + may block the
    # event loop for minutes during INGEST. Run in a thread to keep the
    # uvicorn worker responsive for other requests.
    result = await asyncio.to_thread(
        _start_or_continue_session,
        session_store=req.app.state.session_store,
        llm=getattr(req.app.state, "llm", None),
        artifact_store=getattr(req.app.state, "artifact_store", None),
        skill_store=getattr(req.app.state, "skill_store", None),
        user_id=consumer.user_id,
        synth_id=synth_id,
        user_input=user_input,
    )

    # After a PROMOTE the session is done — reload ShimKb so newly promoted
    # KBs are immediately visible without a server restart (Option B).
    if result.get("done"):
        shim_kb = getattr(req.app.state, "shim_kb", None)
        if shim_kb is not None:
            try:
                shim_kb.reload()
                log.info("shim_kb reloaded after session done: synth_id=%s", synth_id)
            except Exception as exc:
                log.warning("shim_kb.reload() failed: %s", exc)

    return _envelope_response(result)


def _envelope_response(result: dict):
    """Convert a result dict to a JSONResponse.

    If result contains ``_error``, return a 404 or 400 error response.
    Otherwise, return a 200 camelCase response.
    """
    if "_error" in result:
        err = result["_error"]
        code = err.get("code", "error")
        status_code = 404 if code == "not_found" else 400
        return JSONResponse(
            status_code=status_code,
            content={"error": err},
        )
    return to_camel_response(result)


def _turn_to_envelope(turn) -> dict:
    """Map a ConversationTurn dataclass -> snake_case dict.

    Fields: synth_id, state, message, data, options, artifacts_preview,
            progress, done.
    """
    return {
        "synth_id": turn.synth_id,
        "state": turn.state,
        "message": turn.message,
        "data": turn.data,
        "options": turn.options,
        "artifacts_preview": turn.artifacts_preview,
        "progress": turn.progress,
        "done": turn.done,
    }


def _session_to_summary(session: dict) -> dict:
    """Extract a compact summary from a session dict for the list endpoint."""
    return {
        "synth_id": session.get("synth_id", ""),
        "state": session.get("state", ""),
        "status": session.get("status", ""),
        "persona": session.get("persona", ""),
        "skill_name": session.get("skill_name", ""),
        "intent_description": session.get("intent_description", ""),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "done": session.get("done", False),
    }


def _not_found(synth_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "not_found",
                "message": f"Session '{synth_id}' not found or access denied.",
                "details": {},
            }
        },
    )
