"""KbfOpsSessionLoader — direct SQL loader for ops quality review.

Loads all data associated with a synth_id from the session store, skill
store, artifact store, and error log.  This is NOT a vector retriever —
no cosine similarity, no embeddings.  It assembles a SessionBundle that
the KbfOpsReviewEngine consumes.

Falls back to filestore implementations (FilestoreSessionStore +
FilestoreSkillStore) when pool is None (laptop mode without ADB).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# SQL to query error log for a synth_id.  Wrapped in a try/except at call
# site to handle the case where the table doesn't exist yet (migration not run).
_SQL_ERRORS = """
    SELECT request_id, timestamp_utc, tool, error_type, message
    FROM KB_SHIM.KBF_ERROR_LOG
    WHERE extra_json LIKE :synth_pattern
       OR message LIKE :synth_pattern
    ORDER BY timestamp_utc
"""

# Simpler lookup if synth_id is stored as a top-level column (future).
_SQL_ERRORS_BY_SYNTH = """
    SELECT request_id, timestamp_utc, tool, error_type, message, extra_json
    FROM KB_SHIM.KBF_ERROR_LOG
    WHERE extra_json LIKE :synth_pattern
    ORDER BY timestamp_utc
"""


@dataclass
class SessionBundle:
    """All data needed for a quality review of a single authoring session."""

    synth_id: str
    persona: str
    skill_names: list[str]
    intent_description: str
    conversation_history: list[dict]
    state_progression: list[str]
    artifacts: dict[str, dict[str, str]]  # {skill_name: {artifact_type: content}}
    uploaded_files: list[dict]            # [{filename, content, artifact_id}]
    errors: list[dict]
    status: str


class KbfOpsSessionLoader:
    """Loads all data for a synth_id from ADB for ops review.

    Falls back to filesystem (FilestoreSessionStore + FilestoreSkillStore) when
    pool is None (laptop mode without ADB).

    Args:
        pool:           oracledb connection pool.  None for laptop/test mode.
        session_store:  SessionStore implementation (ADB or filestore).
        skill_store:    SkillStore implementation (ADB or filestore).
        artifact_store: ArtifactStore implementation or None.
    """

    # Internal user_id used when loading sessions without ownership enforcement.
    # The ops loader is privileged — it loads by synth_id regardless of user.
    _OPS_USER = "kbf-ops"

    def __init__(self, pool, session_store, skill_store, artifact_store=None) -> None:
        self._pool = pool
        self._session_store = session_store
        self._skill_store = skill_store
        self._artifact_store = artifact_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, synth_id: str) -> SessionBundle | None:
        """Return a SessionBundle with all data needed for review, or None if not found.

        Args:
            synth_id: The session ID to load (e.g. "synth-tpm-ec2fad6d").

        Returns:
            A populated SessionBundle, or None if the session does not exist.
        """
        session = self._load_session(synth_id)
        if session is None:
            log.info("KbfOpsSessionLoader: session not found — synth_id=%s", synth_id)
            return None

        persona = session.get("persona", "")
        status = session.get("status", "unknown")

        # Collect skill names: may be a single string or a list.
        raw_skill_name = session.get("skill_name") or session.get("skill_names") or ""
        if isinstance(raw_skill_name, list):
            skill_names = raw_skill_name
        elif raw_skill_name:
            skill_names = [raw_skill_name]
        else:
            skill_names = []

        intent_description = (
            session.get("intent_description")
            or session.get("intent")
            or ""
        )

        # Extract conversation turns.
        conversation_history = self._extract_conversation(session)

        # State progression: if session has history of states, extract them.
        state_progression = self._extract_state_progression(session)

        # Load skill artifacts for each skill name.
        artifacts: dict[str, dict[str, str]] = {}
        for sn in skill_names:
            artifacts[sn] = self._load_skill_artifacts(persona, sn)

        # If no skill names but persona is known, fall through with empty artifacts.

        # Load uploaded files via artifact_store.
        uploaded_files = self._load_uploaded_files(synth_id)

        # Load errors from KBF_ERROR_LOG.
        errors = self._load_errors(synth_id)

        return SessionBundle(
            synth_id=synth_id,
            persona=persona,
            skill_names=skill_names,
            intent_description=intent_description,
            conversation_history=conversation_history,
            state_progression=state_progression,
            artifacts=artifacts,
            uploaded_files=uploaded_files,
            errors=errors,
            status=status,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_session(self, synth_id: str) -> dict | None:
        """Load session dict, trying multiple user_ids for privilege escalation."""
        # AdbSessionStore.load requires user_id but ops reviewer is privileged.
        # Strategy: try with ops sentinel user; if that fails try the raw ADB query.
        session = self._session_store.load(synth_id, user_id=self._OPS_USER)
        if session is not None:
            return session

        # If the session was saved with a real user_id (not ops), we need a direct
        # lookup.  For ADB, query directly when pool is available.
        if self._pool is not None:
            session = self._load_session_direct(synth_id)
            if session is not None:
                return session

        # Filestore fallback: scan all user dirs.
        return self._load_session_scan_filestore(synth_id)

    def _load_session_direct(self, synth_id: str) -> dict | None:
        """Direct ADB query ignoring user_id ownership check."""
        import json

        _SQL = """
            SELECT synth_id, user_id, persona, skill_name, intent, state,
                   session_data, status
            FROM kb_shim.author_skill_sessions
            WHERE synth_id = :synth_id
        """
        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute(_SQL, {"synth_id": synth_id})
                    cols = [d[0].lower() for d in cur.description]
                    cur.rowfactory = lambda *vals: dict(zip(cols, vals))
                    row = cur.fetchone()
        except Exception as exc:
            log.warning("KbfOpsSessionLoader: direct ADB load failed: %s", exc)
            return None

        if row is None:
            return None

        raw = row["session_data"]
        try:
            session: dict = raw if isinstance(raw, dict) else json.loads(raw)
        except Exception:
            session = {}

        session["state"] = row["state"]
        session["status"] = row["status"]
        return session

    def _load_session_scan_filestore(self, synth_id: str) -> dict | None:
        """Scan filestore session dirs to find a session by synth_id.

        Only applicable when session_store is FilestoreSessionStore.
        """
        from pathlib import Path
        import json

        sessions_root = getattr(self._session_store, "_root", None)
        if sessions_root is None:
            return None

        sessions_root = Path(sessions_root)
        if not sessions_root.exists():
            return None

        # Each user has a subdir; look for synth_id.json in any of them.
        for user_dir in sessions_root.iterdir():
            if not user_dir.is_dir():
                continue
            candidate = user_dir / f"{synth_id}.json"
            if candidate.exists():
                try:
                    with open(candidate) as fh:
                        return json.load(fh)
                except Exception as exc:
                    log.warning("session file corrupt: %s — %s", candidate, exc)
        return None

    def _extract_conversation(self, session: dict) -> list[dict]:
        """Extract conversation turns from the session dict."""
        history = session.get("conversation_history") or []
        if isinstance(history, list):
            return history
        return []

    def _extract_state_progression(self, session: dict) -> list[str]:
        """Extract ordered list of states the session passed through."""
        # Some sessions store a state_history list; fall back to current state only.
        state_history = session.get("state_history") or []
        if isinstance(state_history, list) and state_history:
            return [str(s) for s in state_history]
        current_state = session.get("state")
        if current_state:
            return [str(current_state)]
        return []

    def _load_skill_artifacts(
        self, persona: str, skill_name: str
    ) -> dict[str, str]:
        """Load all 4 artifact types for a skill, returning what exists."""
        from framework.deploy.skill_store._base import ARTIFACT_TYPES  # noqa: PLC0415

        result: dict[str, str] = {}
        for artifact_type in ARTIFACT_TYPES:
            content = self._skill_store.read_artifact(persona, skill_name, artifact_type)
            if content is not None:
                result[artifact_type] = content
        return result

    def _load_uploaded_files(self, synth_id: str) -> list[dict]:
        """Load file contents for all artifacts uploaded during the session."""
        if self._artifact_store is None:
            return []

        try:
            artifact_metas = self._artifact_store.list_artifacts(synth_id)
        except Exception as exc:
            log.warning("KbfOpsSessionLoader: list_artifacts failed: %s", exc)
            return []

        uploaded: list[dict] = []
        for meta in artifact_metas:
            artifact_id = meta.get("artifact_id", "")
            filename = meta.get("filename", "")
            try:
                path = self._artifact_store.resolve(artifact_id)
                if path is None:
                    content = ""
                else:
                    content = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                log.warning(
                    "KbfOpsSessionLoader: could not read artifact %s: %s",
                    artifact_id, exc,
                )
                content = ""

            uploaded.append({
                "filename":    filename,
                "content":     content,
                "artifact_id": artifact_id,
            })

        return uploaded

    def _load_errors(self, synth_id: str) -> list[dict]:
        """Load error log entries for this synth_id.

        Queries KBF_ERROR_LOG where synth_id appears in extra_json or message.
        Returns empty list if table does not exist yet (migration not run).
        """
        if self._pool is None:
            return []

        import json as _json

        synth_pattern = f"%{synth_id}%"
        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        _SQL_ERRORS_BY_SYNTH,
                        {"synth_pattern": synth_pattern},
                    )
                    cols = [d[0].lower() for d in cur.description]
                    cur.rowfactory = lambda *vals: dict(zip(cols, vals))
                    rows = cur.fetchall()
        except Exception as exc:
            # Table may not exist yet — treat as empty
            log.debug("KbfOpsSessionLoader: error log query failed: %s", exc)
            return []

        results: list[dict] = []
        for row in rows:
            entry = {
                "request_id":    row.get("request_id", ""),
                "timestamp_utc": str(row.get("timestamp_utc", "")),
                "tool":          row.get("tool", ""),
                "error_type":    row.get("error_type", ""),
                "message":       row.get("message", ""),
            }
            # Try to parse extra_json for richer context
            raw_extra = row.get("extra_json")
            if raw_extra:
                try:
                    extra = raw_extra if isinstance(raw_extra, dict) else _json.loads(raw_extra)
                    entry.update(extra)
                except Exception:
                    pass
            results.append(entry)

        return results
