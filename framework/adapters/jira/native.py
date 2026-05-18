"""Jira native (REST) adapter — full implementation.

Uses Jira REST API v2:
  - GET /rest/api/2/search?jql=... for listing
  - GET /rest/api/2/issue/{key}?expand=changelog,comments for fetch
  - Webhook receiver for change streaming

Per ADR-011. Untested against a live Jira instance — needs phase-1 integration
verification once auth tokens are populated.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

import re

from .._base import (
    Adapter, RawItem, RawItemRef, SourceQuery, ChangeEvent, HealthReport,
    AdapterWithIdentity, CanonicalRef, CanonicalResult, Unresolvable,
    UNRESOLVABLE_NOT_FOUND, UNRESOLVABLE_NO_ACCESS,
    UNRESOLVABLE_TRANSIENT, UNRESOLVABLE_INVALID_REF,
)
from .shared import resolve_token, to_raw_item

log = logging.getLogger(__name__)


class JiraNativeAdapter(AdapterWithIdentity):
    name = "jira:native"
    kind = "jira"
    mode = "native"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.token = resolve_token(cfg["auth"]["token_secret"])
        self.page_size = cfg.get("pagination", {}).get("page_size", 100)
        self.rpm = cfg.get("rate_limit", {}).get("requests_per_minute", 200)
        self.issuetypes = cfg.get("issuetypes_supported", [])
        self._session = self._build_session()
        self._last_request = 0.0

    def _build_session(self):
        try:
            import requests
        except ImportError:
            log.warning("requests package not installed; adapter is stub-mode")
            return None
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        return s

    def _throttle(self) -> None:
        if not self.rpm:
            return
        min_interval = 60.0 / self.rpm
        elapsed = time.time() - self._last_request
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request = time.time()

    def healthcheck(self) -> HealthReport:
        if self._session is None:
            return HealthReport(False, self.mode, "requests not installed")
        try:
            r = self._session.get(f"{self.base_url}/rest/api/2/serverInfo", timeout=10)
            healthy = r.status_code == 200
            return HealthReport(healthy, self.mode,
                                f"{r.status_code} from /rest/api/2/serverInfo")
        except Exception as e:
            return HealthReport(False, self.mode, str(e))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if self._session is None:
            return
        if not q.jql:
            raise ValueError("Jira list requires SourceQuery.jql")

        start = 0
        while True:
            self._throttle()
            params = {
                "jql": q.jql,
                "fields": "key,updated,summary,issuetype",
                "startAt": start,
                "maxResults": self.page_size,
            }
            r = self._session.get(f"{self.base_url}/rest/api/2/search",
                                  params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            issues = data.get("issues", [])
            for issue in issues:
                yield RawItemRef(
                    kind="jira_issue",
                    source="jira",
                    source_id=issue["key"],
                    last_modified=_parse_iso(issue.get("fields", {}).get("updated")),
                )
            start += len(issues)
            if start >= data.get("total", 0) or not issues:
                break

    def fetch(self, ref: RawItemRef) -> RawItem:
        if self._session is None:
            raise RuntimeError("requests not installed; cannot fetch")
        self._throttle()
        params = {"expand": "changelog,comments"}
        r = self._session.get(
            f"{self.base_url}/rest/api/2/issue/{ref.source_id}",
            params=params, timeout=30,
        )
        r.raise_for_status()
        payload = r.json()

        fields = payload.get("fields", {})
        metadata = {
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "author": (fields.get("creator") or {}).get("displayName"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "labels": fields.get("labels", []),
            "components": [c.get("name") for c in fields.get("components", [])],
            "issuetype": (fields.get("issuetype") or {}).get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "status": (fields.get("status") or {}).get("name"),
            "project": (fields.get("project") or {}).get("key"),
        }
        return to_raw_item(payload=payload, metadata=metadata, source_id=ref.source_id)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        """Polls JQL `updated >= since` as fallback when webhooks aren't wired."""
        jql = f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
        for ref in self.list(SourceQuery(jql=jql)):
            yield ChangeEvent(
                kind="updated",
                source="jira",
                source_id=ref.source_id,
                timestamp=ref.last_modified or datetime.utcnow(),
            )

    # ------------------------------------------------------------------
    # ADR-039 (DECISION-020): canonical_identity implementation
    # ------------------------------------------------------------------

    def canonical_identity(self, reference: str, resource_type: str) -> CanonicalResult:
        """Resolve any Jira reference to a CanonicalRef with the numeric internal ID.

        ADR-039 §5: canonical identity = numeric internal ID (issue.id), NOT the
        issue key. Issue keys change on cross-project moves; issue.id is invariant.

        Supported resource_types: issue, epic, sprint, filter, project.
        Raw JQL strings are not resolvable as pinned identities — returns Unresolvable
        with ERROR_INVALID_REF directing authors to use saved filters instead.

        When self._session is None (unit-test / no credentials), the numeric ID
        fast-path is accepted as-is (no API validation call).
        """
        resource_type = resource_type.lower()
        return _jira_resolve_canonical(
            reference=reference,
            resource_type=resource_type,
            session=self._session,
            base_url=self.base_url,
        )


