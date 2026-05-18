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
        if kb.get("kind") not in {"vector", "wiki", "graph", "sql_passthrough", "code_index", "filestore", "adb"}:
            errors.append(f"kb {kb.get('name')}: invalid kind {kb.get('kind')!r}")
    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    print(f"✓ {cfg_path.name} valid")
    return 0


def cmd_ingest(args):
    """Run Confluence ingestion for a persona-builder config.

    A3 (BUG-queue-990fe): --persona is forwarded to ConfluenceWikiIngestor so
    pages with no raw persona field are stored with the correct persona in
    wiki_metadata (RC1 fix).  If --persona is omitted the persona is read from
    the config YAML.  If neither is determinable, ingest fails loudly (never
    silently stores null persona when the persona IS known from the config).
    """
    import yaml as _yaml
    from pathlib import Path as _Path
    from ..ingestion.confluence_wiki_ingest import ConfluenceWikiIngestor

    cfg_path = _Path(args.config)
    if not cfg_path.exists():
        print(f"ERROR: config not found: {cfg_path}", file=sys.stderr)
        return 1

    with open(cfg_path) as f:
        pb_cfg = _yaml.safe_load(f) or {}

    # Resolve persona: CLI flag wins, then config file, then fail loudly.
    persona = getattr(args, "persona", None) or pb_cfg.get("persona") or None
    if not persona:
        print(
            "ERROR: persona could not be determined. Pass --persona <persona> or "
            "set 'persona:' in the config YAML. Refusing to ingest with null persona "
            "(BUG-queue-990fe RC1 — null persona pages are filtered out at retrieval).",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"▶ ingest {cfg_path} --persona {persona} (dry-run — no writes)")
        sources = pb_cfg.get("sources", [])
        confluence_sources = [s for s in sources if s.get("kind") == "confluence"]
        print(f"  Confluence sources: {len(confluence_sources)}")
        for src in confluence_sources:
            print(f"  space={src.get('space', '?')} labels={src.get('include_labels') or src.get('labels') or '(none)'}")
        return 0

    print(f"▶ ingest {cfg_path} --persona {persona}")

    kbf_env = os.environ.get("KBF_ENV", "laptop")
    config_env_path = REPO_ROOT / "framework" / "config" / f"{kbf_env}.yaml"
    env_cfg: dict = {}
    if config_env_path.exists():
        try:
            import yaml as _yaml2
            env_cfg = _yaml2.safe_load(config_env_path.read_text()) or {}
        except Exception:
            pass

    from ..deploy.ingestion_worker import _build_confluence_adapter
    from ..stores.wiki_metadata_store import WikiMetadataStore

    confluence_adapter = _build_confluence_adapter(env_cfg, kbf_env)
    wiki_store = WikiMetadataStore()
    ingestor = ConfluenceWikiIngestor(
        adapter=confluence_adapter,
        wiki_store=wiki_store,
        persona=persona,
    )

    sources = pb_cfg.get("sources", [])
    total = {"pages_new": 0, "pages_updated": 0, "pages_unchanged": 0}
    for src in sources:
        if src.get("kind") != "confluence":
            continue
        space = src.get("space", "")
        labels = src.get("include_labels") or src.get("labels") or []
        pages = src.get("pages") or []
        if pages:
            stats = ingestor.ingest_pages(pages)
        else:
            stats = ingestor.ingest_space(space, labels or None)
        for k in ("pages_new", "pages_updated", "pages_unchanged"):
            total[k] += stats.get(k, 0)
        print(f"  space={space}: new={stats.get('pages_new',0)} "
              f"updated={stats.get('pages_updated',0)} "
              f"unchanged={stats.get('pages_unchanged',0)}")

    print(f"Done. total new={total['pages_new']} updated={total['pages_updated']} "
          f"unchanged={total['pages_unchanged']}")
    return 0


def cmd_eval(args):
    print(f"▶ eval {args.config}")
    print("  [needs real ADB/OpenAI to run; laptop-mode eval against fixtures coming Phase 2]")
    return 0


