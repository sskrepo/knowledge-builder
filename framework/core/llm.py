"""LLMClient façade — picks oci_genai (default) or openai_direct based on config.

Per ADR-014. Callers import `LLMClient` from this module; the actual concrete
class (OciGenAiLLMClient or DirectOpenAILLMClient) is selected at construction
time from `framework/config/adapters/llm.yaml` (or env-overlay).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .llm_openai import CostEvent, DirectOpenAILLMClient, _price  # re-export
from .llm_oci import OciGenAiLLMClient

log = logging.getLogger(__name__)


def LLMClient(*args, **kwargs) -> Any:
    """Factory: returns the concrete LLM client for the configured provider.

    Honors:
      1. Explicit `provider=` kwarg
      2. Env var KBF_LLM_PROVIDER (`oci_genai` | `openai_direct`)
      3. framework/config/adapters/llm.yaml::provider
      4. Default: oci_genai
    """
    provider = kwargs.pop("provider", None) or os.environ.get("KBF_LLM_PROVIDER")
    cfg = _load_llm_config()
    if not provider:
        provider = cfg.get("provider", "oci_genai")

    if provider == "oci_genai":
        oci_cfg = cfg.get("oci_genai", {})
        return OciGenAiLLMClient(
            endpoint=kwargs.pop("endpoint", oci_cfg.get("endpoint", "")),
            compartment_ocid=kwargs.pop("compartment_ocid",
                                        _resolve_compartment(oci_cfg.get("compartment_ocid"))),
            auth=kwargs.pop("auth", oci_cfg.get("auth", "instance_principal")),
            config_profile=kwargs.pop("config_profile", oci_cfg.get("config_profile", "DEFAULT")),
            models=kwargs.pop("models", oci_cfg.get("models")),
            timeout_s=kwargs.pop("timeout_s", oci_cfg.get("timeout_s", 60)),
            **kwargs,
        )
    if provider == "openai_direct":
        return DirectOpenAILLMClient(*args, **kwargs)

    raise ValueError(f"unknown LLM provider: {provider}")


def _load_llm_config() -> dict:
    """Read framework/config/adapters/llm.yaml; return empty dict if absent."""
    here = Path(__file__).resolve()
    repo = here.parents[2]  # core/ → framework/ → repo/
    path = repo / "framework" / "config" / "adapters" / "llm.yaml"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("failed to load %s: %s", path, e)
        return {}


def _resolve_compartment(value) -> str:
    """Resolve a literal OCID, an env-overlay reference, or a vault reference."""
    if not value:
        # Fall back to env config compartment OCID
        return os.environ.get("KBF_COMPARTMENT_OCID", "")
    if isinstance(value, str) and value.startswith("vault://"):
        from .vault import VaultClient
        return VaultClient().resolve(value)
    if isinstance(value, str) and value.startswith("${"):
        # Simple ${vault.compartment_ocid} interpolation from env config
        # (Phase 1: best-effort; fuller templating in Phase 2)
        return os.environ.get("KBF_COMPARTMENT_OCID", "")
    return str(value)


# Re-export for convenience and backward compat
__all__ = ["LLMClient", "CostEvent", "_price", "OciGenAiLLMClient", "DirectOpenAILLMClient"]
