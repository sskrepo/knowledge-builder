"""KBF build version — reads git commit at import time.

Used by /healthz, /api/v1/version, and the startup banner so operators can
confirm which code revision is running without shelling into the process.

GIT_SHA   — short commit hash (7 hex chars) + "-dirty" if working tree is modified
BUILD_REF — "commit_subject (YYYY-MM-DD)" for the one-line startup banner
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    """Run a git command and return stripped stdout, or '' on failure."""
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=str(_REPO_ROOT),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return ""


def _build_git_sha() -> str:
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return "unknown"
    dirty = _git("status", "--porcelain")
    return sha + ("-dirty" if dirty else "")


def _build_ref() -> str:
    """Return 'abc1234 — Commit subject (YYYY-MM-DD)' for the startup banner."""
    sha = _git("rev-parse", "--short", "HEAD")
    subject = _git("log", "-1", "--format=%s")
    date = _git("log", "-1", "--format=%cs")  # YYYY-MM-DD
    if not sha:
        return "unknown"
    parts = [sha]
    if subject:
        parts.append(f"— {subject}")
    if date:
        parts.append(f"({date})")
    return " ".join(parts)


GIT_SHA: str = _build_git_sha()
BUILD_REF: str = _build_ref()
API_VERSION: str = "v1"
SCHEMA_VERSION: str = "1.0.0"