def cmd_wiki_meta_backfill_persona(args):
    """A4 (BUG-queue-990fe): Idempotent backfill of persona on null-persona
    wiki_metadata records.

    Rules (strict):
    - ONLY overwrites records whose stored persona IS NULL / empty.
    - NEVER overwrites a record that already has a non-null persona.
    - When --page-id is given, targets exactly that record (or reports not found).
    - Without --page-id, targets ALL null-persona records in the store root.
    - Re-running is a no-op (idempotent).
    - When persona cannot be determined for a record, reports it and skips
      (never guesses).

    Returns 0 on success, 1 on error.
    """
    import json as _json
    import re as _re
    from pathlib import Path as _Path

    store_root_env = os.environ.get("KBF_STORE_ROOT")
    if store_root_env:
        store_root = _Path(store_root_env) / "wiki_metadata"
    else:
        store_root = _Path.home() / ".kbf" / "store" / "wiki_metadata"

    persona = args.persona
    page_id = getattr(args, "page_id", None)
    dry_run = getattr(args, "dry_run", False)

    if not persona:
        print("ERROR: --persona is required for backfill-persona", file=sys.stderr)
        return 1

    if not store_root.exists():
        print(f"ERROR: wiki_metadata store not found: {store_root}", file=sys.stderr)
        return 1

    # Collect candidate JSON files
    if page_id:
        safe_stem = _re.sub(r"[^\w.-]", "_", page_id) or "_unnamed"
        candidates = [store_root / f"{safe_stem}.json"]
        if not candidates[0].exists():
            print(f"ERROR: wiki_metadata record for page_id={page_id} not found at {candidates[0]}", file=sys.stderr)
            return 1
    else:
        candidates = sorted(store_root.glob("*.json"))

    updated = 0
    skipped_nonnull = 0
    not_found = 0
    noop = 0

    for record_path in candidates:
        try:
            record = _json.loads(record_path.read_text())
        except Exception as exc:
            print(f"  WARN: could not read {record_path.name}: {exc}", file=sys.stderr)
            continue

        current_persona = record.get("persona")
        rec_page_id = record.get("page_id", record_path.stem)

        if current_persona:
            # Non-null persona — do NOT overwrite
            skipped_nonnull += 1
            continue

        # Null persona — eligible for backfill
        if dry_run:
            print(f"  [dry-run] would set persona={persona!r} on page_id={rec_page_id}")
            updated += 1
            continue

        record["persona"] = persona
        record_path.write_text(_json.dumps(record, indent=2, default=str))
        print(f"  backfilled: page_id={rec_page_id} persona=null → {persona!r}")
        updated += 1

    action = "would update" if dry_run else "updated"
    print(
        f"\nBackfill complete: {action} {updated} record(s); "
        f"skipped {skipped_nonnull} already-set record(s). "
        f"(dry_run={dry_run})"
    )
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

            print("  Running kb_incidents migration …")
            # llm=None: migration only runs DDL — no embedding calls, no OCI GenAI init
            store = IncidentVectorStore(adb_pool=pool, llm=None)
            store.migrate()
            print("  ✓ kb_incidents: OK")

        if run_all or schema == "kb_shim":
            print("  Running kb_shim migration …")
            _run_sql_ddl(pool, sql_dir / "kb_shim.sql")
            print("  ✓ kb_shim: OK")

            # Apply numbered incremental migrations (framework/db/migrations/*.sql),
            # sorted by filename so they run in order (001, 002, … 005, 006, …).
            # All migration files are idempotent — safe to re-run.
            migrations_dir = REPO_ROOT / "framework" / "db" / "migrations"
            if migrations_dir.exists():
                migration_files = sorted(migrations_dir.glob("*.sql"))
                for mf in migration_files:
                    print(f"  Applying {mf.name} …")
                    _run_sql_ddl(pool, mf)
                    print(f"  ✓ {mf.name}: OK")

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

def cmd_export_skills(args):
    """Export skill artifacts from KBF_SKILL_ARTIFACTS (ADB) to the local filesystem.

    For each skill (optionally filtered by --persona and/or --status), reads the
    4 artifact CLOBs from the database and writes them to their canonical rel_path
    under --out-dir (default: REPO_ROOT).

    This is a read-only operation — the server never calls it.

    Usage:
        kb-cli export-skills [--persona tpm] [--status promoted] [--out-dir .]
    """
    persona_filter = getattr(args, "persona", None)
    status_filter  = getattr(args, "status", None)
    out_dir = Path(getattr(args, "out_dir", None) or ".").resolve()

    # ── Build ADB pool ──────────────────────────────────────────────────────
    env = getattr(args, "env", "") or os.environ.get("KBF_ENV", "")
    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml" if env else None

    pool = None
    if config_path and config_path.exists():
        try:
            with open(config_path) as fh:
                cfg = yaml.safe_load(fh)
            adb_cfg = cfg.get("adb", {})
            from ..core.adb_pool import create_adb_pool  # type: ignore[import]
            wallet_path = str(Path(adb_cfg.get("wallet_path", "")).expanduser())
            wallet_password = _resolve_secret_cli(adb_cfg.get("wallet_password_secret", ""))
            admin_user = adb_cfg.get("admin_user", "Admin")
            admin_password = _resolve_secret_cli(adb_cfg.get("admin_password_secret", ""))
            service_name = adb_cfg.get("dsn") or adb_cfg.get("service_name", "")
            pool = create_adb_pool({
                "deployment_mode": cfg.get("deployment_mode", "laptop"),
                "adb": {
                    "service_name":    service_name,
                    "wallet_path":     wallet_path,
                    "user":            admin_user,
                    "password":        admin_password,
                    "wallet_password": wallet_password,
                },
                "bastion": cfg.get("bastion", {}),
            })
        except Exception as exc:
            print(f"❌ Failed to create ADB pool: {exc}", file=sys.stderr)
            return 1
    else:
        print(
            "⚠ No env config found — using filestore (ADB export skipped).",
            file=sys.stderr,
        )

    from ..deploy.skill_store import build_skill_store
    store = build_skill_store(pool=pool, env=env)

    # ── List skills from store ──────────────────────────────────────────────
    skills = store.list_skills(persona=persona_filter)

    if status_filter:
        skills = [s for s in skills if s.get("status") == status_filter]

    if not skills:
        print("No matching skills found.")
        if pool is not None:
            _close_pool(pool)
        return 0

    print(f"Exporting {len(skills)} skill(s) to {out_dir} …")
    written_total = 0

    from ..deploy.skill_store._base import ARTIFACT_TYPES

    for skill in skills:
        p = skill["persona"]
        sn = skill["skill_name"]
        skill_written = 0

        for artifact_type in sorted(ARTIFACT_TYPES):
            content = store.read_artifact(persona=p, skill_name=sn, artifact_type=artifact_type)
            if content is None:
                continue

            # Compute destination path
            rel_path_templates = {
                "workflow_skill":         f"framework/workflow_skills/{p}/{sn}.yaml",
                "persona_builder_delta":  f"framework/persona_builders/{p}.yaml.new_kb",
                "eval_extraction":        f"eval/gold_sets/{p}-{sn}-extraction.jsonl",
                "eval_workflow":          f"eval/gold_sets/{p}-{sn}-workflow.jsonl",
            }
            rel = rel_path_templates[artifact_type]
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            print(f"  ✓ {rel}")
            skill_written += 1
            written_total += 1

        print(f"  └─ {p}.{sn}: {skill_written} artifact(s)")

    print(f"\n✓ export-skills complete — {written_total} file(s) written")

    if pool is not None:
        _close_pool(pool)

    return 0


# ============================================================================
# export-bugs — generate markdown snapshots from ADB bug stores (DECISION-008)
# ============================================================================

