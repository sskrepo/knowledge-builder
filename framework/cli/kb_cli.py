"""kb-cli — persona team's primary interface.

V2 commands:
  kb-cli laptop-init                 — bootstrap laptop dev mode (~/.kbf/)
  kb-cli skill-builder --intent-file <yaml> [--dry-run]
                                      — synthesize artifacts from intent file
  kb-cli skill-list                  — list registered workflow skills
  kb-cli workflow-list               — alias for skill-list
  kb-cli workflow-run <skill_name> --inputs '<json>'
                                      — execute on_request workflow skill
  kb-cli validate <persona-builder.yaml>
                                      — lint a persona builder
  kb-cli ingest --dry-run --sample N <persona-builder.yaml>
                                      — preview parser output
  kb-cli eval <persona-builder.yaml>  — run gold-set eval
  kb-cli promote <persona-builder.yaml>
                                      — flip status to production
  kb-cli migrate --schema <name> --env <env>
                                      — apply DDL
  kb-cli gold-feed --persona <persona> [--skill <skill>]
                                      — interactive gold-set feeder (workshop mode)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ============================================================================
# laptop-init
# ============================================================================
def cmd_laptop_init(args):
    """Set up ~/.kbf for laptop dev mode (no ADB / Vault / OCI required)."""
    home_kbf = Path.home() / ".kbf"
    secrets_path = home_kbf / "secrets.yaml"
    store_path = home_kbf / "store"
    outputs_path = home_kbf / "outputs"
    outbox_path = home_kbf / "outbox"
    slack_outbox = home_kbf / "slack-outbox"

    print("▶ KBF laptop-init")

    home_kbf.mkdir(exist_ok=True)
    home_kbf.chmod(0o700)
    store_path.mkdir(exist_ok=True)
    outputs_path.mkdir(exist_ok=True)
    outbox_path.mkdir(exist_ok=True)
    slack_outbox.mkdir(exist_ok=True)

    if not secrets_path.exists():
        template = REPO_ROOT / "framework" / ".secrets.local.yaml.example"
        if template.exists():
            secrets_path.write_text(template.read_text())
        else:
            secrets_path.write_text("secrets: {}\n")
        secrets_path.chmod(0o600)
        print(f"✓ created {secrets_path}")
    else:
        print(f"✓ {secrets_path} exists")
    print(f"✓ store dir: {store_path}")
    print(f"✓ outputs dir: {outputs_path}")
    print()
    print("Set in your shell:")
    print("    export KBF_ENV=dev")
    print("    export KBF_SECRETS_BACKEND=local")
    print(f"    export KBF_SECRETS_FILE={secrets_path}")
    print(f"    export KBF_STORE_BACKEND=filestore")
    print(f"    export KBF_STORE_ROOT={store_path}")
    print(f"    export KBF_LLM_PROVIDER=stub")
    print()
    print("Then try:")
    print("    python -m framework.cli.kb_cli skill-list")
    print("    python -m framework.cli.kb_cli workflow-run ops_eng.incident_summary \\")
    print("        --inputs '{\"incident_id\": \"INC-EXAMPLE-001\"}'")
    return 0


# ============================================================================
# validate (existing, kept)
# ============================================================================
def cmd_validate(args):
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"❌ {cfg_path} does not exist", file=sys.stderr)
        return 1
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    errors: list[str] = []
    required = ["persona", "schema_version", "status", "knowledge_bases", "metadata_defaults", "eval"]
    for r in required:
        if r not in cfg:
            errors.append(f"missing required top-level field: {r}")
    for kb in cfg.get("knowledge_bases", []):
        if "name" not in kb:
            errors.append(f"kb missing name: {kb}")
        if kb.get("kind") not in {"vector", "wiki", "graph", "sql_passthrough", "code_index", "filestore"}:
            errors.append(f"kb {kb.get('name')}: invalid kind {kb.get('kind')!r}")
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
    print("  [needs real ADB/OpenAI to run; laptop-mode eval against fixtures coming Phase 2]")
    return 0


def cmd_promote(args):
    cfg_path = Path(args.config)
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    if args.validate_links:
        from ..skill_builder.validate_links import validate_workflow_links
        skills_dir = REPO_ROOT / "framework" / "workflow_skills"
        pb_dir = REPO_ROOT / "framework" / "persona_builders"
        persona = cfg.get("persona", "")
        persona_skills = list((skills_dir / persona).glob("*.yaml")) if persona else []
        all_errors: list[str] = []
        for skill_path in persona_skills:
            if skill_path.name.startswith("_"):
                continue
            errors = validate_workflow_links(str(skill_path), str(pb_dir))
            all_errors.extend(errors)
        if all_errors:
            print(f"❌ Link validation failed for {cfg_path.name}:", file=sys.stderr)
            for e in all_errors:
                print(f"  • {e}", file=sys.stderr)
            return 1
        print(f"✓ Link validation passed for {cfg_path.name}")

    cfg["status"] = "production"
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"✓ promoted {cfg_path.name} → status: production")
    return 0


def _resolve_secret_cli(ref: str) -> str:
    """Resolve env:// secret references (same logic as mcp_server._resolve_secret)."""
    if ref and ref.startswith("env://"):
        var = ref[6:]
        val = os.environ.get(var, "")
        if not val:
            raise RuntimeError(f"Secret env var not set: {var}")
        return val
    return ref or ""


