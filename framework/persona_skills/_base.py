"""Base persona context skill — implements ADR-007 contract.

V2 (per ADR-007 amend 5 + 6):
- Tier 1: try this persona's authored workflow skills first (shim_workflows.cards_for(persona))
- Tier 2: fall back to KB retrieval over ACL-visible KBs (shim_kb.cards_visible_to(persona))
- Returns ContextPacket OR a workflow-artifact reference, depending on which tier fired.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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

    # Confidence thresholds (per ADR-006 amend 3)
    tier1_threshold: float = 0.85
    tier2_threshold: float = 0.6

    def __init__(
        self,
        llm: LLMClient,
        shim_kb: ShimKb,
        retrievers: dict,
        shim_workflows=None,                  # ADR-006 amend 2 / ADR-016
        workflow_executor=None,
        model: str = "gpt-4o",
    ):
        self.llm = llm
        self.shim_kb = shim_kb
        self.retrievers = retrievers
        self.shim_workflows = shim_workflows
        self.workflow_executor = workflow_executor
        self.model = model

    def __call__(self, query: str, intent_signal: IntentSignal, budget: Budget):
        # ADR-007 amend 5: Tier 1 — try persona's workflow skills first
        if self.shim_workflows and self.workflow_executor:
            wf_match = self._match_workflow_skill(query, intent_signal)
            if wf_match and wf_match.get("confidence", 0) >= self.tier1_threshold:
                log.info("Tier 1: invoking workflow skill %s (confidence=%.2f)",
                         wf_match["skill"], wf_match["confidence"])
                return self._invoke_workflow(wf_match, query, intent_signal, budget)

        # ADR-007 amend 6: Tier 2 — KB retrieval over ACL-visible KBs (cards_visible_to)
        # 1. Build prompt with shim_kb_filtered for this persona (read scope, not authoring)
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

    # =====================================================================
    # ADR-007 amend 5: Tier 1 — workflow skill match
    # =====================================================================
    def _match_workflow_skill(self, query: str, intent_signal: IntentSignal) -> dict | None:
        """LLM-classifies whether the query matches a persona-authored workflow skill.

        Returns: {"skill": <name>, "skill_card": ..., "inputs": {...}, "confidence": float}
                 or None if no card present.
        """
        cards = self.shim_workflows.cards_for(self.persona)
        on_request_cards = [c for c in cards if c.get("on_request")]
        if not on_request_cards:
            return None

        cards_block = self.shim_workflows.render_for_persona_prompt(self.persona)
        system = f"""You decide whether a user query matches one of the {self.persona}
persona's workflow skills.

{cards_block}

Output JSON ONLY:
{{
  "skill": "<exact skill name from above, or null if no match>",
  "inputs": {{...}},
  "confidence": 0.0-1.0,
  "reasoning": "<one line>"
}}

Rules:
- Match the user's intent against each card's `use_when` and `example_invocations`.
- Extract input values from the query (e.g., "for INC-12345" → {{"incident_id": "INC-12345"}}).
- If no skill cleanly matches, return skill=null with confidence=0.
"""
        try:
            response = self.llm.chat(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": f"Query: {query}"}],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=300,
            )
            d = json.loads(response["text"])
            if d.get("skill"):
                return {
                    "skill": d["skill"],
                    "inputs": d.get("inputs", {}),
                    "confidence": float(d.get("confidence", 0.0)),
                    "reasoning": d.get("reasoning"),
                }
        except Exception as e:
            log.warning("workflow-skill match failed: %s", e)
        return None

    def _invoke_workflow(self, match: dict, query: str, intent: IntentSignal, budget: Budget):
        """Invoke the matched workflow skill via the workflow_executor."""
        cards = self.shim_workflows.cards_for(self.persona)
        target = next((c for c in cards if c.get("name") == match["skill"]), None)
        if not target:
            log.warning("matched skill %s not found in cards", match["skill"])
            return None
        result = self.workflow_executor.execute(Path(target["_path"]), match["inputs"])
        # Wrap result in a workflow-artifact response shape
        from ..orchestrator.context_builder import ContextPacket
        return ContextPacket(
            persona=self.persona,
            passages=[],                                    # no passages; this is an artifact
            citations=[],
            used_kbs=[],
            used_tools=[f"workflow:{match['skill']}"],
            cost={"workflow_artifact": True},
            confidence=match["confidence"],
            notes=f"Tier 1: invoked workflow skill {match['skill']}; "
                  f"artifact at {result.get('delivery', {}).get('url') or result.get('delivery', {}).get('path')}",
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