def cmd_export_bugs(args):
    """Export bug records from ADB (KBF_BUG_REPORTS + KBF_AUDIT_RUNS) to markdown.

    Reads from two ADB tables:
      - KB_SHIM.KBF_BUG_REPORTS  — user-reported bugs (via reportBug MCP tool)
      - KB_SHIM.KBF_AUDIT_RUNS   — critic-found audit findings (via reviewSkillSession)

    Generates one .md file per record plus an INDEX.md summary table.
    Files use YAML frontmatter for metadata and <details> blocks for full content,
    so they render nicely in GitHub / IDE markdown viewers with expandable sections.

    These files are READ-ONLY snapshots — ADB is the source of truth (DECISION-008).
    Re-running overwrites previous exports.

    Usage:
        kb-cli export-bugs [--out-dir pmo/bugs] [--env laptop] [--status open]
    """
    import datetime as _dt

    out_dir = Path(getattr(args, "out_dir", None) or (REPO_ROOT / "pmo" / "bugs")).resolve()
    env = getattr(args, "env", "") or os.environ.get("KBF_ENV", "laptop")

    # ── Build ADB pool ──────────────────────────────────────────────────────
    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml"
    if not config_path.exists():
        print(f"❌ Config not found: {config_path}", file=sys.stderr)
        return 1

    pool = None
    try:
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh)

        # DECISION-009: export-bugs reads from the dedicated bug_db config.
        # Any field not set in bug_db is inherited from the adb section.
        adb_cfg = cfg.get("adb", {})
        bug_db_cfg = cfg.get("bug_db", {})

        # Merge: adb base, bug_db overrides
        service_name = (
            bug_db_cfg.get("dsn")
            or bug_db_cfg.get("service_name")
            or adb_cfg.get("dsn")
            or adb_cfg.get("service_name", "")
        )
        wallet_path = str(
            Path(
                bug_db_cfg.get("wallet_path") or adb_cfg.get("wallet_path", "")
            ).expanduser()
        )
        wallet_password = _resolve_secret_cli(
            bug_db_cfg.get("wallet_password_secret")
            or adb_cfg.get("wallet_password_secret", "")
        )
        # For the export we connect as the bug DB user (KBF_BUGS) when available;
        # fall back to adb admin_user for envs that have not yet migrated.
        user = bug_db_cfg.get("user") or adb_cfg.get("admin_user", "Admin")
        password = _resolve_secret_cli(
            bug_db_cfg.get("password_secret")
            or adb_cfg.get("admin_password_secret", "")
        )

        from ..core.adb_pool import create_adb_pool  # type: ignore[import]
        pool = create_adb_pool({
            "deployment_mode": cfg.get("deployment_mode", "laptop"),
            "adb": {
                "service_name":    service_name,
                "wallet_path":     wallet_path,
                "user":            user,
                "password":        password,
                "wallet_password": wallet_password,
            },
            "bastion": cfg.get("bastion", {}),
        })
    except Exception as exc:
        print(f"❌ Failed to create ADB pool: {exc}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Fetch user-reported bugs ────────────────────────────────────────────
    user_bugs: list[dict] = []
    try:
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT queue_id, request_id, timestamp_utc, tool, description, extra_json
                       FROM KB_SHIM.KBF_BUG_REPORTS
                       ORDER BY timestamp_utc DESC"""
                )
                cols = [d[0].lower() for d in cur.description]
                for row in cur.fetchall():
                    rec = dict(zip(cols, row))
                    # Materialise any Oracle LOB values to str
                    for k, v in rec.items():
                        if hasattr(v, "read"):
                            rec[k] = v.read()
                    extra: dict = {}
                    if rec.get("extra_json"):
                        try:
                            extra = json.loads(rec["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    rec.update(extra)
                    user_bugs.append(rec)
        print(f"  Fetched {len(user_bugs)} user bug(s) from KBF_BUG_REPORTS")
    except Exception as exc:
        print(f"⚠ Could not read KBF_BUG_REPORTS: {exc}", file=sys.stderr)

    # ── Fetch audit run findings ────────────────────────────────────────────
    audit_runs: list[dict] = []
    try:
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT review_id, synth_id, depth, overall_score, recommendation,
                              bugs_filed, triggered_by, report_json, run_at
                       FROM KB_SHIM.KBF_AUDIT_RUNS
                       WHERE recommendation IN ('must-fix', 'should-fix', 'minor-issues')
                       ORDER BY run_at DESC"""
                )
                cols = [d[0].lower() for d in cur.description]
                for row in cur.fetchall():
                    rec = dict(zip(cols, row))
                    # Materialise any Oracle LOB values to str
                    for k, v in rec.items():
                        if hasattr(v, "read"):
                            rec[k] = v.read()
                    if rec.get("report_json"):
                        try:
                            rec["_report"] = json.loads(rec["report_json"])
                        except (json.JSONDecodeError, TypeError):
                            rec["_report"] = {}
                    audit_runs.append(rec)
        print(f"  Fetched {len(audit_runs)} audit run(s) from KBF_AUDIT_RUNS")
    except Exception as exc:
        print(f"⚠ Could not read KBF_AUDIT_RUNS: {exc}", file=sys.stderr)

    _close_pool(pool)

    # ── Write per-bug markdown files ────────────────────────────────────────
    written_bugs: list[dict] = []

    for bug in user_bugs:
        qid = bug.get("queue_id") or bug.get("request_id", "unknown")
        safe_id = qid.replace("/", "-").replace(" ", "-")
        filename = f"{safe_id}.md"
        filepath = out_dir / filename

        ts = _fmt_ts(bug.get("timestamp_utc"))
        tool = bug.get("tool", "unknown")
        description = bug.get("description", "")
        # Oracle CLOBs come back as LOB objects — materialise to str before slicing
        if hasattr(description, "read"):
            description = description.read()
        description = str(description) if description else ""
        summary_line = description[:100] + ("…" if len(description) > 100 else "")

        input_data = bug.get("input", bug.get("triggering_input", {}))
        input_block = (
            "```json\n" + json.dumps(input_data, indent=2) + "\n```"
            if isinstance(input_data, dict) and input_data
            else (str(input_data) if input_data else "_not recorded_")
        )

        md = f"""---
queue_id: {qid}
source: user_report
tool: {tool}
filed_at: {ts}
status: open
---

# {qid}

**Tool**: `{tool}` | **Filed**: {ts[:10] if ts else "unknown"} | **Status**: open

{summary_line}

<details>
<summary>Full details</summary>

**Description**:
{description}

**Triggering input**:
{input_block}

**User ID**: {bug.get("user_id", "_anon_")}
**Request ID**: {bug.get("request_id", "_unknown_")}

</details>
"""
        filepath.write_text(md, encoding="utf-8")
        written_bugs.append({
            "filename": filename, "id": qid,
            "summary": summary_line, "filed_at": ts,
            "source": "user_report", "tool": tool,
        })
        print(f"  ✓ {filename}")

    # ── Write per-audit-run markdown files ──────────────────────────────────
    written_audits: list[dict] = []

    for run in audit_runs:
        rid = str(run.get("review_id", "unknown"))
        synth_id = run.get("synth_id", "unknown")
        filename = f"audit-{rid.replace('/', '-')}.md"
        filepath = out_dir / filename

        ts = _fmt_ts(run.get("run_at"))
        score = run.get("overall_score", "?")
        rec = run.get("recommendation", "?")
        bugs_filed = run.get("bugs_filed", 0)

        report = run.get("_report", {})
        dimensions = report.get("dimensions", [])
        dim_lines = (
            "| Dimension | Score | Verdict | Note |\n|---|---|---|---|\n"
            + "\n".join(
                f"| {d.get('name','?')} | {d.get('score','?')} | "
                f"{d.get('verdict','?')} | {(d.get('note') or '')[:80]} |"
                for d in dimensions
            )
        ) if dimensions else "_dimension data not available_"

        issues = report.get("issues", [])
        issues_block = (
            "\n".join(
                f"- **[{i.get('severity','?')}]** {i.get('description','')}"
                for i in issues[:20]
            ) if issues else "_none recorded_"
        )
        report_snippet = json.dumps(report, indent=2)[:4000]

        md = f"""---
review_id: {rid}
synth_id: {synth_id}
source: audit_run
overall_score: {score}
recommendation: {rec}
bugs_filed: {bugs_filed}
run_at: {ts}
---

# Audit: {synth_id}

**Review ID**: `{rid}` | **Score**: {score}/1.0 | **Recommendation**: {rec} | **Run**: {ts[:10] if ts else "?"}

{rec.upper()} — {bugs_filed} bug(s) filed by this review.

<details>
<summary>Dimension scores</summary>

{dim_lines}

</details>

<details>
<summary>Issues found ({len(issues)})</summary>

{issues_block}

</details>

<details>
<summary>Full JSON report</summary>

```json
{report_snippet}
```

</details>
"""
        filepath.write_text(md, encoding="utf-8")
        written_audits.append({
            "filename": filename, "id": rid,
            "synth_id": synth_id,
            "summary": f"{rec} — score {score}/1.0 — {bugs_filed} bug(s)",
            "filed_at": ts, "source": "audit_run", "recommendation": rec,
        })
        print(f"  ✓ {filename}")

    # ── Write INDEX.md ───────────────────────────────────────────────────────
    now_str = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    bug_rows = "\n".join(
        f"| [{b['id']}]({b['filename']}) | {b['tool']} | {b['summary']} | {(b['filed_at'] or '')[:10]} |"
        for b in written_bugs
    ) or "_none_"

    audit_rows = "\n".join(
        f"| [{a['id']}]({a['filename']}) | [{a['synth_id']}]({a['filename']}) | {a['recommendation']} | {(a['filed_at'] or '')[:10]} |"
        for a in written_audits
    ) or "_none_"

    index_md = f"""# KBF Bug Reports — {now_str}

> **Source of truth**: ADB (`KBF_BUG_REPORTS` + `KBF_AUDIT_RUNS`). See DECISION-008.
> Generated by `kb-cli export-bugs`. Re-run to refresh. **Do not edit these files manually.**

## User-Reported Bugs ({len(written_bugs)})

| ID | Tool | Description | Filed |
|---|---|---|---|
{bug_rows}

## Audit Findings — must-fix / should-fix / minor-issues ({len(written_audits)})

| Review ID | Session | Recommendation | Run |
|---|---|---|---|
{audit_rows}
"""
    (out_dir / "INDEX.md").write_text(index_md, encoding="utf-8")
    print(f"  ✓ INDEX.md")
    print(f"\n✓ export-bugs — {len(written_bugs)} user bug(s) + {len(written_audits)} audit run(s) → {out_dir}/")
    return 0


