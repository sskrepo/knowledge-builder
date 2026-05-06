"""Budget — bounds resource use per query (ADR-007 v2 amend)."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Budget:
    max_tokens_in: int = 8000
    max_tokens_out: int = 1500
    max_latency_ms: int = 3000
    max_dollars: float = 0.10
    max_tool_calls: int = 6
    max_context_chars: int = 50000  # ADR-007 amendment 1 (AIRA pattern)
