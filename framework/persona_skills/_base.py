"""Base persona context skill — implements ADR-007 contract."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..core.llm import LLMClient
from ..orchestrator.budget import Budget
from ..orchestrator.intent_classifier import IntentSignal, IntentFilter
from ..orchestrator.shim_kb import ShimKb

log = logging.getLogger(__name__)


@dataclass
class Citation:
    kind: str
    url: str
    content_id: str
    chunk_id: str | None = None
    excerpt_offset: tuple[int, int] | None = None


@dataclass
class Passage:
    text: str
    score: float
    citation: Citation
    metadata: dict = field(default_factory=dict)


@dataclass
class CostReport:
    tokens_in: int = 0
    tokens_out: int = 0
    dollars: float = 0.0
    latency_ms: int = 0
    tool_calls: int = 0


class BasePersonaSkill:
    """ADR-007 contract impl — concrete persona skills override prompt fragment."""
    persona: str = ""
    PROMPT_FRAGMENT: str = ""

    def __init__(
        self,
        llm: LLMClient,
        shim_kb: ShimKb,
        retrievers: dict,
        model: str = "gpt-4o",
    ):
        self.llm = llm
        self.shim_kb = shim_kb
        self.retrievers = retrievers
        self.model = model

    def __call__(self, query: str, intent_signal: IntentSignal, budget: Budget):
        # 1. Build prompt with shim_kb_filtered for this persona
        kb_block = self.shim_kb.render_for_persona_prompt(self.persona)
        system = f"""You are the {self.persona} retrieval skill.

{self.PROMPT_FRAGMENT}

You DO NOT answer the user's question. You decide which knowledge_bases to query
and with what filters. The orchestrator's synthesizer will produce the answer.

{kb_block}

Output JSON:
{{
  "kbs_to_query": [
    {{"name": "<kb_name>", "tools": ["<tool_name>", ...]}}
  ],
  "filters": [
    {{"field": "...", "values": [...], "strictness": "hard|soft"}}
  ],
  "reasoning": "one-line note for debugging"
}}

Rules:
- Pick the smallest set of KBs that covers the query.
- Inherit filters from intent_signal; add KB-specific filters when useful.
- If no KB matches, return empty kbs_to_query with reasoning.
"""
        user = f"Query: {query}\nIntent filters: {[f.field+':'+f.strictness for f in intent_signal.filters]}\n\nReturn JSON only."

        response = self.llm.chat(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=400,
        )
        try:
            plan = json.loads(response["text"])
        except Exception as e:
            log.warning("skill returned non-JSON: %s; falling back to default", e)
            plan = self._default_plan()

        merged_filters = self._merge_filters(intent_signal.filters, plan.get("filters", []))

        # 2. Dispatch retrieval tools
        passages: list[Passage] = []
        used_kbs: list[str] = []
        used_tools: list[str] = []
        tool_calls = 0

        for kb in plan.get("kbs_to_query", []):
            if tool_calls >= budget.max_tool_calls:
                break
            kb_name = kb.get("name")
            for tool_name in kb.get("tools", []):
                if tool_calls >= budget.max_tool_calls:
                    break
                tool = self.retrievers.get(tool_name)
                if not tool:
                    continue
                try:
                    if tool_name == "vector_search":
                        results = tool(
                            corpus=kb_name,
                            query=query,
                            filters=[{"field": f.field, "values": f.values,
                                      "strictness": f.strictness,
                                      "soft_multiplier": f.soft_multiplier}
                                     for f in merged_filters],
                            k=10,
                            persona=self.persona,
                        )
                    elif tool_name == "get_incident_summary":
                        # extract incident id from query if present
                        results = []
                    elif tool_name == "list_sources":
                        results = []
                    else:
                        results = []
                except NotImplementedError:
                    log.info("retriever %s not yet implemented", tool_name)
                    continue
                except Exception as e:
                    log.warning("retriever %s failed: %s", tool_name, e)
                    continue
                tool_calls += 1
                used_tools.append(tool_name)
                if results:
                    used_kbs.append(kb_name)
                    for r in results:
                        passages.append(Passage(
                            text=r.text,
                            score=r.score,
                            citation=Citation(
                                kind="content",
                                url=r.citation_url,
                                content_id=r.content_id,
                                chunk_id=r.chunk_id,
                            ),
                            metadata=r.metadata,
                        ))

        # 3. Dedupe + score-merge + char cap (ADR-007 amend 1)
        passages = self._dedupe_and_cap(passages, budget.max_context_chars)

        from ..orchestrator.context_builder import ContextPacket
        return ContextPacket(
            persona=self.persona,
            passages=passages,
            citations=[p.citation for p in passages],
            used_kbs=list(set(used_kbs)),
            used_tools=list(set(used_tools)),
            cost={"tool_calls": tool_calls},
            confidence=intent_signal.confidence,
            notes=plan.get("reasoning"),
        )

    def _merge_filters(self, intent_filters: list[IntentFilter],
                       skill_filters: list[dict]) -> list[IntentFilter]:
        out = list(intent_filters)
        existing_fields = {f.field for f in out}
        for sf in skill_filters:
            if sf.get("field") in existing_fields:
                continue
            out.append(IntentFilter(
                field=sf["field"],
                values=sf.get("values", []),
                strictness=sf.get("strictness", "hard"),
                soft_multiplier=float(sf.get("soft_multiplier", 0.90)),
            ))
        return out

    def _dedupe_and_cap(self, passages: list[Passage], max_chars: int) -> list[Passage]:
        seen: set[str] = set()
        unique: list[Passage] = []
        for p in sorted(passages, key=lambda x: -x.score):
            key = p.citation.chunk_id or p.citation.content_id
            if key in seen:
                continue
            seen.add(key)
            unique.append(p)
        # Char cap
        out: list[Passage] = []
        total = 0
        for p in unique:
            if total + len(p.text) > max_chars:
                break
            out.append(p)
            total += len(p.text)
        return out

    def _default_plan(self) -> dict:
        cards = self.shim_kb.cards_for(self.persona)
        if not cards:
            return {"kbs_to_query": [], "filters": [], "reasoning": "no KBs available"}
        # default: try the first KB with vector_search
        first = cards[0]
        return {
            "kbs_to_query": [{"name": first["name"], "tools": ["vector_search"]}],
            "filters": [],
            "reasoning": "default fallback (no LLM plan)",
        }
