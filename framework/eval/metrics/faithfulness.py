"""Ragas-based faithfulness wrapper. Sampled per-run."""
from __future__ import annotations

import logging
log = logging.getLogger(__name__)


def faithfulness_score(question: str, answer: str, contexts: list[str]) -> float | None:
    """Returns 0..1 faithfulness; None if Ragas unavailable.

    Phase 1: this is a stub that returns None. Phase 2 wires in Ragas:
        from ragas import evaluate
        from ragas.metrics import faithfulness
    """
    try:
        from ragas import evaluate  # type: ignore
        from ragas.metrics import faithfulness as f  # type: ignore
        # Real impl wiring TBD Phase 2
        return None
    except ImportError:
        return None