def _run_sql_ddl(pool, sql_path: Path) -> None:
    """Execute a DDL script against *pool*, splitting on `;` and PL/SQL `/` blocks.

    Swallows ORA-00955 (object already exists) and ORA-01920 (user already
    exists) for idempotent re-runs.  All other errors propagate.
    """
    from ..stores.incident_vector_store import IncidentVectorStore

    sql = sql_path.read_text()
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            for stmt in IncidentVectorStore._split_sql(sql):
                stmt = stmt.strip()
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                except Exception as exc:
                    msg = str(exc)
                    # ORA-00955: name already used by existing object
                    # ORA-01408: such column list already indexed
                    # ORA-01920: user name already exists
                    if any(code in msg for code in ("ORA-00955", "ORA-01408", "ORA-01920")):
                        log.debug("migrate: ignored existing-object: %s", msg.split("\n")[0])
                    else:
                        log.error("migrate: failing stmt:\n%s\n%s", stmt[:200], msg)
                        raise
        conn.commit()


def cmd_migrate(args):
    """Apply DDL migrations to the configured Oracle ADB.

    Usage:
      kb-cli migrate --schema kb_incidents --env laptop
      kb-cli migrate --schema kb_shim --env laptop
      kb-cli migrate --schema all --env laptop
    """
    schema = args.schema
    env = args.env

    print(f"▶ migrate schema={schema} env={env}")

    # ── Load environment config ────────────────────────────────────────────
    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml"
    if not config_path.exists():
        print(f"❌ config not found: {config_path}", file=sys.stderr)
        return 1

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    adb_cfg = cfg.get("adb", {})
    bastion_cfg_dict = cfg.get("bastion", {})

    # ── Build ADB pool ─────────────────────────────────────────────────────
    # create_adb_pool() takes a raw dict and calls AdbPoolConfig.from_dict()
    # internally.  We resolve env:// secrets here, then pass the normalised
    # dict so field names match what from_dict() expects.
    try:
        from ..core.adb_pool import create_adb_pool

        wallet_path = str(Path(adb_cfg.get("wallet_path", "")).expanduser())
        wallet_password = _resolve_secret_cli(adb_cfg.get("wallet_password_secret", ""))
        admin_user = adb_cfg.get("admin_user", "Admin")
        admin_password = _resolve_secret_cli(adb_cfg.get("admin_password_secret", ""))
        service_name = adb_cfg.get("dsn") or adb_cfg.get("service_name", "")

        pool_dict = {
            "deployment_mode": cfg.get("deployment_mode", "laptop"),
            "adb": {
                "service_name":   service_name,
                "wallet_path":    wallet_path,
                "user":           admin_user,
                "password":       admin_password,
                "wallet_password": wallet_password,
            },
            "bastion": bastion_cfg_dict,   # passed through as-is; from_dict handles it
        }

        print(f"  Connecting to ADB ({service_name} @ localhost:{adb_cfg.get('port', 1522)}) …")
        pool = create_adb_pool(pool_dict)
        print("  ✓ ADB pool ready")

    except Exception as exc:
        print(f"❌ Failed to create ADB pool: {exc}", file=sys.stderr)
        return 1

    # ── Run migrations ─────────────────────────────────────────────────────
    run_all = (schema == "all")
    sql_dir = REPO_ROOT / "framework" / "stores" / "sql"

    try:
        if run_all or schema == "kb_incidents":
            from ..stores.incident_vector_store import IncidentVectorStore
            from ..core.llm import LLMClient

            print("  Running kb_incidents migration …")
            llm = LLMClient()   # stub — LLM not needed for DDL
            store = IncidentVectorStore(adb_pool=pool, llm=llm)
            store.migrate()
            print("  ✓ kb_incidents: OK")

        if run_all or schema == "kb_shim":
            print("  Running kb_shim migration …")
            _run_sql_ddl(pool, sql_dir / "kb_shim.sql")
            print("  ✓ kb_shim: OK")

        if schema not in ("kb_incidents", "kb_shim", "all"):
            print(f"❌ Unknown schema '{schema}'. Valid: kb_incidents | kb_shim | all",
                  file=sys.stderr)
            _close_pool(pool)
            return 1

    except Exception as exc:
        print(f"❌ Migration failed: {exc}", file=sys.stderr)
        _close_pool(pool)
        return 1

    _close_pool(pool)
    print("✓ migrate complete")
    return 0