# ---------------------------------------------------------------------------
# Jira canonical identity resolution helpers (ADR-039 §5)
# ---------------------------------------------------------------------------

# Pattern to detect an issue key: PROJECT-NNN (letters, optional digits/dash)
_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

# Filter URL: /issues/?filter=NNN or /issues?filter=NNN
_FILTER_ID_PATTERN = re.compile(r"[?&]filter=(\d+)", re.IGNORECASE)

# Sprint URL: /boards/NNN/sprints/MMM or agile API sprint id
_SPRINT_ID_PATTERN = re.compile(r"/sprints?/(\d+)", re.IGNORECASE)

# Project URL: /projects/{KEY} or /projects/{id}
_PROJECT_URL_PATTERN = re.compile(r"/projects?/([A-Z0-9_\-]+)", re.IGNORECASE)

# /browse/{ISSUE-KEY} form
_BROWSE_PATTERN = re.compile(r"/browse/([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


def _jira_resolve_canonical(
    reference: str,
    resource_type: str,
    session,       # requests.Session or None
    base_url: str,
) -> CanonicalResult:
    """Resolve a Jira reference to a CanonicalRef with numeric internal ID.

    Per ADR-039 §5: canonical_id = issue.id (numeric string), not the key.
    """
    if resource_type in ("issue", "epic"):
        return _resolve_jira_issue(reference, resource_type, session, base_url)
    elif resource_type == "filter":
        return _resolve_jira_filter(reference, session, base_url)
    elif resource_type == "sprint":
        return _resolve_jira_sprint(reference, session, base_url)
    elif resource_type == "project":
        return _resolve_jira_project(reference, session, base_url)
    else:
        return Unresolvable(
            connector_id="jira",
            resource_type=resource_type,
            reference=reference,
            reason=UNRESOLVABLE_INVALID_REF,
            detail=(
                f"Unknown Jira resource_type: {resource_type!r}. "
                "Supported types: issue, epic, sprint, filter, project."
            ),
            retryable=False,
        )


def _resolve_jira_issue(
    reference: str,
    resource_type: str,
    session,
    base_url: str,
) -> CanonicalResult:
    """Resolve a Jira issue or epic reference to its numeric internal issue.id."""
    # Fast path: bare numeric string → already the internal id
    stripped = reference.strip()
    if stripped.isdigit():
        return CanonicalRef(
            connector_id="jira",
            resource_type=resource_type,
            canonical_id=stripped,
            display_hint="",
        )

    # Extract issue key from URL (/browse/PROJECT-NNN) or bare key
    key: str | None = None
    m = _BROWSE_PATTERN.search(reference)
    if m:
        key = m.group(1)
    else:
        m = _ISSUE_KEY_PATTERN.search(reference)
        if m:
            key = m.group(1)

    if key is None:
        # Raw JQL? Explicitly reject — raw JQL has no stable canonical ID
        if re.search(r"\b(AND|OR|ORDER BY|project\s*=|issuetype\s*=)\b", reference, re.IGNORECASE):
            return Unresolvable(
                connector_id="jira",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_INVALID_REF,
                detail=(
                    "Raw JQL does not have a stable canonical_id. "
                    "Use a saved filter (resource_type=filter) for pinning, "
                    "or use ask_parameterized mode (ADR-032) for JQL-based skills."
                ),
                retryable=False,
            )
        return Unresolvable(
            connector_id="jira",
            resource_type=resource_type,
            reference=reference,
            reason=UNRESOLVABLE_INVALID_REF,
            detail=(
                f"Cannot parse Jira issue reference: {reference!r}. "
                "Provide an issue key (PROJECT-NNN), a /browse/ URL, "
                "or the bare numeric issue.id."
            ),
            retryable=False,
        )

    # Key found — resolve to numeric id via API
    if session is None:
        # No session: cannot validate — return Unresolvable(TRANSIENT)
        return Unresolvable(
            connector_id="jira",
            resource_type=resource_type,
            reference=reference,
            reason=UNRESOLVABLE_TRANSIENT,
            detail=(
                f"Jira issue key {key!r} requires a live Jira session to resolve "
                "to a stable internal ID. Retry when credentials are available."
            ),
            retryable=True,
        )

    try:
        r = session.get(
            f"{base_url}/rest/api/2/issue/{key}",
            params={"fields": "id,summary"},
            timeout=15,
        )
        if r.status_code == 404:
            return Unresolvable(
                connector_id="jira",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NOT_FOUND,
                detail=f"Jira issue {key!r} not found.",
                retryable=False,
            )
        if r.status_code in (401, 403):
            return Unresolvable(
                connector_id="jira",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NO_ACCESS,
                detail=f"Access denied to Jira issue {key!r}.",
                retryable=False,
            )
        r.raise_for_status()
        data = r.json()
        issue_id = str(data["id"])  # numeric internal id — invariant across key changes
        summary = (data.get("fields") or {}).get("summary", "")
        return CanonicalRef(
            connector_id="jira",
            resource_type=resource_type,
            canonical_id=issue_id,
            display_hint=f"{key}: {summary}",
        )
    except (CanonicalRef, Unresolvable):
        raise
    except Exception as exc:
        return Unresolvable(
            connector_id="jira",
            resource_type=resource_type,
            reference=reference,
            reason=UNRESOLVABLE_TRANSIENT,
            detail=f"Network error resolving {key!r}: {type(exc).__name__}: {exc}",
            retryable=True,
        )


def _resolve_jira_filter(reference: str, session, base_url: str) -> CanonicalResult:
    """Resolve a Jira filter reference to its numeric filter.id."""
    stripped = reference.strip()
    m = _FILTER_ID_PATTERN.search(reference)
    filter_id = m.group(1) if m else (stripped if stripped.isdigit() else None)

    if filter_id is None:
        return Unresolvable(
            connector_id="jira",
            resource_type="filter",
            reference=reference,
            reason=UNRESOLVABLE_INVALID_REF,
            detail=(
                f"Cannot parse Jira filter reference: {reference!r}. "
                "Provide a filter URL (?filter=NNN) or a bare numeric filter ID."
            ),
            retryable=False,
        )

    if session is None:
        return CanonicalRef(
            connector_id="jira", resource_type="filter",
            canonical_id=filter_id, display_hint="",
        )

    try:
        r = session.get(f"{base_url}/rest/api/2/filter/{filter_id}", timeout=15)
        if r.status_code == 404:
            return Unresolvable(connector_id="jira", resource_type="filter",
                                reference=reference, reason=UNRESOLVABLE_NOT_FOUND,
                                detail=f"Filter {filter_id!r} not found.", retryable=False)
        if r.status_code in (401, 403):
            return Unresolvable(connector_id="jira", resource_type="filter",
                                reference=reference, reason=UNRESOLVABLE_NO_ACCESS,
                                detail=f"Access denied to filter {filter_id!r}.", retryable=False)
        r.raise_for_status()
        name = r.json().get("name", "")
        return CanonicalRef(connector_id="jira", resource_type="filter",
                            canonical_id=filter_id, display_hint=name)
    except Exception as exc:
        return Unresolvable(connector_id="jira", resource_type="filter",
                            reference=reference, reason=UNRESOLVABLE_TRANSIENT,
                            detail=f"Network error: {exc}", retryable=True)


def _resolve_jira_sprint(reference: str, session, base_url: str) -> CanonicalResult:
    """Resolve a Jira sprint reference to its numeric sprint.id."""
    stripped = reference.strip()
    m = _SPRINT_ID_PATTERN.search(reference)
    sprint_id = m.group(1) if m else (stripped if stripped.isdigit() else None)

    if sprint_id is None:
        return Unresolvable(
            connector_id="jira", resource_type="sprint", reference=reference,
            reason=UNRESOLVABLE_INVALID_REF,
            detail=f"Cannot parse sprint reference: {reference!r}. Provide a numeric sprint ID.",
            retryable=False,
        )
    # Accept numeric sprint ID without further validation (Agile API varies by plugin)
    return CanonicalRef(connector_id="jira", resource_type="sprint",
                        canonical_id=sprint_id, display_hint="")


def _resolve_jira_project(reference: str, session, base_url: str) -> CanonicalResult:
    """Resolve a Jira project reference to its numeric project.id."""
    stripped = reference.strip()
    # Numeric: accept as-is
    if stripped.isdigit():
        return CanonicalRef(connector_id="jira", resource_type="project",
                            canonical_id=stripped, display_hint="")

    # Extract key from URL or bare key
    m = _PROJECT_URL_PATTERN.search(reference)
    project_key = m.group(1).upper() if m else (stripped.upper() if stripped.isalpha() else None)

    if project_key is None:
        return Unresolvable(
            connector_id="jira", resource_type="project", reference=reference,
            reason=UNRESOLVABLE_INVALID_REF,
            detail=f"Cannot parse Jira project reference: {reference!r}.",
            retryable=False,
        )

    if session is None:
        return Unresolvable(
            connector_id="jira", resource_type="project", reference=reference,
            reason=UNRESOLVABLE_TRANSIENT,
            detail=f"Project key {project_key!r} requires live session to resolve to numeric ID.",
            retryable=True,
        )

    try:
        r = session.get(f"{base_url}/rest/api/2/project/{project_key}",
                        params={"fields": "id,name"}, timeout=15)
        if r.status_code == 404:
            return Unresolvable(connector_id="jira", resource_type="project",
                                reference=reference, reason=UNRESOLVABLE_NOT_FOUND,
                                detail=f"Project {project_key!r} not found.", retryable=False)
        if r.status_code in (401, 403):
            return Unresolvable(connector_id="jira", resource_type="project",
                                reference=reference, reason=UNRESOLVABLE_NO_ACCESS,
                                detail=f"Access denied to project {project_key!r}.", retryable=False)
        r.raise_for_status()
        data = r.json()
        project_id = str(data["id"])
        return CanonicalRef(connector_id="jira", resource_type="project",
                            canonical_id=project_id, display_hint=data.get("name", ""))
    except Exception as exc:
        return Unresolvable(connector_id="jira", resource_type="project",
                            reference=reference, reason=UNRESOLVABLE_TRANSIENT,
                            detail=f"Network error: {exc}", retryable=True)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
