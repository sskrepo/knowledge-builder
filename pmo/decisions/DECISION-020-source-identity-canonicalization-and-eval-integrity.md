# DECISION-020: Source Identity is an Adapter-Owned Contract; EVAL Integrity Requires Eager Author-Time Access

**Status**: Accepted
**Date**: 2026-05-18
**Decided by**: User (architectural direction)
**Supersedes**: RC1 / RC1-A patch lineage — DECISION-019 RC1-A and the `_resolve_page_id` / `_passage_matches_page_id` / `_passage_matches_display_url` heuristic reconcilers
**Related**: ADR-036 (connector registry + conformance — this extends it), ADR-035 / DECISION-015, ADR-032, DECISION-019

---

## Context

Authoring an `author_fixed` skill bound to a Confluence page has failed across ~7 iterations. Root cause: there is no single canonical "source identity" shared between the write path and the read path. `framework/skill_builder/synthesize_workflow.py:derive_pinned_source()` stores the author's raw URL form verbatim (deliberately un-canonicalized; its docstring states "a wrong pinned_ref is less dangerous than no binding"). INGEST + the Confluence adapter `normalize()` store the page under a content-hash / numeric pageId with a canonical citation URL — a different identifier shape. `framework/workflow_runtime/executor.py:_retrieve_author_fixed_pinned()` then heuristically reconciles the two shapes at runtime via `_resolve_page_id` (regex, numeric-only patterns; returns the raw string unchanged on no-match) and `_passage_matches_page_id` / `_passage_matches_display_url`. Each new author URL form (numeric viewpage → bare id → /display/SPACE/Title) misses the heuristic and a new matcher patch is added. The display-by-title URL form (`https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project`) cannot be reconciled, producing `ConfluencePageNotInKBError` even though the page is ingested and retrievable (recall@k=1.0). Separately, this session saw repeated FALSE/HOLLOW EVAL successes: skills promoted while Path-A genuinely failed and the structural comparator never ran (including two agent runs that fabricated gate passes by editing eval gold sets). The common enabler: promoting without a genuine EVAL that exercised real source access.

---

## Options Considered

### Identity layer

**Option A — Keep extending per-call heuristic reconcilers in executor (status quo)**

Continue adding per-URL-form matchers to `_resolve_page_id`, `_passage_matches_page_id`, and `_passage_matches_display_url` in `executor.py` as each new author URL form is encountered.

**Pros**: No structural change; incremental patches.

**Cons**: This is the 7-iteration whack-a-mole. Each new URL form creates a new patch surface. The heuristics are regex-based, cannot be proven complete, and the "return the string unchanged on no-match" fallback silently degrades. Rejected.

**Option B — Make source identity an adapter-owned contract, computed once, compared canonical==canonical everywhere (CHOSEN)**

Every adapter implements `canonical_identity(reference, resource_type) -> CanonicalRef` as a first-class contract method. All write paths (INGEST, adapter `normalize()`) stamp the canonical id. All read/match/route paths compare canonical-id == canonical-id. No heuristic reconciliation anywhere.

**Pros**: Eliminates the class of bug entirely. New URL forms are handled by adapters at registration time, not by executor patches. Canonical ids are proven-correct by the adapter test harness (ADR-036 conformance). Identity is defined per-connector AND per-resource-type.

**Cons**: Requires implementing `canonical_identity` in all registered adapters; existing artifacts carry raw-URL pinned refs and must be re-authored (consistent with prior migration stance — no backfill).

### Resolution timing

**Option 1 — Lazy + cached at read time**

Resolve raw reference → canonical id on first read, cache the mapping.

**Pros**: No author-time access required.

**Cons**: Institutionalizes the silent/hollow-success failure mode. An author can promote a skill whose source cannot be resolved at read time. EVAL integrity requires genuine author-time access; lazy fallback undermines that guarantee. Rejected.

**Option 2 — Eager at author/bind time, hard-fail if unresolvable/inaccessible (CHOSEN)**

Resolution + access + ingest + a genuine EVAL run against the REAL sources must all succeed at author time before PROMOTE. No lazy fallback. No promote-without-genuine-EVAL.

**Pros**: The EVAL/PROMOTE gate carries a real guarantee. A hard failure at author time is "fail loud + state exactly what to fix or retry" — better than a skill that silently misbehaves at runtime for every user. Consistent with the no-silent-degradation invariant.

**Cons**: Stricter authoring — an author cannot promote a skill against a source they cannot currently reach. Accepted as the deliberate tradeoff.

**Option 3 — Hybrid (lazy with warn)**

Attempt eager resolution; fall back lazily with a warning if source is unavailable at author time.

**Pros**: More permissive authoring experience.

**Cons**: Rejected alongside Option 1 for the same reason: hybrid fallback institutionalizes the silent/hollow-success failure mode. A warning without a gate is not a gate.

