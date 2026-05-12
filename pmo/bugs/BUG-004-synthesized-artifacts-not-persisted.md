---
title: BUG-004 — authorSkill commits 0 artifacts: synthesized_artifacts missing from to_dict() serialization
status: verified
created: 2026-05-12
verified: 2026-05-12
owner: qa
assigned-to: backend-dev
severity: blocker
related-story: V3-deployment-layer
component: framework/skill_builder/conversation.py
discovered-by: user (live MCP session via /mcp tools/call)
fixed-in: (this commit)
---

## Steps to reproduce

1. Start the KB server and begin a full `authorSkill` session:
   ```bash
   # Start session
   curl -sS -X POST http://localhost:8080/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"authorSkill","arguments":{"input":"start"}}}'
   ```

2. Advance through all states until PREVIEW is reached (persona → fields → schema
   review → sources → triggers). The server returns "Here's what I'll commit" with
   5 artifact summaries.

3. In the **next MCP call**, confirm the commit:
   ```bash
   curl -sS -X POST http://localhost:8080/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
          "params":{"name":"authorSkill","arguments":{
            "synthId":"<synth_id_from_step_2>",
            "input":"yes, commit"}}}'
   ```

## Expected

```
Committed 5 artifact(s):
  • framework/workflow_skills/tpm/<skill_name>.yaml
  • framework/parsers/schemas/tpm/<skill_name>/v1.json
  • ...
```

Session advances to COMMITTED state, then to VALIDATE.

## Actual

```
Committed 0 artifact(s):
```

Then validation immediately fails:
```
workflow skill file does not exist:
/Users/.../framework/workflow_skills/tpm/<skill_name>.yaml
```

## Root cause

`SkillBuilderConversation.to_dict()` serializes the session for ADB persistence
by delegating entirely to `get_state()`:

```python
# Before fix
def to_dict(self) -> dict:
    return {"state": self._state, "persona": self._persona, **self.get_state()}
```

`get_state()` intentionally omits `synthesized_artifacts` (and `slide_mapping`)
to keep the GET-endpoint snapshot lean — artifact content can be several KB of
YAML/JSON and has no value to API consumers.

However, `to_dict()` used the same method for **persistence**, so
`synthesized_artifacts` was never written into `session_data` in ADB.

The flow across MCP calls:

1. **MCP call N — PREVIEW**: `_advance_to_preview()` synthesises 5 artifacts and
   stores them in `self._data.synthesized_artifacts`. Session is saved via
   `to_dict()` → ADB. Because `get_state()` omits `synthesized_artifacts`, the
   ADB row's `session_data` JSON has `"synthesized_artifacts": {}` (default from
   `from_dict`).

2. **MCP call N+1 — COMMIT**: session is loaded from ADB via `from_dict()`. 
   `from_dict()` reads `d.get("synthesized_artifacts", {})` → empty dict.
   `_write_artifacts()` iterates over the empty dict → commits nothing.

`slide_mapping` has the same omission: set during `ANALYZE_ARTIFACT`, never
included in `to_dict()`, lost on resume.

## Fix

Separate the persistence dict from the API snapshot dict. `get_state()` stays
lean (for the GET endpoint). `to_dict()` adds the two heavy fields on top:

```python
def to_dict(self) -> dict:
    d = {"state": self._state, "persona": self._persona, **self.get_state()}
    d["synthesized_artifacts"] = dict(self._data.synthesized_artifacts)
    if self._data.slide_mapping is not None:
        d["slide_mapping"] = dict(self._data.slide_mapping)
    return d
```

`from_dict()` was already correct — it reads both fields with safe defaults.

## Verification

After server restart, the commit call correctly writes all 5 artifacts to disk
and session advances to COMMITTED → VALIDATE.

## Test gap to close

`test_skill_builder_conversation.py` (or equivalent) needs a round-trip test:

1. Advance a `SkillBuilderConversation` to PREVIEW state
2. Call `to_dict()` → assert `"synthesized_artifacts"` is present and non-empty
3. Call `from_dict()` on the result → assert `synthesized_artifacts` restored
4. Call `_handle_commit()` on the restored session → assert committed_paths is non-empty
5. Assert the files were written to a tmp dir (patch `REPO_ROOT`)

This test would have caught this bug immediately.
