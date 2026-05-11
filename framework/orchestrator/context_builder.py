"""context_builder — top-level orchestrator (ADR-006 / ADR-007).

Phase 3: four-tier routing with Tier-3 multi-persona fanout and cross-source resolution.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from ..core.llm import LLMClient
from .budget import Budget
from .intent_classifier import IntentClassifier, IntentClassification, IntentSignal
from .shim_faaas import ShimFaaas
from .shim_kb import ShimKb
from .synthesizer import Synthesizer, INCIDENT_RCA, GENERIC_QA

log = logging.getLogger(__name__)

# Cross-source reference patterns for stub-mode detection
_INCIDENT_ID_RE = re.compile(r"\bINC-[A-Z0-9-]+\b", re.IGNORECASE)
_TENANT_RE = re.compile(r"\btenant-[a-z0-9-]+\b", re.IGNORECASE)
_POD_RE = re.compile(r"\bpod-[a-z0-9-]+\b", re.IGNORECASE)
_RELEASE_RE = re.compile(r"\b\d{2}\.\d{2}(?:\.\d+)?\b")


@dataclass
class ContextPacket:
    persona: str
    passages: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    used_kbs: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    cost: dict = field(default_factory=dict)
    confidence: float = 0.0
    notes: str | None = None


class CrossSourceResolver:
    """Follow cross-source references found in primary retrieval results.

    Example: an incident passage mentions tenant-99 → fetch tenant-99 fleet data.
    In stub mode: pattern-match for cross-references; look them up in the store.
    """

    def __init__(self, store=None):
        self.store = store   # BaseStore; may be None if no store is wired

    def resolve(self, query: str, primary_passages: list) -> list:
        """Return additional passages found by following cross-references.

        Looks for incident IDs, tenant IDs, pod IDs, and release versions in
        the primary passage texts, then fetches them from the store.
        """
        if not self.store:
            return []

        refs = self._extract_refs(query, primary_passages)
        if not refs:
            return []

        extra: list = []
        for ref_kind, ref_id in refs:
            try:
                passages = self._fetch_ref(ref_kind, ref_id)
                extra.extend(passages)
            except Exception as e:
                log.debug("cross-source ref fetch failed %s/%s: %s", ref_kind, ref_id, e)

        return extra

    def _extract_refs(self, query: str, passages: list) -> list[tuple[str, str]]:
        """Extract (kind, id) pairs from query text + passage texts."""
        combined = query + " " + " ".join(getattr(p, "text", "") for p in passages)
        refs: list[tuple[str, str]] = []

        for m in _INCIDENT_ID_RE.finditer(combined):
            refs.append(("incident", m.group().upper()))
        for m in _TENANT_RE.finditer(combined):
            refs.append(("tenant", m.group().lower()))
        for m in _POD_RE.finditer(combined):
            refs.append(("pod", m.group().lower()))

        # Deduplicate while preserving order
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for r in refs:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return unique

    def _fetch_ref(self, kind: str, ref_id: str) -> list:
        """Fetch a cross-referenced entity from the store.

        Returns a list of passage-like objects (plain dicts or Passage).
        """
        from ..core.interfaces import Query

        if kind == "incident":
            results = self.store.query(Query(
                kind="incident_summary",
                payload={"incident_id": ref_id},
                limit=1,
            ))
        elif kind in ("tenant", "pod"):
            results = self.store.query(Query(
                kind="filter",
                payload={"source_id": ref_id},
                limit=3,
            ))
        else:
            results = []

        return results


class ContextBuilder:
    """Orchestrator agent. Phase 3: four-tier routing."""

    def __init__(
        self,
        llm: LLMClient,
        shim_faaas: ShimFaaas,
        shim_kb: ShimKb,
        skills_by_persona: dict,         # persona_id -> PersonaContextSkill
        synthesizer: Synthesizer,
        shim_workflows=None,
        cross_source_resolver: CrossSourceResolver | None = None,
        skill_suggester=None,
    ):
        self.llm = llm
        self.shim_faaas = shim_faaas
        self.shim_kb = shim_kb
        self.skills = skills_by_persona
        self.synthesizer = synthesizer
        self.shim_workflows = shim_workflows
        self.cross_source_resolver = cross_source_resolver
        self.skill_suggester = skill_suggester
        self.classifier = IntentClassifier(llm, shim_faaas)

    def answer(self, query: str, budget: Budget | None = None) -> dict:
        budget = budget or Budget()
        t0 = time.time()

        # Build available shim cards for classifier
        available_workflows = self.shim_workflows.all_cards() if self.shim_workflows else []
        available_kbs = self.shim_kb.all_cards() if self.shim_kb else []

        # 1. Classify into four tiers
        classification = self.classifier.classify(
            query,
            available_workflows=available_workflows,
            available_kbs=available_kbs,
        )
        log.info(
            "intent: tier=%d confidence=%.2f persona=%s personas=%s skill=%s",
            classification.tier, classification.confidence,
            classification.persona, classification.personas, classification.workflow_skill,
        )

        # 2. Dispatch by tier
        if classification.tier == 1:
            packet = self._dispatch_tier1(query, classification, budget)
        elif classification.tier == 2:
            packet = self._dispatch_tier2(query, classification, budget)
        elif classification.tier == 3:
            packet = self._dispatch_tier3(query, classification, budget)
        else:  # Tier 4
            packet = self._dispatch_tier4(query, classification, budget)

        # 3. Cross-source enrichment (applies to Tiers 2 and 3 with passages)
        if self.cross_source_resolver and packet.passages:
            extra_results = self.cross_source_resolver.resolve(query, packet.passages)
            if extra_results:
                log.info("cross-source resolver added %d extra results", len(extra_results))
                packet = self._enrich_packet_with_cross_source(packet, extra_results)

        # 4. Synthesize
        schema = INCIDENT_RCA if (classification.persona == "ops_eng") else GENERIC_QA
        answer = self.synthesizer.synthesize(query, packet.passages, schema=schema, budget=budget)

        elapsed_ms = int((time.time() - t0) * 1000)

        return {
            "answer": answer,
            "schema": schema.name,
            "tier": classification.tier,
            "intent": {
                "persona": classification.persona,
                "personas": classification.personas,
                "confidence": classification.confidence,
                "workflow_skill": classification.workflow_skill,
                "reasoning": classification.reasoning,
            },
            "passages": [{"text": p.text, "citation": p.citation.url, "score": p.score}
                         for p in packet.passages],
            "citations": list({p.citation.url for p in packet.passages}),
            "used_kbs": packet.used_kbs,
            "used_tools": packet.used_tools,
            "cost": packet.cost,
            "latency_ms": elapsed_ms,
        }

    # -------------------------------------------------------------------------
    # Tier dispatch methods
    # -------------------------------------------------------------------------
    def _dispatch_tier1(
        self, query: str, cl: IntentClassification, budget: Budget
    ) -> ContextPacket:
        """Tier 1: invoke the matched workflow skill directly."""
        persona = cl.persona or "ops_eng"
        skill = self.skills.get(persona)
        if not skill:
            log.warning("Tier 1: no skill for persona %s; falling back to Tier 2", persona)
            cl_t2 = IntentClassification(
                tier=2, confidence=cl.confidence, persona=persona,
                personas=None, workflow_skill=None, reasoning="tier1 skill missing"
            )
            return self._dispatch_tier2(query, cl_t2, budget)

        intent = IntentSignal(
            primary_persona=persona,
            secondary_personas=[],
            confidence=cl.confidence,
        )
        return skill(query=query, intent_signal=intent, budget=budget)

    def _dispatch_tier2(
        self, query: str, cl: IntentClassification, budget: Budget
    ) -> ContextPacket:
        """Tier 2: single persona KB retrieval."""
        persona = cl.persona or "ops_eng"
        skill = self.skills.get(persona)
        if not skill:
            log.warning("Tier 2: no skill for persona %s; falling back to ops_eng", persona)
            skill = self.skills.get("ops_eng")

        if not skill:
            return ContextPacket(persona=persona, confidence=0.0, notes="no skill available")

        intent = IntentSignal(
            primary_persona=persona,
            secondary_personas=[],
            confidence=cl.confidence,
        )
        return skill(query=query, intent_signal=intent, budget=budget)

    def _dispatch_tier3(
        self, query: str, cl: IntentClassification, budget: Budget
    ) -> ContextPacket:
        """Tier 3: multi-persona fanout — query N personas, merge results."""
        personas = cl.personas or ["ops_eng"]
        if not personas:
            personas = ["ops_eng"]

        intent_signal = cl.to_intent_signal()

        # Split token budget across personas
        per_persona_budget = Budget(
            max_tokens_in=budget.max_tokens_in // max(len(personas), 1),
            max_tokens_out=budget.max_tokens_out,
            max_latency_ms=budget.max_latency_ms,
            max_dollars=budget.max_dollars / max(len(personas), 1),
            max_tool_calls=max(2, budget.max_tool_calls // max(len(personas), 1)),
            max_context_chars=budget.max_context_chars // max(len(personas), 1),
        )

        return self.build_context_multi_persona(
            query=query,
            intent_signal=intent_signal,
            personas=personas,
            budget=per_persona_budget,
        )

    def _dispatch_tier4(
        self, query: str, cl: IntentClassification, budget: Budget
    ) -> ContextPacket:
        """Tier 4: no grounded knowledge — log miss, surface closest matches."""
        persona = cl.persona or "ops_eng"

        # Log the miss to skill_suggester
        if self.skill_suggester:
            try:
                self.skill_suggester.log_miss(
                    query=query,
                    persona=persona,
                    context={
                        "reasoning": cl.reasoning,
                        "confidence": cl.confidence,
                        "available_kbs": len(self.shim_kb.all_cards()),
                    },
                )
            except Exception as e:
                log.warning("skill_suggester.log_miss failed: %s", e)

        # Find closest KB matches to surface as hints
        closest_kbs = self._find_closest_kbs(query, top_n=3)

        notes = (
            "Tier 4: no grounded knowledge for this query. "
            f"Closest KBs: {[k.get('name') for k in closest_kbs]}. "
            "Suggestion: 'Want me to scaffold a workflow skill for queries like this?'"
        )

        return ContextPacket(
            persona=persona,
            passages=[],
            citations=[],
            used_kbs=[],
            used_tools=[],
            cost={"tier": 4, "tool_calls": 0},
            confidence=0.0,
            notes=notes,
        )

    # -------------------------------------------------------------------------
    # Multi-persona fanout (Tier 3)
    # -------------------------------------------------------------------------
    def build_context_multi_persona(
        self,
        query: str,
        intent_signal: IntentSignal,
        personas: list[str],
        budget: Budget,
    ) -> ContextPacket:
        """Tier 3: query N persona context skills, merge ContextPackets.

        Merges by:
        1. Tagging each passage with source persona in metadata.
        2. Deduplicating chunks by content_hash (chunk_id or content_id).
        3. Including per-persona attribution in citations.
        4. Respecting total budget (budget is already split by caller).
        """
        all_passages: list = []
        all_used_kbs: list[str] = []
        all_used_tools: list[str] = []
        total_tool_calls = 0
        per_persona_notes: list[str] = []

        for persona in personas:
            skill = self.skills.get(persona)
            if not skill:
                log.warning("Tier 3 fanout: no skill for persona %s; skipping", persona)
                continue

            per_intent = IntentSignal(
                primary_persona=persona,
                secondary_personas=[p for p in personas if p != persona],
                filters=intent_signal.filters,
                time_window=intent_signal.time_window,
                confidence=intent_signal.confidence,
            )

            try:
                packet = skill(query=query, intent_signal=per_intent, budget=budget)
            except Exception as e:
                log.warning("Tier 3 fanout: skill %s failed: %s", persona, e)
                continue

            # Tag each passage with the source persona
            for passage in packet.passages:
                if not hasattr(passage, "metadata"):
                    passage.metadata = {}
                passage.metadata["source_persona"] = persona
                all_passages.append(passage)

            all_used_kbs.extend(packet.used_kbs)
            all_used_tools.extend(packet.used_tools)
            total_tool_calls += packet.cost.get("tool_calls", 0)
            if packet.notes:
                per_persona_notes.append(f"{persona}: {packet.notes}")

        # Deduplicate passages across personas (by chunk_id → content_id fallback)
        merged_passages = self._dedupe_multi_persona(all_passages)

        merged_notes = (
            f"Tier 3 multi-persona fanout across {personas}. "
            + (" | ".join(per_persona_notes) if per_persona_notes else "")
        ).strip()

        avg_confidence = (
            sum(getattr(p, "score", 0.0) for p in merged_passages) / max(len(merged_passages), 1)
        )

        return ContextPacket(
            persona=f"multi:{','.join(personas)}",
            passages=merged_passages,
            citations=[p.citation for p in merged_passages],
            used_kbs=list(dict.fromkeys(all_used_kbs)),   # preserve order, dedupe
            used_tools=list(dict.fromkeys(all_used_tools)),
            cost={"tool_calls": total_tool_calls, "personas": personas},
            confidence=min(avg_confidence, 0.59),          # Tier 3 cap
            notes=merged_notes or None,
        )

    def _dedupe_multi_persona(self, passages: list) -> list:
        """Deduplicate by chunk_id/content_id; keep highest-scored copy."""
        seen: dict[str, int] = {}   # key -> index in `out`
        out: list = []
        for p in sorted(passages, key=lambda x: -(getattr(x, "score", 0.0))):
            citation = getattr(p, "citation", None)
            if citation is None:
                out.append(p)
                continue
            key = getattr(citation, "chunk_id", None) or getattr(citation, "content_id", "")
            if not key:
                out.append(p)
                continue
            if key not in seen:
                seen[key] = len(out)
                out.append(p)
            # If already seen, the existing copy has equal or higher score (sorted desc)
        return out

    # -------------------------------------------------------------------------
    # Cross-source enrichment
    # -------------------------------------------------------------------------
    def _enrich_packet_with_cross_source(
        self, packet: ContextPacket, extra_results: list
    ) -> ContextPacket:
        """Append cross-source results to the packet, tagging them."""
        from ..core.interfaces import Result
        from ..persona_skills._base import Passage, Citation

        extra_passages = []
        for r in extra_results:
            if isinstance(r, Result):
                extra_passages.append(Passage(
                    text=r.text,
                    score=r.score * 0.85,   # slight penalty for cross-source hop
                    citation=Citation(
                        kind="cross_source",
                        url=r.citation_url,
                        content_id=r.content_id,
                        chunk_id=r.chunk_id,
                    ),
                    metadata={**r.metadata, "cross_source": True},
                ))

        merged = self._dedupe_multi_persona(packet.passages + extra_passages)
        return ContextPacket(
            persona=packet.persona,
            passages=merged,
            citations=[p.citation for p in merged],
            used_kbs=packet.used_kbs,
            used_tools=packet.used_tools + ["cross_source_resolver"],
            cost=packet.cost,
            confidence=packet.confidence,
            notes=(packet.notes or "") + f" | cross-source: +{len(extra_passages)} results",
        )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _find_closest_kbs(self, query: str, top_n: int = 3) -> list[dict]:
        """Return top-N KB cards by keyword overlap with the query."""
        all_kbs = self.shim_kb.all_cards()
        scored: list[tuple[float, dict]] = []
        q_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        for kb in all_kbs:
            use_when = kb.get("use_when", "")
            kb_tokens = set(re.findall(r"[a-z0-9]+", use_when.lower()))
            overlap = len(q_tokens & kb_tokens) / max(len(q_tokens | kb_tokens), 1)
            scored.append((overlap, kb))
        scored.sort(key=lambda x: -x[0])
        return [kb for _, kb in scored[:top_n]]
