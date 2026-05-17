# BUG-011: persona=null ingestion + space-form regex gap → silent wrong-page result

**Queue ID**: BUG-queue-990fe
**Status**: FIXED
**Severity**: HIGH (silent wrong output — request scoped to page 18625350641 cited page 20030556732; no error surface)
**Session**: 2026-05-16 hardening session
**Filed**: 2026-05-16
**Fixed in**: 280451a (RC1+RC2 fixes + backfill), 8c947dc (P3 hard-fail structural guard)
  - RC1: `framework/ingestion/confluence_wiki_ingest.py` (`ConfluenceWikiIngestor.__init__`, `ingest_page`)
  - RC2: `framework/workflow_runtime/executor.py` (`_CONFLUENCE_PAGE_REF_PATTERNS`)
  - Structural P3 guard: `executor.py` (`_retrieve_for_inputs`), `framework/orchestrator/context_builder.py`, `framework/deploy/routes/ask.py`

---

## Symptom

`askKnowledgeBase` for `tpm.project_tracking_stakeholder_status_email` was explicitly scoped to pageId `18625350641` (the stakeholder page) but returned `no_answer` stating the page is not in KB, while still citing pageId `20030556732` (a different project page). The promoted skill's ingested KB contained the wrong page because the ingestor had stored it under `persona=null`. The system produced no error — the wrong-page result was served silently.

---

## Root Cause

Two independent root causes, both required for full resolution:

**RC1 — persona=null ingest gap:** `ConfluenceWikiIngestor.__init__` had no `persona` parameter. When called from `conversation.py::_run_ingest` and `ingestion_worker.py`, the persona was never propagated to the ingestor. Pages were stored with `persona: null` in `WikiMetadataStore`. The P3 guard (`_retrieve_for_inputs`) was comparing the requested page_id against passages that had null persona — the semantic retriever could return any passage regardless of persona, leading to cross-contamination.

**RC2 — space-form page-ref regex gap:** `_CONFLUENCE_PAGE_REF_PATTERNS` in `executor.py` matched four URL/querystring forms of a page reference but did not match the prose form `"pageId: 18625350641"` or `"for Confluence pageId 18625350641"`. Inputs in this form bypassed the guard entirely.

The structural P3 hard-fail guard (commit 8c947dc) was a prerequisite — without it the retriever silently substituted the wrong page with no error. The P3 guard added `ConfluencePageNotInKBError`, but the RC2 gap meant the guard was never triggered for space-form inputs.

---

## Fix

**A1 (RC2) — space-form regex guard** (280451a, `executor.py`): Added fifth pattern to `_CONFLUENCE_PAGE_REF_PATTERNS`: `re.compile(r"(?i)\bpage[\s_-]?id\b[\s:]+(\d{8,})")`. Length constraint `{8,}` prevents false-positives on short prose numbers. Guard now fires on `"pageId: 18625350641"` and `"for Confluence pageId 18625350641"`.

**A2 (RC1) — persona propagation** (280451a, `confluence_wiki_ingest.py`): `ConfluenceWikiIngestor.__init__` gains `persona: str | None = None` param. `ingest_page` uses `effective_persona = _raw.get("persona") or self._persona` (raw wins; fallback prevents null-persona storage when persona is determinable).

**A3 (callers updated)** (280451a): `conversation.py::_run_ingest` passes `persona=self._data.persona or None`. `ingestion_worker.py` moved ingestor construction inside the per-entry loop; each entry builds `ConfluenceWikiIngestor(adapter=..., persona=entry["persona"])`. `kb_cli.py` `cmd_ingest` fully implemented with `--persona` flag; fails loudly if neither arg nor config yields a persona.

**A4 (idempotent backfill)** (280451a, `kb_cli.py`): `wiki-meta backfill-persona` subcommand added. Executed for the affected session: page 18625350641 updated from `persona: null` to `persona: "tpm"`. Idempotent (re-run: no-op).

**Structural P3 hard-fail** (8c947dc): `_retrieve_for_inputs` raises `ConfluencePageNotInKBError` on page-mismatch (guard added in 8c947dc). `context_builder.py` and `ask.py` catch and surface actionable message. Never propagates as silent substitution or unhandled 500.

---

## How Found

User `reportBug` → BUG-queue-990fe (2026-05-16T22:13). User observed explicit `pageId 18625350641` scope returning citation for `20030556732` and filed via MCP `reportBug`. Architect performed root-cause analysis separating RC1 (persona propagation) from RC2 (regex gap); both required separate fixes.

---

## Related

- ADR-032: ask-time source ingestion — P3 guard is prerequisite (8c947dc)
- BUG-010 (BUG-queue-2ad9a): ShimWorkflows draft contamination — sibling silent-wrong-output
- BUG-014 (ADR-032 D1+D2): ask route input threading and single-fetch model
