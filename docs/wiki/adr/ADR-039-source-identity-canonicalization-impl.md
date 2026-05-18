---
title: ADR-039 — Source Identity Canonicalization Implementation
status: accepted
created: 2026-05-18
owner: architect
deciders: user, architect
supersedes: RC1/RC1-A patch lineage in executor.py (DECISION-019)
related: [DECISION-020, ADR-036, ADR-035, ADR-032, ADR-031]
tags: [adr, identity, canonicalization, adapter-abc, eval-integrity, source-binding]
---

# ADR-039 — Source Identity Canonicalization Implementation

> **Status: ACCEPTED — merged to main per user approval (2026-05-18)**

---

## Context

DECISION-020 (Accepted, 2026-05-18) authorized the implementation design for:

1. An adapter-owned `canonical_identity` contract replacing all heuristic URL reconcilers.
2. Two-sided canonical stamping (write path stamps canonical id; read/match/route path
   compares canonical == canonical).
3. Deletion of the RC1/RC1-A reconciler lineage in `executor.py`.
4. A hard-gated author-time EVAL-integrity gate (no promote without genuine EVAL).
5. ADR-036 conformance update: `canonical_identity` becomes a required contract method.

This ADR specifies the EXACT implementation of each point: the function signatures,
call sites, URL resolution logic per connector, corner cases, and test coverage.

The following functions in `framework/workflow_runtime/executor.py` are the primary
targets for deletion (the RC1/RC1-A lineage):

- `_resolve_page_id(page_ref)` — regex heuristic returning unchanged string on no-match
- `_passage_matches_page_id(passage, requested_page_id)` — heuristic passage filter
- `_passage_matches_display_url(passage, space_key, title_slug)` — RC1-A title-slug matcher
- `_is_display_url(ref)` — RC1-A helper
- `_extract_display_url_parts(ref)` — RC1-A helper
- `_CONFLUENCE_PAGE_REF_PATTERNS` — the regex pattern list
- `_DISPLAY_URL_PATTERN` — the RC1-A display-URL regex
- `_extract_confluence_page_ids(inputs)` — the P3 heuristic scanner

The `_retrieve_author_fixed_pinned` method in `WorkflowExecutor` is rewritten to
compare `canonical_id == canonical_id` instead of calling the above heuristics.

---

## Decision

### 1. `CanonicalRef` and `Unresolvable` types

Location: `framework/adapters/_base.py` (extension of the existing Protocol).

```python
from dataclasses import dataclass
from typing import Union

@dataclass(frozen=True)
class CanonicalRef:
    """Canonical identifier for a source resource, computed once at author/bind time.

    Attributes:
        connector_id: the registered connector (e.g. "confluence", "jira", "git").
        resource_type: e.g. "page", "issue", "filter", "file".
        canonical_id:  the stable, connector-defined primary key for the resource.
                       For Confluence: the numeric page/content ID (str).
                       For Jira: the numeric internal issue ID (str), or filter_id,
                                 or project_id, depending on resource_type.
                       For Git: "{repo_url}:{ref}:{path}" (normalized).
                       For UDAP: SQL primary key string.
        display_hint:  optional human-readable label (page title, issue key).
                       NEVER used for identity comparison — for display only.
    """
    connector_id: str
    resource_type: str
    canonical_id: str
    display_hint: str = ""

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CanonicalRef):
            return False
        return (
            self.connector_id == other.connector_id
            and self.resource_type == other.resource_type
            and self.canonical_id == other.canonical_id
        )

    def __hash__(self) -> int:
        return hash((self.connector_id, self.resource_type, self.canonical_id))


@dataclass(frozen=True)
class Unresolvable:
    """Returned when canonical_identity cannot resolve a reference.

    Attributes:
        connector_id: the connector that attempted resolution.
        resource_type: the resource type that was requested.
        reference: the original reference string that could not be resolved.
        reason: ERROR_NOT_FOUND | ERROR_NO_ACCESS | ERROR_TRANSIENT | ERROR_INVALID_REF
        detail: human-readable detail suitable for an actionable author-time message.
        retryable: True if the failure is transient (connection issue, rate limit);
                   False if the resource genuinely does not exist or access is denied.
    """
    connector_id: str
    resource_type: str
    reference: str
    reason: str   # one of the ERROR_ constants below
    detail: str = ""
    retryable: bool = False

# Reason constants
UNRESOLVABLE_NOT_FOUND   = "ERROR_NOT_FOUND"
UNRESOLVABLE_NO_ACCESS   = "ERROR_NO_ACCESS"
UNRESOLVABLE_TRANSIENT   = "ERROR_TRANSIENT"
UNRESOLVABLE_INVALID_REF = "ERROR_INVALID_REF"

CanonicalResult = Union[CanonicalRef, Unresolvable]
```