def _close_pool(pool) -> None:
    """Close an oracledb pool, unwrapping RetryWrapper if needed."""
    underlying = getattr(pool, "_pool", pool)
    if hasattr(underlying, "close"):
        try:
            underlying.close()
        except Exception:
            pass


# ============================================================================
# Interactive skill-builder (conversation.py)
# ============================================================================
def _run_interactive_skill_builder(args):
    from ..skill_builder.conversation import SkillBuilderConversation

    persona = args.persona or ""
    if not persona:
        persona = input("Persona (e.g. ops_eng, pm, tpm): ").strip()

    conv = SkillBuilderConversation(persona=persona)
    turn = conv.start()
    print(f"\n{turn.message}\n")

    while not turn.done:
        if turn.options:
            print(f"  Suggestions: {turn.options}")
        user_input = input("> ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Session cancelled.")
            return 0
        turn = conv.respond(user_input)
        print(f"\n{turn.message}\n")
        if turn.artifacts_preview:
            print("  Artifacts preview:")
            for k, v in turn.artifacts_preview.items():
                print(f"    {k}: {v}")
            print()

    return 0


# ============================================================================
# code-wiki-build (Phase 2 / Track B)
# ============================================================================
def cmd_code_wiki_build(args):
    """Build a structural code wiki index from a Python repository.

    Scans .py files, extracts module docstrings / class names / function
    signatures, writes ContentItems to the filestore and a fast-lookup index
    at {store_root}/code_wiki_index.json.
    """
    from ..adapters.code_wiki_builder import CodeWikiBuilder

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.exists():
        print(f"❌ repo path does not exist: {repo_path}", file=sys.stderr)
        return 1

    store_root = args.store_root or os.environ.get(
        "KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store")
    )

    print(f"▶ code-wiki-build: scanning {repo_path}")
    print(f"  store root: {store_root}")

    builder = CodeWikiBuilder(repo_path=repo_path, store_root=store_root)
    index_path = builder.run()

    import json as _json
    index = _json.loads(Path(index_path).read_text())
    print(f"✓ Indexed {len(index)} Python modules")
    print(f"  Index written to: {index_path}")
    print()
    print("Now you can use the MCP tools:")
    print("  find_symbol <name>")
    print("  read_code_page <module_path>")
    return 0


