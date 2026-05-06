"""DirectOpenAILLMClient — direct OpenAI API path. Fallback for local dev.

Production path is OCI GenAI Inference per ADR-014; this module is selected
only when `framework/config/adapters/llm.yaml::provider == openai_direct`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger(__name__)


@dataclass
class CostEvent:
    """Written to kb_shim.cost_log per call. See ADR-005."""
    operation: str          # "chat" | "embed"
    model: str
    tokens_in: int
    tokens_out: int
    dollars: float
    latency_ms: int
    request_id: str | None = None


# Pricing table — keep in sync with eval/prices.yaml
PRICES_USD_PER_1M_TOKENS = {
    "gpt-4o": {"in": 2.50, "out": 10.00},
    "text-embedding-3-large": {"in": 0.13, "out": 0.0},
}


def _price(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICES_USD_PER_1M_TOKENS.get(model)
    if not p:
        return 0.0
    return (tokens_in / 1_000_000) * p["in"] + (tokens_out / 1_000_000) * p["out"]


class DirectOpenAILLMClient:
    """Direct OpenAI client. Selected when `provider: openai_direct`."""
    provider = "openai_direct"

    def __init__(
        self,
        api_key: str | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
        on_cost_event: Callable[[CostEvent], None] | None = None,
    ):
        # Lazy import so unit tests can stub without OpenAI installed.
        try:
            from openai import OpenAI  # type: ignore
        except ImportError:
            log.warning("openai package not installed; LLMClient is stub-mode")
            self._client = None
        else:
            self._client = OpenAI(
                api_key=api_key or os.environ.get("OPENAI_API_KEY"),
                organization=org_id or os.environ.get("OPENAI_ORG_ID"),
                project=project_id or os.environ.get("OPENAI_PROJECT_ID"),
            )
        self.on_cost_event = on_cost_event or self._log_cost

    @staticmethod
    def _log_cost(ev: CostEvent) -> None:
        log.info(
            "cost op=%s model=%s tin=%d tout=%d $=%.5f lat=%dms",
            ev.operation, ev.model, ev.tokens_in, ev.tokens_out, ev.dollars, ev.latency_ms,
        )

    # ------------------------------------------------------------------
    # chat() — used by parser, synthesizer, intent classifier, eval judge
    # ------------------------------------------------------------------
    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        timeout_s: int = 60,
    ) -> dict:
        """Returns {"text": str, "tokens_in": int, "tokens_out": int}."""
        if self._client is None:
            return {"text": '{"_stub": true}', "tokens_in": 0, "tokens_out": 0}

        t0 = time.time()
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout_s,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = int((time.time() - t0) * 1000)

        text = resp.choices[0].message.content or ""
        tin = getattr(resp.usage, "prompt_tokens", 0)
        tout = getattr(resp.usage, "completion_tokens", 0)

        ev = CostEvent("chat", model, tin, tout, _price(model, tin, tout), latency_ms,
                       request_id=getattr(resp, "id", None))
        self.on_cost_event(ev)

        return {"text": text, "tokens_in": tin, "tokens_out": tout, "request_id": ev.request_id}

    # ------------------------------------------------------------------
    # embed() — query-time only; bulk ingestion uses DBMS_VECTOR (ADR-012)
    # ------------------------------------------------------------------
    def embed(
        self,
        model: str,
        input: list[str],
        *,
        timeout_s: int = 60,
    ) -> list[list[float]]:
        """Returns a list of vectors, one per input string."""
        if self._client is None:
            # Stub mode for tests
            return [[0.0] * 3072 for _ in input]

        t0 = time.time()
        resp = self._client.embeddings.create(
            model=model, input=input, timeout=timeout_s,
        )
        latency_ms = int((time.time() - t0) * 1000)

        vectors = [d.embedding for d in resp.data]
        # validate dim — fail loud if model returns wrong size
        for v in vectors:
            if len(v) != 3072:
                raise ValueError(
                    f"unexpected embedding dim {len(v)} from model {model}; expected 3072"
                )

        tin = getattr(resp.usage, "total_tokens", 0)
        ev = CostEvent("embed", model, tin, 0, _price(model, tin, 0), latency_ms)
        self.on_cost_event(ev)

        return vectors