The contract is strict: `canonical_identity` returns exactly one of these two types —
never a raw string, never None. The historical bug was `_resolve_page_id` returning
the original string unchanged on no-match; this is the structural elimination of that.

---

### 2. Adapter ABC extension: `canonical_identity` abstract method

Location: `framework/adapters/_base.py` — extend the existing `Adapter` Protocol.

```python
from abc import abstractmethod, ABC
from .._base import CanonicalResult  # or same file

class AdapterWithIdentity(ABC):
    """Extension of the Adapter Protocol requiring canonical_identity.

    All adapters registered in the Connector Registry MUST implement this.
    Per DECISION-020 §1 and ADR-036 Amendment 2 (conformance harness).
    """

    @abstractmethod
    def canonical_identity(
        self,
        reference: str,
        resource_type: str,
    ) -> CanonicalResult:
        """Resolve a reference to its canonical identity for this connector.

        This is called ONCE at author/bind time. The result is stamped onto
        every ContentItem produced by INGEST (adapter.normalize()). The executor
        compares canonical_id == canonical_id; no heuristic reconciliation.

        Parameters
        ----------
        reference:
            Any reference form the author might supply. For Confluence: could be
            a full URL with ?pageId=, a /display/SPACE/Title URL, a bare numeric
            string, a REST /rest/api/content/<id> URL. For Jira: an issue key
            (PROJECT-123), a full issue URL, a bare numeric issue ID, a filter URL.
        resource_type:
            Per ADR-036 manifest resource_types. Examples: "page", "issue",
            "filter", "file", "commit".

        Returns
        -------
        CanonicalRef
            On success: the stable canonical identity. canonical_id is the
            connector-defined primary key (numeric string for Confluence/Jira).
        Unresolvable
            On failure: typed failure with reason and retryable flag.
            NEVER return the raw reference unchanged (that is the bug we fix).
        """
        raise NotImplementedError
```

The `AdapterWithIdentity` ABC is subclassed by every concrete adapter. ADR-036
conformance now fails if `canonical_identity` is not implemented (see §10 below).

---

### 3. Registry chokepoint: `registry.canonical_identity(connector_id, ref, resource_type)`

Location: `framework/connectors/registry.py` — add to `ConnectorRegistry`.

```python
def canonical_identity(
    self,
    connector_id: str,
    reference: str,
    resource_type: str,
) -> CanonicalResult:
    """Single chokepoint for all canonical-identity resolution.

    Retrieves the registered adapter instance for connector_id and delegates
    to its canonical_identity(reference, resource_type). No code outside this
    chokepoint resolves source identity — not executor.py, not synthesize_workflow.py.

    Raises ConnectorNotRegisteredError if connector_id is not in the registry.
    Returns CanonicalRef or Unresolvable — never a raw string.
    """
```

All callers (INGEST normalize, executor retrieval, author-time bind) route through
this single function. This makes identity resolution auditable and testable in one place.

---

### 4. Confluence canonical-identity logic (EXACT)

**Deployment context**: Oracle runs Confluence Server/Data-Center at
`confluence.oraclecorp.com` (NOT Confluence Cloud). The DC URL forms differ from
Cloud in path prefixes and id encoding, but the numeric content ID is stable across
both DC and Cloud deployments.

**Canonical identity for Confluence**: the **stable numeric content/page ID** (e.g.
`"18625350641"`). This is the value returned by the Confluence REST API `content.id`
field. It is assigned at page creation and never changes, even when the page title
changes, when the page is moved to a different space, or when the display URL changes.

**Resolution algorithm** (implemented in `ConfluenceAdapter.canonical_identity()`):

