"""Workflow executor — runs a workflow skill end-to-end.

Per ADR-016. Steps: source discovery → extract (or read cached) → retrieve →
synthesize → render → deliver → cost telemetry → eval recording.

Laptop-mode friendly: uses FilestoreContentStore + filesystem deliverer when no
ADB/Vault/OCI configured.
"""
from __future__ import annotations
import json
import logging
import os
import re
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
]


class ConfluencePageNotInKBError(Exception):
    """Raised when the user requested a specific Confluence page that is not
    in the knowledge base and no matching passage was retrieved.

    This is a hard-fail — the executor MUST NOT substitute a different page
    or return partial/empty content silently. ADR-032 P3 / ADR-031.
    """
    def __init__(self, page_id: str, skill_name: str = ""):
        self.page_id = page_id
        self.skill_name = skill_name
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


class WorkflowExecutor:
    def __init__(self, store=None, llm=None, retrievers=None, shim_kb=None):
        """retrievers: dict of {name -> retriever_callable} (e.g. search_wiki,
        vector_search). When provided, _retrieve_for_inputs uses the
        retriever named in each requires_extractions[i].kb's KB-card
        `retrieval_tools` instead of falling back to fixtures.

        shim_kb: ShimKb instance — used to resolve a KB name like
        'tpm.weekly_exec_review_26ai' to its card (kind, retrieval_tools).
        """
        self.store = store
        self.llm = llm
        self.retrievers = retrievers or {}
        self.shim_kb = shim_kb

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

        return {
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
        """Retrieve passages for each required KB. Priority order:
          1. Live retrievers (search_wiki, vector_search, ...) when KB-card
             specifies retrieval_tools and the corresponding retriever is
             registered. This is the prod/live path and the only way to get
             actual ingested Confluence content into the rendered artifact.
          2. Legacy direct store query (incident_summary / vector_knn).
          3. Fixture data — last resort for laptop dev when no live data
             exists. Previously this fell through too eagerly and rendered
             tpm_weekly_ops fixture content into a 26ai weekly-exec PPT,
             surfaced when the artifact-render hook went live.
        """
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
        # ADR-032 P3 guard — no-silent-substitution assertion.
        #
        # If the user supplied an explicit Confluence page reference in inputs,
        # verify that at least one retrieved passage actually corresponds to that
        # page.  If none do, hard-fail with an actionable message — NEVER fall
        # through to a different page's content.
        #
        # This is the "no-silent-degradation" invariant from ADR-031 applied to
        # source selection: if a page was requested, the wrong page is always
        # wrong, and silence is the worst possible error signal.
        #
        # TEMPORARY heuristic: regex-on-input detection will be replaced by
        # source_binding.input_param schema field when ADR-032 P1 ships.
        # DECISION-012 options A/B/C remain open and unconstrained by this guard.
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
                        "executor P3 guard: requested Confluence page %s not found in "
                        "retrieved passages (skill=%s). Hard-failing — retrieved %d "
                        "passage(s) from different page(s); substitution is forbidden.",
                        requested_pid, skill_name, len(passages),
                    )
                    raise ConfluencePageNotInKBError(
                        page_id=requested_pid, skill_name=skill_name
                    )

        return passages

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
