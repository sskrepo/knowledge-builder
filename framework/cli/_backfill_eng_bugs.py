"""One-shot backfill: file the 2026-05-16 hardening session proactive defects into ADB.

DECISION-013: agent/architect-discovered defects are filed into KBF_BUG_REPORTS
via record_user_bug() with discovered_by in extra_json.

Usage (from repo root, with ADB tunnel up):
    export KBF_BUGS_PASSWORD=<pw>
    export WALLET_PASSWORD=<pw>
    python -m framework.cli._backfill_eng_bugs [--env laptop] [--dry-run]

The script is idempotent in the sense that it prints each queue_id as it files it;
re-running will INSERT duplicates into ADB (Oracle has no UPSERT on queue_id).
Do NOT re-run after a successful backfill.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _resolve_secret(ref: str) -> str:
    if ref and ref.startswith("env://"):
        var = ref[6:]
        val = os.environ.get(var, "")
        if not val:
            raise RuntimeError(f"Secret env var not set: {var}")
        return val
    return ref or ""


def _build_pool(env: str):
    import yaml
    from framework.core.adb_pool import create_adb_pool  # type: ignore[import]

    config_path = REPO_ROOT / "framework" / "config" / f"{env}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    adb_cfg = cfg.get("adb", {})
    bug_db_cfg = cfg.get("bug_db", {})

    # Mirror exact logic from cmd_export_bugs / cmd_setup_bug_user
    service_name = (
        bug_db_cfg.get("dsn")
        or bug_db_cfg.get("service_name")
        or adb_cfg.get("dsn")
        or adb_cfg.get("service_name", "")
    )
    wallet_path = str(
        Path(bug_db_cfg.get("wallet_path") or adb_cfg.get("wallet_path", "")).expanduser()
    )
    wallet_password = _resolve_secret(
        bug_db_cfg.get("wallet_password_secret") or adb_cfg.get("wallet_password_secret", "")
    )
    user = bug_db_cfg.get("user") or adb_cfg.get("admin_user", "Admin")
    password = _resolve_secret(
        bug_db_cfg.get("password_secret") or adb_cfg.get("admin_password_secret", "")
    )

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
    return pool


# ---------------------------------------------------------------------------
# Defect definitions
# All timestamps are the commit author date (ISO 8601, -0700 local = PDT).
# queue_ids are pre-generated and stable — do not regenerate on re-run.
# ---------------------------------------------------------------------------

DEFECTS = [
    # 1. shim_workflows status gate
    {
        "request_id":    "arch-rca-8c2bec1",
        "queue_id":      "BUG-queue-6510d",  # stable — do not regenerate
        "timestamp":     "2026-05-16T12:41:32-07:00",
        "tool":          "askKnowledgeBase",
        "description": (
            "Architect-RCA companion to user report BUG-queue-2ad9a. "
            "shim_workflows had no status gate: all_cards() returned ALL on-disk cards "
            "including drafts, so the Tier-1 LLM router received draft+promoted skills "
            "indistinguishably. A promoted .eml skill silently returned a .pptx artifact "
            "(wrong-output silent substitution). Fix: ShimWorkflows made ADB-aware "
            "(mirrors ShimKb/ADR-015 Option B); all_cards() now filters to "
            "list_promoted_workflow_skills() from ADB. Disk YAML is authoring-only."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "8c2bec1",
        "severity":      "HIGH",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "ShimWorkflows was disk-only; all_cards() returned ALL on-disk cards including "
            "drafts. Tier-1 LLM classifier received draft+promoted skills "
            "indistinguishably and picked the wrong artifact type (.pptx instead of .eml). "
            "Per commit message and diff: added ADB-aware filtering via "
            "list_promoted_workflow_skills(); disk YAML is now authoring artifact only. "
            "RCA of user-reported BUG-queue-2ad9a."
        ),
    },
    # 2. persona=null ingestion + space-form regex gap (two commits, one logical defect)
    {
        "request_id":    "arch-rca-280451a",
        "queue_id":      "BUG-queue-e9eda",  # stable
        "timestamp":     "2026-05-16T15:31:57-07:00",
        "tool":          "authorSkill",
        "description": (
            "Architect-RCA companion to user report BUG-queue-990fe. "
            "Two root causes: (RC1) ConfluenceWikiIngestor had no persona param so "
            "pages stored with persona=null, losing persona association downstream. "
            "(RC2) _CONFLUENCE_PAGE_REF_PATTERNS missing the natural-language "
            "'pageId 18625350641' form (no '=') so the hard-fail guard was bypassed "
            "and a different ingested page was silently substituted. "
            "Fix commits: 280451a (RC1+RC2+A3+A4) + 8c947dc (P3 mismatch hard-fail)."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "280451a",
        "fix_commit_cluster": ["280451a", "8c947dc"],
        "severity":      "HIGH",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "RC1: ConfluenceWikiIngestor had no persona constructor param; ingested pages "
            "stored with persona=null, breaking persona-scoped retrieval. "
            "RC2: _CONFLUENCE_PAGE_REF_PATTERNS missing space-form 'pageId NNNN' pattern; "
            "P3 guard bypassed, different ingested page silently substituted. "
            "8c947dc added hard-fail on page-mismatch (executor guard). "
            "Per commit messages + diffs confirming both patterns and propagation fixes."
        ),
    },
    # 3. deleteSkill blocking ADB calls on event loop
    {
        "request_id":    "arch-rca-322d946",
        "queue_id":      "BUG-queue-5e368",  # stable
        "timestamp":     "2026-05-16T20:39:39-07:00",
        "tool":          "deleteSkill",
        "description": (
            "Part 2 of BUG-queue-280f1 (Part 1 was NOT a defect — server was down "
            "during teardown). deleteSkill handler declared async but ran three "
            "synchronous blocking ADB I/O calls (delete, delete_persona_builder_kb, "
            "shim_kb.reload) directly on the asyncio event loop. Under bastion/ADB "
            "reconnect, these freeze the event loop and uvicorn kills the unresponsive "
            "worker. Same d3ec0-class latent blocking that 309db5d fixed for authorSkill "
            "but was missed for deleteSkill. Fix: offload via asyncio.to_thread."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "322d946",
        "severity":      "Medium",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "_make_delete_skill_handler declared async def but called three synchronous "
            "blocking ADB I/O calls directly on the event loop. Per commit diff: "
            "collected into _do_delete_blocking() + offloaded via asyncio.to_thread, "
            "matching the authorSkill pattern from 309db5d. Found while investigating "
            "BUG-queue-280f1 Part 1 (which was server-down during teardown, not a code defect)."
        ),
    },
    # 4. Arbitrary content/maxLength/token caps (cluster: bf6dfab + f3baf51 + e6b0a65 + ec84c0d)
    {
        "request_id":    "arch-rca-bf6dfab",
        "queue_id":      "BUG-queue-049a6",  # stable
        "timestamp":     "2026-05-16T13:28:36-07:00",
        "tool":          "authorSkill",
        "description": (
            "Architect-RCA companion to user report BUG-queue-44364. "
            "Arbitrary app-layer content caps silently truncated extraction output: "
            "max_tokens ceilings (e.g. 4096) on extraction/design prompts caused "
            "20–32 field RODS/26ai schemas to render as blank placeholders. "
            "Additionally: arbitrary maxLength in synthesized schema defaults and "
            "char-level source-text caps clipped LLM input before extraction. "
            "All were silent truncations — ADB/CLOB storage is unbounded; the caps "
            "were vestigial app-layer constraints. "
            "Fix cluster: bf6dfab (max_tokens raised), f3baf51 (maxLength removed), "
            "e6b0a65 (source-text caps raised), ec84c0d (LLM-JSON parse guards)."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "bf6dfab",
        "fix_commit_cluster": ["bf6dfab", "f3baf51", "e6b0a65", "ec84c0d"],
        "severity":      "HIGH",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "Three layers of arbitrary caps silently truncated extraction: "
            "(1) max_tokens 4096 on extraction/design prompts — real schemas hit the ceiling; "
            "(2) maxLength in synthesized JSON schema defaults clipped field values; "
            "(3) char-level source-text caps clipped LLM input. "
            "Per errors.jsonl: a 20-field RODS schema hit tokens_out=4096; 32-field 26ai worse. "
            "ADB/CLOB storage is unbounded — all caps were vestigial app-layer constraints. "
            "bf6dfab (stop-bleed), f3baf51 (schema maxLength), e6b0a65 (source caps), "
            "ec84c0d (parse guards). ADR-031 documents the comprehensive fix."
        ),
    },
    # 5. maybe_render_artifact called without body= on MCP ask path
    {
        "request_id":    "arch-rca-fd18916",
        "queue_id":      "BUG-queue-885bc",  # stable
        "timestamp":     "2026-05-16T18:44:14-07:00",
        "tool":          "askKnowledgeBase",
        "description": (
            "D1 Priority-1 (ask-time ingestion) was structurally dead for all MCP "
            "consumers. _make_ask_handler in mcp_tools.py called maybe_render_artifact "
            "with no body= kwarg; the D1 Priority-1 branch was unreachable. MCP callers "
            "had no structured page_id parameter and relied exclusively on Priority-2 "
            "question-string regex. Surfaced while investigating req-7d351fb1 — that "
            "session itself was NOT a live defect (killed stub + stale pre-fix artifact); "
            "the structural dead-branch was the real finding. Fix: ask_handler gains "
            "page_id param; body= threaded into maybe_render_artifact."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "fd18916",
        "severity":      "Medium",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "_make_ask_handler called maybe_render_artifact(app.state, result, question) "
            "with no body= kwarg. The D1 Priority-1 branch "
            "(`if body and input_param in body`) was structurally dead for all MCP consumers. "
            "Per commit diff: ask_handler gains page_id: str = '' + body dict + "
            "maybe_render_artifact(..., body=body). req-7d351fb1 investigation context: "
            "that request hit a killed stub + stale pre-fix artifact, not the dead-branch defect."
        ),
    },
    # 6. reviewSkillSession _check_kb_references_resolve iterated dict keys not name values
    {
        "request_id":    "agent-rca-0f0214f",
        "queue_id":      "BUG-queue-98734",  # stable
        "timestamp":     "2026-05-16T18:35:49-07:00",
        "tool":          "reviewSkillSession",
        "description": (
            "_check_kb_references_resolve iterated top-level dict KEYS of the "
            "persona_builder_delta artifact (name/kind/extraction_schema/...) instead of "
            "the knowledge_bases[].name VALUE. The known-KB set was always wrong, causing "
            "every correctly-authored skill to file a spurious 'major: hallucinated KB "
            "reference' finding (false-positive). Found investigating reported "
            "synth-tpm-fe0f9e9f (review score 9.1 with 3 false major findings)."
        ),
        "discovered_by": "agent",
        "status":        "fixed",
        "fix_commit":    "0f0214f",
        "severity":      "Medium",
        "session":       "synth-tpm-fe0f9e9f investigation / 2026-05-16 hardening",
        "root_cause": (
            "Production artifact-dict shape has keys name/kind/extraction_schema/... — "
            "not KB names — so iterating top-level keys built the wrong known-KB set. "
            "Every correctly-authored skill filed a spurious 'major: hallucinated KB reference' "
            "bug (false-positive in QA tooling). Fix: detect production artifact-dict shape, "
            "add pb_doc['name'] + qualified form; also load persona_builders/{persona}.yaml "
            "for reused KBs absent from the delta. Per commit diff A1-A4."
        ),
    },
    # 7. ContextBuilder tier-1 synthesis no-answer for empty passages + stale artifact
    {
        "request_id":    "agent-rca-0995189",
        "queue_id":      "BUG-queue-a0a9a",  # stable
        "timestamp":     "2026-05-16T20:02:09-07:00",
        "tool":          "askKnowledgeBase",
        "description": (
            "For ask_parameterized/ephemeral skills, ContextBuilder tier-1 synthesis ran "
            "with NO passages (the page is fetched separately in the executor chain). "
            "Synthesizer emitted the '(no relevant context found)' sentinel even though "
            "the executor produced a complete correct artifact. Response lied: "
            "answer='(no relevant context found)' + citations=[] alongside a valid "
            "artifact_path. maybe_render_artifact never backfilled answer/citations. "
            "Traced from user-pasted response (req-7d351fb1-class confusion)."
        ),
        "discovered_by": "agent",
        "status":        "fixed",
        "fix_commit":    "0995189",
        "severity":      "HIGH",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "ContextBuilder.answer() ran tier-1 passage synthesis BEFORE maybe_render_artifact. "
            "For ask_parameterized skills with ephemeral fetch, there are no ingested passages "
            "for tier-1 to use, so it emitted the no-answer sentinel. maybe_render_artifact "
            "never backfilled result['answer']/citations from the executor's rendered_data. "
            "Fix: after successful artifact render, when upstream answer is empty or the "
            "sentinel, backfill answer+citations from rendered_data. Per commit diff."
        ),
    },
    # 8. ops _run_llm_review leaked OCI content-filter provider internals
    {
        "request_id":    "arch-rca-d59bbda",
        "queue_id":      "BUG-queue-7074b",  # stable
        "timestamp":     "2026-05-16T20:21:33-07:00",
        "tool":          "reviewSkillSession",
        "description": (
            "The broad 'except Exception as exc' in _run_llm_review embedded raw OCI error "
            "details (opc-request-id, endpoint, status) directly into the persisted "
            "BugToFile.detail, and misclassified a provider content-safety block as a skill "
            "defect (llm_review_failed: minor). Fix: detect content-filter error, emit "
            "advisory check (llm_review_content_filtered) with KBF- correlation ID only "
            "and no provider internals."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "d59bbda",
        "severity":      "Medium",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "Broad except Exception in _run_llm_review passed raw exc (containing "
            "opc-request-id, OCI endpoint, status code) directly into BugToFile.detail, "
            "and used the generic llm_review_failed check_name — misclassifying a "
            "provider content-safety block as a skill quality defect. "
            "Fix: import shared _is_content_filter_error + ContentFilterRejection; "
            "add content-filter branch before generic fallback with advisory severity, "
            "check_name=llm_review_content_filtered, KBF- ID only, zero provider internals. "
            "Per commit diff and ADR-023 amendment."
        ),
    },
    # 9. ADR-032 space_allow_list hardcoded [FA, PROJ] instead of OCIFACP
    {
        "request_id":    "agent-rca-9b6cc1f",
        "queue_id":      "BUG-queue-ded96",  # stable
        "timestamp":     "2026-05-16T16:21:24-07:00",
        "tool":          "askKnowledgeBase",
        "description": (
            "ADR-032 P1-E set space_allow_list: [FA, PROJ] from the impl-plan literal "
            "placeholder, but every 26ai/FA-DB/project-tracking Confluence page lives in "
            "space OCIFACP. All consumers of the 4 ask_parameterized TPM email skills "
            "received hard runtime error 'space not in allow-list' for all OCIFACP pages. "
            "Caught in e2e verification by agent."
        ),
        "discovered_by": "agent",
        "status":        "fixed",
        "fix_commit":    "9b6cc1f",
        "severity":      "HIGH",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "P1-E implementation used the literal placeholder from the ADR-032 impl-plan "
            "example ([FA, PROJ]) as the actual allow-list value. The real space key for all "
            "affected Confluence pages is OCIFACP (observed all session: .../display/OCIFACP/...). "
            "Trust check enforced AFTER fetch but BEFORE extraction; disallowed space caused "
            "hard failure for every ask_parameterized skill invocation. "
            "Fix: corrected to [OCIFACP] for all 4 tpm project_tracking_*_email skills. "
            "Per commit message and e2e verification run confirming the hard-fail."
        ),
    },
    # 10. synthesize_workflow.py never emitted source_binding for ask_parameterized skills
    {
        "request_id":    "arch-rca-47ec90d",
        "queue_id":      "BUG-queue-e1463",  # stable
        "timestamp":     "2026-05-16T21:30:35-07:00",
        "tool":          "authorSkill",
        "description": (
            "Architect-RCA companion to reported synth-tpm-5b3e690f VALIDATE failure. "
            "synthesize_workflow_skill() never emitted a source_binding block. Every newly "
            "authored ask_parameterized skill committed with author_fixed defaults and "
            "immediately failed _validate_source_binding_contract at VALIDATE. The ADR-032 "
            "core use case (conversational authoring -> PROMOTE) was completely unreachable "
            "via the normal skill authoring flow."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "47ec90d",
        "severity":      "HIGH",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "synthesize_workflow.py synthesize_workflow_skill() had no source_binding emission "
            "path for ask_parameterized mode. All newly-authored skills fell through to "
            "author_fixed defaults, which lack the source_binding block required by "
            "_validate_source_binding_contract. VALIDATE correctly hard-failed every new "
            "ask_parameterized skill. Also: derive_space_allow_list() missing; space keys "
            "were not derivable from session state at synthesis time. "
            "Per commit diff: 6-field source_binding block + typed confluence_page_ref trigger "
            "added; derive_space_allow_list derives space from source_samples > URL patterns > "
            "explicit source.space; author_fixed output byte-identical to pre-ADR-032."
        ),
    },
    # 11. Test assertion on ADB-only skill's on-disk YAML (spurious failure)
    {
        "request_id":    "agent-rca-fcae5e0",
        "queue_id":      "BUG-queue-ae5cd",  # stable
        "timestamp":     "2026-05-16T20:26:30-07:00",
        "tool":          "authorSkill",
        "description": (
            "test_non_email_skill_has_no_source_binding asserted on-disk existence of "
            "framework/workflow_skills/tpm/26ai_confluence_pptx.yaml. That skill is "
            "promoted in ADB and its authoring YAML was never committed (ADB is the source "
            "of truth). When untracked authoring byproducts were cleaned off disk, the test "
            "failed on a missing transient artifact — not a product regression. "
            "Failure count regressed from 8 to 9 baseline. Caught by agent trust-but-verify."
        ),
        "discovered_by": "agent",
        "status":        "fixed",
        "fix_commit":    "fcae5e0",
        "severity":      "Low",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "NON_EMAIL_SKILLS list included 26ai_confluence_pptx (ADB-only, no on-disk YAML). "
            "test_non_email_skill_has_no_source_binding did a disk existence check and failed "
            "when git clean removed the transient authoring artifact. "
            "Fix: scope NON_EMAIL_SKILLS to tracked on-disk canonical skills only "
            "(26ai_fa_db_upgrade_pptx, weekly_exec_review). Property still asserted meaningfully "
            "against skills that actually live in the repo. Per commit diff."
        ),
    },
    # 12. D1+D2 ask_parameterized defects + P2-API contract (two commits, one logical entry)
    {
        "request_id":    "arch-rca-4330bd0",
        "queue_id":      "BUG-queue-a7788",  # stable
        "timestamp":     "2026-05-16T16:17:45-07:00",
        "tool":          "askKnowledgeBase",
        "description": (
            "D1 (ask route input threading): maybe_render_artifact in ask.py never passed "
            "the page reference to executor.execute() for ask_parameterized skills; executor "
            "always received inputs={'input': question} so inputs['page_id'] was always '' — "
            "the blank-page-id hard-fail. "
            "D2 (single-fetch space model): _retrieve_ask_parameterized called "
            "confluence_adapter.fetch_metadata(page_id) which does not exist on any adapter "
            "(all implement only fetch()), causing AttributeError surfaced as FileNotFoundError. "
            "P2-API: executor.execute() now returns source_fetched_on_demand + "
            "source_fetched_page_id (cfea4db contract). "
            "Fix commits: 4330bd0 (D1+D2+P2-API) + cfea4db (OpenAPI contract)."
        ),
        "discovered_by": "architect",
        "status":        "fixed",
        "fix_commit":    "4330bd0",
        "fix_commit_cluster": ["4330bd0", "cfea4db"],
        "severity":      "Medium",
        "session":       "2026-05-16 hardening",
        "root_cause": (
            "D1: maybe_render_artifact detected ask_parameterized mode but did not resolve "
            "or thread page_id into executor inputs. executor always saw inputs['page_id']=''. "
            "D2: _retrieve_ask_parameterized called fetch_metadata() which is not part of "
            "the Confluence adapter interface; AttributeError crashed ephemeral fetch path. "
            "Both defects were introduced when D1/D2 were implemented before the e2e "
            "integration test existed. P2-API (cfea4db) also missed response wiring. "
            "Per commit diffs 4330bd0 and cfea4db confirming all three fixes."
        ),
    },
]

def main():
    parser = argparse.ArgumentParser(description="Backfill 2026-05-16 hardening defects to ADB")
    parser.add_argument("--env", default="laptop", help="Config env name (default: laptop)")
    parser.add_argument("--dry-run", action="store_true", help="Print entries without writing to ADB")
    args = parser.parse_args()

    if args.dry_run:
        print("[DRY RUN] Would file the following entries:")
        for d in DEFECTS:
            print(f"  {d['queue_id']}  {d['discovered_by']:12s}  {d['severity']:6s}  {d['fix_commit']}")
        return 0

    # Build pool (mirrors cmd_export_bugs exactly)
    try:
        pool = _build_pool(args.env)
    except Exception as exc:
        print(f"ERROR: Failed to build ADB pool: {exc}", file=sys.stderr)
        return 1

    store_root = Path("~/.kbf/store").expanduser()
    from framework.deploy.error_store import AdbErrorStore  # type: ignore[import]
    store = AdbErrorStore(pool=pool, store_root=store_root)

    success = []
    failed = []

    for d in DEFECTS:
        qid = d["queue_id"]
        try:
            store.record_user_bug(d)
            # Verify it landed in ADB by querying back
            with pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM KB_SHIM.KBF_BUG_REPORTS WHERE queue_id = :qid",
                        {"qid": qid},
                    )
                    (count,) = cur.fetchone()
            if count > 0:
                print(f"  OK  {qid}  {d['discovered_by']:12s}  {d['severity']:6s}  {d['fix_commit']}")
                success.append(qid)
            else:
                # record_user_bug fell back to JSONL-only (ADB exception was swallowed internally)
                print(f"  SILENT-FALLBACK  {qid}  {d['discovered_by']:12s}  {d['fix_commit']}", file=sys.stderr)
                print(f"  ADB write silently failed — record is in JSONL only. STOP.", file=sys.stderr)
                failed.append(qid)
        except Exception as exc:
            print(f"  ERROR  {qid}: {exc}", file=sys.stderr)
            failed.append(qid)

    print(f"\nResult: {len(success)} written to ADB, {len(failed)} failed.")
    if failed:
        print("SILENT-DEGRADATION DETECTED. The following queue_ids are JSONL-only:", file=sys.stderr)
        for qid in failed:
            print(f"  {qid}", file=sys.stderr)
        return 1

    # Close pool
    try:
        pool.close()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
