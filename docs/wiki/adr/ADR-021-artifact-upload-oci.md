---
title: ADR-021 — Artifact upload for remote authorSkill sessions
status: accepted
created: 2026-05-12
owner: architect
deciders: user
tags: [adr, deploy, skill-builder, oci, storage, mcp]
related: [ADR-015, ADR-019, ADR-020, ADR-010, DECISION-005]
---

# ADR-021 — Artifact upload for remote authorSkill sessions

## Status

Accepted (2026-05-12). DECISION-005 filed for OCI bucket/namespace/region confirmation before Backend Dev starts the OCI store path.

---

## Context

### The bug

`conversation.py:_handle_analyze_artifact()` receives `user_input` (the text typed by the LLM client into `authorSkill`) and interprets it as a filesystem path. It calls `analyze_artifact(path)`, which checks `Path(path).exists()`. When the server runs on an OCI VM, the path is a local laptop path that does not exist on the server. `analyze_artifact` falls through to its fallback branch and returns the generic field list `["title", "summary", "details"]`. The user's real artifact structure — slide titles, heading hierarchy, section names — is silently lost. All downstream states (REVIEW_FIELDS, REVIEW_SCHEMA, extraction schemas) are degraded.

### Why this wasn't caught earlier

In laptop mode the MCP server and the LLM client run on the same machine. `Path(path).exists()` is `True` and the code works. The remote-VM deployment topology (ADR-020) uncovered the gap.

### What must remain unchanged

`analyze_artifact(path: str) -> tuple[list[str], dict | None]` reads a file that exists on the server's filesystem. Its internal logic (PPTX slide title extraction, DOCX heading traversal, Markdown heading parse) must not change. The fix must deliver a local-path-resident file to the server before `_handle_analyze_artifact` calls `analyze_artifact`.

### Constraints from CLAUDE.md

- Python throughout; OCI-native (no S3/boto3).
- Follow the dual-mode pattern from the existing `SessionStore` (filestore for `KBF_ENV=laptop`, OCI Object Storage for `staging`/`production`).
- Artifact binary content must never be persisted into the session dict or ADB — only the artifact_id reference and extracted fields.
- `analyze_artifact` must remain unchanged.

---

## Decision

### Summary

Introduce a new MCP tool, `uploadArtifact`, that accepts base64-encoded file bytes from the LLM client and stores them server-side. The client receives back an `artifactId`. On the next `authorSkill` turn the client sends `"artifact:<filename> id:<artifactId>"` as the `input` field; `_handle_analyze_artifact` detects the `artifact:` prefix, resolves the `artifactId` to a local path via an `ArtifactStore`, and calls the existing `analyze_artifact(path)` unchanged.

Storage is dual-mode, mirroring the `SessionStore` pattern:
- `KBF_ENV=laptop` (or `KBF_STORE_BACKEND=filestore`) → `FilestoreArtifactStore` writes under `~/.kbf/store/uploads/{synth_id}/{artifact_id}/`.
- `KBF_ENV=staging|production` (or `KBF_STORE_BACKEND=oci`) → `OciArtifactStore` writes to OCI Object Storage with key prefix `kbf-uploads/{synth_id}/{artifact_id}/{filename}`.

Cleanup is tied to session lifecycle: when a session reaches `DONE` or `committed` status, the framework calls `artifact_store.cleanup(synth_id)`. A 7-day OCI lifecycle rule provides a hard TTL backstop for any uploads that were orphaned before a clean session close.

---

## Component map

```
framework/deploy/
  artifact_store/
    __init__.py          — package marker; re-exports ArtifactStore, build_artifact_store
    _base.py             — ArtifactStore ABC
    filestore.py         — FilestoreArtifactStore
    oci.py               — OciArtifactStore
    factory.py           — build_artifact_store(pool, env) -> ArtifactStore

  mcp_tools.py           — add uploadArtifact to EXTERNAL_TOOLS_SCHEMA;
                           add _make_upload_artifact_handler() to build_external_tool_registry()
  skill_prompt.py        — v1.2.0: add "## Using local files as example artifacts" section
  mcp_server.py          — lifespan: build artifact_store, mount as app.state.artifact_store;
                           pass artifact_store to _start_or_continue_session() where needed

framework/skill_builder/
  conversation.py        — _handle_analyze_artifact: detect "artifact:" prefix,
                           resolve artifact_id -> path, call artifact_store.cleanup on DONE
```

