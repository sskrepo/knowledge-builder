"""Base class for persona context skills (per ADR-007)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class IntentSignal:
    primary_persona: str
    secondary_personas: list[str] = field(default_factory=list)
    functional_area: list[str] | None = None
    resources: list[str] | None = None
    services: list[str] | None = None
    kind: list[str] | None = None
    time_window: tuple[datetime, datetime] | None = None
    confidence: float = 0.0


@dataclass
class Budget:
    max_tokens_in: int = 8000
    max_tokens_out: int = 1500
    max_latency_ms: int = 3000
    max_dollars: float = 0.10
    max_tool_calls: int = 6


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


@dataclass
class ContextPacket:
    persona: str
    passages: list[Passage] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    used_kbs: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    cost: CostReport = field(default_factory=CostReport)
    confidence: float = 0.0
    notes: str | None = None


@runtime_checkable
class PersonaContextSkill(Protocol):
    persona: str
    def __call__(self, query: str, intent_signal: IntentSignal, budget: Budget) -> ContextPacket: ...


class BasePersonaSkill:
    """Default implementation. Subclasses override `persona` and (rarely) custom dedup."""
    persona: str = ""

    def __init__(self, shim_kb_filtered: dict, prompt_addition: str | None = None):
        self.shim_kb_filtered = shim_kb_filtered
        self.prompt_addition = prompt_addition or ""

    def __call__(self, query: str, intent_signal: IntentSignal, budget: Budget) -> ContextPacket:
        # TODO Phase 1: implement the LangGraph node that:
        #   1) renders prompt with shim_kb_filtered
        #   2) asks LLM which KBs to query
        #   3) dispatches retrieval tools (parallel where possible)
        #   4) merges + dedupes
        #   5) returns ContextPacket
        return ContextPacket(persona=self.persona, notes="STUB — Phase 1 implementation")