def _fmt_ts(ts_value) -> str:
    """Format a timestamp value (datetime object or ISO string) to an ISO string."""
    if ts_value is None:
        return ""
    if hasattr(ts_value, "isoformat"):
        return ts_value.isoformat()
    return str(ts_value)


# ============================================================================
# setup-bug-user — create KBF_BUGS Oracle user and grant bug-table access
# ============================================================================

def cmd_setup_bug_user(args):
    """Create the KBF_BUGS Oracle user and grant it access to bug tables.

    Connects using the admin pool (from the adb config section), resolves the
    KBF_BUGS password from bug_db.password_secret, then:

      1. Creates KBF_BUGS (idempotent — ORA-01920 suppressed).
      2. Grants CREATE SESSION + INSERT/SELECT on bug tables.

    Run once per environment after setting up the schema (migration-005/006).
    Subsequent runs are safe (GRANTs are idempotent in Oracle).

    Usage:
        export KBF_BUGS_PASSWORD=<password>
        kb-cli setup-bug-user --env laptop
    """
    env = getattr(args, "env", "") or os.environ.get("KBF_ENV", "laptop")
    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml"
    if not config_path.exists():
        print(f"❌ Config not found: {config_path}", file=sys.stderr)
        return 1

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    adb_cfg = cfg.get("adb", {})
    bug_db_cfg = cfg.get("bug_db", {})

    if not bug_db_cfg:
        print(
            f"❌ No 'bug_db' section found in {config_path.name}. "
            "Add it first (DECISION-009).",
            file=sys.stderr,
        )
        return 1

    # Resolve KBF_BUGS password from bug_db.password_secret
    try:
        bugs_password = _resolve_secret_cli(bug_db_cfg.get("password_secret", ""))
    except RuntimeError as exc:
        print(f"❌ Cannot resolve KBF_BUGS password: {exc}", file=sys.stderr)
        return 1

    if not bugs_password:
        print(
            "❌ KBF_BUGS password is empty. "
            "Set the env var referenced by bug_db.password_secret.",
            file=sys.stderr,
        )
        return 1

    bugs_user = bug_db_cfg.get("user", "KBF_BUGS")

    # Build admin pool (same as cmd_migrate: uses adb admin credentials)
    try:
        from ..core.adb_pool import create_adb_pool  # type: ignore[import]

        wallet_path = str(Path(adb_cfg.get("wallet_path", "")).expanduser())
        wallet_password = _resolve_secret_cli(adb_cfg.get("wallet_password_secret", ""))
        admin_user = adb_cfg.get("admin_user", "Admin")
        admin_password = _resolve_secret_cli(adb_cfg.get("admin_password_secret", ""))
        service_name = adb_cfg.get("dsn") or adb_cfg.get("service_name", "")

        pool = create_adb_pool({
            "deployment_mode": cfg.get("deployment_mode", "laptop"),
            "adb": {
                "service_name":    service_name,
                "wallet_path":     wallet_path,
                "user":            admin_user,
                "password":        admin_password,
                "wallet_password": wallet_password,
            },
            "bastion": cfg.get("bastion", {}),
        })
    except Exception as exc:
        print(f"❌ Failed to create admin ADB pool: {exc}", file=sys.stderr)
        return 1

    print(f"▶ setup-bug-user: creating {bugs_user} on {service_name} (env={env})")

    try:
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                # 1. Create user — idempotent (ORA-01920 = user already exists)
                create_stmt = (
                    f'BEGIN\n'
                    f'  EXECUTE IMMEDIATE \'CREATE USER {bugs_user} '
                    f'IDENTIFIED BY "{bugs_password}"\';\n'
                    f'EXCEPTION\n'
                    f'  WHEN OTHERS THEN\n'
                    f'    IF SQLCODE = -1920 THEN NULL;  -- user already exists\n'
                    f'    ELSE RAISE;\n'
                    f'    END IF;\n'
                    f'END;'
                )
                cur.execute(create_stmt)
                print(f"  ✓ CREATE USER {bugs_user} (or already exists)")

                # 2. GRANT CREATE SESSION
                cur.execute(f"GRANT CREATE SESSION TO {bugs_user}")
                print(f"  ✓ GRANT CREATE SESSION TO {bugs_user}")

                # 3. GRANT on bug tables
                cur.execute(
                    f"GRANT INSERT, SELECT ON KB_SHIM.KBF_BUG_REPORTS TO {bugs_user}"
                )
                print(f"  ✓ GRANT INSERT, SELECT ON KB_SHIM.KBF_BUG_REPORTS TO {bugs_user}")

                cur.execute(
                    f"GRANT INSERT, SELECT ON KB_SHIM.KBF_AUDIT_RUNS TO {bugs_user}"
                )
                print(f"  ✓ GRANT INSERT, SELECT ON KB_SHIM.KBF_AUDIT_RUNS TO {bugs_user}")

            conn.commit()

    except Exception as exc:
        print(f"❌ setup-bug-user failed: {exc}", file=sys.stderr)
        _close_pool(pool)
        return 1

    _close_pool(pool)
    print(f"✓ setup-bug-user complete — {bugs_user} is ready (DECISION-009)")
    return 0


