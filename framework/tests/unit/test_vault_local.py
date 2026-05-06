"""Test local-file secrets backend."""
import os
import pytest
import textwrap
from pathlib import Path

from framework.core.vault import VaultClient

@pytest.fixture
def secrets_file(tmp_path: Path):
    p = tmp_path / "secrets.yaml"
    p.write_text(textwrap.dedent("""
        secrets:
          openai-api-key: test-key-12345
          jira-readonly: test-jira-token
          adb-admin-dev: test-pwd
    """).strip())
    return p

def test_local_backend_resolves(secrets_file, monkeypatch):
    monkeypatch.setenv("KBF_SECRETS_BACKEND", "local")
    monkeypatch.setenv("KBF_SECRETS_FILE", str(secrets_file))
    v = VaultClient()
    assert v.resolve("vault://kb/openai-api-key") == "test-key-12345"
    assert v.resolve("vault://kb/jira-readonly") == "test-jira-token"

def test_local_backend_missing_falls_back_to_env(secrets_file, monkeypatch):
    monkeypatch.setenv("KBF_SECRETS_BACKEND", "local")
    monkeypatch.setenv("KBF_SECRETS_FILE", str(secrets_file))
    monkeypatch.setenv("KBF_SECRET_OTHER_THING", "from-env")
    v = VaultClient()
    assert v.resolve("vault://kb/other-thing") == "from-env"

def test_env_backend(monkeypatch):
    monkeypatch.setenv("KBF_SECRETS_BACKEND", "env")
    monkeypatch.setenv("KBF_SECRET_THING", "value")
    v = VaultClient()
    assert v.resolve("vault://kb/thing") == "value"

def test_caches_within_ttl(secrets_file, monkeypatch):
    monkeypatch.setenv("KBF_SECRETS_BACKEND", "local")
    monkeypatch.setenv("KBF_SECRETS_FILE", str(secrets_file))
    v = VaultClient()
    a = v.resolve("vault://kb/openai-api-key")
    b = v.resolve("vault://kb/openai-api-key")
    assert a == b
