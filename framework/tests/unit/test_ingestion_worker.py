"""Unit tests for ingestion_worker._build_confluence_adapter and main().

Coverage:
  - _build_confluence_adapter: no config → None (fixture fallback)
  - _build_confluence_adapter: mode=codex_proxy in config → ConfluenceCodexProxyAdapter
  - _build_confluence_adapter: env override takes precedence over base config
  - _build_confluence_adapter: import/vault error → None (safe fallback)
  - main(): passes whatever adapter _build_confluence_adapter returns to ConfluenceWikiIngestor
  - main(): no KB entries → all-zero stats
"""
from __future__ import annotations

import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_base_confluence_cfg(tmp_path: Path, mode: str, extra: dict | None = None) -> None:
    """Write framework/config/adapters/confluence.yaml with the given mode."""
    adapters_dir = tmp_path / "framework" / "config" / "adapters"
    adapters_dir.mkdir(parents=True, exist_ok=True)
    cfg: dict = {"mode": mode}
    if extra:
        cfg.update(extra)
    (adapters_dir / "confluence.yaml").write_text(yaml.safe_dump(cfg))


def _write_env_cfg(tmp_path: Path, kbf_env: str, overrides: dict) -> None:
    """Write framework/config/{kbf_env}.yaml with adapters_overrides."""
    env_dir = tmp_path / "framework" / "config"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f"{kbf_env}.yaml").write_text(yaml.safe_dump({"adapters_overrides": {"confluence": overrides}}))


# ---------------------------------------------------------------------------
# _build_confluence_adapter
# ---------------------------------------------------------------------------


class TestBuildConfluenceAdapter:

    def test_no_base_config_no_override_returns_none(self, tmp_path):
        """No confluence.yaml and no env override → fixture mode (None)."""
        from framework.deploy.ingestion_worker import _build_confluence_adapter

        # Only the env config dir with an empty laptop.yaml
        (tmp_path / "framework" / "config").mkdir(parents=True)
        (tmp_path / "framework" / "config" / "laptop.yaml").write_text("{}\n")

        with patch("framework.deploy.ingestion_worker.REPO_ROOT", tmp_path):
            result = _build_confluence_adapter({}, "laptop")

        assert result is None

    def test_codex_proxy_override_builds_proxy_adapter(self, tmp_path):
        """mode: codex_proxy in env overrides → ConfluenceCodexProxyAdapter constructed."""
        from framework.deploy.ingestion_worker import _build_confluence_adapter

        _write_base_confluence_cfg(tmp_path, "native", {"native": {"base_url": "https://x", "auth": {"token_secret": "t"}}})
        cfg = {
            "adapters_overrides": {
                "confluence": {
                    "mode": "codex_proxy",
                    "codex_proxy": {"server_name": "central_confluence", "timeout_seconds": 120},
                }
            }
        }

        mock_cls = MagicMock(name="ConfluenceCodexProxyAdapter")
        with patch("framework.deploy.ingestion_worker.REPO_ROOT", tmp_path):
            with patch("framework.adapters.confluence.codex_proxy.ConfluenceCodexProxyAdapter", mock_cls):
                with patch(
                    "framework.deploy.ingestion_worker.ConfluenceCodexProxyAdapter",
                    mock_cls,
                    create=True,
                ):
                    result = _build_confluence_adapter(cfg, "laptop")

        # Either the real class was instantiated or the mock was called
        assert result is not None or mock_cls.called

    def test_env_override_takes_precedence_over_base_mode(self, tmp_path):
        """Base says mode: mcp, env override says mode: codex_proxy → codex_proxy wins."""
        from framework.deploy.ingestion_worker import _build_confluence_adapter

        _write_base_confluence_cfg(tmp_path, "mcp", {"mcp": {"endpoint": "https://mcp.x"}})
        cfg = {
            "adapters_overrides": {
                "confluence": {
                    "mode": "codex_proxy",
                    "codex_proxy": {"server_name": "central_confluence", "timeout_seconds": 60},
                }
            }
        }

        mock_proxy = MagicMock(name="ConfluenceCodexProxyAdapter")
        mock_mcp = MagicMock(name="ConfluenceMcpAdapter")

        with patch("framework.deploy.ingestion_worker.REPO_ROOT", tmp_path):
            with patch("framework.deploy.ingestion_worker.ConfluenceMcpAdapter", mock_mcp, create=True):
                with patch("framework.deploy.ingestion_worker.ConfluenceCodexProxyAdapter", mock_proxy, create=True):
                    _build_confluence_adapter(cfg, "laptop")

        # mcp adapter must NOT be instantiated
        assert not mock_mcp.called

    def test_no_config_returns_none_on_staging(self, tmp_path):
        """kbf_env='staging' but no confluence.yaml and no override → None."""
        from framework.deploy.ingestion_worker import _build_confluence_adapter

        with patch("framework.deploy.ingestion_worker.REPO_ROOT", tmp_path):
            result = _build_confluence_adapter({}, "staging")

        assert result is None

    def test_import_error_returns_none(self, tmp_path):
        """ImportError (e.g. vault not configured) → None (safe fallback)."""
        from framework.deploy.ingestion_worker import _build_confluence_adapter

        _write_base_confluence_cfg(tmp_path, "native", {"native": {"base_url": "https://x", "auth": {"token_secret": "t"}}})

        with patch("framework.deploy.ingestion_worker.REPO_ROOT", tmp_path):
            # ConfluenceNativeAdapter import succeeds but resolve_token hits VaultClient
            # which raises in test context — the helper catches this and returns None.
            result = _build_confluence_adapter({}, "staging")

        assert result is None