# ============================================================================
# watch-bugs — fast local error watcher (reads JSONL hot-cache, not ADB)
# For a full queryable view of all bugs use: kb-cli export-bugs
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

    # Index user_bugs by request_id for fast join.
    # A bug is "filed" (known) when it has a queue_id in user_bugs.jsonl —
    # that means it was submitted via reportBug and written to KBF_BUG_REPORTS.
    # We no longer check pmo/bugs/*.md files (DECISION-008: ADB is the source
    # of truth; those files are generated exports, not primary records).
    bugs_by_request_id: dict[str, dict] = {
        bug["request_id"]: bug for bug in user_bugs if bug.get("request_id")
    }
    filed_request_ids: set[str] = {
        bug["request_id"] for bug in user_bugs
        if bug.get("request_id") and bug.get("queue_id")
    }

    # Group errors by (error_type, message[:80]) for fuzzy dedup
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for err in errors:
        key = (err.get("error_type", ""), (err.get("message") or "")[:80])
        groups[key].append(err)

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
            queue_id = user_report.get("queue_id", "")
            report_str = f'YES ({queue_id}) — "{desc}"' if queue_id else f'YES — "{desc}"'

        # "Known" = this request_id was already user-filed (has a queue_id in KBF_BUG_REPORTS)
        is_known = rid in filed_request_ids
        known_label = f"FILED ({user_report.get('queue_id','')})" if is_known else ""

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
    print(f"{new_candidates} new candidate(s), {known_count} already filed, {len(errors)} total errors")
    print("Tip: run `kb-cli export-bugs` for a full queryable view from ADB (DECISION-008)")
    return 0