```
Input: reference (str), resource_type ("page" | "space" | "attachment" | "blog_post")

Step 1: Attempt fast-path numeric extraction from known URL forms (no API call):
  - ?pageId=<digits>           → extract <digits>
  - /pages/viewpage.action?pageId=<digits> → extract <digits>
  - /pages/<digits>(?:/|$)     → extract <digits>
  - /rest/api/content/<digits> → extract <digits>  [REST API URL form]
  - bare all-digit string      → use directly

  If a numeric ID is extracted in step 1 → go to step 3 (validate via API).

Step 2: Title-lookup for display-by-title URLs (API call required):
  - /confluence/display/{SPACE}/{Title} → Confluence DC display URL
  - /display/{SPACE}/{Title}            → alternate DC form
  Extract (SPACE_KEY, title_slug) from URL.
  URL-decode: replace "+" with space, %XX with char.
  Call: GET /wiki/rest/api/content?spaceKey={SPACE}&title={decoded_title}&limit=1
        (Confluence Server/DC endpoint)
  If 200 and results[0].id exists → use results[0].id as numeric ID.
  If 404 or empty results → return Unresolvable(reason=ERROR_NOT_FOUND,
      detail="Page not found: space={SPACE}, title={title}", retryable=False).
  On network error → return Unresolvable(reason=ERROR_TRANSIENT, retryable=True).

Step 3: Validate the numeric ID (API call; verifies existence and access):
  Call: GET /wiki/rest/api/content/{numeric_id}?expand=space,version
  If 200 → return CanonicalRef(connector_id="confluence",
                               resource_type=resource_type,
                               canonical_id=str(numeric_id),
                               display_hint=response["title"])
  If 404 → return Unresolvable(reason=ERROR_NOT_FOUND, retryable=False)
  If 403/401 → return Unresolvable(reason=ERROR_NO_ACCESS, retryable=False)
  On network error → return Unresolvable(reason=ERROR_TRANSIENT, retryable=True)

Step 4: If reference matches no known pattern → return
  Unresolvable(reason=ERROR_INVALID_REF,
               detail=f"Cannot parse Confluence reference: {reference!r}",
               retryable=False)
```

**Which function does the resolution**: `ConfluenceAdapter.canonical_identity(reference, resource_type)` — implemented in `framework/adapters/confluence/native.py` (primary) and `framework/adapters/confluence/mcp.py`/`emcp_direct.py` (parity required). The function is also exposed via the registry chokepoint `registry.canonical_identity("confluence", ref, "page")`.

**DC-specific notes**:
- DC base URL: `https://confluence.oraclecorp.com/confluence` (trailing path segment `/confluence` is present in DC but not Cloud — the adapter config `base_url` captures this).
- DC REST endpoint: `{base_url}/rest/api/content` (not `/wiki/rest/api/content` as in Cloud). The adapter already uses `base_url` from config so this is handled by configuration, not hardcoding.
- DC display URL: `{base_url}/display/{SPACE}/{Title}` — captured by Step 2 pattern.
- DC pageId forms are identical to Cloud (numeric, globally unique within the instance).

---

### 5. Jira canonical-identity logic (EXACT)

**Canonical identity for Jira issues**: the **stable numeric internal issue ID**, NOT
the issue key. The issue key (e.g. `PROJECT-123`) is NOT stable — it changes when an
issue is moved across projects. The internal numeric issue ID (returned as `issue.id`
in the Jira REST API) is assigned at creation and never changes.

**Per resource_type** (per ADR-036 manifest resource_types: issue, epic, sprint, filter, project):

| resource_type | canonical_id | Notes |
|---|---|---|
| `issue` / `epic` | `issue.id` (numeric string, e.g. `"100042"`) | Stable across key changes and project moves |
| `sprint` | `sprint.id` (numeric) | Sprint IDs are stable; sprint names change |
| `filter` | `filter.id` (numeric) | Saved filter ID; stable |
| `project` | `project.id` (numeric) | Project ID; stable across key renames |

**Resolution algorithm** (implemented in `JiraAdapter.canonical_identity()`):

