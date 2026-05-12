---
id: DECISION-005
title: OCI Object Storage configuration for artifact upload bucket
status: decided
created: 2026-05-12
decided: 2026-05-12
owner: architect
tags: [oci, storage, deploy, skill-builder]
related: [ADR-021]
---

# DECISION-005 — OCI Object Storage configuration for artifact upload bucket

## Context

ADR-021 introduces dual-mode artifact storage for the `uploadArtifact` MCP tool. In `staging` and `production` environments the server stores uploaded files (PPT/DOCX/MD/TXT) in OCI Object Storage before analysis, and deletes them when the authoring session completes.

Three configuration values must be confirmed before Backend Dev can implement `OciArtifactStore` and populate `staging.yaml` / `prod.yaml`:

1. **OCI tenancy namespace** — used in every Object Storage API call.
2. **Bucket name** — where uploads live. Options: a dedicated `kbf-uploads` bucket, or a sub-prefix inside the existing `kb-raw-dev` / `kb-raw-prod` buckets.
3. **Region** — OCI region for the bucket. The project already uses `eu-frankfurt-1` for ADB and GenAI; the bucket should be co-located to avoid cross-region egress.

---

## Options

### Option A — Dedicated `kbf-uploads` bucket (recommended)

Create a new bucket `kbf-uploads` (one per environment: `kbf-uploads-dev`, `kbf-uploads-staging`, `kbf-uploads-prod`, or just `kbf-uploads` with env-prefix in object keys).

- Lifecycle rule: delete any object older than 7 days. Scoped to the whole bucket — simple, no prefix filtering needed.
- IAM policy: instance principal on the OCI VM gets `manage objects` on `kbf-uploads` only. No read access to audit/eval artifacts in `kb-raw-*`.
- Namespace: same tenancy namespace as existing `object_storage` config in dev.yaml.
- Region: `eu-frankfurt-1` (same as ADB + GenAI).

**Pros:** Clean lifecycle rule. Least-privilege IAM. Audit artifacts in `kb-raw-*` cannot be accidentally touched by the upload IAM policy. Easy to measure storage cost for uploads separately.

**Cons:** One more OCI resource to create (bucket + lifecycle rule + IAM statement). Estimated time: 10 minutes in OCI Console.

### Option B — Sub-prefix in existing `kb-raw-*` bucket

Use the existing `kb-raw-dev` / `kb-raw-prod` bucket with object key prefix `kbf-uploads/`. The lifecycle rule targets only objects matching `kbf-uploads/*` prefix.

- Namespace + bucket: already defined in dev.yaml `object_storage` section.
- Lifecycle rule: prefix-scoped to `kbf-uploads/`, TTL 7 days.
- IAM: the existing instance principal policy that grants `kb-raw-*` access already covers this — no new IAM statement needed.

**Pros:** No new OCI resource to create. One fewer bucket to manage.

**Cons:** OCI Object Storage lifecycle rules that filter on prefix are slightly more complex to configure. Uploads share IAM surface with audit / eval artifacts (less separation of concerns). Storage billing for uploads appears inside the existing bucket, not separately visible.

### Option C — Use OCI Pre-Authenticated Requests (PAR)

Server generates a Pre-Authenticated Request URL for each upload; the MCP tool redirects the client to upload directly. Rejected in ADR-021 (LLM clients cannot make out-of-band HTTP PUT calls). Listed here for completeness.

---

## Decision

**Option A — dedicated `kbf-uploads` bucket. Decided 2026-05-12.**

User confirmation:
- **Lifecycle rule**: None — no auto-cleanup. OCI Object Storage is cheap; cleanup is a v2 concern.
- **Auth (laptop)**: OCI CLI subprocess (`--auth security_token --profile adpcpprod`).
- **Auth (production)**: OCI Python SDK with instance principals.
- **Compartment**: `adp_faops_network` compartment.
- **Region**: `eu-frankfurt-1` (co-located with ADB + GenAI).
- **Bucket name**: `kbf-uploads`.

**Tenancy namespace**: Needs to be confirmed by user via OCI Console → Object Storage → any bucket → "Namespace" field. The `adpcpprod` OCI CLI profile uses operator-access auth and returned namespace `bmc_operator_access`, which is the operator namespace — not the user-tenant namespace where `kbf-uploads` should be created. Once confirmed, populate `prod.yaml:artifact_store.oci.namespace` and `KBF_ARTIFACT_OCI_NAMESPACE` env var.

---

## Bucket creation steps (10 min, OCI Console or CLI from the OCI VM)

See §Setup steps below. The bucket must be created in `adp_faops_network` compartment. IAM: add one policy statement allowing the MCP server's compute dynamic group to `manage objects` on `kbf-uploads`.

---

## Setup steps (~10 minutes, OCI Console)

1. **Create the bucket**
   - OCI Console → Object Storage → Buckets → Create Bucket
   - Compartment: `adp_faops_network`
   - Name: `kbf-uploads`
   - Visibility: Private
   - Versioning: Disabled (uploads are ephemeral, versioning adds cost)
   - No lifecycle rule needed.

2. **Add an IAM policy statement** (instance principal on OCI VM)
   - Navigate to IAM → Policies → your existing KBF instance principal policy
   - Add one statement:
     ```
     Allow dynamic-group kbf-compute to manage objects
       in compartment adp_faops_network
       where target.bucket.name = 'kbf-uploads'
     ```

3. **Confirm namespace**
   - OCI Console → Object Storage → any bucket → copy the "Namespace" value.
   - Populate `prod.yaml:artifact_store.oci.namespace` and set `KBF_ARTIFACT_OCI_NAMESPACE` env var on the OCI VM.

---

## Done when

The following can be verified by Backend Dev before implementing `OciArtifactStore`:

```bash
oci os object put \
  --namespace <namespace> \
  --bucket-name kbf-uploads \
  --name test-probe/probe.txt \
  --file /dev/stdin <<< "probe"

oci os object get \
  --namespace <namespace> \
  --bucket-name kbf-uploads \
  --name test-probe/probe.txt \
  --file /dev/stdout

oci os object delete \
  --namespace <namespace> \
  --bucket-name kbf-uploads \
  --name test-probe/probe.txt \
  --force
```

All three commands succeed from the OCI VM (or from your laptop using `adpcpprod` profile).

---

## Deliver to agents

Once bucket is created and namespace is confirmed, provide:

```
KBF_ARTIFACT_OCI_NAMESPACE=<tenancy-namespace>
KBF_ARTIFACT_OCI_BUCKET=kbf-uploads
KBF_ARTIFACT_OCI_REGION=eu-frankfurt-1
```

Or equivalently, in `prod.yaml`:

```yaml
artifact_store:
  mode: oci
  max_file_size_mb: 10
  oci:
    namespace: <tenancy-namespace>
    bucket: kbf-uploads
    region: eu-frankfurt-1
```

Note: no `ttl_days` in prod.yaml — there is no lifecycle rule on the bucket. Cleanup is application-driven via `artifact_store.cleanup(synth_id)`.

Backend Dev needs `namespace` confirmed to wire `OciArtifactStore` and populate the staging + prod configs.
