---
id: DECISION-005
title: OCI Object Storage configuration for artifact upload bucket
status: resolved
created: 2026-05-12
decided: 2026-05-12
resolved: 2026-05-12
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

**Tenancy namespace**: `axq4m61mcei3` — confirmed 2026-05-12 by user via OCI Console.

**Bucket confirmed:**
- Name: `kbf-uploads`
- OCID: `ocid1.bucket.oc1.eu-frankfurt-1.aaaaaaaah3kxe5ldbr5sny5phrtidoaxgorfbusqxojdnicpf46lxe6ckqrq`
- Namespace: `axq4m61mcei3`
- Compartment OCID: `ocid1.compartment.oc1..aaaaaaaax7wbfdtfl7axhfae7q5lwvrmf2nlcdii3scarukqmuos7u5mokla`
- Compartment name: `adp_faops_network`
- Region: `eu-frankfurt-1`
- Public access: None
- Versioning: Disabled
- Lifecycle rule: None (application-driven cleanup via `artifact_store.cleanup(synth_id)`)
- Created by: `ssunkara`, 2026-05-12

All three config files (`dev.yaml`, `staging.yaml`, `prod.yaml`) updated with real namespace + compartment OCID. `OciArtifactStore` updated to auto-discover namespace via SDK `get_namespace()` in production (InstancePrincipals) so the config value is a safe fallback, not a hard requirement.

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
  --namespace axq4m61mcei3 \
  --bucket-name kbf-uploads \
  --name test-probe/probe.txt \
  --profile adpcpprod --auth security_token \
  --file /dev/stdin <<< "probe"

oci os object get \
  --namespace axq4m61mcei3 \
  --bucket-name kbf-uploads \
  --name test-probe/probe.txt \
  --profile adpcpprod --auth security_token \
  --file /dev/stdout

oci os object delete \
  --namespace axq4m61mcei3 \
  --bucket-name kbf-uploads \
  --name test-probe/probe.txt \
  --profile adpcpprod --auth security_token \
  --force
```

**Status: ✅ RESOLVED** — bucket exists, namespace confirmed, configs updated.

---

## Deliver to agents

**Resolved. Set these env vars on the OCI VM if you want to override the config:**

```
KBF_ARTIFACT_OCI_NAMESPACE=axq4m61mcei3
KBF_ARTIFACT_OCI_BUCKET=kbf-uploads
KBF_ARTIFACT_OCI_REGION=eu-frankfurt-1
```

These are already populated in `prod.yaml` and `staging.yaml`. In production with InstancePrincipals, `OciArtifactStore` auto-discovers the namespace via `get_namespace()` as a fallback — env var / config takes precedence.
