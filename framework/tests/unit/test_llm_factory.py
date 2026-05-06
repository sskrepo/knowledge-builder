"""Test LLMClient factory selects the right provider."""
import os
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