# ============================================================================
# skill-builder (V2 / ADR-015)
# ============================================================================
def cmd_skill_builder(args):
    if not args.intent_file:
        return _run_interactive_skill_builder(args)
    intent_path = Path(args.intent_file)
    if not intent_path.exists():
        print(f"❌ {intent_path} does not exist", file=sys.stderr)
        return 1
    intent = yaml.safe_load(intent_path.read_text())
    from ..skill_builder.intent_to_artifacts import SkillBuilder
    builder = SkillBuilder()
    result = builder.synthesize(intent, dry_run=args.dry_run)
    print(f"✓ Skill builder complete (dry_run={args.dry_run})")
    print(f"  Persona:        {result['persona']}")
    print(f"  Skill name:     {result['skill_name']}")
    print(f"  KB name:        {result['kb_name']}")
    print(f"  Required fields: {result['required_fields']}")
    print(f"  Reuse covered:  {len(result['reuse']['covered'])} fields from existing KBs")
    print(f"  New extraction needed for: {result['reuse']['gaps']}")
    print(f"  Artifacts {'would-be ' if args.dry_run else ''}written:")
    for a in result["artifacts"]:
        print(f"    • {a}")
    if not args.dry_run:
        print()
        print("Next steps:")
        print(f"  1. Review the synthesized artifacts (git diff)")
        print(f"  2. Apply the persona-builder diff (if produced) to the YAML")
        print(f"  3. kb-cli validate framework/persona_builders/{result['persona']}.yaml")
        print(f"  4. kb-cli workflow-run {result['persona']}.{result['skill_name']} --inputs '{{}}'")
    return 0


# ============================================================================
# skill-list / workflow-list
# ============================================================================
def cmd_skill_list(args):
    from ..workflow_runtime.skill_registry import discover_workflow_skills
    skills_dir = REPO_ROOT / "framework" / "workflow_skills"
    skills = discover_workflow_skills(skills_dir)
    if not skills:
        print("No workflow skills registered yet.")
        return 0
    print(f"{'Name':<40} {'Persona':<15} {'Status':<10} {'Triggers':<25}")
    print("-" * 90)
    for s in skills:
        cfg = s.skill_config
        triggers = []
        if (cfg.get("trigger") or {}).get("on_request", {}).get("enabled"):
            triggers.append("on_request")
        if (cfg.get("trigger") or {}).get("on_schedule", {}).get("cron"):
            triggers.append("on_schedule")
        status = cfg.get("status", "draft")
        print(f"{s.skill_name or '(unnamed)':<40} {s.persona or '?':<15} "
              f"{status:<10} {','.join(triggers):<25}")
    return 0


# ============================================================================
# workflow-run (V2 / ADR-016)
# ============================================================================
def cmd_workflow_run(args):
    from ..workflow_runtime.executor import WorkflowExecutor
    from ..workflow_runtime.skill_registry import discover_workflow_skills
    skills_dir = REPO_ROOT / "framework" / "workflow_skills"
    skills = discover_workflow_skills(skills_dir)

    # Find the named skill (accept "persona.skill_name" or just "skill_name")
    target = None
    if "." in args.skill_name:
        persona, name = args.skill_name.split(".", 1)
        target = next(
            (s for s in skills if s.persona == persona and s.skill_name == name), None
        )
    else:
        candidates = [s for s in skills if s.skill_name == args.skill_name]
        if len(candidates) == 1:
            target = candidates[0]
        elif len(candidates) > 1:
            print(f"Ambiguous skill name {args.skill_name!r}; please qualify with persona:")
            for c in candidates:
                print(f"  {c.persona}.{c.skill_name}")
            return 1

    if not target:
        print(f"❌ unknown workflow skill: {args.skill_name}", file=sys.stderr)
        print(f"Available: {[s.skill_name for s in skills]}")
        return 1

    inputs = json.loads(args.inputs) if args.inputs else {}

    # Pick a store backend
    store = None
    if os.environ.get("KBF_STORE_BACKEND") == "filestore":
        from ..stores.filestore_content_store import FilestoreContentStore
        store_root = os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))
        store = FilestoreContentStore(root=store_root)

    executor = WorkflowExecutor(store=store)
    result = executor.execute(Path(target._path), inputs)

    print(f"✓ Workflow {target.persona}.{target.skill_name} executed")
    print(f"  Inputs:   {result['inputs']}")
    print(f"  Delivery: {result['delivery']}")
    print(f"  Output:   {result['delivery'].get('url') or result['delivery'].get('path') or '(sync return)'}")
    print()
    if args.show_data:
        print("Rendered data:")
        print(json.dumps(result["rendered_data"], indent=2, default=str))
    return 0