No changes to `framework/skill_builder/analyze_artifact.py`.

---

## Data flow (ASCII)

```
LLM client (laptop)                    KBF MCP server (OCI VM)
─────────────────────────────────────────────────────────────────────
1. User provides Q2 PPT file

2. LLM reads file bytes
   base64-encodes them

3. tools/call uploadArtifact ─────────────────────────────────────►
   { content: "<base64>",                ArtifactStore.upload(
     filename: "q2.pptx",                  synth_id, artifact_id,
     synthId:  "synth-tpm-abc" }           filename, bytes)
                                               │
                                    KBF_ENV=laptop:
                                      ~/.kbf/store/uploads/
                                        synth-tpm-abc/
                                          art-xyz/q2.pptx
                                               │
                                    KBF_ENV=production:
                                      OCI Object Storage
                                        kbf-uploads/
                                          synth-tpm-abc/
                                            art-xyz/q2.pptx
                                               │
◄──────────────────────────────────────────────
4. { artifactId: "art-xyz",
     filename: "q2.pptx",
     sizeBytes: 148230,
     expiresAt: "2026-05-19T..." }

5. LLM sends authorSkill turn ──────────────────────────────────────►
   { synthId: "synth-tpm-abc",         _handle_analyze_artifact(
     input: "artifact:q2.pptx           "artifact:q2.pptx id:art-xyz")
             id:art-xyz" }                  │
                                    detect "artifact:" prefix
                                    resolve: artifact_store.resolve(
                                               "art-xyz") -> local Path
                                               │
                                    KBF_ENV=laptop: path already local
                                    KBF_ENV=production: OciArtifactStore
                                      downloads to temp dir -> Path
                                               │
                                    analyze_artifact(path)   (unchanged)
                                    -> fields=["title","q2_highlights",
                                               "risk_areas","exec_asks"]
                                       mapping={...slide positions...}
                                               │
                                    _data.fields = fields
                                    _data.slide_mapping = mapping
                                    advance to REVIEW_FIELDS

◄──────────────────────────────────────────────
6. { state: "REVIEW_FIELDS",
     message: "From artifact at
       'q2.pptx' I found these fields:
       ..." }

... session continues normally ...

7. Session reaches DONE ────────────────────────────────────────────►
                                    artifact_store.cleanup(
                                      "synth-tpm-abc")
                                    (deletes upload; session dict
                                     retains only artifact_id ref
                                     + extracted fields)
```

---

## ArtifactStore ABC

```
ArtifactStore (ABC)
  upload(synth_id: str,
         artifact_id: str,
         filename: str,
         data: bytes) -> None
    — Persists the raw file bytes.
    — For OciArtifactStore: PutObject to
      kbf-uploads/{synth_id}/{artifact_id}/{filename}.
    — For FilestoreArtifactStore: writes to
      {store_root}/uploads/{synth_id}/{artifact_id}/{filename}.
    — Idempotent: re-uploading same artifact_id overwrites.

  resolve(artifact_id: str) -> Path | None
    — Returns a local filesystem Path to the file.
    — For FilestoreArtifactStore: returns the stored path directly.
    — For OciArtifactStore: GetObject, writes to a temp dir under
      {store_root}/uploads/tmp/{artifact_id}/{filename}, returns path.
    — Returns None if not found.

  cleanup(synth_id: str) -> None
    — Deletes all uploads scoped to synth_id.
    — For FilestoreArtifactStore: shutil.rmtree on
      {store_root}/uploads/{synth_id}/.
    — For OciArtifactStore: ListObjects on prefix
      kbf-uploads/{synth_id}/, then bulk DeleteObject per key.

  list_artifacts(synth_id: str) -> list[dict]
    — Returns [{artifact_id, filename, size_bytes, uploaded_at}] for
      all uploads under synth_id.
    — Used by uploadArtifact handler to confirm storage before returning.
```

---

## uploadArtifact MCP tool

### Schema (added to EXTERNAL_TOOLS_SCHEMA in mcp_tools.py)