---

## Decision

### 1. Source identity is an adapter-owned contract

Every adapter/connector implements `canonical_identity(reference, resource_type) -> CanonicalRef` as part of ADR-036 connector conformance, surfaced through a single registry chokepoint `registry.canonical_identity(connector_id, reference, resource_type)`. No other code derives source identity. The RC1/RC1-A lineage (`_resolve_page_id`, `_passage_matches_page_id`, `_passage_matches_display_url` in executor.py, and the verbatim-URL `derive_pinned_source` behavior) is retired in favor of this contract. Identity is per-connector AND per-resource-type (Confluence: page→numeric pageId, plus space/attachment/blog_post; Jira: issue key vs filter-id vs project-key; Git: file=repo+ref+path, commit=sha; UDAP: SQL primary key / query identity). The ADR-036 manifest `resource_types` is the enumeration over which identity is defined.

### 2. Canonicalization is typed-fail, never pass-through

The contract returns either a `CanonicalRef` or a typed `Unresolvable` error. It MUST NEVER return a non-canonical raw value (the current `_resolve_page_id` "return the string unchanged on no-match" behavior is the exact silent-degradation bug being eliminated).

### 3. Two-sided and canonical-only comparison

The write path (INGEST / adapter `normalize()`) MUST stamp the canonical id as the stored KB identity/metadata key. Every read / match / route path compares canonical-id == canonical-id. No string/regex reconciliation of differing forms anywhere.

### 4. EVAL integrity implies eager author-time access; no lazy fallback; never promote without a genuine EVAL

Resolution + access + ingest + a genuine EVAL run against the REAL sources must all succeed at author time before PROMOTE. There is NO lazy fallback and NO promote-without-genuine-EVAL. If any required source/reference cannot be canonicalized, accessed, ingested, or genuinely exercised by EVAL at author time, authoring HARD-FAILS with a typed, actionable error; the author re-runs when access is available. Rationale: a hard EVAL/PROMOTE gate is only meaningful if EVAL genuinely exercised real source access and produced the real outcome; lazy fallback institutionalizes the silent/hollow-success failure mode observed repeatedly this session.

### 5. Mode-aware definition of "the source" (must NOT break ADR-032 ask_parameterized)

For `author_fixed` pinned skills — the LITERAL pinned page must canonicalize + be accessible + be ingested + be genuinely EVAL'd at author time; failure implies no promote. For `ask_parameterized` / `ingest_on_demand` skills — the specific runtime page is by design unknown at author time; EVAL integrity instead requires (a) the connector access itself proven at author time, and (b) a genuine EVAL run against an author-supplied REPRESENTATIVE/SAMPLE page in the bound space. The principle is identical in both modes (no genuine EVAL implies no promote); only the referent of "the source" differs by mode.

### 6. Typed, actionable, retryable failure

Author-time hard-fail must distinguish "no such resource / no permission" (author must fix the source) from "transient outage" (retry when access restored). It is "fail loud + state exactly what to fix or retry," NOT a permanent rejection of the user's intent.

### 7. Migration stance

Existing skills are RE-AUTHORED to adopt the new contract; NO backfill/migration tooling (consistent with prior session decisions).

### 8. Scope of this DECISION: it fixes the MODEL

Implementation is deferred to a follow-up ADR (the adapter `canonical_identity` ABC method + registry chokepoint + INGEST stamping + executor retrieval rewrite + the author-time EVAL-integrity hard-gate wiring + ADR-036 conformance update). This DECISION authorizes that ADR; it does not itself change code.

---

## Consequences

- Stricter authoring: an author cannot draft/promote a skill against a source they cannot currently reach. This is the deliberate, accepted tradeoff — a skill that cannot be genuinely EVAL'd offers no guarantee it does what the user intends, so promoting it is worse than failing. ADR-035's front-loaded access-verify gate should surface these failures EARLY (at CONFIGURE_SOURCES / INSPECT_SOURCES), not after the full authoring flow.
- The hard EVAL/PROMOTE gate now carries a real guarantee.
- Deletes the heuristic-reconciliation tech-debt lineage; new connectors cannot pass ADR-036 conformance without implementing `canonical_identity`.
- Eliminates the class of false/hollow promotions seen this session.

---

## Cross-References

| Reference | Relevance |
|---|---|
| ADR-036 | Connector registry + conformance — this DECISION extends it with the `canonical_identity` contract requirement |
| ADR-035 / DECISION-015 | Front-loaded source/reference/output access-verify gate + single-source-of-truth |
| ADR-032 | ask_parameterized ephemeral fetch — preserved by Decision §5 |
| DECISION-019 + RC1/RC1-A | Superseded by Decision §1 |
| 2026-05-17/18 false-success + fabricated-gold-set incidents | Motivating context for Decision §4 |

Note: a follow-up implementation ADR is REQUIRED before any code changes.
