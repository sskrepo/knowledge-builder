"""intent_classifier — per-query four-tier routing decision (ADR-006 amend 3).

Tier 1: workflow skill match (confidence >= 0.85)
Tier 2: single-persona KB retrieval (confidence >= 0.60)
Tier 3: multi-persona fanout (confidence >= 0.40)
Tier 4: honest no-answer + skill suggestion (< 0.30)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..core.llm import LLMClient

log = logging.getLogger(__name__)

# Default thresholds per ADR-006 amend 3 (overridable via config)
TIER1_THRESHOLD = 0.85
TIER2_THRESHOLD = 0.60
TIER3_THRESHOLD = 0.40
TIER4_FLOOR = 0.30

# Multi-persona keyword patterns for stub mode
_MULTI_PERSONA_PATTERNS: list[tuple[list[str], list[str]]] = [
    # (keywords, personas)
    (["incident", "release", "affecting", "impact"], ["ops_eng", "pm"]),
    (["status", "all teams", "across teams", "all personas"], ["tpm", "pm", "ops_eng"]),
    (["cross-team", "cross team", "dependencies"], ["tpm", "eng_mgr"]),
    (["ops", "pm", "tpm", "engineer"], ["ops_eng", "pm", "tpm"]),
    (["fleet", "tenant", "incident"], ["ops_eng", "ops_mgr"]),
    (["release", "incidents", "impacted"], ["pm", "ops_eng"]),
    (["weekly", "status", "blocked", "blocked initiatives"], ["tpm", "pm"]),
    (["architecture", "design", "code", "service"], ["architect", "developer"]),
]


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


@dataclass
class IntentClassification:
    """Result of classify() — routing decision with tier assignment."""
    tier: int                         # 1, 2, 3, or 4
    confidence: float
    persona: str | None               # primary persona for Tier 1/2
    personas: list[str] | None        # multiple personas for Tier 3
    workflow_skill: str | None        # matched workflow skill name (Tier 1)
    reasoning: str

    def to_intent_signal(self) -> IntentSignal:
        """Convert to IntentSignal for backward-compat with persona skills."""
        primary = self.persona or (self.personas[0] if self.personas else "ops_eng")
        secondary = []
        if self.personas and len(self.personas) > 1:
            secondary = [p for p in self.personas if p != primary]
        return IntentSignal(
            primary_persona=primary,
            secondary_personas=secondary,
            confidence=self.confidence,
        )


class IntentClassifier:
    """Asks the LLM: given a user query and the FAaaS ontology, which
    persona(s) should handle it, which tier, and what filters apply?

    In stub LLM mode (KBF_LLM_PROVIDER=stub) uses keyword matching.
    """

    def __init__(self, llm: LLMClient, shim_faaas, model: str = "gpt-4o"):
        self.llm = llm
        self.shim_faaas = shim_faaas
        self.model = model
        self._is_stub = getattr(llm, "provider", "") not in ("oci_genai", "openai_direct")

    def classify(
        self,
        query: str,
        persona: str = "",
        available_workflows: list[dict] | None = None,
        available_kbs: list[dict] | None = None,
    ) -> IntentClassification:
        """Classify a query into one of the four tiers.

        In stub mode: deterministic keyword/pattern matching.
        In LLM mode: small model call that reasons over ontology + shim cards.
        """
        if self._stub_mode():
            return self._classify_stub(query, persona, available_workflows or [], available_kbs or [])
        return self._classify_llm(query, persona, available_workflows or [], available_kbs or [])

    def classify_simple(self, query: str) -> IntentSignal:
        """Backward-compat: returns an IntentSignal (pre-V2 callers)."""
        result = self.classify(query)
        return result.to_intent_signal()

    def classify_multi(self, query: str) -> list[tuple[str, float]]:
        """Return ranked list of (persona, confidence) for multi-persona queries.

        Used by context_builder for Tier 3 fanout.
        """
        producer_personas = self.shim_faaas.producer_personas()

        if self._stub_mode():
            return self._multi_persona_stub(query, producer_personas)

        ontology = self.shim_faaas.render_for_prompt()
        system = f"""You are the routing layer of a knowledge-retrieval system.
Given a user query and the FAaaS ontology, list ALL personas relevant to answering it.
Return a ranked JSON array of persona-confidence pairs (highest confidence first).