```
name: uploadArtifact
description: |
  Upload a local file (PPT, DOCX, Markdown, text) to the server for
  analysis during an authorSkill session. Call this before providing
  an artifact path in an authorSkill turn.
  Returns an artifactId to include in the next authorSkill input.
inputSchema:
  type: object
  required: [content, filename, synthId]
  properties:
    content:
      type: string
      description: Base64-encoded file bytes.
    filename:
      type: string
      description: Original filename including extension (e.g. q2.pptx).
                   Extension is used to select the analyzer (.pptx/.docx/.md/.txt).
    synthId:
      type: string
      description: The authorSkill session ID. Scopes the upload for cleanup.
```

### Response envelope

```
{
  "artifactId":  "art-{uuid8}",     -- opaque key, pass back in authorSkill input
  "filename":    "q2.pptx",
  "sizeBytes":   148230,
  "expiresAt":   "2026-05-19T10:34:00Z"   -- now + ttl_days
}
```

### Auth

`uploadArtifact` requires `write` scope, enforced by the same `_authenticate` + `require_scope` guard that `authorSkill` uses. The MCP dispatch layer passes `_consumer` into the handler factory; the handler checks `"write" in consumer.scopes` and returns an isError response if not. Anonymous / read-only consumers cannot upload.

### Validation in the handler

The handler enforces before decoding:

| Check | Rejection response |
|---|---|
| `filename` extension not in {.pptx, .docx, .md, .txt} | isError: "Unsupported file type. Accepted: .pptx .docx .md .txt" |
| `synthId` not provided or empty | isError: "synthId is required" |
| Decoded byte length > 10 MB | isError: "File exceeds 10 MB limit" |
| base64 decode failure | isError: "content must be valid base64" |

Decoded size check happens after base64 decode (not before), because base64 overhead is ~33%.

### Handler factory signature (in mcp_tools.py)

```python
def _make_upload_artifact_handler(app):
    async def upload_artifact_handler(
        *,
        content: str,       # base64-encoded bytes
        filename: str,
        synthId: str,
        _consumer=None,
    ) -> dict:
        ...
    return upload_artifact_handler
```

The handler is added to `build_external_tool_registry`:

```python
return {
    "reportBug":        _make_report_bug_handler(app),
    "askKnowledgeBase": _make_ask_handler(app),
    "authorSkill":      _make_author_skill_handler(app),
    "uploadArtifact":   _make_upload_artifact_handler(app),   # NEW
}
```

---

## Conversation state machine changes

### _handle_analyze_artifact (conversation.py)

Current logic (simplified):

```
if Path(path).exists() and Path(path).suffix in (...):
    fields, mapping = analyze_artifact(path)
else:
    fields, mapping = self._parse_fields_from_input(user_input)
```

New logic:

```
if user_input starts with "artifact:":
    parse filename, artifact_id from "artifact:<filename> id:<artifact_id>"
    local_path = artifact_store.resolve(artifact_id)
    if local_path is not None:
        fields, mapping = analyze_artifact(str(local_path))
        source = f"artifact at {filename!r}"
    else:
        fall back to generic fields; warn user artifact_id not found
elif Path(path).exists() and Path(path).suffix in (...):
    fields, mapping = analyze_artifact(path)      # existing laptop path handling
else:
    fields, mapping = self._parse_fields_from_input(user_input)
```

The `artifact_store` reference must be threaded into `SkillBuilderConversation`. Options:

**Option A** — Constructor injection: `SkillBuilderConversation.__init__` gains an optional `artifact_store` parameter (defaults to `None`; when `None`, upload-path is skipped and only local path + manual fields are supported).

**Option B** — Module-level singleton: `artifact_store` initialised once in `mcp_server.py` lifespan and stored in a module-level variable imported by `conversation.py`.

**Recommended: Option A** (constructor injection). Mirrors how `llm` is already injected. Testable without module-level side effects. Backward-compatible: existing code that constructs `SkillBuilderConversation(persona=..., user_id=..., llm=...)` without an `artifact_store` continues to work — artifact upload path is silently skipped.

`_start_or_continue_session` in `routes/author_skill.py` passes `artifact_store=app.state.artifact_store` when constructing or restoring a `SkillBuilderConversation`. The MCP handler factory for `authorSkill` also needs access to `artifact_store`; it already has `app` in scope.

