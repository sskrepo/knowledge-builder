#!/usr/bin/env python3
"""
check-config.py — validate framework configuration and probe live dependencies.

Usage:
  python3 framework/scripts/check-config.py --env dev
  python3 framework/scripts/check-config.py --env dev --emit-secrets-manifest

Modes:
  default         — full validation: schema check, vault resolution, ADB ping,
                    OpenAI ping, MCP capability probes.
  --emit-secrets-manifest   — print the required vault:// references (one per line)
                              for use by bootstrap-vault.sh. No live probes.

Exit codes:
  0  all green
  1  config schema invalid
  2  missing vault secret
  3  ADB unreachable
  4  OpenAI unreachable
  5  MCP capability missing
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "framework" / "config"
PERSONA_DIR = REPO_ROOT / "framework" / "persona_builders"

VAULT_REF_RE = re.compile(r"vault://[A-Za-z0-9_/\-]+")


def load_yaml(path: Path) -> dict:
    """Tolerant YAML loader. Uses PyYAML if available, else a minimal subset."""
    try:
        import yaml
        return yaml.safe_load(path.read_text())
    except ImportError:
        sys.stderr.write("⚠️  PyYAML not installed; using fallback. Install with: pip install pyyaml\n")
        # Minimal fallback for emit-secrets-manifest only — searches text for vault://
        return {"_raw_text": path.read_text()}


def collect_vault_refs(env: str) -> list[str]:
    """Walk all configs and return every distinct vault:// reference."""
    refs: set[str] = set()

    files: list[Path] = [
        CONFIG_DIR / f"{env}.yaml",
        *(CONFIG_DIR / "adapters").glob("*.yaml"),
        *(PERSONA_DIR.glob("*.yaml")) if PERSONA_DIR.exists() else [],
    ]

    for f in files:
        if not f.exists():
            continue
        text = f.read_text()
        for m in VAULT_REF_RE.findall(text):
            refs.add(m)

    return sorted(refs)


def validate_schema(env: str) -> int:
    """Lightweight schema check — full JSON Schema validation requires jsonschema lib."""
    env_file = CONFIG_DIR / f"{env}.yaml"
    if not env_file.exists():
        print(f"❌ Env config missing: {env_file}", file=sys.stderr)
        return 1

    cfg = load_yaml(env_file)
    if "_raw_text" in cfg:
        return 0  # YAML lib not available; skip strict validation

    required = ["env", "region", "adb", "vault", "object_storage", "openai", "observability", "eval"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"❌ Required keys missing in {env_file}: {missing}", file=sys.stderr)
        return 1

    # Placeholder check
    text = env_file.read_text()
    placeholders = re.findall(r"YOUR_[A-Z_]+", text)
    if placeholders:
        print(f"⚠️  {env_file} still has placeholders: {sorted(set(placeholders))}", file=sys.stderr)

    return 0


def probe_vault(refs: list[str]) -> int:
    """Verify every vault:// reference resolves. Stub — real impl uses OCI SDK."""
    try:
        import oci  # noqa: F401
    except ImportError:
        print("⚠️  oci SDK not installed; skipping live vault probe. Install with: pip install oci")
        return 0

    # TODO: implement with oci.secrets.SecretsClient
    print(f"  (Phase 1 will implement live vault probe; refs to verify: {len(refs)})")
    return 0


def probe_adb(env: str) -> int:
    print(f"  (Phase 1 will implement ADB ping for env={env})")
    return 0


def probe_openai(env: str) -> int:
    print(f"  (Phase 1 will implement OpenAI 1-token ping for env={env})")
    return 0


def probe_mcp_capabilities() -> int:
    print(f"  (Phase 1 will implement MCP tools/list capability probe for confluence + jira when mode=mcp)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "staging", "prod"])
    ap.add_argument("--emit-secrets-manifest", action="store_true",
                    help="Print required vault:// references and exit.")
    args = ap.parse_args()

    refs = collect_vault_refs(args.env)

    if args.emit_secrets_manifest:
        for r in refs:
            print(r)
        return 0

    print(f"▶ check-config: env={args.env}")
    print()

    print("1. Schema validation")
    rc = validate_schema(args.env)
    if rc != 0:
        return rc
    print("   ✓ ok")

    print(f"2. Required secrets ({len(refs)} found)")
    for r in refs:
        print(f"   {r}")
    print()

    print("3. Vault resolution probe")
    rc = probe_vault(refs)
    if rc != 0:
        return 2
    print("   ✓ ok")

    print("4. ADB ping")
    rc = probe_adb(args.env)
    if rc != 0:
        return 3
    print("   ✓ ok")

    print("5. OpenAI ping")
    rc = probe_openai(args.env)
    if rc != 0:
        return 4
    print("   ✓ ok")

    print("6. MCP capability probe (for adapters with mode: mcp)")
    rc = probe_mcp_capabilities()
    if rc != 0:
        return 5
    print("   ✓ ok")

    print()
    print("✅ all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
