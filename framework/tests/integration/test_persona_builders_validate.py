"""Validate that all shipped persona-builder configs pass kb-cli validate."""
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]

def test_each_persona_builder_passes_validate():
    for cfg in (REPO / "framework" / "persona_builders").glob("*.yaml"):
        if cfg.name.startswith("_"):
            continue
        result = subprocess.run(
            [sys.executable, "-m", "framework.cli.kb_cli", "validate", str(cfg)],
            capture_output=True, text=True, cwd=REPO,
        )
        assert result.returncode == 0, f"{cfg.name} failed: {result.stderr}"
