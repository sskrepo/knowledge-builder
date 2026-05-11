"""Abstract base class for author_skill session persistence (PDD V3 §16, Track D)."""
from __future__ import annotations
from abc import ABC, abstractmethod


class SessionStore(ABC):
    """Abstract session persistence for author_skill sessions.

    All methods operate on session dicts with snake_case keys,
    matching Python convention and the to_dict()/from_dict() contract
    of SkillBuilderConversation.

    The API layer converts snake_case -> camelCase before returning to clients.
    """

    @abstractmethod
    def save(self, session: dict, user_id: str, ttl_days: int = 7) -> None:
        """Persist (upsert) a session.

        Sets updated_at to now. Sets expires_at = now + ttl_days if status is
        in_progress. Creates any required parent directories or DB rows.
        """

    @abstractmethod
    def load(self, synth_id: str, user_id: str) -> dict | None:
        """Return session dict if it belongs to user_id, else None.

        Returns None for expired sessions (auto-expires in-progress sessions
        whose expires_at is in the past, setting status=expired).
        Returns None when synth_id does not exist.
        """

    @abstractmethod
    def list_for_user(self, user_id: str) -> list[dict]:
        """Return all sessions for user_id, ordered by updated_at descending."""

    @abstractmethod
    def abandon(self, synth_id: str, user_id: str) -> None:
        """Set status=abandoned and expires_at=None on the session.

        No content is removed. Committed artifacts in git are preserved.
        """

    @abstractmethod
    def expire_stale(self) -> int:
        """Expire in-progress sessions whose TTL has elapsed.

        Sets status=expired on sessions where expires_at < now() and
        status=in_progress. Returns the count of sessions expired.
        Called by the TTL cleanup background job.
        """