# ============================================================================
# backfill-skills-to-adb
# ============================================================================
def cmd_backfill_skills(args):
    """One-time backfill of on-disk skills into KBF_SKILL_ARTIFACTS (ADB).

    Scans framework/workflow_skills/{persona}/{skill_name}.yaml and writes each
    skill's artifacts to ADB via AdbSkillStore.write_artifacts().  Skills that
    already exist in ADB are overwritten (MERGE INTO is idempotent).

    Artifact coverage per skill:
      workflow_skill    — framework/workflow_skills/{persona}/{skill_name}.yaml
      eval_extraction   — eval/gold_sets/{persona}-{skill_name}-extraction.jsonl
      eval_workflow     — eval/gold_sets/{persona}-{skill_name}-workflow.jsonl
      (persona_builder_delta skipped for legacy skills — no reliable per-skill delta on disk)

    Usage:
      kb-cli backfill-skills-to-adb --env laptop
      kb-cli backfill-skills-to-adb --env laptop --persona tpm --dry-run
    """
    env = args.env or os.environ.get("KBF_ENV", "laptop")
    persona_filter = getattr(args, "persona", None) or ""
    dry_run = getattr(args, "dry_run", False)

    print(f"▶ backfill-skills-to-adb  env={env}  persona={persona_filter or '(all)'}  dry_run={dry_run}")

    # ── Discover on-disk skills ────────────────────────────────────────────
    skills_dir = REPO_ROOT / "framework" / "workflow_skills"
    if not skills_dir.exists():
        print(f"❌ workflow_skills dir not found: {skills_dir}", file=sys.stderr)
        return 1

    if persona_filter:
        search_dirs = [skills_dir / persona_filter]
    else:
        search_dirs = [d for d in sorted(skills_dir.iterdir()) if d.is_dir()]

    _REL_TEMPLATES = {
        "workflow_skill":  "framework/workflow_skills/{persona}/{skill_name}.yaml",
        "eval_extraction": "eval/gold_sets/{persona}-{skill_name}-extraction.jsonl",
        "eval_workflow":   "eval/gold_sets/{persona}-{skill_name}-workflow.jsonl",
    }

    skills: list[tuple[str, str, dict[str, str]]] = []  # (persona, skill_name, artifacts)
    for persona_dir in search_dirs:
        if not persona_dir.is_dir():
            continue
        p_name = persona_dir.name
        for skill_file in sorted(persona_dir.glob("*.yaml")):
            if skill_file.name.startswith("_"):
                continue
            skill_n = skill_file.stem
            typed: dict[str, str] = {}
            for artifact_type, tmpl in _REL_TEMPLATES.items():
                path = REPO_ROOT / tmpl.format(persona=p_name, skill_name=skill_n)
                if path.exists():
                    try:
                        typed[artifact_type] = path.read_text(encoding="utf-8")
                    except OSError as exc:
                        print(f"  ⚠ could not read {path.name}: {exc}")
            if typed:
                skills.append((p_name, skill_n, typed))

    if not skills:
        print("  No skills found on disk — nothing to backfill.")
        return 0

    print(f"  Found {len(skills)} skill(s) on disk:")
    for p, s, arts in skills:
        print(f"    {p}.{s}  ({', '.join(sorted(arts.keys()))})")

    if dry_run:
        print("\n  [dry-run] No changes written to ADB.")
        return 0

    # ── Build ADB pool (same logic as cmd_migrate) ────────────────────────
    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml"
    if not config_path.exists():
        print(f"❌ config not found: {config_path}", file=sys.stderr)
        return 1

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    adb_cfg = cfg.get("adb", {})
    bastion_cfg_dict = cfg.get("bastion", {})

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
                "service_name":    service_name,
                "wallet_path":     wallet_path,
                "user":            admin_user,
                "password":        admin_password,
                "wallet_password": wallet_password,
            },
            "bastion": bastion_cfg_dict,
        }

        print(f"\n  Connecting to ADB ({service_name}) …")
        pool = create_adb_pool(pool_dict)
        print("  ✓ ADB pool ready")

    except Exception as exc:
        print(f"❌ Failed to create ADB pool: {exc}", file=sys.stderr)
        return 1

    # ── Write to ADB ──────────────────────────────────────────────────────
    from ..deploy.skill_store.adb import AdbSkillStore

    store = AdbSkillStore(pool)

    ok = 0
    failed = 0
    for persona, skill_name, typed_artifacts in skills:
        try:
            store.write_artifacts(
                synth_id="backfill",
                persona=persona,
                skill_name=skill_name,
                artifacts=typed_artifacts,
            )
            print(f"  ✓ {persona}.{skill_name}")
            ok += 1
        except Exception as exc:
            print(f"  ✗ {persona}.{skill_name}: {exc}", file=sys.stderr)
            failed += 1

    print(f"\n  Backfill complete: {ok} written, {failed} failed.")
    return 0 if failed == 0 else 1


# ============================================================================
# session recover — operator tool for stuck sessions (BUG-queue-573e3)
# ============================================================================

# Per-state fields that must be cleared / initialized when the operator
# manually advances a session to a new state.  Only states that need
# special treatment are listed; others need no field resets.
_STATE_RESET_FIELDS: dict[str, dict] = {
    # PREVIEW_EXTRACTION re-derives extraction_preview from source_samples
    # via review_extractions().  Clearing it forces a fresh derivation on
    # the next advance rather than replaying stale (possibly corrupt) data.
    "PREVIEW_EXTRACTION": {
        "extraction_preview": None,
    },
    # CONFIRM re-derives synthesized_artifacts from the design.  Leave them
    # in place so the operator sees what was already designed; the session
    # can move forward from there.
}

# Steps that were already completed at the state the session is stuck at.
# Used to print the "you are bypassing these steps" warning.
_BYPASSED_STEPS: dict[str, list[str]] = {
    "PREVIEW_EXTRACTION": [
        "CONFIGURE_TRIGGERS — operator must ensure trigger + output_format are "
        "already set correctly in the session data (they were for synth-tpm-9571f396).",
    ],
}