```
Input: reference (str), resource_type ("issue"|"epic"|"sprint"|"filter"|"project")

Step 1: Attempt key/URL normalization (no API call):
  Issue key pattern: /browse/{PROJECT-NNN} or bare PROJECT-NNN → extract key
  Issue URL: /issues/{id} or ?selectedIssue=... → extract
  Bare numeric string: check if all digits → could be direct issue.id

Step 2: Resolve to internal numeric ID (API call always required for issues):
  For resource_type in ("issue", "epic"):
    If reference looks like an issue key (PROJECT-NNN) or URL containing one:
      Call: GET /rest/api/2/issue/{key_or_url_extracted_key}?fields=id
      On 200 → canonical_id = response["id"]  (numeric string, NOT the key)
      On 404 → Unresolvable(ERROR_NOT_FOUND, retryable=False)
      On 403 → Unresolvable(ERROR_NO_ACCESS, retryable=False)
      On network error → Unresolvable(ERROR_TRANSIENT, retryable=True)
    If reference is bare numeric string → use as canonical_id directly
      (already the internal id form; skip the key-lookup API call)

  For resource_type == "filter":
    Parse filter ID from URL (/issues/?filter=NNN) or bare numeric string.
    Call: GET /rest/api/2/filter/{filter_id} to validate access.
    canonical_id = filter_id (numeric string).

  For resource_type == "sprint":
    Parse sprint ID from URL or bare numeric. Validate via Agile API.
    canonical_id = sprint_id (numeric string).

  For resource_type == "project":
    Parse project key or ID from URL or bare string.
    Call: GET /rest/api/2/project/{key_or_id}?fields=id
    canonical_id = project.id (numeric string).

Step 3: For raw JQL references (resource_type not an entity with a stable ID):
  Raw JQL is NOT a pinned identity — it is a query parameter, not a resource.
  For ask_parameterized skills that use JQL, the connector reports:
    Unresolvable(ERROR_INVALID_REF,
                 detail="Raw JQL does not have a stable canonical_id. "
                        "Use a saved filter (resource_type=filter) for pinning. "
                        "For ask_parameterized JQL-based skills, canonical_identity "
                        "is not applicable — use the ask_parameterized mode "
                        "(ADR-032) where identity is the filter_id or project_id.",
                 retryable=False)
  This forces JQL-sourced skills to use ask_parameterized mode, not author_fixed pinning.
```

**Which function does the resolution**: `JiraAdapter.canonical_identity(reference, resource_type)` in `framework/adapters/jira/native.py`. Called via registry chokepoint.

---

### 6. Corner cases — WE-handle vs SOURCE-handles

#### (a) Confluence page title changes → URL changes

**Who handles it**: SOURCE SYSTEM handles it by construction.

The Confluence Server/DC numeric `content.id` (page ID) is assigned at page creation
and is invariant across title changes, space moves, and URL renames. A display URL
`/display/SPACE/OldTitle` for page `18625350641` becomes `/display/SPACE/NewTitle`,
but the page ID `18625350641` never changes.

Our canonical identity IS the numeric page ID — so we are robust to title changes by
construction. The only way a title change can affect us is if we are given a
display-by-title URL and call the title-lookup API to resolve it. In that case, if
the title has changed and the old title no longer exists, the lookup returns 404 →
we return `Unresolvable(ERROR_NOT_FOUND)`. The author re-supplies the new URL or the
numeric ID directly. This is correct behavior — the skill must be re-authored to
point to the new title if the URL form was used.

If the numeric ID was stored (which is the normal post-ADR-039 case), title changes
are entirely transparent.

#### (b) Jira issue transferred across projects → key changes (e.g. PROJ-123 → OTHER-456)

**Who handles it**: SOURCE SYSTEM handles it by construction.

The internal numeric issue ID (`issue.id`) is invariant across project moves and key
reassignments. A key change from `PROJ-123` to `OTHER-456` does NOT change the
`issue.id`. Our canonical identity IS the numeric `issue.id`.

If we stored the canonical numeric ID at author time (which is the post-ADR-039 case),
project moves are entirely transparent. If a legacy skill stored an issue key as its
pinned_ref (pre-ADR-039), re-authoring is required — consistent with DECISION-020 §7
(no backfill; re-author to adopt the new contract).

#### (c) Page/issue deleted or access revoked

**Who handles it**: WE handle this explicitly via the typed `Unresolvable` return.

At author time: if the page has been deleted or access revoked, `canonical_identity()`
returns `Unresolvable(ERROR_NOT_FOUND)` or `Unresolvable(ERROR_NO_ACCESS)` respectively.
The author-time gate catches this and hard-fails with an actionable message:
`"The page <id> could not be accessed: not found / access denied. Verify the page
exists and your credentials have read access."` The skill is not promoted.

At INGEST time: if a previously-valid canonical ID becomes invalid (page deleted after
authoring), the ingest pipeline receives a 404/403 on the fetch call. The pipeline logs
a typed error and skips the item. The KB may become stale (the page entry will persist
until re-ingest removes it via the content-hash idempotency mechanism). This is a
known-accepted gap for v1 (incremental deletion detection is a v2 item per spec §8).

