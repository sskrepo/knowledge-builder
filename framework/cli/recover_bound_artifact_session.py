"""
Recovery tool: re-bind a reference artifact for a session where it was silently cleared
on FSM re-entry (the pre-ADR-035 defect).

Problem class (ADR-035 / DECISION-015): Session reached a state where:
  - artifact_reference_id was nulled by the skip/reset branch of _handle_upload_artifact_example
  - design.workflow_shape.layout TEXT still contains the artifact name (never cleared)
  - REVIEW_DESIGN showed the artifact as present; _run_eval found artifact_reference_id=None
    and emitted "No reference artifact was uploaded" (silent wrong output)
  - Artifact BYTES still exist in the filestore

Fix: set artifact_reference_id, artifact_reference_type, artifact_reference_name atomically
in the persisted session_data (matching the invariant established by ADR-035), set state
so the user can re-run EVAL cleanly.

Usage:
    source ~/.kbf/secrets.env
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        framework/cli/recover_bound_artifact_session.py <synth_id> \\
        --artifact-id <artifact_id> \\
        --artifact-name <filename> \\
        --artifact-type pptx \\
        [--target-state EVAL|REVIEW_DESIGN] \\
        [--dry-run]

Example (the canonical stuck session):
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        framework/cli/recover_bound_artifact_session.py synth-tpm-c3ef4ef2 \\
        --artifact-id art-92062549 \\
        --artifact-name "2026-05-14 FAaaS-LCM Update Kiwi Slide only 2.pptx" \\
        --artifact-type pptx \\
        --target-state EVAL
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
    parser = argparse.ArgumentParser(
        description="Re-bind a reference artifact for a session affected by the pre-ADR-035 silent-clear bug"
    )
    parser.add_argument("synth_id", help="The session ID (e.g. synth-tpm-c3ef4ef2)")
    parser.add_argument(
        "--artifact-id", required=True,
        help="The artifact_id to rebind (e.g. art-92062549)",
    )
    parser.add_argument(
        "--artifact-name", required=True,
        help="The filename/label of the artifact (e.g. '2026-05-14 FAaaS-LCM Update Kiwi Slide only 2.pptx')",
    )
    parser.add_argument(
        "--artifact-type", default="pptx",
        help="The artifact type: pptx | docx | md | txt (default: pptx)",
    )
    parser.add_argument(
        "--target-state", default="EVAL",
        help="The FSM state to set after recovery: EVAL | REVIEW_DESIGN (default: EVAL)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing",
    )
    args = parser.parse_args()

    synth_id: str = args.synth_id
    artifact_id: str = args.artifact_id
    artifact_name: str = args.artifact_name
    artifact_type: str = args.artifact_type
    target_state: str = args.target_state
    dry_run: bool = args.dry_run

    try:
        import oracledb
    except ImportError:
        print("ERROR: oracledb not installed. Run with /Library/Developer/CommandLineTools/usr/bin/python3.")
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

    print(f"    persona={session.get('persona')!r}, skill_name={session.get('skill_name')!r}")
    print(f"    current artifact_reference_id={session.get('artifact_reference_id')!r}")
    print(f"    current artifact_reference_name={session.get('artifact_reference_name')!r}")
    print(f"    design.workflow_shape.layout={((session.get('design') or {}).get('workflow_shape') or {}).get('layout')!r}")

    # Resolve artifact bytes path for verification
    artifact_store_base = Path.home() / ".kbf" / "store" / "uploads" / synth_id / artifact_id
    artifact_bytes_found: bool = False
    artifact_local_path: str = ""

    if artifact_store_base.exists():
        # Find the file under the artifact store directory
        for f in artifact_store_base.iterdir():
            if f.is_file():
                artifact_bytes_found = True
                artifact_local_path = str(f)
                print(f"[3] Artifact bytes found: {artifact_local_path} ({f.stat().st_size / 1024 / 1024:.1f} MB)")
                break

    if not artifact_bytes_found:
        print(f"[3] WARNING: artifact bytes NOT found at {artifact_store_base}/")
        print(f"    Recovery will still set the binding fields, but _run_eval will not")
        print(f"    find bytes unless the artifact store is populated.")
        if not dry_run:
            confirm = input("Continue without bytes verification? (y/N): ").strip().lower()
            if confirm != "y":
                print("Aborted.")
                conn.close()
                sys.exit(0)

    # Validate round-trip before modifying
    from framework.skill_builder.conversation import SkillBuilderConversation
    from unittest.mock import MagicMock

    restored_pre = SkillBuilderConversation.from_dict(session, llm=None, artifact_store=None, skill_store=MagicMock())
    print(f"[4] Pre-fix state: has_bound_reference_artifact()={restored_pre.has_bound_reference_artifact()}")
    print(f"    artifact_reference_id={restored_pre._data.artifact_reference_id!r}")
    print(f"    artifact_reference_name={restored_pre._data.artifact_reference_name!r}")

    if dry_run:
        print("\n[DRY RUN] Would update session_data with:")
        print(f"  artifact_reference_id   = {artifact_id!r}")
        print(f"  artifact_reference_type = {artifact_type!r}")
        print(f"  artifact_reference_name = {artifact_name!r}")
        print(f"  state = {target_state!r}")
        print("\nNo changes written (--dry-run mode).")
        conn.close()
        return

    # Apply the binding atomically (matching ADR-035 _bind_reference_artifact invariant)
    session["artifact_reference_id"] = artifact_id
    session["artifact_reference_type"] = artifact_type
    session["artifact_reference_name"] = artifact_name
    # Do NOT touch artifact_layout or design.workflow_shape.layout —
    # the layout text in design is correct (set by DESIGN_SKILL); we are only
    # restoring the binding fields that were wrongly cleared by the pre-ADR-035 code.
    session["state"] = target_state
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    session["updated_at"] = now_iso
    session_json = json.dumps(session)

    # Persist session
    cur2 = conn.cursor()
    cur2.setinputsizes(session_data=oracledb.DB_TYPE_CLOB)
    cur2.execute("""
        UPDATE kb_shim.author_skill_sessions
        SET state        = :state,
            session_data = :session_data,
            updated_at   = :updated_at
        WHERE synth_id = :synth_id
    """, {
        "state": target_state,
        "session_data": session_json,
        "updated_at": datetime.now(tz=timezone.utc),
        "synth_id": synth_id,
    })
    n = cur2.rowcount
    cur2.close()
    conn.commit()
    print(f"[5] Session updated: {n} row(s), state={target_state!r}.")

    # Verify round-trip — confirm has_bound_reference_artifact() is now True
    restored_post = SkillBuilderConversation.from_dict(
        session, llm=None, artifact_store=None, skill_store=MagicMock()
    )
    assert restored_post.has_bound_reference_artifact(), (
        f"from_dict round-trip FAILED: has_bound_reference_artifact()=False "
        f"(id={restored_post._data.artifact_reference_id!r} "
        f"name={restored_post._data.artifact_reference_name!r})"
    )
    assert restored_post._data.artifact_reference_id == artifact_id
    assert restored_post._data.artifact_reference_name == artifact_name
    assert restored_post._state == target_state

    print(f"[6] Verification PASSED: has_bound_reference_artifact()=True")
    print(f"    artifact_reference_id   = {restored_post._data.artifact_reference_id!r}")
    print(f"    artifact_reference_name = {restored_post._data.artifact_reference_name!r}")
    print(f"    state = {restored_post._state!r}")

    # Verify ADB read-back
    cur3 = conn.cursor()
    cur3.execute("""
        SELECT state, session_data
        FROM kb_shim.author_skill_sessions
        WHERE synth_id = :sid
    """, sid=synth_id)
    cols3 = [d[0].lower() for d in cur3.description]
    cur3.rowfactory = lambda *vals: dict(zip(cols3, vals))
    row3 = cur3.fetchone()
    cur3.close()

    raw3 = row3["session_data"]
    if hasattr(raw3, "read"):
        raw3 = raw3.read()
    readback: dict = json.loads(raw3) if isinstance(raw3, str) else raw3

    assert readback.get("artifact_reference_id") == artifact_id, (
        f"ADB read-back mismatch: artifact_reference_id={readback.get('artifact_reference_id')!r}"
    )
    assert readback.get("artifact_reference_name") == artifact_name, (
        f"ADB read-back mismatch: artifact_reference_name={readback.get('artifact_reference_name')!r}"
    )
    print(f"[7] ADB read-back confirmed: artifact_reference_id={readback.get('artifact_reference_id')!r}")

    print("\n=== RECOVERY COMPLETE ===")
    print(f"Session {synth_id!r} artifact binding restored.")
    print(f"  artifact_id  = {artifact_id!r}")
    print(f"  artifact_name = {artifact_name!r}")
    print(f"  state        = {target_state!r}")
    if artifact_bytes_found:
        print(f"  bytes path   = {artifact_local_path!r}")
        print(f"  _run_eval will resolve bytes via artifact_store.resolve('{artifact_id}')")
    else:
        print(f"  WARNING: artifact bytes not confirmed on disk — verify filestore manually.")

    print(f"\nResume with: authorSkill {{\"session_id\": {synth_id!r}, \"message\": \"evaluate\"}}")

    conn.close()


if __name__ == "__main__":
    main()