### Session DONE / cleanup hookup

When `turn.done == True` in `_start_or_continue_session`, add:

```python
if turn.done and artifact_store is not None:
    artifact_store.cleanup(synth_id)
```

This is the primary cleanup path. The OCI TTL lifecycle rule (7 days) is the fallback for sessions abandoned without reaching DONE.

---

## FilestoreArtifactStore layout

```
{store_root}/uploads/
  {synth_id}/
    {artifact_id}/
      {filename}       — the raw file bytes
      _meta.json       — {artifact_id, filename, size_bytes, uploaded_at, expires_at}
```

`cleanup(synth_id)` calls `shutil.rmtree({store_root}/uploads/{synth_id}/)`.

`resolve(artifact_id)` scans `{store_root}/uploads/` for any directory whose name matches `artifact_id` (one level down from any synth_id) and returns the file path inside it. Because `artifact_id` values are `art-{uuid8}` and are globally unique, a flat glob `{store_root}/uploads/**/{artifact_id}/*` is unambiguous.

---

## OciArtifactStore design

### Object key scheme

```
kbf-uploads/{synth_id}/{artifact_id}/{filename}
```

Example: `kbf-uploads/synth-tpm-abc123/art-d4e5f6a7/q2-highlights.pptx`

The `synth_id` prefix enables bulk cleanup via `ListObjects(prefix="kbf-uploads/{synth_id}/")`
followed by `DeleteObject` per key.

### OCI SDK calls

| Operation | OCI SDK method |
|---|---|
| Upload | `ObjectStorageClient.put_object(namespace, bucket, object_name, put_object_body)` |
| Download (resolve) | `ObjectStorageClient.get_object(namespace, bucket, object_name)` → stream to temp file |
| List for cleanup | `ObjectStorageClient.list_objects(namespace, bucket, prefix="kbf-uploads/{synth_id}/")` |
| Delete per key | `ObjectStorageClient.delete_object(namespace, bucket, object_name)` |

The OCI SDK to use is `oci.object_storage.ObjectStorageClient` (Python package `oci`). Auth is `oci.auth.signers.InstancePrincipalsSecurityTokenSigner` on the VM (same as OCI GenAI in ADR-014). On laptop (if ever used), falls back to config-file signer from `~/.oci/config`.

### Temp directory for resolve()

`OciArtifactStore.resolve(artifact_id)` downloads to `{store_root}/uploads/tmp/{artifact_id}/{filename}` (a local temp area), returns the path. The temp file is not cleaned up immediately — it survives until `cleanup(synth_id)` is called, which also removes `{store_root}/uploads/tmp/{artifact_id}/`. This is acceptable because sessions are short-lived and storage is bounded by session TTL.

---

## factory.py

```
build_artifact_store(pool, env: str) -> ArtifactStore
  env = os.environ.get("KBF_ENV", "dev")
  backend = os.environ.get("KBF_ARTIFACT_STORE_BACKEND", "")

  if backend == "oci" or (not backend and env in ("staging", "production")):
      from .oci import OciArtifactStore
      return OciArtifactStore(cfg=_load_artifact_cfg())

  # Default: filestore
  store_root = os.environ.get("KBF_STORE_ROOT", str(Path.home() / ".kbf" / "store"))
  from .filestore import FilestoreArtifactStore
  return FilestoreArtifactStore(store_root=store_root)
```

`_load_artifact_cfg()` reads the `artifact_store.oci` section from the active env YAML (same mechanism as `_init_laptop_adb_pool` reads `laptop.yaml` — open the YAML at `framework/config/{env}.yaml`).

The factory is called once in `mcp_server.py` lifespan:

```python
from .artifact_store.factory import build_artifact_store
app.state.artifact_store = build_artifact_store(
    pool=adb_pool,
    env=kbf_env,
)
```

---

## Configuration plane additions

### dev.yaml / prod.yaml — new top-level section

```yaml
# ---- Artifact Store (uploadArtifact MCP tool; per ADR-021) -------------------
artifact_store:
  mode: filestore          # filestore | oci
  max_file_size_mb: 10
  ttl_days: 7
  oci:
    namespace: YOUR_TENANCY_NAMESPACE      # same namespace as object_storage above
    bucket: kbf-uploads                   # separate bucket from kb-raw-dev
    region: eu-frankfurt-1                # confirm with DECISION-005
```

