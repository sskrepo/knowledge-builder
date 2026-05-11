"""Tests for framework.deploy.session — FilestoreSessionStore.

Uses pytest's tmp_path fixture so all files live under a temporary directory
that is cleaned up automatically after each test. No external services needed.

Run:
    cd /Users/sravansunkaranam/github/Knowledgebase/.claude/worktrees/agitated-villani-eeed95
    python -m pytest framework/tests/test_session_store.py -v
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from framework.deploy.session.filestore import FilestoreSessionStore
from framework.deploy.session._base import SessionStore
from framework.deploy.session.factory import build_session_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> FilestoreSessionStore:
    """Fresh FilestoreSessionStore rooted under a temp directory."""
    return FilestoreSessionStore(store_root=tmp_path)


def _make_session(
    synth_id: str = "synth-tpm-skill-20260510-abcd",
    user_id: str = "user-001",
    persona: str = "tpm",
    skill_name: str = "weekly_exec_review",
    status: str = "in_progress",
    state: str = "CONFIGURE_SOURCES",
    **extra,
) -> dict:
    """Build a minimal valid session dict for testing."""
    now = datetime.now(tz=timezone.utc).isoformat()
    return {
        "synth_id": synth_id,
        "user_id": user_id,
        "persona": persona,
        "skill_name": skill_name,
        "intent_description": "I want to build a weekly exec review skill",
        "state": state,
        "status": status,
        "created_at": now,
        "updated_at": now,
        **extra,
    }


# ---------------------------------------------------------------------------
# Test: save() creates the JSON file in the correct path
# ---------------------------------------------------------------------------


def test_save_creates_file(store: FilestoreSessionStore, tmp_path: Path) -> None:
    session = _make_session()
    store.save(session, user_id="user-001")

    expected_path = tmp_path / "sessions" / "user-001" / "synth-tpm-skill-20260510-abcd.json"
    assert expected_path.exists(), "session file not created at expected path"

    with open(expected_path) as fh:
        data = json.load(fh)
    assert data["synth_id"] == "synth-tpm-skill-20260510-abcd"
    assert data["user_id"] == "user-001"


def test_save_creates_parent_dirs(store: FilestoreSessionStore, tmp_path: Path) -> None:
    """Directories are created on-demand even for new user_ids."""
    session = _make_session(user_id="new-user-xyz")
    store.save(session, user_id="new-user-xyz")

    expected_dir = tmp_path / "sessions" / "new-user-xyz"
    assert expected_dir.is_dir()


def test_save_sets_expires_at_for_in_progress(store: FilestoreSessionStore, tmp_path: Path) -> None:
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001", ttl_days=7)

    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)

    assert "expires_at" in data
    expires = datetime.fromisoformat(data["expires_at"])
    now = datetime.now(tz=timezone.utc)
    # Should expire roughly 7 days from now (allow a 5-second window either side)
    assert expires > now + timedelta(days=6, hours=23)
    assert expires < now + timedelta(days=7, seconds=5)


def test_save_does_not_set_expires_at_for_non_in_progress(
    store: FilestoreSessionStore, tmp_path: Path
) -> None:
    session = _make_session(status="abandoned")
    store.save(session, user_id="user-001", ttl_days=7)

    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)

    # Non-in_progress sessions should not have an unexpired expires_at
    assert data.get("expires_at") is None


# ---------------------------------------------------------------------------
# Test: load() returns the session dict
# ---------------------------------------------------------------------------


def test_load_returns_session(store: FilestoreSessionStore) -> None:
    session = _make_session()
    store.save(session, user_id="user-001")

    loaded = store.load("synth-tpm-skill-20260510-abcd", user_id="user-001")
    assert loaded is not None
    assert loaded["synth_id"] == "synth-tpm-skill-20260510-abcd"
    assert loaded["persona"] == "tpm"


def test_load_returns_none_for_nonexistent(store: FilestoreSessionStore) -> None:
    result = store.load("does-not-exist", user_id="user-001")
    assert result is None


# ---------------------------------------------------------------------------
# Test: load() returns None for wrong user_id (ownership check)
# ---------------------------------------------------------------------------


def test_load_returns_none_for_wrong_user_id(store: FilestoreSessionStore) -> None:
    session = _make_session(user_id="user-001")
    store.save(session, user_id="user-001")

    # Different user should get None
    result = store.load("synth-tpm-skill-20260510-abcd", user_id="user-999")
    assert result is None


# ---------------------------------------------------------------------------
# Test: load() returns None for expired session (expires_at in the past)
# ---------------------------------------------------------------------------


def test_load_returns_none_for_expired_session(
    store: FilestoreSessionStore, tmp_path: Path
) -> None:
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001", ttl_days=7)

    # Manually overwrite the expires_at to be in the past
    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)
    past = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()
    data["expires_at"] = past
    with open(path, "w") as fh:
        json.dump(data, fh)

    result = store.load(session["synth_id"], user_id="user-001")
    assert result is None, "expired session should return None"


def test_load_expired_session_updates_status_to_expired(
    store: FilestoreSessionStore, tmp_path: Path
) -> None:
    """After auto-expiry on load(), the file should reflect status=expired."""
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001", ttl_days=7)

    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)
    past = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()
    data["expires_at"] = past
    with open(path, "w") as fh:
        json.dump(data, fh)

    store.load(session["synth_id"], user_id="user-001")  # triggers auto-expire

    with open(path) as fh:
        updated = json.load(fh)
    assert updated["status"] == "expired"


# ---------------------------------------------------------------------------
# Test: list_for_user() returns sessions sorted by updated_at desc
# ---------------------------------------------------------------------------


def test_list_for_user_returns_sessions_sorted_desc(store: FilestoreSessionStore) -> None:
    # Save three sessions with different updated_at values
    s1 = _make_session(synth_id="synth-001")
    s2 = _make_session(synth_id="synth-002")
    s3 = _make_session(synth_id="synth-003")

    store.save(s1, user_id="user-001")
    time.sleep(0.01)  # ensure distinct timestamps
    store.save(s2, user_id="user-001")
    time.sleep(0.01)
    store.save(s3, user_id="user-001")

    sessions = store.list_for_user("user-001")
    assert len(sessions) == 3
    # Most recently updated first
    assert sessions[0]["synth_id"] == "synth-003"
    assert sessions[1]["synth_id"] == "synth-002"
    assert sessions[2]["synth_id"] == "synth-001"


def test_list_for_user_returns_empty_for_unknown_user(store: FilestoreSessionStore) -> None:
    result = store.list_for_user("ghost-user")
    assert result == []


def test_list_for_user_returns_only_own_sessions(store: FilestoreSessionStore) -> None:
    store.save(_make_session(synth_id="synth-A", user_id="user-001"), user_id="user-001")
    store.save(_make_session(synth_id="synth-B", user_id="user-002"), user_id="user-002")

    results = store.list_for_user("user-001")
    assert len(results) == 1
    assert results[0]["synth_id"] == "synth-A"


# ---------------------------------------------------------------------------
# Test: abandon() sets status=abandoned
# ---------------------------------------------------------------------------


def test_abandon_sets_status_abandoned(store: FilestoreSessionStore) -> None:
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001")

    store.abandon("synth-tpm-skill-20260510-abcd", user_id="user-001")

    loaded = store.load("synth-tpm-skill-20260510-abcd", user_id="user-001")
    assert loaded is not None
    assert loaded["status"] == "abandoned"


def test_abandon_clears_expires_at(store: FilestoreSessionStore, tmp_path: Path) -> None:
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001", ttl_days=7)

    store.abandon("synth-tpm-skill-20260510-abcd", user_id="user-001")

    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)
    assert data.get("expires_at") is None


def test_abandon_noop_for_nonexistent(store: FilestoreSessionStore) -> None:
    # Should not raise even if session does not exist
    store.abandon("does-not-exist", user_id="user-001")


# ---------------------------------------------------------------------------
# Test: expire_stale() marks stale sessions as expired
# ---------------------------------------------------------------------------


def test_expire_stale_marks_stale_sessions(
    store: FilestoreSessionStore, tmp_path: Path
) -> None:
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001", ttl_days=7)

    # Manually backdate expires_at
    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    data["expires_at"] = past
    with open(path, "w") as fh:
        json.dump(data, fh)

    expired_count = store.expire_stale()
    assert expired_count == 1

    with open(path) as fh:
        updated = json.load(fh)
    assert updated["status"] == "expired"


def test_expire_stale_skips_non_in_progress(
    store: FilestoreSessionStore, tmp_path: Path
) -> None:
    """Already-abandoned or committed sessions should not be touched."""
    session = _make_session(status="committed")
    store.save(session, user_id="user-001", ttl_days=0)

    # Backdate just in case the file has an expires_at
    path = tmp_path / "sessions" / "user-001" / f"{session['synth_id']}.json"
    with open(path) as fh:
        data = json.load(fh)
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    data["expires_at"] = past
    with open(path, "w") as fh:
        json.dump(data, fh)

    expired_count = store.expire_stale()
    assert expired_count == 0


def test_expire_stale_returns_count(store: FilestoreSessionStore, tmp_path: Path) -> None:
    """Expire multiple stale sessions and verify count."""
    for i in range(3):
        session = _make_session(synth_id=f"synth-{i:03d}", status="in_progress")
        store.save(session, user_id="user-001", ttl_days=7)

        path = tmp_path / "sessions" / "user-001" / f"synth-{i:03d}.json"
        with open(path) as fh:
            data = json.load(fh)
        past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        data["expires_at"] = past
        with open(path, "w") as fh:
            json.dump(data, fh)

    count = store.expire_stale()
    assert count == 3


def test_expire_stale_leaves_fresh_sessions(
    store: FilestoreSessionStore, tmp_path: Path
) -> None:
    """Sessions with future expires_at must not be touched."""
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001", ttl_days=7)

    expired_count = store.expire_stale()
    assert expired_count == 0

    loaded = store.load(session["synth_id"], user_id="user-001")
    assert loaded is not None
    assert loaded["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Test: save() with existing session updates updated_at
# ---------------------------------------------------------------------------


def test_save_updates_updated_at(store: FilestoreSessionStore) -> None:
    session = _make_session()
    store.save(session, user_id="user-001")

    first = store.load("synth-tpm-skill-20260510-abcd", user_id="user-001")
    assert first is not None
    first_updated_at = first["updated_at"]

    time.sleep(0.05)  # ensure clock advances

    updated = dict(first)
    updated["state"] = "PREVIEW"
    store.save(updated, user_id="user-001")

    second = store.load("synth-tpm-skill-20260510-abcd", user_id="user-001")
    assert second is not None
    assert second["updated_at"] != first_updated_at, "updated_at should change on re-save"
    assert second["state"] == "PREVIEW"


def test_save_does_not_mutate_caller_dict(store: FilestoreSessionStore) -> None:
    session = _make_session()
    original_updated_at = session["updated_at"]
    original_id = id(session)

    store.save(session, user_id="user-001")

    # Caller's dict object identity and updated_at value must be unchanged
    assert id(session) == original_id, "save() must not replace the caller's dict"
    assert session["updated_at"] == original_updated_at, (
        "save() must not mutate updated_at in the caller's dict"
    )


# ---------------------------------------------------------------------------
# Test: load() after abandon() still returns session (with status=abandoned)
# ---------------------------------------------------------------------------


def test_load_after_abandon_returns_abandoned_session(store: FilestoreSessionStore) -> None:
    session = _make_session(status="in_progress")
    store.save(session, user_id="user-001")

    store.abandon("synth-tpm-skill-20260510-abcd", user_id="user-001")

    # load() should still return the session (now status=abandoned)
    loaded = store.load("synth-tpm-skill-20260510-abcd", user_id="user-001")
    assert loaded is not None, "abandoned session should still be loadable"
    assert loaded["status"] == "abandoned"


# ---------------------------------------------------------------------------
# Test: SessionStore ABC enforcement
# ---------------------------------------------------------------------------


def test_session_store_is_abstract() -> None:
    """FilestoreSessionStore must be a subclass of the ABC."""
    assert issubclass(FilestoreSessionStore, SessionStore)


# ---------------------------------------------------------------------------
# Test: factory selects filestore by default
# ---------------------------------------------------------------------------


def test_factory_returns_filestore_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KBF_STORE_BACKEND", raising=False)
    monkeypatch.setenv("KBF_STORE_ROOT", str(tmp_path))

    result = build_session_store(pool=None)
    assert isinstance(result, FilestoreSessionStore)


def test_factory_returns_filestore_when_explicitly_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KBF_STORE_BACKEND", "filestore")
    monkeypatch.setenv("KBF_STORE_ROOT", str(tmp_path))

    result = build_session_store(pool=None)
    assert isinstance(result, FilestoreSessionStore)


def test_factory_returns_adb_store_when_set(monkeypatch) -> None:
    from framework.deploy.session.adb_store import AdbSessionStore

    monkeypatch.setenv("KBF_STORE_BACKEND", "adb")

    result = build_session_store(pool=None)
    assert isinstance(result, AdbSessionStore)


# ---------------------------------------------------------------------------
# Test: AdbSessionStore stub mode (pool=None)
# ---------------------------------------------------------------------------


def test_adb_store_stub_save_is_noop() -> None:
    from framework.deploy.session.adb_store import AdbSessionStore

    store = AdbSessionStore(pool=None)
    session = _make_session()
    store.save(session, user_id="user-001")  # should not raise


def test_adb_store_stub_load_returns_none() -> None:
    from framework.deploy.session.adb_store import AdbSessionStore

    store = AdbSessionStore(pool=None)
    assert store.load("any-id", user_id="user-001") is None


def test_adb_store_stub_list_returns_empty() -> None:
    from framework.deploy.session.adb_store import AdbSessionStore

    store = AdbSessionStore(pool=None)
    assert store.list_for_user("user-001") == []


def test_adb_store_stub_abandon_is_noop() -> None:
    from framework.deploy.session.adb_store import AdbSessionStore

    store = AdbSessionStore(pool=None)
    store.abandon("any-id", user_id="user-001")  # should not raise


def test_adb_store_stub_expire_stale_returns_zero() -> None:
    from framework.deploy.session.adb_store import AdbSessionStore

    store = AdbSessionStore(pool=None)
    assert store.expire_stale() == 0
