"""OciGenAiLLMClient — chat + embed via OCI Generative AI Inference.

Default LLM path per ADR-014. Same model family as direct OpenAI
(`openai.gpt-4o`, `openai.text-embedding-3-large`); OCI is the credential-managed
proxy. Mirrors the AIRA pattern (in-DB DBMS_VECTOR.UTL_TO_EMBEDDING uses the
same provider; this is the app-side equivalent).

External deps to verify against:
- A real OCI tenancy with Generative AI Inference enabled in the chosen region
- Models `openai.gpt-4o` and `openai.text-embedding-3-large` available in tenancy
- Auth: instance_principal (OCI Compute/Functions) | resource_principal | config_file (local dev)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .llm_openai import CostEvent, _price  # reuse pricing table + cost-event shape

log = logging.getLogger(__name__)


class OciGenAiLLMClient:
    """OCI Generative AI Inference client. Default LLMClient impl.

    Init args:
      endpoint: e.g. "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com"
      compartment_ocid: Oracle compartment OCID for billing/auth scope
      auth: "instance_principal" | "resource_principal" | "config_file"
      config_profile: only used when auth == "config_file"
      models: dict mapping framework concept → OCI model id (with "openai." prefix)
      on_cost_event: optional callback for cost telemetry
    """
    provider = "oci_genai"

    def __init__(
        self,
        endpoint: str,
        compartment_ocid: str,
        auth: str = "instance_principal",
        config_profile: str = "DEFAULT",
        models: dict | None = None,
        on_cost_event: Callable[[CostEvent], None] | None = None,
        timeout_s: int = 60,
    ):
        self.endpoint = endpoint
        self.compartment_ocid = compartment_ocid
        self.auth = auth
        self.config_profile = config_profile
        self.timeout_s = timeout_s
        self.models = models or {
            "chat": "openai.gpt-4o",
            "synthesis": "openai.gpt-4o",
            "eval_judge": "openai.gpt-4o",
            "embedding": "openai.text-embedding-3-large",
        }
        self.on_cost_event = on_cost_event or self._log_cost
        self._client = self._build_client()

    @staticmethod
    def _log_cost(ev: CostEvent) -> None:
        log.info("cost op=%s model=%s tin=%d tout=%d $=%.5f lat=%dms",
                 ev.operation, ev.model, ev.tokens_in, ev.tokens_out,
                 ev.dollars, ev.latency_ms)

    def _build_client(self):
        try:
            import oci  # type: ignore
            from oci.generative_ai_inference import GenerativeAiInferenceClient  # type: ignore
        except ImportError:
            log.warning("oci SDK not installed; OciGenAiLLMClient is stub-mode")
            return None

        if self.auth == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            return GenerativeAiInferenceClient(
                config={}, signer=signer, service_endpoint=self.endpoint,
                timeout=self.timeout_s,
            )
        if self.auth == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            return GenerativeAiInferenceClient(
                config={}, signer=signer, service_endpoint=self.endpoint,
                timeout=self.timeout_s,
            )
        # config_file fallback (local dev)
        config = oci.config.from_file(profile_name=self.config_profile)
        return GenerativeAiInferenceClient(
            config=config, service_endpoint=self.endpoint,
            timeout=self.timeout_s,
        )

    # ------------------------------------------------------------------
    # chat()
    # ------------------------------------------------------------------
    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        timeout_s: int | None = None,
    ) -> dict:
        """Same surface as DirectOpenAILLMClient.chat — translates internally.

        `model` accepts either:
          - framework keys: "gpt-4o" / "synthesis" / "eval_judge"
          - OCI model id direct: "openai.gpt-4o"
        """
        if self._client is None:
            return {"text": '{"_stub": true}', "tokens_in": 0, "tokens_out": 0}

        oci_model_id = self._resolve_model(model)

        try:
            from oci.generative_ai_inference.models import (  # type: ignore
                ChatDetails, GenericChatRequest, OnDemandServingMode,
                Message, TextContent,
            )
        except ImportError:
            return {"text": '{"_stub": true}', "tokens_in": 0, "tokens_out": 0}

        # Translate role mapping: OCI uses uppercase enums
        role_map = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT"}
        oci_messages = [
            Message(
                role=role_map.get(m.get("role", "user"), "USER"),
                content=[TextContent(text=m.get("content", ""))],
            )
            for m in messages
        ]

        # JSON-mode equivalent (OCI calls it response_format.type = JSON_OBJECT)
        oci_response_format = None
        if response_format and response_format.get("type") == "json_object":
            try:
                from oci.generative_ai_inference.models import ResponseFormat  # type: ignore
                oci_response_format = {"type": "JSON_OBJECT"}
            except Exception:
                # Older SDKs may not support; fall back to system-prompt JSON guidance
                oci_response_format = None

        chat_request_kwargs = {
            "api_format": "GENERIC",
            "messages": oci_messages,
            "temperature": temperature,
            "is_stream": False,
        }
        if max_tokens is not None:
            chat_request_kwargs["max_tokens"] = max_tokens
        if oci_response_format is not None:
            chat_request_kwargs["response_format"] = oci_response_format

        details = ChatDetails(
            compartment_id=self.compartment_ocid,
            serving_mode=OnDemandServingMode(model_id=oci_model_id),
            chat_request=GenericChatRequest(**chat_request_kwargs),
        )

        t0 = time.time()
        resp = self._client.chat(details)
        latency_ms = int((time.time() - t0) * 1000)

        # Response shape varies slightly across SDK versions; pull defensively
        choice = resp.data.chat_response.choices[0]
        # message.content may be a list of TextContent or a string
        msg = choice.message
        text = ""
        if hasattr(msg, "content") and msg.content:
            if isinstance(msg.content, list):
                # list of {text} content parts
                text = "".join(getattr(c, "text", "") or "" for c in msg.content)
            else:
                text = str(msg.content)

        usage = getattr(resp.data.chat_response, "usage", None)
        tin = getattr(usage, "prompt_tokens", 0) if usage else 0
        tout = getattr(usage, "completion_tokens", 0) if usage else 0

        # Map OCI model id back to a price-table key (strip "openai." prefix)
        price_key = oci_model_id.split(".", 1)[-1] if "." in oci_model_id else oci_model_id
        ev = CostEvent(
            operation="chat",
            model=price_key,
            tokens_in=tin, tokens_out=tout,
            dollars=_price(price_key, tin, tout),
            latency_ms=latency_ms,
            request_id=getattr(resp, "request_id", None) or
                       getattr(resp.headers, "opc-request-id", None) if hasattr(resp, "headers") else None,
        )
        self.on_cost_event(ev)
        return {"text": text, "tokens_in": tin, "tokens_out": tout, "request_id": ev.request_id}

    # ------------------------------------------------------------------
    # embed()
    # ------------------------------------------------------------------
    def embed(
        self,
        model: str,
        input: list[str],
        *,
        timeout_s: int | None = None,
    ) -> list[list[float]]:
        """Returns list[list[float]] (one per input)."""
        if self._client is None:
            return [[0.0] * 3072 for _ in input]

        oci_model_id = self._resolve_model(model)

        try:
            from oci.generative_ai_inference.models import (  # type: ignore
                EmbedTextDetails, OnDemandServingMode,
            )
        except ImportError:
            return [[0.0] * 3072 for _ in input]

        details = EmbedTextDetails(
            compartment_id=self.compartment_ocid,
            serving_mode=OnDemandServingMode(model_id=oci_model_id),
            inputs=list(input),
        )

        t0 = time.time()
        resp = self._client.embed_text(details)
        latency_ms = int((time.time() - t0) * 1000)

        # OCI returns embeddings as nested float lists
        vectors = list(resp.data.embeddings)
        for v in vectors:
            if len(v) != 3072:
                raise ValueError(
                    f"unexpected embedding dim {len(v)} from {oci_model_id}; expected 3072"
                )

        # OCI exposes total_tokens or input chars; default to char-len heuristic if absent
        usage = getattr(resp.data, "usage", None)
        tin = getattr(usage, "total_tokens", 0) if usage else sum(len(x) // 4 for x in input)

        price_key = oci_model_id.split(".", 1)[-1] if "." in oci_model_id else oci_model_id
        ev = CostEvent(
            operation="embed",
            model=price_key,
            tokens_in=tin, tokens_out=0,
            dollars=_price(price_key, tin, 0),
            latency_ms=latency_ms,
        )
        self.on_cost_event(ev)
        return vectors

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _resolve_model(self, model: str) -> str:
        """Map either a framework concept or an OCI id to the OCI model id."""
        # Already a fully-qualified OCI id?
        if "." in model and model.startswith(("openai.", "cohere.", "meta.")):
            return model
        # Framework concept names
        if model in self.models:
            return self.models[model]
        # Bare model name → assume openai. prefix
        if not model.startswith("openai."):
            return f"openai.{model}"
        return model