Output ONLY:
{{"personas": [{{"persona": "<id>", "confidence": 0.0-1.0}}, ...]}}

{ontology}
"""
        user = f"Query: {query}\n\nReturn JSON only."
        response = self.llm.chat(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        try:
            d = json.loads(response["text"])
            pairs = [(p["persona"], float(p["confidence"])) for p in d.get("personas", [])]
            return sorted(pairs, key=lambda x: -x[1])
        except Exception as e:
            log.warning("classify_multi LLM response unparseable: %s", e)
            return [(producer_personas[0], 0.5)] if producer_personas else []

    # -------------------------------------------------------------------------
    # Internal: stub mode
    # -------------------------------------------------------------------------
    def _stub_mode(self) -> bool:
        """True when running with stub or non-functional LLM."""
        if hasattr(self.llm, "_client") and self.llm._client is None:
            return True
        provider = getattr(self.llm, "provider", "")
        return provider == "stub"

    def _classify_stub(
        self,
        query: str,
        persona: str,
        available_workflows: list[dict],
        available_kbs: list[dict],
    ) -> IntentClassification:
        """Deterministic stub classification — keyword matching only."""
        q_lower = query.lower()

        # Tier 1: exact/fuzzy match against workflow skill example_invocations
        for card in available_workflows:
            for example in card.get("example_invocations", []):
                if self._fuzzy_matches(q_lower, example.lower()):
                    return IntentClassification(
                        tier=1,
                        confidence=0.90,
                        persona=card.get("persona") or persona,
                        personas=None,
                        workflow_skill=card.get("name"),
                        reasoning=f"stub: matched example invocation '{example}'",
                    )

        # Tier 3: multi-persona detection before Tier 2 single-persona
        multi = self._multi_persona_stub(query, self.shim_faaas.producer_personas())
        if len(multi) >= 2 and multi[0][1] >= TIER3_THRESHOLD:
            top_personas = [p for p, _ in multi if _ >= TIER3_THRESHOLD]
            avg_conf = sum(c for _, c in multi[:len(top_personas)]) / max(len(top_personas), 1)
            if avg_conf < TIER2_THRESHOLD:
                return IntentClassification(
                    tier=3,
                    confidence=avg_conf,
                    persona=None,
                    personas=top_personas,
                    workflow_skill=None,
                    reasoning=f"stub: multi-persona fanout to {top_personas}",
                )

        # Tier 2: keyword overlap with KB use_when descriptions
        best_score = 0.0
        best_persona = persona or "ops_eng"
        for card in available_kbs:
            use_when = card.get("use_when", "")
            score = self._keyword_overlap(q_lower, use_when.lower())
            if score > best_score:
                best_score = score
                best_persona = card.get("persona") or best_persona

        if best_score >= 0.15:  # reasonable overlap threshold for stub
            confidence = min(0.75, 0.5 + best_score)
            return IntentClassification(
                tier=2,
                confidence=confidence,
                persona=best_persona,
                personas=None,
                workflow_skill=None,
                reasoning=f"stub: KB keyword overlap score={best_score:.2f}",
            )

        # Tier 4: nothing matched
        primary = persona or "ops_eng"
        return IntentClassification(
            tier=4,
            confidence=0.20,
            persona=primary,
            personas=None,
            workflow_skill=None,
            reasoning="stub: no workflow or KB match found",
        )

    def _multi_persona_stub(self, query: str, producer_personas: list[str]) -> list[tuple[str, float]]:
        """Identify which personas are relevant to a query via keyword patterns."""
        q_lower = query.lower()
        persona_scores: dict[str, float] = {}

        for keywords, personas in _MULTI_PERSONA_PATTERNS:
            matched = sum(1 for kw in keywords if kw in q_lower)
            if matched >= 2:
                confidence = min(0.55, 0.35 + (matched * 0.05))
                for p in personas:
                    if p in producer_personas:
                        persona_scores[p] = max(persona_scores.get(p, 0.0), confidence)

        if not persona_scores:
            # Default: route to ops_eng with low confidence
            return [("ops_eng", 0.45)]

        return sorted(persona_scores.items(), key=lambda x: -x[1])

    def _fuzzy_matches(self, query: str, example: str) -> bool:
        """True if query shares enough significant tokens with example."""
        q_tokens = set(re.findall(r"[a-z0-9]+", query)) - _STOPWORDS
        e_tokens = set(re.findall(r"[a-z0-9]+", example)) - _STOPWORDS
        if not q_tokens or not e_tokens:
            return False
        overlap = len(q_tokens & e_tokens)
        # Require at least 40% of example tokens to be present
        return overlap / max(len(e_tokens), 1) >= 0.4

    def _keyword_overlap(self, query: str, text: str) -> float:
        """Jaccard-like score between query tokens and text tokens."""
        q_tokens = set(re.findall(r"[a-z0-9]+", query)) - _STOPWORDS
        t_tokens = set(re.findall(r"[a-z0-9]+", text)) - _STOPWORDS
        if not q_tokens or not t_tokens:
            return 0.0
        return len(q_tokens & t_tokens) / max(len(q_tokens | t_tokens), 1)

    # -------------------------------------------------------------------------
    # Internal: LLM mode (full classification)
    # -------------------------------------------------------------------------
    def _classify_llm(
        self,
        query: str,
        persona: str,
        available_workflows: list[dict],
        available_kbs: list[dict],
    ) -> IntentClassification:
        ontology = self.shim_faaas.render_for_prompt()
        producer_personas = self.shim_faaas.producer_personas()

        workflow_block = ""
        if available_workflows:
            lines = ["## Available workflow skills (Tier 1):"]
            for w in available_workflows:
                lines.append(f"- {w['name']}: {w.get('use_when', '')} | examples: {w.get('example_invocations', [])}")
            workflow_block = "\n".join(lines)

        kb_block = ""
        if available_kbs:
            lines = ["## Available KBs (Tier 2):"]
            for k in available_kbs:
                lines.append(f"- {k['name']} (persona={k.get('persona')}): {k.get('use_when', '')}")
            kb_block = "\n".join(lines)

        system = f"""You are the routing layer of a knowledge-retrieval system.