For `dev.yaml` (laptop mode), `mode: filestore` is the default; the `oci:` sub-section is present but ignored when `mode: filestore`. For `staging.yaml` and `prod.yaml`, `mode: oci`.

### Environment variables (alternative override)

| Variable | Purpose |
|---|---|
| `KBF_ARTIFACT_STORE_BACKEND` | Override: `filestore` or `oci`. Supersedes yaml `mode`. |
| `KBF_ARTIFACT_OCI_NAMESPACE` | OCI tenancy namespace |
| `KBF_ARTIFACT_OCI_BUCKET` | Bucket name (default: `kbf-uploads`) |
| `KBF_ARTIFACT_OCI_REGION` | OCI region (default: inherited from `region:` in env yaml) |
| `KBF_ARTIFACT_MAX_MB` | Max file size in MB (default: 10) |

---

## Skill prompt v1.2.0

`skill_prompt.py` gains a new section appended to `_PROMPT_TEXT` before the version footer:

```
## Using local files as example artifacts

If you have a local PPT, DOCX, Markdown, or plain-text file to use as the
example outcome during skill authoring:

1. Read the file as raw bytes and base64-encode them using your available tools.
2. Call `uploadArtifact` with:
   - `content`:  the base64-encoded bytes
   - `filename`: the original filename including extension (e.g. "q2-highlights.pptx")
   - `synthId`:  the session ID from the current authorSkill session
3. Note the `artifactId` returned in the response.
4. When the authorSkill session asks you to provide an artifact path,
   respond with: "artifact:<filename> id:<artifactId>"
   Example: "artifact:q2-highlights.pptx id:art-3f7a1b2c"

Important:
- The file must be .pptx, .docx, .md, or .txt.
- The file must be 10 MB or smaller.
- If you do not have a file, you can type field names manually instead
  (the session will prompt you for this).
- uploadArtifact requires write scope — the same token used for authorSkill.
```

`SKILL_PROMPT_VERSION` bumped from `"1.1.0"` to `"1.2.0"`.

---

## Lifecycle / TTL summary

| Event | Action |
|---|---|
| Session DONE / committed | `artifact_store.cleanup(synth_id)` called by `_start_or_continue_session` |
| Session abandoned via DELETE | Same cleanup hook (add to `author_skill.py:author_skill_delete`) |
| Session expired by TTL job | `cleanup_job.py` should also call `artifact_store.cleanup(synth_id)` for each expired session |
| OCI lifecycle rule | Deletes any objects under `kbf-uploads/` prefix older than 7 days — catches orphaned uploads |

---

## File constraints enforced server-side

| Constraint | Value | Enforcement point |
|---|---|---|
| Accepted types | .pptx, .docx, .md, .txt | `uploadArtifact` handler, before decode |
| Max size | 10 MB decoded bytes | `uploadArtifact` handler, after decode |
| Encoding | base64 (standard) | `uploadArtifact` handler; `base64.b64decode` with `validate=True` |
| Virus scanning | Not in scope v1 | Internal tool, authenticated users only |

---

## Consequences

### Positive

- Fixes the silent artifact-analysis fallback on remote deployments. Users authoring skills against a Q2 PPT on a remote OCI server get correct slide-structure-derived fields.
- `analyze_artifact` unchanged — no regression risk on the analysis logic.
- Dual-mode storage mirrors the established `SessionStore` pattern. Developers already familiar with that pattern will find the `ArtifactStore` immediately legible.
- Artifact bytes never enter the session dict or ADB — session persistence layer is unaffected.
- Cleanup is tied to session lifecycle, so uploads don't accumulate indefinitely.

### Negative / trade-offs

- A 10 MB PPT must be base64-encoded (13.3 MB over the wire) within the MCP text protocol. This is one-time per session, not per turn, but it is a noticeable payload.
- OCI Object Storage adds a new SDK dependency (`oci.object_storage.ObjectStorageClient`) for the production path. On laptop, this dependency is never exercised.
- The `OciArtifactStore.resolve()` call downloads the file to the VM's local temp area on each resolution. For the current single-instance deployment this is fine; multi-instance deployments would need shared storage or sticky sessions (not a concern for v1).
- `SkillBuilderConversation` gains an `artifact_store` constructor parameter, increasing the constructor surface. This is a backward-compatible addition (default `None`).