# ============================================================================
# gold-feed — interactive gold-set feeder (workshop mode)
# ============================================================================
def cmd_gold_feed(args):
    """Run the interactive GoldSetFeeder loop.

    Prompts workshop participants for query/citation pairs and appends them to
    framework/eval/gold_sets/{persona}.jsonl.
    """
    from ..eval.gold_set_feeder import GoldSetFeeder, count_entries

    persona = args.persona.strip()
    skill = getattr(args, "skill", "") or ""

    existing = count_entries(persona)
    if existing:
        print(f"  ({existing} entries already in gold set for '{persona}')")

    feeder = GoldSetFeeder(persona=persona, skill_name=skill)
    turn = feeder.start()
    print(f"\n{turn.message}\n")

    while not turn.done:
        if turn.options:
            print(f"  Suggestions: {turn.options}")
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSession interrupted.")
            return 0
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Session cancelled.")
            return 0
        turn = feeder.respond(user_input)
        print(f"\n{turn.message}\n")

    return 0


# ============================================================================
# watch-bugs — deduplicating error/bug watcher
# ============================================================================

def cmd_watch_bugs(args):
    """Read errors.jsonl and user_bugs.jsonl, deduplicate, and print diagnosis blocks."""
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict

    store_root = Path(
        args.store_root
        or os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))
    )

    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        records: list[dict] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    errors = _read_jsonl(store_root / "errors.jsonl")
    user_bugs = _read_jsonl(store_root / "user_bugs.jsonl")

    # Optional: filter by --since MINUTES
    if args.since:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=args.since)
        def _after_cutoff(rec: dict) -> bool:
            ts_str = rec.get("timestamp", "")
            if not ts_str:
                return True
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts >= cutoff
            except ValueError:
                return True
        errors = [e for e in errors if _after_cutoff(e)]
        user_bugs = [b for b in user_bugs if _after_cutoff(b)]

    # Index user_bugs by request_id for fast join
    bugs_by_request_id: dict[str, dict] = {
        bug["request_id"]: bug for bug in user_bugs if bug.get("request_id")
    }

    # Group errors by (error_type, message[:80]) for fuzzy dedup
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for err in errors:
        key = (err.get("error_type", ""), (err.get("message") or "")[:80])
        groups[key].append(err)

    # Load known bugs from pmo/bugs/ for dedup
    bugs_dir = REPO_ROOT / "pmo" / "bugs"
    known_bug_texts: list[tuple[str, str]] = []
    if bugs_dir.exists():
        for bug_file in sorted(bugs_dir.glob("BUG-*.md")):
            content = bug_file.read_text(encoding="utf-8", errors="replace")
            known_bug_texts.append((bug_file.stem, content))

    SEP = "━" * 40
    new_candidates = 0
    known_count = 0

    for (error_type, message_prefix), group_errors in sorted(
        groups.items(),
        key=lambda kv: min(e.get("timestamp", "") for e in kv[1]),
    ):
        rep = min(group_errors, key=lambda e: e.get("timestamp", ""))
        rid = rep.get("request_id", "?")
        tool = rep.get("tool", "?")
        first_seen = rep.get("timestamp", "?")
        count = len(group_errors)
        full_message = rep.get("message", message_prefix)
        display_error = f"{error_type} — {full_message}" if error_type else full_message

        user_report = bugs_by_request_id.get(rid)
        report_str = "NO"
        if user_report:
            desc = (user_report.get("description") or "")[:60]
            report_str = f'YES — "{desc}"'

        known_label = ""
        for bug_stem, bug_content in known_bug_texts:
            if message_prefix and message_prefix.lower() in bug_content.lower():
                known_label = f"KNOWN — already filed as {bug_stem}"
                break

        if known_label:
            known_count += 1
        else:
            new_candidates += 1

        print(SEP)
        status_tag = known_label if known_label else "[BUG-candidate]"
        print(f"{status_tag} {rid}  ({count} occurrence{'s' if count != 1 else ''})")
        print(f"Tool:       {tool}")
        print(f"Error:      {display_error}")
        print(f"First seen: {first_seen}")
        print(f"User report: {report_str}")
        print(SEP)

    print()
    print(f"{new_candidates} new candidates, {known_count} known bugs, {len(errors)} total errors")
    return 0


