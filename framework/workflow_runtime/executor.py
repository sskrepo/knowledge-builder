"""Workflow executor — runs a workflow skill end-to-end.

Per ADR-016. Steps: source discovery → extract (or read cached) → retrieve →
synthesize → render → deliver → cost telemetry → eval recording.

Laptop-mode friendly: uses FilestoreContentStore + filesystem deliverer when no
ADB/Vault/OCI configured.

ADR-032 P2-Exec: ask_parameterized skills fetch the user-supplied Confluence page
ephemerally (never written to any persistent store).  Author_fixed skills are
unchanged.  The P3 regex heuristic guard is retained ONLY for author_fixed skills
to prevent silent wrong-page substitution; it is not applied to ask_parameterized
skills (which use the schema-driven source_binding.input_param path instead).
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Any

import yaml

from framework.skill_builder.prompt_registry import get_registry  # ADR-030 C4

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ADR-032 P3 guard — Confluence page-reference detection + source-match assertion
#
# This heuristic detects whether the skill invocation carries an explicit
# Confluence page reference in the user-supplied inputs and hard-fails if the
# retrieved passages do not match the requested page.
#
# TEMPORARY: this regex-on-input heuristic will be replaced by the
# source_binding.input_param schema field once ADR-032 P1 ships. See
# ADR-032 §C and §D.3 for the full design. DECISION-012 options A/B/C remain
# open and are NOT pre-empted by this guard.
# ---------------------------------------------------------------------------

# Patterns for recognising a Confluence page reference in a free-text input.
# Ordered most-specific first. All groups capture the numeric page id.
_CONFLUENCE_PAGE_REF_PATTERNS = [
    # querystring form: pageId=18625350641
    re.compile(r"[?&]pageId=(\d+)", re.IGNORECASE),
    # viewpage.action form: /pages/viewpage.action?pageId=<id>
    re.compile(r"/pages/viewpage\.action\?pageId=(\d+)", re.IGNORECASE),
    # REST short-form: /pages/<id>  (must NOT match /pages/viewpage — covered above)
    re.compile(r"/pages/(\d+)(?:[/?#]|$)"),
    # bare all-digit token presented as an explicit pageId= key-value pair
    # in the raw input string (e.g. "pageId=18625350641" without leading ?)
    re.compile(r"\bpageId=(\d+)\b", re.IGNORECASE),
    # A1 (BUG-queue-990fe): space-separated / natural-language form, e.g.
    # "for Confluence pageId 18625350641" or "page id: 18625350641".
    # The LENGTH CONSTRAINT (≥8 digits) avoids false-positives on short prose
    # numbers; Confluence pageIds in this env are ~11 digits (e.g. 18625350641).
    # RETIRED when ADR-032 P2-Exec ships (P1 source_binding.input_param
    # replaces all regex heuristics — see ADR-032 §E.4).
    re.compile(r"(?i)\bpage[\s_-]?id\b[\s:]+(\d{8,})"),
]


class ConfluencePageNotInKBError(Exception):
    """Raised when the user requested a specific Confluence page that is not
    in the knowledge base, is not allow-listed for ephemeral fetch, or when
    the Confluence adapter is unavailable for an ask_parameterized skill.

    This is a hard-fail — the executor MUST NOT substitute a different page
    or return partial/empty content silently. ADR-032 P3 / ADR-031.

    Parameters
    ----------
    page_id:
        The Confluence page ID the consumer requested.
    skill_name:
        The workflow skill name (for actionable error messages).
    reason:
        Optional additional context for the error message (e.g., allow-list
        violation, adapter unavailability).  Consumer-safe — no provider
        internals.
    """
    def __init__(self, page_id: str, skill_name: str = "", reason: str = ""):
        self.page_id = page_id
        self.skill_name = skill_name
        self.reason = reason
        if reason:
            msg = (
                f"Requested Confluence page {page_id} cannot be used by skill "
                f"'{skill_name}': {reason}"
            )
        else:
            msg = (
                f"Requested Confluence page {page_id} is not in the knowledge base. "
                "This skill does not substitute a different page. "
                f"Run: kb-cli ingest --page-id {page_id} --persona tpm. "
                "Then retry your request."
            )
        super().__init__(msg)


def _extract_confluence_page_ids(inputs: dict) -> list[str]:
    """Extract Confluence page IDs referenced in the skill inputs.

    Scans every string value in `inputs` against known Confluence URL/id
    patterns. Returns a deduplicated list of numeric page-id strings, or an
    empty list if no explicit page reference is found.

    Conservative: only all-digit tokens that match an explicit Confluence URL
    pattern or a bare `pageId=<digits>` key-value are treated as page refs.
    Arbitrary numbers embedded in prose are NOT matched.

    ADR-032 P3 — heuristic guard; replaced by source_binding.input_param in P1.
    """
    found: list[str] = []
    seen: set[str] = set()
    for v in inputs.values():
        if not isinstance(v, str):
            continue
        for pattern in _CONFLUENCE_PAGE_REF_PATTERNS:
            for m in pattern.finditer(v):
                pid = m.group(1)
                if pid not in seen:
                    seen.add(pid)
                    found.append(pid)
    return found


def _passage_matches_page_id(passage: dict, requested_page_id: str) -> bool:
    """Return True if the passage's citation/metadata corresponds to the requested
    Confluence page id.

    Checks (in order):
      1. passage["metadata"]["page_id"] — set by SearchWikiRetriever and
         ReadWikiPageRetriever on every Result object.
      2. requested_page_id appears anywhere in passage["citation"] — covers
         Confluence URLs (https://.../wiki/...?pageId=<id>), wiki:// URIs
         (wiki://<page_id>), and fixture paths.

    ADR-032 P3 — bound to the real field names in Result + retriever metadata.
    """
    meta = passage.get("metadata") or {}
    meta_page_id = str(meta.get("page_id", "")).strip()
    if meta_page_id and meta_page_id == requested_page_id:
        return True
    citation = str(passage.get("citation", "")).strip()
    if citation and requested_page_id in citation:
        return True
    return False

REPO_ROOT = Path(__file__).resolve().parents[2]
_TELEMETRY_DIR = Path.home() / ".kbf" / "telemetry"


def _any_promoted_skill_requires_ephemeral(workflow_skills_dir) -> bool:
    """Return True if any skill YAML under workflow_skills_dir has
    source_binding.mode == ask_parameterized and source_binding.ingest_on_demand == true.

    Used by mcp_server lifespan to decide whether to initialize the Confluence
    adapter at startup.  Graceful: any unreadable/unparseable YAML is skipped.

    ADR-032 P2-Infra.
    """
    for skill_path in Path(workflow_skills_dir).rglob("*.yaml"):
        try:
            cfg = yaml.safe_load(skill_path.read_text()) or {}
            sb = cfg.get("source_binding") or {}
            if sb.get("mode") == "ask_parameterized" and sb.get("ingest_on_demand", False):
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# ADR-032 P2-Exec — Ephemeral in-process TTL cache
#
# Stores fetched-page passages per (page_id, content_hash) key for up to
# ephemeral_ttl_seconds.  Thread-safe; process-local; NEVER written to disk
# or any persistent store.  See ADR-032 §E.5 for the full spec.
#
# INVARIANT: _EphemeralCache.put() is the ONLY place ephemeral content is
# stored.  WikiMetadataStore.add() and IncidentVectorStore.upsert() are
# NEVER called in the ephemeral path.  A future developer MUST NOT add
# calls to those stores from _retrieve_ask_parameterized.
# ---------------------------------------------------------------------------

class _EphemeralCache:
    """In-process TTL cache for ephemeral Confluence page fetch results.

    Thread-safe via threading.Lock.  Never persisted to disk.  Process-local
    (each uvicorn worker maintains an independent cache — correctness does not
    depend on cross-worker sharing; the cache is a latency optimisation only).

    ADR-032 §E.5.
    """

    _MAX_SIZE = 50  # LRU eviction at cap — prevents unbounded growth

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key → (value, fetched_at: float, ttl_seconds: int)
        self._store: dict[str, tuple[Any, float, int]] = {}

    def get(self, key: str, ttl: int) -> Any | None:
        """Return cached value if present and within TTL, else None (evict expired)."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, fetched_at, stored_ttl = entry
            effective_ttl = min(ttl, stored_ttl)
            if time.time() - fetched_at > effective_ttl:
                del self._store[key]
                return None
            return value

    def put(self, key: str, value: Any, ttl: int) -> None:
        """Insert or replace an entry.  LRU-evicts oldest entry at cap."""
        with self._lock:
            if len(self._store) >= self._MAX_SIZE:
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest_key]
            self._store[key] = (value, time.time(), ttl)

    def clear(self) -> None:
        """Evict all entries (used in tests; not called in production paths)."""
        with self._lock:
            self._store.clear()


# Module-level singleton — shared across all WorkflowExecutor instances in this
# process.  Each uvicorn worker has its own copy (separate memory space).
_ephemeral_cache = _EphemeralCache()


def _resolve_page_id(page_ref: str) -> str:
    """Extract a numeric Confluence page ID from a page reference string.

    Handles the following forms (in order of specificity):
      - Full Confluence URL with ?pageId= query param
      - /pages/viewpage.action?pageId=<id> REST form
      - /wiki/spaces/.../pages/<id>/ path form
      - Bare pageId=<digits> key-value form (with or without leading ?)
      - Natural-language "pageId 18625350641" or "pageId: 18625350641" form
      - Bare all-digit string (passed directly as the page_id input value)

    Returns the numeric string on match, or the original string unchanged if
    no pattern matches (e.g., caller passed an unrecognised reference — the
    upstream trust check will reject it as unusable).

    ADR-032 P2-Exec: used for structural input_param resolution (not regex
    heuristic scanning over free-form prose).
    """
    for pattern in _CONFLUENCE_PAGE_REF_PATTERNS:
        m = pattern.search(page_ref)
        if m:
            return m.group(1)
    # Bare all-digit string — consumer passed the page ID directly (e.g., "18625350641")
    if page_ref.strip().isdigit():
        return page_ref.strip()
    return page_ref


def _extract_space_key_from_url(page_ref: str) -> str | None:
    """Extract a Confluence space key from a /spaces/<SPACE>/ URL path segment.

    Returns the space key string (e.g. "FA") on match, or None if the page ref
    is a bare numeric ID or URL without a /spaces/ component.

    ADR-032 §Known Gaps: bare numeric IDs cannot be space-checked from URL alone;
    callers must fetch metadata to determine the space.
    """
    m = re.search(r"/spaces/([^/?#]+)/", page_ref, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _make_raw_item_ref(page_id: str):
    """Build a RawItemRef (or compatible dict) for a Confluence page fetch.

    Uses the core.interfaces.RawItemRef dataclass when available; falls back
    to a plain object with the expected attributes so callers that treat the
    adapter's `fetch()` return as a plain dict still work in unit tests.
    """
    try:
        from ..core.interfaces import RawItemRef
        return RawItemRef(kind="confluence_page", source="confluence", source_id=page_id)
    except (ImportError, TypeError):
        # Fallback: adapter.fetch() accepts a dict in some test environments.
        class _Ref:
            kind = "confluence_page"
            source = "confluence"
            source_id = page_id
        return _Ref()


def _extract_body_text(raw_item) -> str:
    """Extract usable body text from a Confluence adapter fetch response.

    Supports multiple response shapes:
      - raw_item.payload["body"]["storage"]["value"]   (native Confluence REST v1)
      - raw_item.payload["body"]                       (simplified adapter output)
      - raw_item.payload["content"]                    (emcp_direct variant)
      - raw_item.text                                  (pre-extracted text field)
      - str(raw_item)                                  (last resort)

    Returns empty string if no usable text found.
    ADR-032 §E.2 body_html chain.
    """
    payload = getattr(raw_item, "payload", None) or {}
    if isinstance(payload, dict):
        # Native Confluence REST shape
        body_storage = (
            payload.get("body", {})
            .get("storage", {})
            .get("value", "")
        ) if isinstance(payload.get("body"), dict) else ""
        if body_storage:
            return body_storage
        # Simplified body string
        body_plain = payload.get("body", "")
        if isinstance(body_plain, str) and body_plain:
            return body_plain
        # emcp_direct content field
        content = payload.get("content", "")
        if isinstance(content, str) and content:
            return content
    # Pre-extracted text attribute
    text_attr = getattr(raw_item, "text", None)
    if isinstance(text_attr, str) and text_attr:
        return text_attr
    return ""


class WorkflowExecutor:
    def __init__(
        self,
        store=None,
        llm=None,
        retrievers=None,
        shim_kb=None,
        confluence_adapter=None,
    ):
        """Construct a WorkflowExecutor.

        Parameters
        ----------
        store:
            Legacy direct store (IncidentVectorStore, etc.). Used only for the
            fallback direct-store path in _retrieve_for_inputs.
        llm:
            LLMClient for schema-bounded extraction in _llm_extract_fields.
        retrievers:
            dict of {name -> retriever_callable} (e.g. search_wiki,
            vector_search). When provided, _retrieve_for_inputs uses the
            retriever named in each requires_extractions[i].kb's KB-card
            `retrieval_tools` instead of falling back to fixtures.
        shim_kb:
            ShimKb instance — used to resolve a KB name like
            'tpm.weekly_exec_review_26ai' to its card (kind, retrieval_tools).
        confluence_adapter:
            Optional Confluence adapter instance (emcp_direct / native / etc.)
            for ask_parameterized skill ephemeral fetch (ADR-032 P2-Exec).
            None = adapter not available; ask_parameterized skills will
            hard-fail with an actionable message at consumption time (never
            silently fall back to a different page or empty content).
            Backward-compatible: existing constructions that omit this param
            default to None — author_fixed skills are unaffected.
        """
        self.store = store
        self.llm = llm
        self.retrievers = retrievers or {}
        self.shim_kb = shim_kb
        # ADR-032 P2-Exec: Confluence adapter for ask_parameterized ephemeral fetch.
        # None = not configured; ask_parameterized skills hard-fail actionably.
        # NEVER used for author_fixed skills.
        self.confluence_adapter = confluence_adapter

    def execute(self, skill_yaml_path: Path, inputs: dict) -> dict:
        t_start = time.monotonic()
        cfg = yaml.safe_load(Path(skill_yaml_path).read_text())
        skill_name = cfg.get("workflow_skill")
        persona = cfg.get("persona")
        log.info("executing workflow skill %s for persona %s with inputs=%s",
                 skill_name, persona, inputs)

        # 1. Resolve source set (procedural discovery or static)
        sources = self._resolve_sources(cfg, inputs)
        log.info("resolved %d sources", len(sources))

        # 2. Retrieve relevant ContentItems for the inputs
        t_retrieve_start = time.monotonic()
        passages = self._retrieve_for_inputs(cfg, inputs, sources)
        retrieve_ms = int((time.monotonic() - t_retrieve_start) * 1000)

        # 3. Synthesize structured data per slide_mapping
        rendered_data = self._synthesize(cfg, inputs, passages)

        # 4. Render to artifact
        t_render_start = time.monotonic()
        artifact_bytes = self._render(cfg, rendered_data)
        render_ms = int((time.monotonic() - t_render_start) * 1000)

        # 5. Deliver
        t_deliver_start = time.monotonic()
        delivery_result = self._deliver(cfg, artifact_bytes, inputs)
        deliver_ms = int((time.monotonic() - t_deliver_start) * 1000)

        total_ms = int((time.monotonic() - t_start) * 1000)

        output_path = (
            delivery_result.get("url")
            or delivery_result.get("path")
            or delivery_result.get("archive")
            or ""
        )

        # 9. Cost telemetry
        self._record_cost(skill_name, persona, {
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_calls": 0,
            "latency_ms": total_ms,
            "render_ms": render_ms,
            "deliver_ms": deliver_ms,
        })

        # 10. Eval gold-set recording
        self._record_eval_entry(skill_name, inputs, output_path)

        # ADR-032 P2-API: detect ephemeral fetch from passages metadata.
        # Any passage with metadata.ephemeral=True indicates an ask_parameterized
        # ephemeral fetch occurred.  Propagate to caller (ask.py response builder).
        ephemeral_fetched = any(
            p.get("metadata", {}).get("ephemeral") is True for p in passages
        )
        ephemeral_page_id = ""
        if ephemeral_fetched:
            for p in passages:
                if p.get("metadata", {}).get("ephemeral") is True:
                    ephemeral_page_id = str(p.get("metadata", {}).get("page_id", ""))
                    break

        result: dict = {
            "skill": skill_name,
            "persona": persona,
            "inputs": inputs,
            "rendered_data": rendered_data,
            "delivery": delivery_result,
            "executed_at": datetime.utcnow().isoformat() + "Z",
            "metrics": {
                "latency_ms": total_ms,
                "render_ms": render_ms,
                "deliver_ms": deliver_ms,
            },
        }
        # ADR-032 P2-API response fields — present only for ephemeral fetches.
        if ephemeral_fetched:
            result["source_fetched_on_demand"] = True
            result["source_fetched_page_id"] = ephemeral_page_id
        return result

    # ------------------------------------------------------------------
    def _record_cost(self, skill_name: str, persona: str, metrics: dict) -> None:
        """Write cost telemetry for this workflow execution.
        metrics: {tokens_in, tokens_out, llm_calls, latency_ms, render_ms, deliver_ms}
        """
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "operation_kind": "workflow_execute",
            "skill_name": skill_name,
            "persona": persona,
            **metrics,
        }
        costs_file = _TELEMETRY_DIR / "workflow_costs.jsonl"
        with costs_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        log.debug("cost telemetry written: skill=%s latency_ms=%d", skill_name, metrics.get("latency_ms", 0))

    def _record_eval_entry(self, skill_name: str, inputs: dict, output_path: str) -> None:
        """Record this execution as a potential gold-set entry for eval."""
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "skill_name": skill_name,
            "inputs": inputs,
            "output_path": output_path,
            "candidate": True,
        }
        eval_file = _TELEMETRY_DIR / "workflow_eval_candidates.jsonl"
        with eval_file.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        log.debug("eval candidate recorded: skill=%s", skill_name)

    # ------------------------------------------------------------------
    def _resolve_sources(self, cfg: dict, inputs: dict) -> list[dict]:
        # Phase 1: simple stub — return inputs as the source ref.
        # Phase 2/3: procedural discovery via Adapter.discover()
        if "sources" in cfg:
            return cfg["sources"]
        return []

    def _retrieve_for_inputs(self, cfg: dict, inputs: dict, sources: list[dict]) -> list[dict]:
        """Retrieve passages for a skill invocation.

        ADR-032 P2-Exec: the first branch handles ask_parameterized skills via
        the ephemeral Confluence fetch path.  Author_fixed skills follow the
        original retriever → store → fixture path below.

        ask_parameterized path:
          Returns passages from the Confluence page specified by
          inputs[source_binding.input_param].  Uses ephemeral in-process TTL
          cache; NEVER writes to WikiMetadataStore or any persistent store.

        author_fixed path (unchanged):
          1. Live retrievers (search_wiki, vector_search, ...) when KB-card
             specifies retrieval_tools and the corresponding retriever is
             registered.
          2. Legacy direct store query (incident_summary / vector_knn).
          3. Fixture data — last resort for laptop dev when no live data exists.

        P3 guard (author_fixed only):
          If the user supplied an explicit Confluence page reference in inputs
          for an author_fixed skill, verify that at least one retrieved passage
          actually corresponds to that page; hard-fail if not.  This guard is
          NOT applied to ask_parameterized skills — they use the schema-driven
          input_param path instead (ADR-032 §E.4 retirement plan).
        """
        # ------------------------------------------------------------------
        # ADR-032 P2-Exec: ask_parameterized branch — MUST be first.
        # ------------------------------------------------------------------
        source_binding = cfg.get("source_binding") or {}
        sb_mode = source_binding.get("mode", "author_fixed")

        if sb_mode == "ask_parameterized":
            # Schema-driven path: page_id comes from source_binding.input_param,
            # NOT from regex scanning over free-form prose.  This is the structural
            # replacement for the BUG-990fe P3 regex heuristic for parameterized skills.
            return self._retrieve_ask_parameterized(cfg, inputs, source_binding)

        # ------------------------------------------------------------------
        # author_fixed path — unchanged behaviour
        # ------------------------------------------------------------------
        passages: list[dict] = []
        query_text = " ".join(str(v) for v in inputs.values() if v)

        if self.retrievers and self.shim_kb:
            # Build a quick lookup: short-name → card. KB names in
            # requires_extractions look like 'tpm.weekly_exec_review_26ai'
            # but cards live keyed by their short name.
            all_cards = self.shim_kb.all_cards() if hasattr(self.shim_kb, "all_cards") else []
            cards_by_name = {c.get("name"): c for c in all_cards if c.get("name")}
            for req in cfg.get("requires_extractions", []):
                kb_full_name = req.get("kb") or ""
                short_name = kb_full_name.split(".")[-1]
                card = cards_by_name.get(short_name) or cards_by_name.get(kb_full_name)
                if not card:
                    log.warning("executor: KB %s not found in shim_kb (short=%s) — skipping",
                                kb_full_name, short_name)
                    continue
                tools = card.get("retrieval_tools") or []
                for tool_name in tools:
                    retriever = self.retrievers.get(tool_name)
                    if retriever is None:
                        log.warning(
                            "executor: KB %s requires retriever %s but it's not registered",
                            kb_full_name, tool_name,
                        )
                        continue
                    try:
                        results = retriever(query=query_text, persona=card.get("persona"))
                    except TypeError:
                        results = retriever(query=query_text)
                    except Exception as exc:  # noqa: BLE001
                        log.error("executor: retriever %s raised: %s", tool_name, exc)
                        continue
                    for r in results or []:
                        # Result protocol: .text, .citation_url, .metadata
                        passages.append({
                            "text": getattr(r, "text", "") or "",
                            "citation": getattr(r, "citation_url", "") or "",
                            "metadata": getattr(r, "metadata", {}) or {},
                            "kb": kb_full_name,
                        })
                    if passages:
                        log.info(
                            "executor: retrieved %d passages from %s via %s",
                            len(results) if results else 0, kb_full_name, tool_name,
                        )
                        break  # first retriever that yielded results wins for this KB

        # Legacy direct-store fallback (incident_summary etc.)
        if not passages and self.store:
            from ..core.interfaces import Query
            for req in cfg.get("requires_extractions", []):
                if "incident_id" in inputs:
                    q = Query(kind="incident_summary", payload={"incident_id": inputs["incident_id"]})
                elif "release_id" in inputs:
                    q = Query(kind="filter", payload={"source_id": inputs["release_id"]})
                else:
                    q = Query(kind="vector_knn", payload={"query": query_text})
                results = self.store.query(q)
                for r in results:
                    passages.append({
                        "text": r.text,
                        "citation": r.citation_url,
                        "metadata": r.metadata,
                    })

        # Last-resort fixture fallback — only when truly nothing else worked.
        if not passages:
            log.warning(
                "executor: no live retriever results — falling back to fixture data "
                "(this is laptop-mode behaviour; production should always hit a real retriever)"
            )
            passages = self._load_fixture_passages(inputs, cfg=cfg)

        # ------------------------------------------------------------------
        # ADR-032 P3 guard — no-silent-substitution assertion (author_fixed only).
        #
        # If the user supplied an explicit Confluence page reference in inputs
        # for an AUTHOR_FIXED skill, verify that at least one retrieved passage
        # actually corresponds to that page.  Hard-fail if not.
        #
        # This guard is CONDITIONAL: it applies only to author_fixed skills.
        # ask_parameterized skills do NOT reach this block — they returned
        # earlier via _retrieve_ask_parameterized which enforces correctness
        # through the schema-driven source_binding.input_param path
        # (ADR-032 §E.4 — structural retirement of regex heuristic for
        # parameterized skills; regex retained for author_fixed).
        # ------------------------------------------------------------------
        requested_page_ids = _extract_confluence_page_ids(inputs)
        if requested_page_ids:
            skill_name = cfg.get("workflow_skill", "")
            for requested_pid in requested_page_ids:
                matching = [
                    p for p in passages
                    if _passage_matches_page_id(p, requested_pid)
                ]
                if not matching:
                    log.error(
                        "executor P3 guard (author_fixed): requested Confluence page %s not "
                        "found in retrieved passages (skill=%s). Hard-failing — retrieved %d "
                        "passage(s) from different page(s); substitution is forbidden.",
                        requested_pid, skill_name, len(passages),
                    )
                    raise ConfluencePageNotInKBError(
                        page_id=requested_pid, skill_name=skill_name
                    )

        return passages

    # ------------------------------------------------------------------
    # ADR-032 P2-Exec — ask_parameterized ephemeral fetch path
    # ------------------------------------------------------------------

    def _retrieve_ask_parameterized(
        self,
        cfg: dict,
        inputs: dict,
        source_binding: dict,
    ) -> list[dict]:
        """Fetch the user-supplied Confluence page ephemerally and extract fields.

        This method implements Option C (ADR-032 §C) for ask_parameterized skills.

        Trust enforcement order:
          1. ingest_on_demand must be true in source_binding.
          2. self.confluence_adapter must not be None (adapter must be configured).
          3. TTL cache check — return cached passages if present (skip fetch).
          4. Single adapter.fetch() call — obtains both space metadata and content
             in one round-trip (D2 fix: no fetch_metadata(); emcp_direct.fetch()
             returns RawItem where metadata["space"] is already the space key string).
          5. space_allow_list check on the fetched space key — enforced BEFORE any
             LLM extraction.  If the page is fetched but its space is not allow-
             listed: discard the content, hard-fail, never extract, never cache.
          6. LLM extraction on the already-fetched content — no second fetch.

        One round-trip total (D2 fix — ADR-032 D2).

        The space check (step 5) runs AFTER the single fetch but BEFORE extraction.
        If the space is not allow-listed the fetched content is discarded immediately
        (never passed to the LLM, never written to the cache).  This preserves the
        no-persist and no-extract-before-allow-list invariants even with the
        single-fetch model.

        emcp_direct.fetch() return shape (confirmed from emcp_direct.normalize()):
          raw_item.metadata["space"] = str  — the space key (e.g. "FA")
            extracted from meta["space"]["key"] in the MCP server response.
          raw_item.metadata["title"]  = str
          raw_item.metadata["url"]    = str | None
          raw_item.payload["body"]["storage"]["value"] = str  — page body (markdown)

        NEVER writes to WikiMetadataStore, IncidentVectorStore, or any other
        persistent store.  Ephemeral content is cached in-process only
        (_ephemeral_cache; TTL from source_binding.ephemeral_ttl_seconds; 300s
        default) and is NEVER persisted to disk.

        Raises
        ------
        ConfluencePageNotInKBError
            On any trust violation (ingest_on_demand false, adapter None, space
            not allow-listed) or adapter fetch failure.  Always actionable;
            never exposes provider internals.
        """
        skill_name = cfg.get("workflow_skill", "")
        input_param = source_binding.get("input_param", "")
        page_ref = str(inputs.get(input_param, "")).strip()
        page_id = _resolve_page_id(page_ref)
        ingest_on_demand = source_binding.get("ingest_on_demand", False)
        space_allow_list: list[str] = source_binding.get("space_allow_list") or []
        ttl = int(source_binding.get("ephemeral_ttl_seconds", 300))

        # ----------------------------------------------------------------
        # Trust check 1: ingest_on_demand must be true.
        # ----------------------------------------------------------------
        if not ingest_on_demand:
            raise ConfluencePageNotInKBError(
                page_id=page_id,
                skill_name=skill_name,
                reason=(
                    "This skill is configured with ingest_on_demand: false. "
                    "The requested page is not in the knowledge base. "
                    f"Run: kb-cli ingest --page-id {page_id} --persona tpm, "
                    "or contact the skill author to enable ingest_on_demand."
                ),
            )

        # ----------------------------------------------------------------
        # Trust check 2: adapter must be configured (never silent fallback).
        # ----------------------------------------------------------------
        if self.confluence_adapter is None:
            raise ConfluencePageNotInKBError(
                page_id=page_id,
                skill_name=skill_name,
                reason=(
                    f"ask_parameterized skill '{skill_name}' requires live Confluence "
                    "access to fetch the page you specified, but the Confluence adapter "
                    "is not configured in this deployment. "
                    "Contact your administrator to configure the Confluence adapter "
                    "(framework/config/adapters/confluence.yaml)."
                ),
            )

        # ----------------------------------------------------------------
        # Space key from URL — fast path (no API call needed).
        # For bare numeric IDs the space cannot be determined without a fetch;
        # we defer the space check to AFTER the single fetch (step 5 below).
        # ----------------------------------------------------------------
        space_key_from_url = _extract_space_key_from_url(page_ref)

        # Early space check: if space is deducible from URL and is not allowed,
        # reject BEFORE making any API call (trust check before network call).
        if space_allow_list and space_key_from_url is not None:
            if space_key_from_url not in space_allow_list:
                log.warning(
                    "ask_parameterized trust check FAILED (pre-fetch): page_id=%s "
                    "space=%r not in allow-list %s for skill=%s. "
                    "Hard-failing — adapter NOT called.",
                    page_id, space_key_from_url, space_allow_list, skill_name,
                )
                raise ConfluencePageNotInKBError(
                    page_id=page_id,
                    skill_name=skill_name,
                    reason=(
                        f"Confluence space '{space_key_from_url}' is not in the skill's "
                        f"allow-list {space_allow_list}. Contact the skill author to add "
                        "this space to source_binding.space_allow_list."
                    ),
                )

        # ----------------------------------------------------------------
        # In-process TTL cache check — skip fetch if recently fetched.
        # Key: "ephemeral:{page_id}".
        # ----------------------------------------------------------------
        cache_key = f"ephemeral:{page_id}"
        cached = _ephemeral_cache.get(cache_key, ttl)
        if cached is not None:
            log.debug(
                "ask_parameterized: TTL cache hit for page_id=%s skill=%s — "
                "returning cached passages without adapter call.",
                page_id, skill_name,
            )
            return cached

        # ----------------------------------------------------------------
        # D2 FIX — Single adapter.fetch() call (ADR-032 D2).
        #
        # emcp_direct.fetch() returns a RawItem where metadata["space"] is
        # already the space key string (e.g. "FA") — emcp_direct.normalize()
        # sets meta_dict["space"] = (meta.get("space") or {}).get("key").
        # No fetch_metadata() method exists on ANY adapter (D2 root cause).
        # One round-trip total: fetch() provides both space info and content.
        #
        # INVARIANT: do NOT add calls to WikiMetadataStore.add(),
        # IncidentVectorStore.upsert(), or any other persistent store in
        # this method.  Ephemeral content must never leak into the shared KB.
        # ----------------------------------------------------------------
        try:
            raw_item = self.confluence_adapter.fetch(
                _make_raw_item_ref(page_id)
            )
        except Exception as exc:
            raise ConfluencePageNotInKBError(
                page_id=page_id,
                skill_name=skill_name,
                reason=(
                    f"Confluence adapter could not fetch page {page_id}: "
                    f"{type(exc).__name__}. Verify the page exists and you have access."
                ),
            ) from None

        # ----------------------------------------------------------------
        # Space allow-list check on fetched space key — BEFORE any extraction.
        #
        # emcp_direct sets raw_item.metadata["space"] to the space key string.
        # We also handle the dict form {"key": "FA"} defensively.
        # If the space is not allow-listed: discard fetched content, hard-fail,
        # never extract, never cache.  The content is NOT passed to the LLM.
        # ----------------------------------------------------------------
        raw_meta = raw_item.metadata or {}
        space_val = raw_meta.get("space") or raw_meta.get("spaceKey") or ""
        # Handle both string form ("FA") and dict form ({"key": "FA"})
        if isinstance(space_val, dict):
            fetched_space: str = space_val.get("key") or space_val.get("spaceKey") or ""
        else:
            fetched_space = str(space_val)

        if space_allow_list and fetched_space and fetched_space not in space_allow_list:
            log.warning(
                "ask_parameterized trust check FAILED (post-fetch): page_id=%s "
                "space=%r not in allow-list %s for skill=%s. "
                "Discarding fetched content — never extracted, never cached.",
                page_id, fetched_space, space_allow_list, skill_name,
            )
            # Discard fetched content — trust invariant: extraction never started.
            raise ConfluencePageNotInKBError(
                page_id=page_id,
                skill_name=skill_name,
                reason=(
                    f"Confluence space '{fetched_space}' is not in the skill's allow-list "
                    f"{space_allow_list}. Contact the skill author to add this space "
                    "to source_binding.space_allow_list."
                ),
            )

        if space_allow_list and not fetched_space:
            # Could not determine space from fetch response — log warning and proceed.
            log.warning(
                "ask_parameterized: could not determine space key for page_id=%s from "
                "fetch response — proceeding without space allow-list check "
                "(space_allow_list=%s).",
                page_id, space_allow_list,
            )

        # Extract page body text from adapter response shape.
        body_text = _extract_body_text(raw_item)
        if not body_text:
            raise ConfluencePageNotInKBError(
                page_id=page_id,
                skill_name=skill_name,
                reason=(
                    f"Confluence page {page_id} has no usable body content. "
                    "Verify the page is not empty."
                ),
            )

        # Content hash for cache key refinement (used in audit log + future dedup).
        content_hash = hashlib.sha256(body_text.encode("utf-8", errors="replace")).hexdigest()[:16]

        # Schema-bounded LLM extraction — uses the skill's authored schema.
        # Same _llm_extract_fields path as the INGEST/synthesize flow.
        # Deterministic schema-bounded extraction, not free-form LLM parsing.
        skill_schema = self._lookup_schema(cfg) or {}
        if skill_schema and self.llm is not None:
            try:
                extracted = self._llm_extract_fields(skill_schema, body_text, inputs)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ask_parameterized: LLM extraction failed for page_id=%s skill=%s: %s — "
                    "using raw body text as fallback passage.",
                    page_id, skill_name, exc,
                )
                extracted = {}
        else:
            extracted = {}

        passage_text = (
            json.dumps(extracted, ensure_ascii=False)
            if extracted
            else body_text[:80000]
        )

        # Build citation URL from adapter metadata or fall back to canonical form.
        citation_url = (
            (raw_item.metadata or {}).get("url")
            or (raw_item.metadata or {}).get("web_url")
            or f"confluence://pages/{page_id}"
        )

        kb_ref = ""
        reqs = cfg.get("requires_extractions") or []
        if reqs:
            kb_ref = reqs[0].get("kb", "")

        passages: list[dict] = [{
            "text": passage_text,
            "citation": citation_url,
            "metadata": {
                "page_id": page_id,
                "space": fetched_space,
                "title": (raw_item.metadata or {}).get("title", ""),
                "content_hash": content_hash,
                # EPHEMERAL flag: this content MUST NOT be written to any
                # persistent store.  The flag is informational for callers;
                # the structural guarantee is that this method NEVER calls
                # WikiMetadataStore.add() or any equivalent.
                "ephemeral": True,
                "fetched_at": datetime.utcnow().isoformat() + "Z",
            },
            "kb": kb_ref,
        }]

        # Audit log — every ephemeral fetch logged (spec §10, trust model).
        self._log_ephemeral_fetch(page_id, fetched_space, skill_name, content_hash)

        # Store in in-process TTL cache — the ONLY place this content is stored.
        # NEVER written to disk or any persistent store.
        _ephemeral_cache.put(cache_key, passages, ttl)

        log.info(
            "ask_parameterized: ephemerally fetched page_id=%s space=%s skill=%s "
            "content_hash=%s ttl=%ds",
            page_id, fetched_space, skill_name, content_hash, ttl,
        )
        return passages

    def _log_ephemeral_fetch(
        self,
        page_id: str,
        space_key: str,
        skill_name: str,
        content_hash: str = "",
    ) -> None:
        """Append an audit log entry for each ephemeral Confluence page fetch.

        Written to ~/.kbf/telemetry/ephemeral_fetch.jsonl.
        Fields: ts, page_id, space_key, skill_name, content_hash.
        ADR-032 trust model: every ephemeral fetch is traceable.
        """
        try:
            _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "page_id": page_id,
                "space_key": space_key,
                "skill_name": skill_name,
                "content_hash": content_hash,
            }
            audit_file = _TELEMETRY_DIR / "ephemeral_fetch.jsonl"
            with audit_file.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.warning("ask_parameterized: audit log write failed: %s", exc)

    def _load_fixture_passages(self, inputs: dict, cfg: dict | None = None) -> list[dict]:
        fixtures_dir = REPO_ROOT / "framework" / "_dev_fixtures"
        if not fixtures_dir.exists():
            return []

        input_values = {str(v) for v in inputs.values() if v is not None}

        # 1. Try exact id-based match across all fixture dirs
        for kind_dir in sorted(fixtures_dir.iterdir()):
            if not kind_dir.is_dir():
                continue
            for fpath in sorted(kind_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text())
                except Exception:
                    continue
                id_candidates = {
                    str(data.get("id", "")),
                    str(data.get("source_id", "")),
                    str(data.get("release_id", "")),
                }
                id_candidates.discard("")
                if input_values & id_candidates:
                    return [{
                        "text": json.dumps(data, indent=2),
                        "citation": f"fixture://{fpath.name}",
                        "metadata": data,
                    }]

        # 2. Match against fixture field values (e.g., project="all" matches data.project)
        for kind_dir in sorted(fixtures_dir.iterdir()):
            if not kind_dir.is_dir():
                continue
            for fpath in sorted(kind_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text())
                except Exception:
                    continue
                if any(str(data.get(k)) == str(v) for k, v in inputs.items()
                       if v is not None and k != "project" or str(v) != "all"):
                    return [{
                        "text": json.dumps(data, indent=2),
                        "citation": f"fixture://{fpath.name}",
                        "metadata": data,
                    }]

        # 3. KB-name-based dir match — if skill requires a KB and a matching fixture dir exists
        if cfg:
            for req in cfg.get("requires_extractions", []):
                kb_name = (req.get("kb") or "").split(".")[-1].replace("_", "-")
                for kind_dir in fixtures_dir.iterdir():
                    if not kind_dir.is_dir():
                        continue
                    dir_name = kind_dir.name.replace("_", "-")
                    if dir_name in kb_name or kb_name in dir_name:
                        passages = []
                        for fpath in sorted(kind_dir.glob("*.json")):
                            try:
                                data = json.loads(fpath.read_text())
                                passages.append({
                                    "text": json.dumps(data, indent=2),
                                    "citation": f"fixture://{fpath.name}",
                                    "metadata": data,
                                })
                            except Exception:
                                continue
                        if passages:
                            return passages

        return []

    def _synthesize(self, cfg: dict, inputs: dict, passages: list[dict]) -> dict:
        """Convert retrieved passages → structured slide data.

        Two paths:
          1. LLM-based extraction (preferred): when the skill's KB has a JSON
             schema at framework/parsers/schemas/{persona}/{kb}/v1.json AND
             passage text content exists AND an LLM client is wired, ask the
             model to extract the schema-defined fields from passage text and
             return them as JSON. This is what produces a real exec-review
             PPT with status_bullets / risks_mitigations / overall_rag /
             key_milestones etc. — not just page metadata.
          2. Metadata-merge fallback: if no schema/text/LLM, merge passage
             metadata dicts and dump them as sections. Preserves the old
             behaviour for incident_summary / release_brief style skills
             where retrievers return pre-structured records.

        Field-mapping (cfg.synthesis.field_mapping) is applied last to rename
        schema field names to human-readable slide section labels.
        """
        sections: dict[str, Any] = {}
        extracted: dict[str, Any] = {}
        merged_meta: dict = {}
        for p in passages:
            if isinstance(p.get("metadata"), dict):
                merged_meta.update(p["metadata"])

        # 1. LLM-based extraction from passage text per first KB's schema.
        schema = self._lookup_schema(cfg)
        full_text = "\n\n---\n\n".join(
            p.get("text", "") for p in passages if p.get("text")
        ).strip()

        if schema and full_text and self.llm is not None:
            try:
                extracted = self._llm_extract_fields(schema, full_text, inputs)
                log.info(
                    "synth: LLM extracted %d/%d schema fields from %d chars of passage text",
                    sum(1 for v in extracted.values() if v),
                    len(schema.get("properties", {})),
                    len(full_text),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("synth: LLM extract failed: %s — falling back to metadata", exc)
                extracted = {}

        # 2. Apply field_mapping (from skill yaml) — maps schema field → section label.
        field_mapping = cfg.get("synthesis", {}).get("field_mapping") or {}
        if extracted and field_mapping:
            for schema_field, fm_cfg in field_mapping.items():
                if not isinstance(fm_cfg, dict):
                    continue
                src = fm_cfg.get("source_field", schema_field)
                label = fm_cfg.get("section", schema_field.replace("_", " ").title())
                val = extracted.get(src)
                if val is not None and val != "" and val != []:
                    sections[label] = val
        elif extracted:
            # No mapping — use Title Case on schema field names.
            sections = {k.replace("_", " ").title(): v for k, v in extracted.items()
                        if v not in (None, "", [])}

        # 3. Apply legacy slide_mapping yaml if present (for skills that still
        # use a separate mapping file rather than inlining it).
        mapping_path = cfg.get("synthesis", {}).get("slide_mapping")
        if not sections and mapping_path:
            mp = REPO_ROOT / mapping_path
            if mp.exists():
                mapping = yaml.safe_load(mp.read_text()) or {}
                source = extracted or merged_meta
                for sec_name, sec_cfg in mapping.get("sections", {}).items():
                    src = sec_cfg.get("source_field") if isinstance(sec_cfg, dict) else sec_cfg
                    val = source.get(src)
                    if val is not None:
                        label = sec_cfg.get("section", sec_name) if isinstance(sec_cfg, dict) else sec_name
                        sections[label] = val

        # 4. Last-resort: dump merged passage metadata as sections.
        if not sections:
            log.warning(
                "synth: no schema-extracted fields — falling back to metadata dump"
                " (this is why slides may look like Page Id / Title / Path)"
            )
            sections = {k.replace("_", " ").title(): v for k, v in merged_meta.items()}

        title = (
            cfg.get("synthesis", {}).get("title")
            or extracted.get("project_name")
            or merged_meta.get("title")
            or cfg.get("workflow_skill", "Generated Output")
        )
        # Pass layout directive so the renderer can choose the correct template
        # (ADR-026 Fix 5: weekly_exec_review_v1 single-slide two-column layout).
        layout = cfg.get("synthesis", {}).get("layout", "")

        # Promote top-level extracted fields to the result dict so the
        # weekly_exec_review_v1 renderer can access jira_id, scope etc. directly.
        result: dict = {
            "title": title,
            "subtitle": f"Generated by {cfg.get('workflow_skill')} for inputs={inputs}",
            "sections": sections,
            "extracted": extracted,
            "citations": [p.get("citation") for p in passages if p.get("citation")],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
        if layout:
            result["layout"] = layout
        # Hoist commonly-accessed extracted fields to the top level
        # so layout renderers don't have to dig into extracted nested dict.
        for top_level_field in (
            "jira_id", "scope", "project_name", "overall_rag",
            "orm_status", "assumptions", "status_bullets", "next_steps",
            "key_milestones", "risks_mitigations", "executive_summary",
        ):
            if extracted.get(top_level_field) is not None:
                result[top_level_field] = extracted[top_level_field]
        return result

    # ------------------------------------------------------------------
    # Synthesis helpers
    # ------------------------------------------------------------------

    def _lookup_schema(self, cfg: dict) -> dict | None:
        """Resolve framework/parsers/schemas/{persona}/{kb}/v1.json from the
        skill's first requires_extractions entry. Returns parsed schema or
        None if not found.
        """
        requires = cfg.get("requires_extractions") or []
        if not requires:
            return None
        kb_full = requires[0].get("kb") or ""
        if "." not in kb_full:
            return None
        persona, kb_name = kb_full.split(".", 1)
        schema_path = (
            REPO_ROOT / "framework" / "parsers" / "schemas"
            / persona / kb_name / "v1.json"
        )
        if not schema_path.exists():
            log.info("synth: no schema at %s", schema_path)
            return None
        try:
            return json.loads(schema_path.read_text())
        except Exception as exc:  # noqa: BLE001
            log.warning("synth: failed to parse schema %s: %s", schema_path, exc)
            return None

    def _llm_extract_fields(
        self, schema: dict, text: str, inputs: dict,
    ) -> dict[str, Any]:
        """Ask the LLM to extract schema-defined fields from ``text``.

        Uses chat(response_format=json_object) and returns the parsed dict.
        Truncates the input text to keep prompt size sane.

        JSON parsing delegates to the shared _parse_llm_json_response helper
        from skill_builder/review.py, which applies the full BUG-queue-573e3
        (control-char sanitization) and BUG-queue-44364 (truncation detection)
        fix sequence.  This ensures executor and review._llm_extract cannot
        drift in their parse logic.

        Raises:
            ValueError: with actionable message when JSON parsing fails (never
                        silently returns {} — no-stub-mode policy).
            Exception:  propagates any LLM-call-level exception.
        """
        from framework.skill_builder.review import (
            _parse_llm_json_response,
            _is_content_filter_error,
            ContentFilterRejection,
        )

        properties = schema.get("properties", {})
        required = schema.get("required", [])
        field_lines = []
        for name, prop in properties.items():
            type_hint = prop.get("type", "string")
            enum = prop.get("enum")
            desc = prop.get("description", "")
            extra = f" (one of: {enum})" if enum else ""
            req_tag = " [required]" if name in required else ""
            field_lines.append(f'  - "{name}" ({type_hint}{extra}){req_tag}: {desc}')

        # ADR-031 Group E: raise cap 24000→80000 chars for parity with
        # review._llm_extract (Group D). gpt-4o input is ~128k tokens.
        # The old 24k cap silently discarded source structure on large pages
        # (e.g. Confluence pages with long WBS tables or multi-section content).
        snippet = text[:80000]

        # ADR-030 C4: prompt via PromptRegistry.
        # Caller pre-joins field_lines with chr(10); template uses {field_lines},
        # {user_request}, {snippet} placeholders (not f-string variables).
        spec = get_registry().get_prompt(
            "executor_extract",
            field_lines=chr(10).join(field_lines),
            user_request=inputs.get("input", ""),
            snippet=snippet,
        )

        try:
            result = self.llm.chat(
                model=spec.model,
                messages=[{"role": "user", "content": spec.text}],
                response_format=spec.response_format,
                max_tokens=spec.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_content_filter_error(exc):
                import uuid as _uuid
                request_id = f"KBF-{_uuid.uuid4().hex[:12].upper()}"
                log.warning(
                    "_llm_extract_fields: content-filter rejection from inference "
                    "provider (requestId=%s): %s", request_id, exc,
                )
                raise ContentFilterRejection(request_id) from None
            raise
        raw = result.get("text", "") if isinstance(result, dict) else str(result)
        tokens_out = result.get("tokens_out") if isinstance(result, dict) else None

        # Delegate to shared parse helper — BUG-queue-573e3 + BUG-queue-44364
        # parity with review._llm_extract.  Raises ValueError with actionable
        # error on irrecoverable failure (no silent {} return).
        try:
            return _parse_llm_json_response(
                raw,
                tokens_out=tokens_out,
                max_tokens=spec.max_tokens,
                n_fields=len(properties),
            )
        except ValueError as exc:
            log.error(
                "_llm_extract_fields: JSON parse failed — "
                "persona=%s skill=[inferred from schema]. Error: %s",
                inputs.get("persona", "?"), exc,
            )
            raise

    def _render(self, cfg: dict, data: dict) -> bytes:
        from ..renderers.registry import get_renderer
        output_format = cfg.get("synthesis", {}).get("output_format") or \
                        cfg.get("trigger", {}).get("on_request", {}).get("output_format") or \
                        "markdown"
        renderer = get_renderer(output_format)
        template = cfg.get("synthesis", {}).get("template")
        return renderer.render(data, template)

    def _deliver(self, cfg: dict, artifact: bytes, inputs: dict) -> dict:
        from ..deliverers.registry import get_deliverer
        delivery = cfg.get("delivery", {"kind": "filesystem"})
        kind = delivery.get("kind", "filesystem")
        deliverer = get_deliverer(kind)
        # Substitute inputs into path template
        dest = dict(delivery)
        if "path" in dest:
            dest["path"] = dest["path"].format(**inputs)
        return deliverer.deliver(artifact, dest)