### Reversibility

High. The `artifact_store` abstraction is cleanly layered. The OCI backend can be swapped for any S3-compatible store by changing `factory.py`. The `_handle_analyze_artifact` change is a 5-line prefix check; removing it reverts to the original behaviour.

---

## Alternatives considered

### Alternative A — Client-side path substitution

Require the LLM client to copy the file to a known shared path on the server before calling `authorSkill`. Rejected: there is no shared filesystem between a laptop LLM client and an OCI VM. Requires SSH or SCP out-of-band from the MCP protocol, which is not possible for all clients.

### Alternative B — Inline artifact content in authorSkill input

Pass the base64 content directly in the `authorSkill` input field (`input: "artifact:<base64...>"`). The `input` field has a `maxLength: 4096` constraint in the current schema. A 10 MB file base64-encodes to 13.3 MB, which is more than 3000x over the limit. Rejected: would require raising the input cap to an unsafe level and would bloat session dicts.

### Alternative C — Separate HTTP upload endpoint (non-MCP)

Add a `POST /api/v1/kb/artifacts` REST endpoint outside the MCP protocol. Rejected: LLM clients in the MCP model cannot make arbitrary HTTP calls outside tool invocations. The upload must be a tool call to remain within the MCP interaction model.

### Alternative D — OCI Object Storage only (no filestore mode)

Skip the dual-mode design and always use OCI Object Storage. Rejected: this breaks laptop mode entirely (OCI credentials, bucket config, and network access to OCI are not required for local development). The existing `SessionStore` precedent justifies the dual-mode pattern.

### Alternative E — Presigned URL upload (client pushes directly to OCI)

Server generates a presigned PutObject URL; client uploads directly to OCI Object Storage without the server as an intermediary. Rejected for v1 because: (a) LLM clients (Claude Code, Codex) do not have HTTP PUT capability outside of tool calls; (b) presigned URL generation requires OCI SDK on the server anyway; (c) the indirection adds a round-trip without reducing server complexity for the current single-client use case.

---

## Open questions (resolved by DECISION-005)

1. **OCI bucket name**: should the artifact bucket be the same as `kb-raw-dev` / `kb-raw-prod` (existing object storage bucket) or a dedicated `kbf-uploads` bucket? A dedicated bucket makes lifecycle rule scoping cleaner and avoids cross-contaminating audit artifacts with transient uploads. Recommendation: dedicated `kbf-uploads` bucket.
2. **OCI region**: the project currently uses `eu-frankfurt-1` for ADB and GenAI (laptop.yaml confirmed). Should the artifact bucket be in the same region? Recommendation: yes, same region.
3. **Namespace**: tenancy namespace is already in `dev.yaml:object_storage.namespace`. Confirm the same namespace for the upload bucket.

See `pmo/decisions/DECISION-005-artifact-store-oci-config.md`.

---

## References

- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md) — defines `ANALYZE_ARTIFACT` state and `analyze_artifact()` contract
- [ADR-019 — Bastion auto-reconnect](ADR-019-bastion-auto-reconnect.md) — lifespan wiring pattern; `artifact_store` follows same approach
- [ADR-020 — Codex CLI MCP transport](ADR-020-codex-cli-mcp-transport.md) — remote deployment topology that exposed this gap
- [ADR-010 — Configuration plane](ADR-010-configuration-plane.md) — env YAML structure; `artifact_store:` section follows same schema
- [DECISION-005 — Artifact store OCI config](../../pmo/decisions/DECISION-005-artifact-store-oci-config.md)
- OCI Object Storage Python SDK: https://docs.oracle.com/en-us/iaas/tools/python/latest/sdk/object-storage.html
- OCI Object Storage lifecycle rules: https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usinglifecyclepolicies.htm
- `framework/deploy/session/` — existing SessionStore pattern this design mirrors
- `framework/skill_builder/conversation.py` lines 328–346 — `_handle_analyze_artifact` call site
- `framework/skill_builder/analyze_artifact.py` — unchanged; reads local Path, returns (fields, mapping)