At skill runtime: if a page was valid at author time and ingest time but deleted before
runtime, the executor performs canonical==canonical lookup in the KB and finds no match.
`ConfluencePageNotInKBError` is raised with an actionable message. This is correct
behavior — the skill cannot work without its source.

#### (d) Deployment variants — Server/DC vs Cloud ID/URL schemes

**Who handles it**: WE handle it in the adapter, parameterized by `base_url` config.

| Form | DC (oraclecorp) | Cloud |
|---|---|---|
| Display URL | `{base_url}/display/{SPACE}/{Title}` | `{base_url}/wiki/spaces/{SPACE}/pages/{ID}/{Title}` |
| REST content | `{base_url}/rest/api/content/{ID}` | `{base_url}/wiki/rest/api/content/{ID}` |
| pageId query | `?pageId={ID}` | Same |
| Numeric ID value | 11-digit integer (e.g. 18625350641) | Same format, different range |

The `ConfluenceAdapter` is configured with `base_url` (e.g. `https://confluence.oraclecorp.com/confluence`).
All URL patterns in Step 1 and Step 2 of the resolution algorithm match path-relative
components (not the host), so the same code handles both DC and Cloud forms. The title
lookup API endpoint differs only in path prefix, and `base_url` absorbs that difference.

The key invariant: the numeric `content.id` is stable and universally meaningful within
a single Confluence instance. Cross-instance identity (e.g. content migrated from
Server to Cloud) is explicitly OUT OF SCOPE for v1 (no migration tooling per DECISION-020 §7).

#### (e) Version pinning — in or out of scope