# ============================================================================
# main
# ============================================================================
def main():
    p = argparse.ArgumentParser("kb-cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("laptop-init", help="bootstrap laptop dev mode")
    p_init.set_defaults(fn=cmd_laptop_init)

    pv = sub.add_parser("validate"); pv.add_argument("config"); pv.set_defaults(fn=cmd_validate)

    pi = sub.add_parser("ingest")
    pi.add_argument("config"); pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--sample", type=int, default=5); pi.set_defaults(fn=cmd_ingest)

    pe = sub.add_parser("eval"); pe.add_argument("config"); pe.set_defaults(fn=cmd_eval)
    pp = sub.add_parser("promote"); pp.add_argument("config")
    pp.add_argument("--validate-links", action="store_true", help="validate workflow links before promoting (ADR-017)")
    pp.set_defaults(fn=cmd_promote)

    pm = sub.add_parser("migrate")
    pm.add_argument("--schema", required=True); pm.add_argument("--env", required=True)
    pm.set_defaults(fn=cmd_migrate)

    psb = sub.add_parser("skill-builder", help="synthesize skills (interactive or from intent file)")
    psb.add_argument("--intent-file", help="YAML intent file (omit for interactive mode)")
    psb.add_argument("--persona", help="persona name for interactive mode")
    psb.add_argument("--dry-run", action="store_true", help="don't write artifacts to disk")
    psb.set_defaults(fn=cmd_skill_builder)

    psl = sub.add_parser("skill-list", help="list registered workflow skills")
    psl.set_defaults(fn=cmd_skill_list)
    pwl = sub.add_parser("workflow-list", help="alias for skill-list")
    pwl.set_defaults(fn=cmd_skill_list)

    pwr = sub.add_parser("workflow-run", help="execute on_request workflow skill")
    pwr.add_argument("skill_name", help="<persona>.<skill_name> or just <skill_name>")
    pwr.add_argument("--inputs", default="{}", help="JSON object of inputs")
    pwr.add_argument("--show-data", action="store_true")
    pwr.set_defaults(fn=cmd_workflow_run)

    pcwb = sub.add_parser("code-wiki-build", help="build structural code wiki index from a Python repo")
    pcwb.add_argument("--repo-path", default=".", help="path to the Python repo root (default: .)")
    pcwb.add_argument("--store-root", default=None, help="filestore root (default: $KBF_STORE_ROOT or ~/.kbf/store)")
    pcwb.set_defaults(fn=cmd_code_wiki_build)

    pgf = sub.add_parser("gold-feed", help="interactive gold-set feeder (workshop mode)")
    pgf.add_argument("--persona", required=True, help="persona id (e.g. ops_eng)")
    pgf.add_argument("--skill", default="", help="skill / KB name (e.g. incident_summary)")
    pgf.set_defaults(fn=cmd_gold_feed)

    pwb = sub.add_parser("watch-bugs", help="deduplicate and diagnose error/bug reports")
    pwb.add_argument("--store-root", default=None,
                     help="path to the KBF store root (default: $KBF_STORE_ROOT or ~/.kbf/store)")
    pwb.add_argument("--since", type=int, default=None, metavar="MINUTES",
                     help="only show errors from the last N minutes")
    pwb.set_defaults(fn=cmd_watch_bugs)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
