"""Filestore-backed session store for dev/laptop mode (PDD V3 Track D-2)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ._base import SessionStore

log = logging.getLogger(__name__)


class FilestoreSessionStore(SessionStore):
    """Filestore session store for dev/laptop mode.

    Layout: {store_root}/sessions/{user_id}/{synth_id}.json

    All timestamps are ISO-8601 UTC strings. Ownership is verified by
    checking the user_id field stored inside the JSON file against the
    user_id passed by the caller.
    """

    def __init__(self, store_root: str | Path) -> None:
        self._root = Path(store_root) / "sessions"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, user_id: str, synth_id: str) -> Path:
        return self._root / user_id / f"{synth_id}.json"

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _write(self, path: Path, session: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(session, fh, indent=2)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    def save(self, session: dict, user_id: str, ttl_days: int = 7) -> None:
        """Upsert a session file.

        - Sets updated_at to now.
        - Sets expires_at = now + ttl_days when status is in_progress AND ttl_days > 0.
        - Creates parent directories automatically.
        - The caller's dict is not mutated (shallow copy is taken first).
        """
        synth_id = session.get("synth_id")
        if not synth_id:
            raise ValueError("session dict must contain 'synth_id'")

        session = dict(session)  # shallow copy — do not mutate caller's dict
        session["user_id"] = user_id
        session["updated_at"] = self._now()

        status = session.get("status", "in_progress")
        if status == "in_progress" and ttl_days > 0:
            expires_at = datetime.now(tz=timezone.utc) + timedelta(days=ttl_days)
            session["expires_at"] = expires_at.isoformat()
        elif "expires_at" not in session:
            # Non-in_progress sessions clear the TTL unless already set
            session["expires_at"] = None

        path = self._path(user_id, synth_id)
        self._write(path, session)
        log.debug("session saved: %s/%s (status=%s)", user_id, synth_id, status)

    def load(self, synth_id: str, user_id: str) -> dict | None:
        """Load a session file and verify ownership.

        Returns None if:
        - file does not exist
        - user_id does not match the stored user_id (ownership check)
        - session is in_progress and expires_at is in the past (auto-expires)
        """
        path = self._path(user_id, synth_id)
        if not path.exists():
            return None

        try:
            with open(path) as fh:
                session = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("failed to read session file %s: %s", path, exc)
            return None

        # Ownership check
        if session.get("user_id") != user_id:
            log.warning(
                "load denied: synth_id=%s belongs to user %s, requested by %s",
                synth_id,
                session.get("user_id"),
                user_id,
            )
            return None

        # Auto-expire check: only applies to in_progress sessions with an expires_at
        if session.get("status") == "in_progress":
            expires_at = self._parse_dt(session.get("expires_at"))
            if expires_at is not None and expires_at < datetime.now(tz=timezone.utc):
                log.info("session %s/%s expired — marking as expired", user_id, synth_id)
                session["status"] = "expired"
                # Persist the expired status; ttl_days=0 prevents re-setting expires_at
                self._write(path, {**session, "updated_at": self._now()})
                return None

        return session

    def list_for_user(self, user_id: str) -> list[dict]:
        """Return all sessions for user_id sorted by updated_at descending.

        Corrupt or unreadable files are skipped with a warning.
        """
        user_dir = self._root / user_id
        if not user_dir.exists():
            return []

        sessions: list[dict] = []
        for path in user_dir.glob("*.json"):
            try:
                with open(path) as fh:
                    sessions.append(json.load(fh))
            except Exception as exc:
                log.warning("failed to read session file %s: %s", path, exc)

        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions

    def abandon(self, synth_id: str, user_id: str) -> None:
        """Set status=abandoned and clear expires_at.

        If the session does not exist or belongs to a different user, this
        is a no-op (the caller should have verified existence beforehand).
        """
        session = self.load(synth_id, user_id=user_id)
        if session is None:
            log.warning(
                "abandon: session %s/%s not found or already expired", user_id, synth_id
            )
            return

        session["status"] = "abandoned"
        session["expires_at"] = None
        session["updated_at"] = self._now()

        path = self._path(user_id, synth_id)
        self._write(path, session)
        log.info("session abandoned: %s/%s", user_id, synth_id)

    def expire_stale(self) -> int:
        """Walk all session files and expire any in_progress sessions past their TTL.

        Returns count of sessions transitioned to status=expired.
        """
        count = 0
        now = datetime.now(tz=timezone.utc)

        for path in self._root.rglob("*.json"):
            try:
                with open(path) as fh:
                    session = json.load(fh)

                if session.get("status") != "in_progress":
                    continue

                expires_at = self._parse_dt(session.get("expires_at"))
                if expires_at is not None and expires_at < now:
                    session["status"] = "expired"
                    session["updated_at"] = now.isoformat()
                    with open(path, "w") as fh:
                        json.dump(session, fh, indent=2)
                    count += 1
                    log.info("expire_stale: expired session at %s", path)

            except Exception as exc:
                log.warning("expire_stale: failed to process %s: %s", path, exc)

        log.info("expire_stale: expired %d sessions", count)
        return count
