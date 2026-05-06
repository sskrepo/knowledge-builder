"""context_builder — top-level orchestrator (ADR-006 / ADR-007).

Phase 1: minimal — fixed routing for incident-class queries goes to ops_eng skill.
Phase 3: full LangGraph state machine with parallel skill dispatch.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..core.llm import LLMClient
from .budget import Budget
from .intent_classifier import IntentClassifier, IntentSignal
from .shim_faaas import ShimFaaas
from .shim_kb import ShimKb
from .synthesizer import Synthesizer, INCIDENT_RCA, GENERIC_QA

log = logging.getLogger(__name__)


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


class ContextBuilder:
    """Orchestrator agent. Phase 1 minimal version."""

    def __init__(
        self,
        llm: LLMClient,
        shim_faaas: ShimFaaas,
        shim_kb: ShimKb,
        skills_by_persona: dict,         # persona_id -> PersonaContextSkill
        synthesizer: Synthesizer,
    ):
        self.llm = llm
        self.shim_faaas = shim_faaas
        self.shim_kb = shim_kb
        self.skills = skills_by_persona
        self.synthesizer = synthesizer
        self.classifier = IntentClassifier(llm, shim_faaas)

    def answer(self, query: str, budget: Budget | None = None) -> dict:
        budget = budget or Budget()
        t0 = time.time()

        # 1. classify
        intent = self.classifier.classify(query)
        log.info("intent: persona=%s confidence=%.2f filters=%d",
                 intent.primary_persona, intent.confidence, len(intent.filters))

        # 2. dispatch primary skill
        skill = self.skills.get(intent.primary_persona)
        if not skill:
            log.warning("no skill for persona %s; falling back to ops_eng", intent.primary_persona)
            skill = self.skills.get("ops_eng")

        # 3. retrieve
        packet = skill(query=query, intent_signal=intent, budget=budget)

        # 4. synthesize
        # Choose schema by persona — ops_eng / Aira gets incident_rca; others get generic_qa
        schema = INCIDENT_RCA if intent.primary_persona == "ops_eng" else GENERIC_QA
        answer = self.synthesizer.synthesize(query, packet.passages, schema=schema, budget=budget)

        elapsed_ms = int((time.time() - t0) * 1000)

        return {
            "answer": answer,
            "schema": schema.name,
            "intent": {
                "persona": intent.primary_persona,
                "confidence": intent.confidence,
                "filters": [{"field": f.field, "values": f.values,
                             "strictness": f.strictness} for f in intent.filters],
            },
            "passages": [{"text": p.text, "citation": p.citation.url, "score": p.score}
                         for p in packet.passages],
            "citations": list({p.citation.url for p in packet.passages}),
            "used_kbs": packet.used_kbs,
            "used_tools": packet.used_tools,
            "cost": packet.cost,
            "latency_ms": elapsed_ms,
        }