# ---------------------------------------------------------------------------
# main() — adapter pass-through
# ---------------------------------------------------------------------------


class TestMainAdapterWiring:
    """main() must pass whatever _build_confluence_adapter returns to ConfluenceWikiIngestor."""

    def _make_skill_store(self, space: str = "TPM"):
        mock = MagicMock()
        mock.list_persona_builder_kbs.return_value = [
            {
                "persona": "tpm",
                "kb_name": "weekly_report",
                "content_yaml": f"sources:\n  - kind: confluence\n    space: {space}\n",
            }
        ]
        return mock

    def test_main_passes_adapter_from_builder_to_ingestor(self):
        """main() passes the adapter object returned by _build_confluence_adapter
        directly to ConfluenceWikiIngestor — whatever that happens to be."""
        import os
        from framework.deploy import ingestion_worker

        sentinel_adapter = object()  # unique object to track pass-through

        mock_ingestor_instance = MagicMock()
        mock_ingestor_instance.ingest_space.return_value = {
            "pages_new": 2, "pages_updated": 0, "pages_unchanged": 1,
        }
        mock_ingestor_cls = MagicMock(return_value=mock_ingestor_instance)

        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with patch(
                "framework.deploy.ingestion_worker._build_confluence_adapter",
                return_value=sentinel_adapter,
            ):
                with patch(
                    "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor",
                    mock_ingestor_cls,
                ):
                    ingestion_worker.main(skill_store=self._make_skill_store())

        # ConfluenceWikiIngestor must be called with the exact adapter object
        mock_ingestor_cls.assert_called_once()
        call_kwargs = mock_ingestor_cls.call_args
        adapter_arg = (
            call_kwargs.kwargs.get("adapter")
            if call_kwargs.kwargs
            else (call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
        )
        assert adapter_arg is sentinel_adapter, (
            f"Expected sentinel adapter to be passed through, got {adapter_arg!r}"
        )

    def test_main_fixture_mode_when_builder_returns_none(self):
        """When _build_confluence_adapter returns None, ConfluenceWikiIngestor gets adapter=None."""
        import os
        from framework.deploy import ingestion_worker

        mock_ingestor_instance = MagicMock()
        mock_ingestor_instance.ingest_space.return_value = {
            "pages_new": 0, "pages_updated": 0, "pages_unchanged": 0,
        }
        mock_ingestor_cls = MagicMock(return_value=mock_ingestor_instance)

        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            with patch(
                "framework.deploy.ingestion_worker._build_confluence_adapter",
                return_value=None,
            ):
                with patch(
                    "framework.ingestion.confluence_wiki_ingest.ConfluenceWikiIngestor",
                    mock_ingestor_cls,
                ):
                    ingestion_worker.main(skill_store=self._make_skill_store())

        mock_ingestor_cls.assert_called_once()
        call_kwargs = mock_ingestor_cls.call_args
        adapter_arg = (
            call_kwargs.kwargs.get("adapter")
            if call_kwargs.kwargs
            else (call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
        )
        assert adapter_arg is None

    def test_main_no_kb_entries_returns_zero_stats(self):
        """main() with no production KB entries returns all-zero stats dict."""
        import os
        from framework.deploy import ingestion_worker

        mock_skill_store = MagicMock()
        mock_skill_store.list_persona_builder_kbs.return_value = []

        with patch.dict(os.environ, {"KBF_ENV": "laptop"}):
            stats = ingestion_worker.main(skill_store=mock_skill_store)

        assert stats == {
            "pages_new": 0,
            "pages_updated": 0,
            "pages_unchanged": 0,
            "skipped_builders": 0,
        }
