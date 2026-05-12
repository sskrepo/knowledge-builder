"""Test LLMClient factory selects the right provider."""
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from framework.core.llm import LLMClient, OciGenAiLLMClient, DirectOpenAILLMClient


def test_factory_default_is_oci(monkeypatch):
    monkeypatch.delenv("KBF_LLM_PROVIDER", raising=False)
    client = LLMClient(
        endpoint="https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com",
        compartment_ocid="ocid1.compartment.oc1..test",
        auth="config_file",
        config_profile="DEFAULT",
    )
    assert isinstance(client, OciGenAiLLMClient)

def test_factory_env_override_to_openai(monkeypatch):
    monkeypatch.setenv("KBF_LLM_PROVIDER", "openai_direct")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = LLMClient()
    assert isinstance(client, DirectOpenAILLMClient)

def test_factory_explicit_provider():
    client = LLMClient(
        provider="openai_direct",
        api_key="test",
    )
    assert isinstance(client, DirectOpenAILLMClient)

def test_oci_model_resolution_via_concept():
    c = OciGenAiLLMClient(
        endpoint="https://example", compartment_ocid="ocid1.x",
        auth="config_file",
    )
    assert c._resolve_model("chat") == "openai.gpt-4o"
    assert c._resolve_model("embedding") == "openai.text-embedding-3-large"
    assert c._resolve_model("openai.gpt-4o") == "openai.gpt-4o"
    assert c._resolve_model("gpt-4o") == "openai.gpt-4o"


# ---------------------------------------------------------------------------
# Tests for _load_env_llm_overrides (the laptop config override path)
# ---------------------------------------------------------------------------

def test_load_env_llm_overrides_laptop(tmp_path):
    """laptop.yaml with llm: auth/config_profile must flow through to OciGenAiLLMClient.

    This is the regression test for the 169.254.169.254 timeout bug:
    auth: instance_principal was used on laptop because the env-config [llm]
    section was not being read.
    """
    from framework.deploy.mcp_server import _load_env_llm_overrides

    laptop_yaml = tmp_path / "framework" / "config" / "laptop.yaml"
    laptop_yaml.parent.mkdir(parents=True)
    laptop_yaml.write_text(textwrap.dedent("""
        env: laptop
        llm:
          provider: oci_genai
          auth: config_file
          config_profile: adpcpprod
    """))

    overrides = _load_env_llm_overrides(tmp_path, "laptop")

    assert overrides["auth"] == "config_file", (
        "laptop auth must be config_file, not instance_principal"
    )
    assert overrides["config_profile"] == "adpcpprod"
    assert overrides["provider"] == "oci_genai"


def test_load_env_llm_overrides_missing_file(tmp_path):
    """Missing env config must return empty dict (graceful fallback)."""
    from framework.deploy.mcp_server import _load_env_llm_overrides

    # No laptop.yaml created — just the bare tmp_path
    overrides = _load_env_llm_overrides(tmp_path, "laptop")
    assert overrides == {}


def test_load_env_llm_overrides_no_llm_section(tmp_path):
    """Env config without [llm] section must return empty dict."""
    from framework.deploy.mcp_server import _load_env_llm_overrides

    cfg = tmp_path / "framework" / "config" / "laptop.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("env: laptop\nadb:\n  service_name: test\n")

    overrides = _load_env_llm_overrides(tmp_path, "laptop")
    assert overrides == {}


def test_laptop_llm_overrides_reach_oci_client(tmp_path, monkeypatch):
    """End-to-end: laptop.yaml auth:config_file must reach OciGenAiLLMClient.__init__.

    Regression guard for the original bug where adapters/llm.yaml's
    auth: instance_principal was used instead of laptop.yaml's auth: config_file.
    """
    from framework.deploy.mcp_server import _load_env_llm_overrides

    # Create a minimal laptop.yaml with the correct auth override
    laptop_yaml = tmp_path / "framework" / "config" / "laptop.yaml"
    laptop_yaml.parent.mkdir(parents=True)
    laptop_yaml.write_text(textwrap.dedent("""
        env: laptop
        llm:
          provider: oci_genai
          auth: config_file
          config_profile: adpcpprod
    """))

    # Patch _load_llm_config so the factory reads adapters/llm.yaml defaults
    # that would normally specify auth: instance_principal
    adapter_defaults = {
        "provider": "oci_genai",
        "oci_genai": {
            "endpoint": "https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com",
            "compartment_ocid": "ocid1.tenancy.oc1..test",
            "auth": "instance_principal",   # <-- the wrong default that caused the bug
            "config_profile": "DEFAULT",
            "timeout_s": 60,
        },
    }
    monkeypatch.delenv("KBF_LLM_PROVIDER", raising=False)

    with patch("framework.core.llm._load_llm_config", return_value=adapter_defaults):
        llm_kwargs = _load_env_llm_overrides(tmp_path, "laptop")
        client = LLMClient(**llm_kwargs)

    assert isinstance(client, OciGenAiLLMClient)
    assert client.auth == "config_file", (
        f"Expected auth=config_file, got auth={client.auth!r}. "
        "laptop.yaml override was not applied — instance_principal would time out "
        "on 169.254.169.254 on a MacBook."
    )
    assert client.config_profile == "adpcpprod", (
        f"Expected config_profile=adpcpprod, got {client.config_profile!r}"
    )
