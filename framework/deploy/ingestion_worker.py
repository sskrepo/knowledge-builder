"""OCI Functions / OCI Compute ingestion worker entrypoint."""
from __future__ import annotations
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def main(persona_builder: str | None = None):
    """Run ingestion for one persona builder (or all if None)."""
    from ..core.llm import LLMClient
    from ..parsers.llm_parser import LLMParser
    from ..stores.incident_vector_store import IncidentVectorStore
    from ..adapters.jira import make_jira_adapter
    from ..adapters.confluence import make_confluence_adapter
    from ..ingestion.pipeline import IngestionPipeline
    from ..core.interfaces import RawItem  # noqa
    import yaml

    REPO_ROOT = Path(__file__).resolve().parents[2]
    builders_dir = REPO_ROOT / "framework" / "persona_builders"
    schemas_dir = REPO_ROOT / "framework" / "parsers" / "schemas"
    adapter_cfgs_dir = REPO_ROOT / "framework" / "config" / "adapters"

    builder_files = sorted(builders_dir.glob("*.yaml"))
    if persona_builder:
        builder_files = [b for b in builder_files if b.stem == persona_builder]

    llm = LLMClient()
    # NB: in real deploy, an oracledb connection pool is created here
    adb_pool = None

    for bf in builder_files:
        if bf.name.startswith("_"):
            continue
        with open(bf) as f:
            cfg = yaml.safe_load(f)
        if cfg.get("status") != "production":
            log.info("skipping %s (status=%s)", bf.stem, cfg.get("status"))
            continue
        log.info("ingesting persona %s", cfg["persona"])
        # ... wire up adapter + parser + store per cfg
        # Phase 1 scope: print intent only — full wiring lands when ADB is live
        log.info("would process %d knowledge_bases", len(cfg.get("knowledge_bases", [])))


if __name__ == "__main__":
    persona = sys.argv[1] if len(sys.argv) > 1 else None
    main(persona)
