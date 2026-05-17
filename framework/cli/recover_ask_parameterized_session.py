"""
recovery tool: re-synthesize the workflow_skill artifact for a stuck ask_parameterized session.

Problem class: session reached COMMITTED before commit 47ec90d (the ADR-032 synthesizer
fix). The committed workflow_skill YAML has no source_binding block (mode defaults to
author_fixed). VALIDATE hard-fails with:
  "source_binding.mode must be 'ask_parameterized' ... YAML has mode='author_fixed'"

Fix: re-run synthesize_workflow_skill() with the fixed code using the session's own
persisted data, then write the corrected YAML back to:
  1. session_data.synthesized_artifacts[wf_key]  — what _run_validate reads for contract check
  2. KB_SHIM.KBF_SKILL_ARTIFACTS                 — what _run_validate reads for link check
Then set session state to COMMITTED so the next authorSkill call triggers VALIDATE cleanly.

Usage:
    source ~/.kbf/secrets.env
    /usr/bin/python3 framework/cli/recover_ask_parameterized_session.py <synth_id>

Example:
    /usr/bin/python3 framework/cli/recover_ask_parameterized_session.py synth-tpm-5b3e690f
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover a stuck ask_parameterized authorSkill session")
    parser.add_argument("synth_id", help="The session ID (e.g. synth-tpm-5b3e690f)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    args = parser.parse_args()

    synth_id: str = args.synth_id
    dry_run: bool = args.dry_run

    try:
        import oracledb
    except ImportError:
        print("ERROR: oracledb not installed. Run with /usr/bin/python3 (Python 3.9 + oracledb).")
        sys.exit(1)

    # Load laptop config
    cfg_path = REPO_ROOT / "framework" / "config" / "laptop.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    adb_cfg = cfg["adb"]
    wallet_path = os.path.expanduser(adb_cfg["wallet_path"])
    wallet_pw = os.environ.get("WALLET_PASSWORD", "")
    admin_pw = os.environ.get("KBF_ADB_ADMIN_PASSWORD", "")

    if not wallet_pw or not admin_pw:
        print("ERROR: WALLET_PASSWORD and KBF_ADB_ADMIN_PASSWORD must be set in env.")
        print("Run: source ~/.kbf/secrets.env")
        sys.exit(1)

    conn = oracledb.connect(
        user=adb_cfg["admin_user"],
        password=admin_pw,
        dsn=adb_cfg["dsn"],
        config_dir=wallet_path,
        wallet_location=wallet_path,
        wallet_password=wallet_pw,
    )
    print(f"[1] Connected to ADB.")

    # Load session
    cur = conn.cursor()
    cur.execute("""
        SELECT synth_id, user_id, persona, skill_name, state, status, session_data
        FROM kb_shim.author_skill_sessions
        WHERE synth_id = :sid
    """, sid=synth_id)
    cols = [d[0].lower() for d in cur.description]
    cur.rowfactory = lambda *vals: dict(zip(cols, vals))
    row = cur.fetchone()
    cur.close()

    if row is None:
        print(f"ERROR: session {synth_id!r} not found in ADB.")
        sys.exit(1)

    print(f"[2] Session loaded: state={row['state']} status={row['status']}")

    raw = row["session_data"]
    if hasattr(raw, "read"):
        raw = raw.read()
    session: dict = json.loads(raw) if isinstance(raw, str) else raw

    persona: str = session.get("persona", "")
    skill_name: str = session.get("skill_name", "")
    sb_mode: str = session.get("source_binding_mode", "author_fixed")

    print(f"    persona={persona!r}, skill_name={skill_name!r}, source_binding_mode={sb_mode!r}")

    if sb_mode != "ask_parameterized":
        print(f"ERROR: session source_binding_mode is {sb_mode!r}, not 'ask_parameterized'.")
        print("This recovery script is only for ask_parameterized sessions stuck at VALIDATE.")
        sys.exit(1)

    # Derive space_allow_list
    from framework.skill_builder.synthesize_workflow import (
        synthesize_workflow_skill,
        derive_space_allow_list,
    )

    sources = session.get("sources", [])
    source_samples = session.get("source_samples", {})
    space_allow_list = derive_space_allow_list(sources, source_samples)
    print(f"[3] derive_space_allow_list => {space_allow_list}")

    if not space_allow_list:
        print("ERROR: space_allow_list is empty — cannot derive from session state.")
        print("  source_samples keys:", list(source_samples.keys()))
        print("  sources:", sources)
        print("Manual action required: determine the Confluence space key from the session's sources.")
        sys.exit(1)

    # Re-synthesize workflow YAML
    design = session.get("design") or {}
    ws_design = design.get("workflow_shape", {}) or {}
    intent = {
        "task_description": session.get("intent_description", ""),
        "sources": sources,
        "trigger": session.get("trigger", {"on_request": True}),
        "output_format": session.get("output_format", "markdown"),
        "layout": ws_design.get("layout"),
        "reuse": session.get("reuse", {}),
    }

    wf_struct = synthesize_workflow_skill(
        persona=persona,
        skill_name=skill_name,
        intent=intent,
        fields=session.get("fields", []),
        template_path=None,
        source_binding_mode="ask_parameterized",
        space_allow_list=space_allow_list,
    )

    sb_block = wf_struct.get("source_binding", {})
    trigger_inputs = wf_struct.get("trigger", {}).get("on_request", {}).get("inputs", [])
    print(f"[4] Workflow re-synthesized.")
    print(f"    source_binding.mode: {sb_block.get('mode')}")
    print(f"    source_binding.space_allow_list: {sb_block.get('space_allow_list')}")
    print(f"    trigger input name: {trigger_inputs[0].get('name') if trigger_inputs else 'NONE'}")

    # Validate contract
    from framework.skill_builder.conversation import _validate_source_binding_contract

    sb_errors = _validate_source_binding_contract(wf_struct, "ask_parameterized")
    if sb_errors:
        print("ERROR: _validate_source_binding_contract FAILED on newly synthesized YAML:")
        for e in sb_errors:
            print(" -", e)
        sys.exit(1)

    print(f"[5] _validate_source_binding_contract: PASSED (0 errors)")

    if dry_run:
        print("\n[DRY RUN] Would update:")
        print(f"  - session_data.synthesized_artifacts[wf_key] with new source_binding block")
        print(f"  - session state from {row['state']!r} -> 'COMMITTED'")
        print(f"  - KBF_SKILL_ARTIFACTS artifact_id={persona}.{skill_name}.workflow_skill content")
        print("\nNo changes written (--dry-run mode).")
        conn.close()
        return

    # Update session dict
    wf_key = f"framework/workflow_skills/{persona}/{skill_name}.yaml"
    if "synthesized_artifacts" not in session:
        session["synthesized_artifacts"] = {}
    session["synthesized_artifacts"][wf_key] = wf_struct
    session["state"] = "COMMITTED"
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    session["updated_at"] = now_iso
    session_json = json.dumps(session)

    # Persist session
    cur2 = conn.cursor()
    cur2.setinputsizes(session_data=oracledb.DB_TYPE_CLOB)
    cur2.execute("""
        UPDATE kb_shim.author_skill_sessions
        SET state        = 'COMMITTED',
            session_data = :session_data,
            updated_at   = :updated_at
        WHERE synth_id = :synth_id
    """, {
        "session_data": session_json,
        "updated_at": datetime.now(tz=timezone.utc),
        "synth_id": synth_id,
    })
    n = cur2.rowcount
    cur2.close()
    conn.commit()
    print(f"[6] Session state updated: {n} row(s) written, state=COMMITTED.")

    # Update KBF_SKILL_ARTIFACTS
    wf_yaml_str = yaml.safe_dump(wf_struct, sort_keys=False, allow_unicode=True)
    artifact_id = f"{persona}.{skill_name}.workflow_skill"
    now_dt = datetime.now(tz=timezone.utc)

    cur3 = conn.cursor()
    cur3.setinputsizes(content=oracledb.DB_TYPE_CLOB)
    cur3.execute("""
        MERGE INTO KB_SHIM.KBF_SKILL_ARTIFACTS tgt
        USING DUAL ON (tgt.artifact_id = :artifact_id)
        WHEN MATCHED THEN UPDATE SET
            content    = :content,
            synth_id   = :synth_id,
            updated_at = :updated_at
        WHEN NOT MATCHED THEN INSERT
            (artifact_id, synth_id, persona, skill_name, artifact_type,
             rel_path, content, status, created_at, updated_at)
        VALUES
            (:artifact_id, :synth_id, :persona, :skill_name, 'workflow_skill',
             :rel_path, :content, 'draft', :created_at, :updated_at)
    """, {
        "artifact_id": artifact_id,
        "synth_id": synth_id,
        "persona": persona,
        "skill_name": skill_name,
        "rel_path": f"framework/workflow_skills/{persona}/{skill_name}.yaml",
        "content": wf_yaml_str,
        "created_at": now_dt,
        "updated_at": now_dt,
    })
    n2 = cur3.rowcount
    cur3.close()
    conn.commit()
    print(f"[7] KBF_SKILL_ARTIFACTS updated: {n2} row(s) for {artifact_id}.")

    # Verify round-trip
    from framework.skill_builder.conversation import SkillBuilderConversation
    from unittest.mock import MagicMock

    restored = SkillBuilderConversation.from_dict(session, llm=None, artifact_store=None, skill_store=MagicMock())
    assert restored._state == "COMMITTED", f"from_dict state mismatch: {restored._state}"
    assert restored._data.source_binding_mode == "ask_parameterized"

    verify_wf = session["synthesized_artifacts"][wf_key]
    final_errors = _validate_source_binding_contract(verify_wf, "ask_parameterized")
    assert not final_errors, f"Final contract check failed: {final_errors}"

    print("[8] Verification: from_dict round-trip OK, contract check PASSED.")

    print("\n=== SOURCE_BINDING BLOCK ===")
    print(yaml.safe_dump(sb_block, sort_keys=False, allow_unicode=True))
    print("=== PAGE_ID TRIGGER INPUT ===")
    print(yaml.safe_dump(trigger_inputs, sort_keys=False, allow_unicode=True))

    print("=== RECOVERY COMPLETE ===")
    print(f"Session {synth_id!r} is now in state=COMMITTED.")
    print("Resume with: authorSkill {\"session_id\": %r, \"message\": \"validate\"}" % synth_id)

    conn.close()


if __name__ == "__main__":
    main()
