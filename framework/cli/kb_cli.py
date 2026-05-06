"""kb-cli — validate / dry-run / eval / promote / migrate / reingest.

Usage:
  kb-cli validate persona_builders/<persona>.yaml
  kb-cli ingest --dry-run --sample 5 persona_builders/<persona>.yaml
  kb-cli eval persona_builders/<persona>.yaml
  kb-cli promote persona_builders/<persona>.yaml
  kb-cli reingest persona_builders/<persona>.yaml --kb <kb_name> --schema-version <N>
  kb-cli migrate --schema kb_incidents --env dev
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def cmd_validate(args):
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"❌ {cfg_path} does not exist", file=sys.stderr)
        return 1
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    errors: list[str] = []

    # Required top-level fields
    required = ["persona", "schema_version", "status", "knowledge_bases", "metadata_defaults", "eval"]
    for r in required:
        if r not in cfg:
            errors.append(f"missing required top-level field: {r}")

    # Each knowledge_base
    for kb in cfg.get("knowledge_bases", []):
        if "name" not in kb:
            errors.append(f"kb missing name: {kb}")
        if kb.get("kind") not in {"vector", "wiki", "graph", "sql_passthrough", "code_index"}:
            errors.append(f"kb {kb.get('name')}: invalid kind {kb.get('kind')!r}")
        if not kb.get("kb_card"):
            errors.append(f"kb {kb.get('name')}: missing kb_card (per ADR-004 v2)")

    # Schema files referenced exist
    schemas_dir = Path(__file__).resolve().parents[1] / "parsers" / "schemas"
    for kb in cfg.get("knowledge_bases", []):
        ref = kb.get("extraction_schema")
        if ref:
            schema_path = Path(ref) if Path(ref).is_absolute() else (schemas_dir.parent.parent / ref)
            if not schema_path.exists():
                errors.append(f"extraction_schema not found: {ref}")

    # Gold set referenced exists
    gold = cfg.get("eval", {}).get("gold_set")
    if gold:
        gp = Path(__file__).resolve().parents[2] / gold
        if not gp.exists():
            errors.append(f"gold_set not found: {gold}")

    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    print(f"✓ {cfg_path.name} valid")
    return 0


def cmd_ingest(args):
    print(f"▶ ingest {args.config} (dry-run={args.dry_run}, sample={args.sample})")
    print("  [Phase 1: needs real adapter auth + ADB pool to actually run]")
    return 0


def cmd_eval(args):
    print(f"▶ eval {args.config}")
    print("  [Phase 1: needs real ADB + OpenAI to actually run]")
    return 0


def cmd_promote(args):
    cfg_path = Path(args.config)
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    cfg["status"] = "production"
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"✓ promoted {cfg_path.name} → status: production")
    return 0


def cmd_migrate(args):
    print(f"▶ migrate schema={args.schema} env={args.env}")
    print("  [Phase 1: needs real ADB connection]")
    return 0


def main():
    p = argparse.ArgumentParser("kb-cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate"); pv.add_argument("config"); pv.set_defaults(fn=cmd_validate)
    pi = sub.add_parser("ingest")
    pi.add_argument("config"); pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--sample", type=int, default=5); pi.set_defaults(fn=cmd_ingest)
    pe = sub.add_parser("eval"); pe.add_argument("config"); pe.set_defaults(fn=cmd_eval)
    pp = sub.add_parser("promote"); pp.add_argument("config"); pp.set_defaults(fn=cmd_promote)
    pm = sub.add_parser("migrate")
    pm.add_argument("--schema", required=True); pm.add_argument("--env", required=True)
    pm.set_defaults(fn=cmd_migrate)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
