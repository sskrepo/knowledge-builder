"""Shared helpers for Confluence adapters.

ADR-039 (DECISION-020): adds _resolve_to_numeric_id() — the canonical
identity resolution algorithm for all Confluence adapter modes. Called by
canonical_identity() in native.py, mcp.py, and emcp_direct.py. All forms
of Confluence reference (URL, display URL, bare id) route through this helper.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from .._base import (
    CanonicalRef, CanonicalResult, Unresolvable,
    UNRESOLVABLE_NOT_FOUND, UNRESOLVABLE_NO_ACCESS,
    UNRESOLVABLE_TRANSIENT, UNRESOLVABLE_INVALID_REF,
    RawItem,
)
from ...core.vault import VaultClient

log = logging.getLogger(__name__)
_vault: VaultClient | None = None


def _get_vault() -> VaultClient:
    global _vault
    if _vault is None:
        _vault = VaultClient()
    return _vault


def resolve_token(secret_ref: str) -> str:
    return _get_vault().resolve(secret_ref)


def to_raw_item(payload: dict, metadata: dict, source_id: str) -> RawItem:
    return RawItem(
        kind="confluence_page",
        source="confluence",
        source_id=source_id,
        payload=payload,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# ADR-039: Confluence canonical identity resolution
#
# Canonical identity = numeric content/page ID (str).
# Stable across: title changes, space moves, URL changes.
# Resolution is two-phase:
#   Phase 1: fast-path numeric extraction from URL (no API call)
#   Phase 2: title-lookup API call for display-by-title URLs
# Both phases may be followed by a validation API call (optional, skipped
# when session is None to support unit-test stubs).
# ---------------------------------------------------------------------------

# Step 1 fast-path patterns (ordered most-specific first)
_NUMERIC_ID_PATTERNS = [
    # /pages/viewpage.action?pageId=NNN
    re.compile(r"/pages/viewpage\.action\?pageId=(\d+)", re.IGNORECASE),
    # ?pageId=NNN or &pageId=NNN
    re.compile(r"[?&]pageId=(\d+)", re.IGNORECASE),
    # /pages/NNN/ or /pages/NNN (path segment, not followed by "viewpage")
    re.compile(r"/pages/(\d+)(?:[/?#]|$)"),
    # /rest/api/content/NNN
    re.compile(r"/rest/api/content/(\d+)(?:[/?#]|$)", re.IGNORECASE),
    # /wiki/rest/api/content/NNN (Cloud path variant)
    re.compile(r"/wiki/rest/api/content/(\d+)(?:[/?#]|$)", re.IGNORECASE),
    # bare pageId=NNN key-value pair (without URL ?)
    re.compile(r"\bpageId=(\d+)\b", re.IGNORECASE),
    # space-separated form: "pageId 18625350641" (BUG-990fe form, natural language)
    re.compile(r"\bpageId\s+(\d{8,})\b", re.IGNORECASE),
]

# Display-by-title URL pattern: /confluence/display/SPACE/Title  or  /display/SPACE/Title
# Captures (SPACE_KEY, title_slug)
_DISPLAY_URL_PATTERN = re.compile(
    r"/display/([A-Z0-9_\-]+)/([^/?#]+)",
    re.IGNORECASE,
)


def _extract_numeric_id_fast(reference: str) -> str | None:
    """Phase 1 fast path: extract numeric page ID from known URL forms.

    Returns the numeric string on match, None if no pattern matches.
    No API call required.
    """
    for pat in _NUMERIC_ID_PATTERNS:
        m = pat.search(reference)
        if m:
            return m.group(1)
    # Bare all-digit string
    stripped = reference.strip()
    if stripped.isdigit():
        return stripped
    return None


def _decode_title_slug(slug: str) -> str:
    """URL-decode a Confluence display-URL title slug.

    Confluence display URLs use + for space and %XX for other chars.
    """
    return unquote(slug.replace("+", " ").replace("_", " ")).strip()


def resolve_to_numeric_id(
    reference: str,
    resource_type: str,
    session,          # requests.Session or None (None = unit-test stub mode)
    base_url: str,    # e.g. "https://confluence.oraclecorp.com/confluence"
) -> CanonicalResult:
    """Resolve any Confluence reference form to a CanonicalRef with a numeric page ID.

    ADR-039 §4: Three-step algorithm.

    Step 1: Fast-path numeric extraction (no API call).
    Step 2: Display-by-title URL title-lookup (API call required).
    Step 3: ID validation (API call; verifies existence + access).

    When session is None (unit test / no credentials), step 3 validation is
    skipped and the numeric ID from step 1 or 2 is accepted as-is.

    Parameters
    ----------
    reference:
        Any Confluence reference form (URL, bare numeric, display URL).
    resource_type:
        "page" | "space" | "attachment" | "blog_post" (per ADR-036 manifest).
    session:
        A configured requests.Session (with Authorization header) or None.
    base_url:
        The Confluence base URL, e.g. "https://confluence.oraclecorp.com/confluence".
        DC deployments append "/confluence"; the REST endpoint is {base_url}/rest/api/...
    """
    # -- Step 1: fast-path numeric extraction ---------------------------------
    numeric_id = _extract_numeric_id_fast(reference)

    if numeric_id is None:
        # -- Step 2: display-by-title URL? -----------------------------------
        m = _DISPLAY_URL_PATTERN.search(reference)
        if m:
            space_key = m.group(1).upper()
            title_slug = _decode_title_slug(m.group(2))
            if session is None:
                # Cannot resolve without a live session — return Unresolvable
                return Unresolvable(
                    connector_id="confluence",
                    resource_type=resource_type,
                    reference=reference,
                    reason=UNRESOLVABLE_TRANSIENT,
                    detail=(
                        f"Display-by-title URL requires a live Confluence session to "
                        f"resolve (space={space_key!r}, title={title_slug!r}). "
                        "Retry when credentials are available."
                    ),
                    retryable=True,
                )
            # API call: title lookup
            try:
                r = session.get(
                    f"{base_url}/rest/api/content",
                    params={
                        "spaceKey": space_key,
                        "title": title_slug,
                        "limit": 1,
                        "expand": "version",
                    },
                    timeout=15,
                )
                if r.status_code == 404:
                    return Unresolvable(
                        connector_id="confluence",
                        resource_type=resource_type,
                        reference=reference,
                        reason=UNRESOLVABLE_NOT_FOUND,
                        detail=(
                            f"Page not found: space={space_key!r}, "
                            f"title={title_slug!r}. "
                            "Verify the page exists in the specified space."
                        ),
                        retryable=False,
                    )
                if r.status_code in (401, 403):
                    return Unresolvable(
                        connector_id="confluence",
                        resource_type=resource_type,
                        reference=reference,
                        reason=UNRESOLVABLE_NO_ACCESS,
                        detail=(
                            f"Access denied to space {space_key!r}. "
                            "Verify your credentials have read access."
                        ),
                        retryable=False,
                    )
                r.raise_for_status()
                results = r.json().get("results", [])
                if not results:
                    return Unresolvable(
                        connector_id="confluence",
                        resource_type=resource_type,
                        reference=reference,
                        reason=UNRESOLVABLE_NOT_FOUND,
                        detail=(
                            f"No page found matching space={space_key!r}, "
                            f"title={title_slug!r}. "
                            "Check the title is exact (case-sensitive in some Confluence versions)."
                        ),
                        retryable=False,
                    )
                numeric_id = str(results[0]["id"])
                display_hint = results[0].get("title", "")
                # Skip step 3 validation — we just validated via the search API
                return CanonicalRef(
                    connector_id="confluence",
                    resource_type=resource_type,
                    canonical_id=numeric_id,
                    display_hint=display_hint,
                )
            except Unresolvable:
                raise  # re-raise; don't catch our own typed failures
            except Exception as exc:
                return Unresolvable(
                    connector_id="confluence",
                    resource_type=resource_type,
                    reference=reference,
                    reason=UNRESOLVABLE_TRANSIENT,
                    detail=f"Network error during title lookup: {type(exc).__name__}: {exc}",
                    retryable=True,
                )
        # -- No pattern matched at all ----------------------------------------
        return Unresolvable(
            connector_id="confluence",
            resource_type=resource_type,
            reference=reference,
            reason=UNRESOLVABLE_INVALID_REF,
            detail=(
                f"Cannot parse Confluence reference: {reference!r}. "
                "Provide a URL with ?pageId=, a /display/SPACE/Title URL, "
                "a /pages/<id>/ URL, or a bare numeric page ID."
            ),
            retryable=False,
        )

    # -- Step 3: validate the numeric ID (if session available) ---------------
    if session is None:
        # Unit-test / no-credentials mode: trust the extracted numeric ID
        return CanonicalRef(
            connector_id="confluence",
            resource_type=resource_type,
            canonical_id=numeric_id,
            display_hint="",
        )

    try:
        r = session.get(
            f"{base_url}/rest/api/content/{numeric_id}",
            params={"expand": "space,version"},
            timeout=15,
        )
        if r.status_code == 404:
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NOT_FOUND,
                detail=(
                    f"Confluence page {numeric_id} not found. "
                    "The page may have been deleted or the ID is incorrect."
                ),
                retryable=False,
            )
        if r.status_code in (401, 403):
            return Unresolvable(
                connector_id="confluence",
                resource_type=resource_type,
                reference=reference,
                reason=UNRESOLVABLE_NO_ACCESS,
                detail=(
                    f"Access denied to Confluence page {numeric_id}. "
                    "Verify your credentials have read access to this page."
                ),
                retryable=False,
            )
        r.raise_for_status()
        payload = r.json()
        display_hint = payload.get("title", "")
        return CanonicalRef(
            connector_id="confluence",
            resource_type=resource_type,
            canonical_id=numeric_id,
            display_hint=display_hint,
        )
    except Unresolvable:
        raise
    except Exception as exc:
        return Unresolvable(
            connector_id="confluence",
            resource_type=resource_type,
            reference=reference,
            reason=UNRESOLVABLE_TRANSIENT,
            detail=f"Network error validating page {numeric_id}: {type(exc).__name__}: {exc}",
            retryable=True,
        )
