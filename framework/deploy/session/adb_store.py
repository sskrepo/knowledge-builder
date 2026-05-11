"""Oracle ADB-backed session store for staging/production (PDD V3 Track D-3).

Reads and writes kb_shim.author_skill_sessions (see PDD V3 §16 for DDL).
Column names use snake_case (DB convention). The session dict contract is
also snake_case. The API layer converts to camelCase before responding.

When pool=None (stub mode), all operations are safe no-ops. This allows
the store to be constructed at startup without a live ADB connection.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from ._base import SessionStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL templates (Oracle syntax)
# ---------------------------------------------------------------------------

_SQL_UPSERT = """
    MERGE INTO kb_shim.author_skill_sessions tgt
    USING DUAL ON (tgt.synth_id = :synth_id)
    WHEN MATCHED THEN UPDATE SET
        state        = :state,
        persona      = :persona,
        skill_name   = :skill_name,
        session_data = :session_data,
        updated_at   = :updated_at,
        expires_at   = :expires_at,
        status       = :status
    WHEN NOT MATCHED THEN INSERT
        (synth_id, user_id, persona, skill_name, intent, state,
         session_data, created_at, updated_at, expires_at, status)
    VALUES
        (:synth_id, :user_id, :persona, :skill_name, :intent, :state,
         :session_data, :created_at, :updated_at, :expires_at, :status)
"""

_SQL_LOAD = """
    SELECT synth_id, user_id, persona, skill_name, intent, state,
           session_data, created_at, updated_at, expires_at, status
    FROM kb_shim.author_skill_sessions
    WHERE synth_id = :synth_id AND user_id = :user_id
"""

_SQL_LIST = """
    SELECT synth_id, persona, skill_name, intent, state,
           created_at, updated_at, expires_at, status,
           JSON_VALUE(session_data, '$.progress') AS progress_json
    FROM kb_shim.author_skill_sessions
    WHERE user_id = :user_id
    ORDER BY updated_at DESC
"""

_SQL_ABANDON = """
    UPDATE kb_shim.author_skill_sessions
    SET status     = 'abandoned',
        expires_at = NULL,
        updated_at = :now
    WHERE synth_id = :synth_id
      AND user_id  = :user_id
"""

_SQL_EXPIRE_STALE = """
    UPDATE kb_shim.author_skill_sessions
    SET status     = 'expired',
        updated_at = SYSTIMESTAMP
    WHERE status    = 'in_progress'
      AND expires_at < SYSTIMESTAMP
"""


class AdbSessionStore(SessionStore):
    """Oracle ADB-backed session store.

    pool: oracledb connection pool (synchronous or async). When None, all
    operations are no-ops — stub mode for dev/testing without a live ADB.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    def save(self, session: dict, user_id: str, ttl_days: int = 7) -> None:
        """Upsert session into kb_shim.author_skill_sessions.

        Stores the full session dict as JSON in session_data column.
        Top-level metadata columns are populated from the dict for
        efficient querying without unpacking session_data.
        """
        if self._pool is None:
            log.warning("AdbSessionStore: no pool configured — save is a no-op (stub mode)")
            return

        now = self._now()
        status = session.get("status", "in_progress")
        expires_at: str | None = None
        if status == "in_progress" and ttl_days > 0:
            expires_at = (
                datetime.now(tz=timezone.utc) + timedelta(days=ttl_days)
            ).isoformat()

        session_data_json = json.dumps(session)

        params = {
            "synth_id":     session["synth_id"],
            "user_id":      user_id,
            "persona":      session.get("persona"),
            "skill_name":   session.get("skill_name"),
            "intent":       session.get("intent_description") or session.get("intent", ""),
            "state":        session.get("state"),
            "session_data": session_data_json,
            "created_at":   session.get("created_at", now),
            "updated_at":   now,
            "expires_at":   expires_at,
            "status":       status,
        }

        with self._pool.acquire() as conn:
            conn.execute(_SQL_UPSERT, params)
            conn.commit()

        log.debug(
            "AdbSessionStore.save: synth_id=%s user_id=%s status=%s",
            session["synth_id"], user_id, status,
        )

    def load(self, synth_id: str, user_id: str) -> dict | None:
        """Load session from ADB and auto-expire if TTL elapsed."""
        if self._pool is None:
            return None

        with self._pool.acquire() as conn:
            row = conn.fetchone(_SQL_LOAD, {"synth_id": synth_id, "user_id": user_id})

        if row is None:
            return None

        # Deserialize the full session dict from the session_data JSON column
        session: dict = json.loads(row["session_data"])

        # Overlay top-level metadata columns — they are authoritative and may
        # differ from session_data if an update occurred outside the session dict
        session["state"]      = row["state"]
        session["status"]     = row["status"]
        session["expires_at"] = str(row["expires_at"]) if row["expires_at"] else None
        session["updated_at"] = str(row["updated_at"])

        # Auto-expire check
        expires_at_val = row["expires_at"]
        if expires_at_val and session.get("status") == "in_progress":
            if isinstance(expires_at_val, str):
                expires_dt = datetime.fromisoformat(expires_at_val)
            else:
                # oracledb may return a datetime object
                expires_dt = expires_at_val
            if expires_dt < datetime.now(tz=timezone.utc):
                log.info(
                    "AdbSessionStore.load: session %s/%s expired — marking expired",
                    user_id, synth_id,
                )
                self.abandon(synth_id, user_id)
                return None

        return session

    def list_for_user(self, user_id: str) -> list[dict]:
        """Return lightweight session summaries for a user, newest first."""
        if self._pool is None:
            return []

        with self._pool.acquire() as conn:
            rows = conn.fetchall(_SQL_LIST, {"user_id": user_id})

        sessions: list[dict] = []
        for row in rows:
            sessions.append({
                "synth_id":   row["synth_id"],
                "persona":    row["persona"],
                "skill_name": row["skill_name"],
                "intent":     row["intent"],
                "state":      row["state"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "expires_at": str(row["expires_at"]) if row["expires_at"] else None,
                "status":     row["status"],
                "progress":   json.loads(row["progress_json"]) if row["progress_json"] else {},
            })
        return sessions

    def abandon(self, synth_id: str, user_id: str) -> None:
        """Set status=abandoned and clear expires_at."""
        if self._pool is None:
            return

        with self._pool.acquire() as conn:
            conn.execute(
                _SQL_ABANDON,
                {"synth_id": synth_id, "user_id": user_id, "now": self._now()},
            )
            conn.commit()

        log.info("AdbSessionStore.abandon: %s/%s", user_id, synth_id)

    def expire_stale(self) -> int:
        """Bulk-expire all in_progress sessions past their TTL. Returns row count."""
        if self._pool is None:
            return 0

        with self._pool.acquire() as conn:
            cursor = conn.execute(_SQL_EXPIRE_STALE)
            count = cursor.rowcount
            conn.commit()

        log.info("AdbSessionStore.expire_stale: expired %d sessions", count)
        return count
