"""intent_classifier — per-query routing decision via gpt-4o (ADR-006)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..core.llm import LLMClient

log = logging.getLogger(__name__)


@dataclass
class IntentFilter:
    field: str
    values: list[str]
    strictness: str = "hard"      # hard | soft | off
    soft_multiplier: float = 0.90


@dataclass
class IntentSignal:
    primary_persona: str
    secondary_personas: list[str] = field(default_factory=list)
    filters: list[IntentFilter] = field(default_factory=list)
    time_window: tuple[datetime, datetime] | None = None
    confidence: float = 0.0


class IntentClassifier:
    """Asks the LLM: given a user query and the FAaaS ontology, which
    persona(s) should handle it and what filters apply?"""

    def __init__(self, llm: LLMClient, shim_faaas, model: str = "gpt-4o"):
        self.llm = llm
        self.shim_faaas = shim_faaas
        self.model = model

    def classify(self, query: str) -> IntentSignal:
        ontology = self.shim_faaas.render_for_prompt()
        producer_personas = self.shim_faaas.producer_personas()

        system = f"""You are the routing layer of a knowledge-retrieval system.
Given a user query and the FAaaS ontology below, decide:
  1. Which persona(s) should retrieve context for this query? (primary + optional secondaries)
  2. What metadata filters apply? (functional_area, resources, services, kind, stack, severity, time_window)
  3. For each filter, is it a hard requirement ("strictness": "hard") or a preference ("strictness": "soft")?

Output a JSON object:
{{
  "primary_persona": "<one of: {producer_personas}>",
  "secondary_personas": [],
  "filters": [
    {{"field": "services", "values": ["auth"], "strictness": "hard"}},
    {{"field": "stack", "values": ["prod"], "strictness": "soft", "soft_multiplier": 0.90}}
  ],
  "confidence": 0.85
}}

{ontology}

Heuristics:
- Service / functional_area / kind in the query => hard
- Stack ("prod" / "preprod") => soft (allow fallback)
- Resources => soft (graph-hop is the better tool, but soft fallback is OK)
"""
        user = f"Query: {query}\n\nReturn JSON only."
        response = self.llm.chat(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=500,
        )
        try:
            d = json.loads(response["text"])
        except Exception as e:
            log.warning("intent classifier returned non-JSON: %s", e)
            return IntentSignal(primary_persona=producer_personas[0] if producer_personas else "ops_eng")

        filters = [IntentFilter(
            field=f["field"], values=f.get("values", []),
            strictness=f.get("strictness", "hard"),
            soft_multiplier=float(f.get("soft_multiplier", 0.90)),
        ) for f in d.get("filters", [])]

        return IntentSignal(
            primary_persona=d.get("primary_persona", "ops_eng"),
            secondary_personas=d.get("secondary_personas", []),
            filters=filters,
            confidence=float(d.get("confidence", 0.5)),
        )