def _build_adb_pool_from_env(env: str):
    """Build an oracledb pool for the given env config. Returns pool or raises."""
    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    adb_cfg = cfg.get("adb", {})
    from ..core.adb_pool import create_adb_pool  # type: ignore[import]

    wallet_path = str(Path(adb_cfg.get("wallet_path", "")).expanduser())
    wallet_password = _resolve_secret_cli(adb_cfg.get("wallet_password_secret", ""))
    admin_user = adb_cfg.get("admin_user", "Admin")
    admin_password = _resolve_secret_cli(adb_cfg.get("admin_password_secret", ""))
    service_name = adb_cfg.get("dsn") or adb_cfg.get("service_name", "")

    pool = create_adb_pool({
        "deployment_mode": cfg.get("deployment_mode", "laptop"),
        "adb": {
            "service_name":    service_name,
            "wallet_path":     wallet_path,
            "user":            admin_user,
            "password":        admin_password,
            "wallet_password": wallet_password,
        },
        "bastion": cfg.get("bastion", {}),
    })
    return pool, service_name


def cmd_session_recover(args):
    """Recover a stuck authorSkill session to a valid state.

    This is an operator-only escape hatch.  It does NOT fabricate skill
    artifacts and does NOT silently skip required work.  The command prints
    a clear warning of what is being bypassed and requires --confirm to write.

    Usage:
        kb-cli session recover --synth-id <id> --to-state <STATE> \\
            [--env laptop] [--confirm]

    Example (BUG-queue-573e3 fix):
        kb-cli session recover \\
            --synth-id synth-tpm-9571f396 \\
            --to-state PREVIEW_EXTRACTION \\
            --env laptop \\
            --confirm
    """
    from ..skill_builder.conversation import STATES

    synth_id = args.synth_id
    to_state = args.to_state
    env = args.env or os.environ.get("KBF_ENV", "laptop")
    do_confirm = args.confirm

    # --- Validate target state is a real member of the ADR-027 STATES list ---
    if to_state not in STATES:
        print(
            f"ERROR: '{to_state}' is not a valid ADR-027 state.\n"
            f"Valid states: {', '.join(STATES)}",
            file=sys.stderr,
        )
        return 1

    # --- Load the session from ADB ----------------------------------------
    print(f"Connecting to ADB (env={env}) ...")
    try:
        pool, service_name = _build_adb_pool_from_env(env)
        print(f"  Connected: {service_name}")
    except Exception as exc:
        print(f"ERROR: Could not connect to ADB: {exc}", file=sys.stderr)
        return 1

    # Direct SQL lookup — bypasses user_id ownership check so the operator
    # can recover any session without knowing the user_id.
    import json as _json
    session: dict | None = None
    user_id_in_db: str = ""
    try:
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT synth_id, user_id, state, status, session_data "
                    "FROM kb_shim.author_skill_sessions "
                    "WHERE synth_id = :synth_id",
                    {"synth_id": synth_id},
                )
                cols = [d[0].lower() for d in cur.description]
                cur.rowfactory = lambda *vals: dict(zip(cols, vals))
                row = cur.fetchone()
    except Exception as exc:
        print(f"ERROR: ADB query failed: {exc}", file=sys.stderr)
        _close_pool(pool)
        return 1

    if row is None:
        print(f"ERROR: Session '{synth_id}' not found in ADB.", file=sys.stderr)
        _close_pool(pool)
        return 1

    user_id_in_db = row["user_id"] or ""
    current_state = row["state"] or ""
    current_status = row["status"] or ""
    raw_data = row["session_data"]
    session = raw_data if isinstance(raw_data, dict) else _json.loads(raw_data)

    print(f"\nFound session:")
    print(f"  synth_id    : {synth_id}")
    print(f"  user_id     : {user_id_in_db}")
    print(f"  current state: {current_state}  (status={current_status})")
    print(f"  target state : {to_state}")

    session_has_last_turn = "last_turn" in session
    if current_state == to_state:
        if not session_has_last_turn:
            print(f"\nSession is already at {to_state} and has no stale last_turn. No change needed.")
            _close_pool(pool)
            return 0
        # State already correct but last_turn may be stale from before the recovery.
        # Clearing it ensures GET returns a fresh envelope derived from the new state.
        print(f"\nSession is already at {to_state} but has a stale last_turn — clearing it.")
        if not do_confirm:
            print("Dry-run: would clear last_turn. Re-run with --confirm to apply.")
            _close_pool(pool)
            return 0
        session.pop("last_turn", None)
        session["status"] = "in_progress"
        from ..deploy.session.adb_store import AdbSessionStore
        store = AdbSessionStore(pool=pool)
        try:
            store.save(session, user_id=user_id_in_db)
        except Exception as exc:
            print(f"ERROR: Failed to save session: {exc}", file=sys.stderr)
            _close_pool(pool)
            return 1
        _close_pool(pool)
        print(f"  Cleared stale last_turn from session '{synth_id}' (state remains {to_state})")
        return 0

    # --- Print bypass warning ---------------------------------------------
    bypassed = _BYPASSED_STEPS.get(to_state, [])
    resets = _STATE_RESET_FIELDS.get(to_state, {})

    print()
    print("WARNING: This operator recovery will:")
    print(f"  - Force state from {current_state!r} -> {to_state!r}")
    if bypassed:
        print("  - Bypass the following steps/checks:")
        for b in bypassed:
            print(f"      * {b}")
    if resets:
        print(f"  - Clear/initialize these per-state cached fields: {list(resets.keys())}")
    print("  - This action MUST NOT be used to fabricate skill artifacts.")
    print("  - Existing session data (fields, design, sources) is preserved.")

    if not do_confirm:
        print(
            "\nDry-run complete (no changes written). "
            "Re-run with --confirm to apply.",
            file=sys.stderr,
        )
        _close_pool(pool)
        return 0

    # --- Apply the state change -------------------------------------------
    session["state"] = to_state

    # Apply per-state field resets
    for field, value in resets.items():
        session[field] = value

    # Clear last_turn so the GET endpoint derives a fresh envelope from the new
    # state rather than serving the stale CONFIGURE_TRIGGERS turn message.
    session.pop("last_turn", None)

    # Keep status in_progress (session is not committed/abandoned)
    session["status"] = "in_progress"

    # Persist back via AdbSessionStore (which handles CLOB binding correctly)
    from ..deploy.session.adb_store import AdbSessionStore
    store = AdbSessionStore(pool=pool)
    try:
        store.save(session, user_id=user_id_in_db)
    except Exception as exc:
        print(f"ERROR: Failed to save session: {exc}", file=sys.stderr)
        _close_pool(pool)
        return 1

    _close_pool(pool)

    print(f"\nSession '{synth_id}' recovered:")
    print(f"  {current_state} -> {to_state}")
    print(f"  status: in_progress")
    print()
    print("Verify with:")
    print(f"  curl -s -H 'Authorization: Bearer dev-only-token-replace-me' \\")
    print(f"    http://localhost:8080/api/v1/kb/authorSkill/{synth_id}")
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

    pi = sub.add_parser("ingest", help="ingest Confluence pages from a persona-builder config")
    pi.add_argument("config")
    pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--sample", type=int, default=5)
    # A3 (BUG-queue-990fe): --persona forwards persona to ConfluenceWikiIngestor
    # so wiki_metadata records never store null persona when persona is known.
    pi.add_argument(
        "--persona",
        default=None,
        help="owning persona (e.g. tpm). Overrides config YAML. Required if 'persona:' "
             "is absent from the config. Never silently stores null.",
    )
    pi.set_defaults(fn=cmd_ingest)

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

    pes = sub.add_parser(
        "export-skills",
        help="export skill artifacts from ADB (KBF_SKILL_ARTIFACTS) to the filesystem",
    )
    pes.add_argument("--persona", default=None, help="filter by persona (e.g. tpm)")
    pes.add_argument("--status", default=None, help="filter by status (e.g. promoted)")
    pes.add_argument("--out-dir", default=".", help="output root directory (default: .)")
    pes.add_argument("--env", default=None, help="config env name (overrides KBF_ENV)")
    pes.set_defaults(fn=cmd_export_skills)

    peb = sub.add_parser(
        "export-bugs",
        help="export bug records from ADB (KBF_BUG_REPORTS + KBF_AUDIT_RUNS) to markdown",
    )
    peb.add_argument(
        "--out-dir",
        default=None,
        help="output directory for .md files (default: pmo/bugs/ under REPO_ROOT)",
    )
    peb.add_argument(
        "--env",
        default=None,
        help="config env name (overrides KBF_ENV, default: laptop)",
    )
    peb.set_defaults(fn=cmd_export_bugs)

    psbu = sub.add_parser(
        "setup-bug-user",
        help="create KBF_BUGS Oracle user and grant bug-table access (DECISION-009)",
    )
    psbu.add_argument(
        "--env",
        default=None,
        help="config env name (overrides KBF_ENV)",
    )
    psbu.set_defaults(fn=cmd_setup_bug_user)

    pbf = sub.add_parser(
        "backfill-skills-to-adb",
        help="one-time backfill of on-disk workflow_skills into KBF_SKILL_ARTIFACTS (ADB)",
    )
    pbf.add_argument("--env", default=None, help="config env name (overrides KBF_ENV, default: laptop)")
    pbf.add_argument("--persona", default=None, help="backfill only this persona (e.g. tpm)")
    pbf.add_argument("--dry-run", action="store_true", help="discover skills but don't write to ADB")
    pbf.set_defaults(fn=cmd_backfill_skills)

    # ── session subcommand group ──────────────────────────────────────────
    p_session = sub.add_parser(
        "session",
        help="operator tools for authorSkill sessions",
    )
    session_sub = p_session.add_subparsers(dest="session_cmd", required=True)

    p_recover = session_sub.add_parser(
        "recover",
        help="force a stuck session to a valid state (operator only — BUG-queue-573e3)",
    )
    p_recover.add_argument(
        "--synth-id",
        required=True,
        help="authorSkill session ID to recover (e.g. synth-tpm-9571f396)",
    )
    p_recover.add_argument(
        "--to-state",
        required=True,
        help="target ADR-027 state to force the session into (e.g. PREVIEW_EXTRACTION)",
    )
    p_recover.add_argument(
        "--env",
        default=None,
        help="config env name (overrides KBF_ENV, default: laptop)",
    )
    p_recover.add_argument(
        "--confirm",
        action="store_true",
        help="actually write the change (omit for dry-run)",
    )
    p_recover.set_defaults(fn=cmd_session_recover)
    p_session.set_defaults(fn=lambda a: (session_sub.print_help() or 1))

    # ── wiki-meta subcommand group (A4 BUG-queue-990fe) ──────────────────
    p_wm = sub.add_parser(
        "wiki-meta",
        help="operator tools for wiki_metadata store (BUG-queue-990fe)",
    )
    wm_sub = p_wm.add_subparsers(dest="wiki_meta_cmd", required=True)

    p_bp = wm_sub.add_parser(
        "backfill-persona",
        help="set persona on null-persona wiki_metadata records (idempotent, A4)",
    )
    p_bp.add_argument(
        "--persona",
        required=True,
        help="persona to assign to null-persona records (e.g. tpm)",
    )
    p_bp.add_argument(
        "--page-id",
        default=None,
        dest="page_id",
        help="target a single page by page_id; omit to target all null-persona records",
    )
    p_bp.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be changed without writing",
    )
    p_bp.set_defaults(fn=cmd_wiki_meta_backfill_persona)
    p_wm.set_defaults(fn=lambda a: (wm_sub.print_help() or 1))

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