**Out of scope for v1.** Version pinning (e.g., "canonical identity at a specific
Confluence page version number") is not implemented. The canonical ID always refers to
the current HEAD of the page. Skills are authored against the current content at
author time. Page version drift is detected at re-ingest via the content-hash
idempotency mechanism (CLAUDE.md cross-cutting requirements). Explicit version pinning
is a v2 item.

---

### 7. INGEST / `normalize()` canonical-id stamping

Location: `ConfluenceAdapter.normalize()` and `JiraAdapter.normalize()`.

Every `ContentItem` produced by `normalize()` MUST carry the canonical id in its
`metadata["canonical_ref"]` field as a serialized `CanonicalRef`:

```python
metadata["canonical_ref"] = {
    "connector_id": "confluence",
    "resource_type": "page",
    "canonical_id": str(page_id),  # the numeric ID, always
}
```

The INGEST pipeline stamps this at the time the `RawItem` is produced. The KB storage
layer (WikiMetadataStore, IncidentVectorStore) persists this metadata field alongside
the content. The executor's retrieval path reads `metadata["canonical_ref"]["canonical_id"]`
for comparison — not any URL form, not any derived value.

The existing `source_id` field on `RawItem` (currently storing numeric page IDs for
Confluence) becomes the canonical_id source. `normalize()` is updated to explicitly
populate `metadata["canonical_ref"]` from `source_id` for backward compatibility.

---

### 8. Executor retrieval rewrite — canonical == canonical

The `_retrieve_author_fixed_pinned` method in `WorkflowExecutor` is rewritten:

**Deleted from executor.py**:
- `_resolve_page_id`
- `_passage_matches_page_id`
- `_passage_matches_display_url`
- `_is_display_url`
- `_extract_display_url_parts`
- `_CONFLUENCE_PAGE_REF_PATTERNS`
- `_DISPLAY_URL_PATTERN`
- `_extract_confluence_page_ids`

**Replaced with** (in `_retrieve_author_fixed_pinned`):

```python
# At skill-load time (once, not per-passage):
canonical = registry.canonical_identity(
    connector_id,
    pinned_ref,
    resource_type,
)
if isinstance(canonical, Unresolvable):
    raise SourceIdentityUnresolvableError(canonical)

# Per-passage comparison:
def _passage_matches_canonical(passage: dict, canonical: CanonicalRef) -> bool:
    cref = (passage.get("metadata") or {}).get("canonical_ref") or {}
    return (
        cref.get("connector_id") == canonical.connector_id
        and cref.get("resource_type") == canonical.resource_type
        and cref.get("canonical_id") == canonical.canonical_id
    )
```

No regex. No string matching on URLs. No "return unchanged on no-match."
The canonical_id is a numeric string; equality is a simple string equality check.

---

### 9. Author-time EVAL-integrity hard gate

Per DECISION-020 §4 and §5, the hard gate fires at the `PROMOTE` step of the
authoring FSM.

**What must succeed before PROMOTE**:

For `author_fixed` mode:
1. `registry.canonical_identity(connector_id, pinned_ref, resource_type)` returns `CanonicalRef` (not `Unresolvable`). If `Unresolvable`, hard-fail with typed error.
2. The canonical ID is present in the KB (ingest has run and the canonical_ref stamp is present in stored metadata). If absent, hard-fail: "Page {id} is not yet ingested. Run kb-cli ingest --page-id {id}."
3. A genuine EVAL run has executed against the real KB content (not mocked, not a fixture). The EVAL result is stored in the session state. If no genuine EVAL run exists, hard-fail: "PROMOTE requires a passing EVAL run. Run EVAL first."
4. The EVAL run exercised Path-A (source retrieval returned the correct canonical page). If Path-A produced no matching passages, hard-fail.

For `ask_parameterized` mode (per DECISION-020 §5):
1. The connector access itself is verified at author time (probe_access passes).
2. A genuine EVAL run was performed against an author-supplied representative/sample page from the bound space. The sample page must have been canonicalized successfully (step 1 above for that sample page).
3. Same PROMOTE gate as above.

**Mode-aware gate**: The referent of "the source" differs by mode. For `author_fixed`,
the LITERAL pinned page must be canonical-resolvable. For `ask_parameterized`, the
REPRESENTATIVE SAMPLE page must be canonical-resolvable. The principle (genuine EVAL,
no lazy fallback) is identical.

**Typed failure at the gate**:

```python
class SourceIdentityUnresolvableError(Exception):
    """Raised at author time when canonical_identity returns Unresolvable.

    Carries the typed Unresolvable result so the error message can distinguish
    'page not found' (author must fix the source) from 'transient outage'
    (retry when access is restored).
    """
    def __init__(self, unresolvable: Unresolvable):
        self.unresolvable = unresolvable
        if unresolvable.retryable:
            msg = (
                f"Transient failure resolving {unresolvable.connector_id} "
                f"{unresolvable.resource_type} {unresolvable.reference!r}: "
                f"{unresolvable.detail}. "
                "This is a temporary outage. Retry when access is restored."
            )
        else:
            msg = (
                f"Cannot resolve {unresolvable.connector_id} "
                f"{unresolvable.resource_type} {unresolvable.reference!r}: "
                f"{unresolvable.reason} — {unresolvable.detail}. "
                "Fix the source reference and re-author."
            )
        super().__init__(msg)
```

The hard-fail is the CORRECT, EXPECTED behavior when a source is inaccessible at
author time. Reporting "authoring correctly hard-failed because the pinned page was
inaccessible" IS the success case for this work. Faking a green result is the worst
possible outcome (DECISION-020 context).

---

### 10. ADR-036 conformance update

`framework/eval/` conformance harness gains a new check:

```
canonical_identity_contract:
  - Adapter implements canonical_identity(reference, resource_type)
  - Returns CanonicalRef or Unresolvable — never raw string or None
  - Fixture: known valid reference → returns CanonicalRef with correct canonical_id
  - Fixture: known invalid reference → returns Unresolvable (not raises exception)
  - Fixture: same resource, two reference forms → same CanonicalRef (equality holds)
  - Fixture: transient error simulation → Unresolvable(retryable=True)
```

Any new connector that fails the `canonical_identity_contract` conformance check
CANNOT be registered in the Connector Registry (ADR-036 §M.3 equivalent extended check).

---

### 11. Git and UDAP — stub contract

Git and UDAP are out of scope for this ADR's full implementation but MUST implement
the ABC stub so they cannot silently skip the contract:

**Git** (conceptual canonical_id):
```
resource_type=file:   "{normalized_repo_url}:{ref}:{path}"
resource_type=commit: "{normalized_repo_url}:{sha40}"
resource_type=ref:    "{normalized_repo_url}:refs/{branch_or_tag}"
```
Full implementation deferred — a `NotImplementedError("Git canonical_identity: full
implementation pending — deferred to follow-up ADR")` with correct typing is
acceptable for this phase.

**UDAP** (not registered in connector registry per ADR-036 Amendment 4 / Section O,
production path not implemented):
```
canonical_identity() raises NotImplementedError("UDAP canonical_identity: deferred
until production JDBC path is implemented")
```
The ABC requires the method to exist; raising `NotImplementedError` explicitly is
correct behavior per ADR-036 §M.2.

---

### 12. Migration stance

Per DECISION-020 §7: NO backfill. Existing promoted skills with `pinned_ref` carrying
raw URLs are re-authored by their authors. The executor continues to handle the
old `pinned_ref` form ONLY during the transition period (one release cycle), then the
heuristics are permanently deleted. This ADR's implementation is the transition:
new skills use canonical_identity; old skills must be re-authored.

---

## Implementation file map

| File | Change |
|---|---|
| `framework/adapters/_base.py` | Add `CanonicalRef`, `Unresolvable`, `UNRESOLVABLE_*` constants, `AdapterWithIdentity` ABC |
| `framework/adapters/confluence/native.py` | Implement `canonical_identity()` (Steps 1-4 above) |
| `framework/adapters/confluence/mcp.py` / `emcp_direct.py` | Implement `canonical_identity()` (parity; may delegate to shared helper) |
| `framework/adapters/jira/native.py` | Implement `canonical_identity()` (resource_type dispatch above) |
| `framework/adapters/git_adapter.py` | Add `canonical_identity()` stub with `NotImplementedError` |
| `framework/adapters/udap_adapter.py` | Add `canonical_identity()` stub with `NotImplementedError` |
| `framework/connectors/registry.py` | Add `canonical_identity(connector_id, ref, resource_type)` chokepoint |
| `framework/workflow_runtime/executor.py` | DELETE RC1/RC1-A reconcilers; rewrite `_retrieve_author_fixed_pinned` to canonical==canonical; add `SourceIdentityUnresolvableError` |
| `framework/adapters/confluence/shared.py` | Add shared `_resolve_to_numeric_id()` helper (Steps 1-2) |
| `framework/tests/unit/test_source_identity.py` | New: unit tests per §13 below |

---

### 13. Test coverage

New test file: `framework/tests/unit/test_source_identity.py`

Required test cases (all must pass; zero new failures in existing 8-baseline):

1. **Confluence: multi-form → same canonical_id**
   - `?pageId=18625350641` → `CanonicalRef(canonical_id="18625350641")`
   - `/pages/viewpage.action?pageId=18625350641` → same
   - `/pages/18625350641/` → same
   - `/rest/api/content/18625350641` → same
   - bare `"18625350641"` → same
   All five produce equal `CanonicalRef` instances (equality via `__eq__`).

2. **Confluence: display-URL → resolved canonical_id**
   - `/display/OCIFACP/FAaaS+Kiwi+Project` → API title-lookup returns id `"18625350641"`
   - Mock the Confluence REST title-lookup API.
   - Resulting `CanonicalRef` equals the one from test case 1.

3. **Jira: key/URL → numeric issue ID**
   - `"PROJ-123"` → API resolves to `issue.id = "100042"` → `CanonicalRef(canonical_id="100042")`
   - `"https://jira.example.com/browse/PROJ-123"` → same
   - bare `"100042"` → `CanonicalRef(canonical_id="100042")` (direct)

4. **Title change / cross-project move robustness**
   - Same numeric ID stored, but display alias changes.
   - Test: store `CanonicalRef(canonical_id="18625350641")`; passage metadata carries
     `canonical_ref.canonical_id = "18625350641"`. `_passage_matches_canonical` returns True
     regardless of any title or URL change (because comparison is canonical_id == canonical_id).

5. **Typed Unresolvable on not-found**
   - Mock 404 from Confluence API → `Unresolvable(reason=ERROR_NOT_FOUND, retryable=False)`
   - Mock 403 → `Unresolvable(reason=ERROR_NO_ACCESS, retryable=False)`
   - Mock network error → `Unresolvable(reason=ERROR_TRANSIENT, retryable=True)`
   - Confirm the result is NEVER a raw string.

6. **INGEST stamps canonical_ref**
   - `normalize()` on a Confluence raw payload with `source_id="18625350641"` produces
     `ContentItem.metadata["canonical_ref"]["canonical_id"] == "18625350641"`.

7. **Path-A: executor matches canonical == canonical**
   - A passage with `metadata.canonical_ref.canonical_id = "18625350641"` and a skill
     with `pinned_ref` resolving to canonical_id `"18625350641"` → `_passage_matches_canonical` True.
   - A passage with a DIFFERENT canonical_id → False.

8. **Author-time gate: hard-fail on Unresolvable blocks promote**
   - Simulate `canonical_identity()` returning `Unresolvable(ERROR_NOT_FOUND)`.
   - Confirm `SourceIdentityUnresolvableError` is raised.
   - Confirm the skill is NOT promoted (promote step is blocked before any artifact write).

9. **ADR-036 conformance fails without canonical_identity**
   - A mock adapter subclass WITHOUT `canonical_identity` implemented fails the
     conformance check (raises `TypeError` or `NotImplementedError` as appropriate
     per ABC enforcement).

---

## Consequences

- Eliminates the 7-iteration heuristic-reconciler class of bug permanently.
- New URL forms for Confluence/Jira are handled by adapter resolution (step 1 fast path) —
  no executor patch required.
- Author-time hard-fail is meaningful: `SourceIdentityUnresolvableError` with typed reason.
- Deletion of ~150 LOC of regex heuristics from executor.py.
- New adapters must implement `canonical_identity` or fail conformance — the contract
  is enforced structurally, not by convention.
- Migration cost: existing skills with raw-URL pinned_refs must be re-authored.
  This is accepted (DECISION-020 §7).

## Implementation Gap Closed: bind-side canonicalization (2026-05-18)

> **Identified by:** independent ADB inspection of session `synth-tpm-58a9780c`
> (committed `tpm.faaas_kiwi_project_pptx` artifact had `source_binding.pinned_ref`=raw display URL).
> **Closed in:** `fix/decision-020-bind-canonicalization` branch; merged to main same day.

### Gap description

The original ADR-039 implementation wired the **read path** (executor) but left the
**write/bind path** (author-time `_synthesize_preview`) un-wired.

Specifically: `derive_pinned_source()` in `synthesize_workflow.py` returned the raw author
URL in `pinned_ref` without calling `canonical_identity()`. The executor
(`_retrieve_author_fixed_pinned`) then called `resolve_to_numeric_id(session=None)` at
retrieval time — which correctly returned `Unresolvable(TRANSIENT)` for display-by-title
URLs, surfacing as `ConfluencePageNotInKBError`. This was the correct §4 hard-fail
behavior but in the wrong place (runtime instead of author time).

### Fix

Added `canonicalize_pinned_source(pinned_source, canonicalize_fn)` in `synthesize_workflow.py`:
- Called at `_synthesize_preview` time in `conversation.py`, immediately after
  `derive_pinned_source()` returns a non-None result.
- Uses the live Confluence adapter (built via `_build_confluence_adapter`) available
  at author time.
- On `CanonicalRef`: stores numeric `canonical_id` in `pinned_ref`; serializes full
  `CanonicalRef` as `canonical_ref`; retains raw URL as non-authoritative `original_ref`.
- On `Unresolvable`: raises `PinnedSourceCanonicalizationError` immediately — per
  DECISION-020 §4, the raw URL is NEVER stored and authoring HARD-FAILs with an
  actionable typed error distinguishing retryable (transient) vs permanent failures.

### INGEST stamping assessment

The INGEST path (`ConfluenceNativeAdapter.normalize()`) already correctly stamps
`canonical_ref` using `raw_item.source_id` (the numeric page ID from the adapter's
`fetch()` call). No changes needed to the INGEST side — it was correctly implemented
in the original ADR-039 merge.

### Parked session note

Session `synth-tpm-58a9780c` retains the artifact with `pinned_ref=raw_display_URL`.
That session will NOT auto-heal. The user must start a fresh authoring session to obtain
a correctly canonicalized artifact. The session remains in ADB as evidence of the bug.

---

## References

- DECISION-020 — authorization for this ADR
- ADR-036 §M — adapter ABC and conformance harness (extended here)
- ADR-032 — ask_parameterized ephemeral fetch (preserved; §5 mode-aware gate)
- ADR-031 — no-silent-degradation invariant
- ADR-035 — author-time access-verify gate (complementary)
- `framework/workflow_runtime/executor.py` — RC1/RC1-A code to be deleted
- `framework/adapters/confluence/native.py` — primary Confluence adapter
- `framework/adapters/jira/native.py` — primary Jira adapter