Classify the user query into exactly one of four tiers:

Tier 1 (workflow skill match, confidence >= 0.85):
  - Query clearly matches a specific workflow skill's purpose and example invocations.
  - Set tier=1, workflow_skill=<name>, persona=<owning persona>.

Tier 2 (single-persona KB retrieval, confidence >= 0.60):
  - Query is about one persona's domain; KB retrieval can answer it.
  - Set tier=2, persona=<best persona>, workflow_skill=null.

Tier 3 (multi-persona fanout, confidence >= 0.40):
  - Query spans multiple persona domains and needs parallel retrieval.
  - Set tier=3, personas=[<list>], persona=null, workflow_skill=null.

Tier 4 (no grounded answer, confidence < 0.30):
  - Query doesn't match any known workflow, KB, or persona domain.
  - Set tier=4, persona=<best guess>, workflow_skill=null.

{workflow_block}

{kb_block}

{ontology}

Output JSON only:
{{
  "tier": 1|2|3|4,
  "confidence": 0.0-1.0,
  "persona": "<persona_id or null>",
  "personas": ["<p1>", "<p2>"] or null,
  "workflow_skill": "<skill_name or null>",
  "reasoning": "<one line>"
}}
"""
        user = f"Query: {query}\n\nReturn JSON only."
        response = self.llm.chat(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=400,
        )
        try:
            d = json.loads(response["text"])
        except Exception as e:
            log.warning("intent classifier returned non-JSON: %s", e)
            return IntentClassification(
                tier=4,
                confidence=0.20,
                persona=producer_personas[0] if producer_personas else "ops_eng",
                personas=None,
                workflow_skill=None,
                reasoning="parse error; defaulting to tier 4",
            )

        return IntentClassification(
            tier=int(d.get("tier", 4)),
            confidence=float(d.get("confidence", 0.20)),
            persona=d.get("persona"),
            personas=d.get("personas"),
            workflow_skill=d.get("workflow_skill"),
            reasoning=d.get("reasoning", ""),
        )


_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "for", "in", "on",
    "at", "to", "from", "with", "by", "of", "and", "or", "but", "not",
    "that", "this", "it", "its", "all", "what", "which", "who", "how",
    "me", "my", "we", "our", "you", "your", "they", "them", "their",
}
