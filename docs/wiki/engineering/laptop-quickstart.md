---
title: Laptop Quickstart — Run the framework end-to-end with no provisioning
created: 2026-05-10
owner: dev-manager
tags: [engineering, quickstart, laptop, v2]
status: current
---

# Laptop Quickstart

How to run the framework on your laptop **without** Oracle 23ai ADB, OCI Vault, OpenAI key, or any provisioning. Everything falls back to local files + stub LLM. Useful for:

- Verifying the framework's pipeline shape end-to-end
- Authoring a workflow skill from an intent file and watching it produce a real artifact
- Demoing the V2 design without waiting on infrastructure

What WILL work:
- ✓ Author a new workflow skill via `kb-cli skill-builder --intent-file ...`
- ✓ Run `kb-cli workflow-run <skill> --inputs '{...}'` and produce a real PPT/DOCX/Markdown
- ✓ The shipped starter skills `ops_eng.incident_summary` and `pm.release_brief`
- ✓ FilestoreContentStore (JSON-on-disk; lexical search instead of vector)

What WON'T fully work without provisioning:
- ✗ Real Confluence/Jira ingestion (use bundled fixture data instead)
- ✗ Real vector similarity (filestore uses Jaccard token overlap)
- ✗ Real LLM-quality synthesis (stub returns templated output)
- ✗ Eval against real gold sets (gold sets are placeholders)

Phase 1 will plug all the above in once ADB + OpenAI/OCI GenAI are provisioned.

---

## 0. Prerequisites

```bash
python --version    # ≥ 3.12
cd /Users/sravansunkaranam/github/Knowledgebase
```

Optional but recommended for full PPT/DOCX rendering:
```bash
pip install python-pptx python-docx pyyaml
```

(Without these, renderers produce stub-bytes that document what they would have rendered — useful for shape-testing.)

---

## 1. Bootstrap laptop dev mode

```bash
python -m framework.cli.kb_cli laptop-init
```

What this does:
- Creates `~/.kbf/` directory (mode 700)
- Copies `framework/.secrets.local.yaml.example` → `~/.kbf/secrets.yaml` (mode 600)
- Creates `~/.kbf/store/` (filestore content store root)
- Creates `~/.kbf/outputs/` (artifact output destination)
- Creates `~/.kbf/outbox/` (email "send" archive)
- Creates `~/.kbf/slack-outbox/` (Slack "send" archive)

Then add to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export KBF_ENV=dev
export KBF_SECRETS_BACKEND=local
export KBF_SECRETS_FILE=$HOME/.kbf/secrets.yaml
export KBF_STORE_BACKEND=filestore
export KBF_STORE_ROOT=$HOME/.kbf/store
export KBF_LLM_PROVIDER=stub
```

(Your `~/.kbf/secrets.yaml` will start with placeholders. Fill in real values when you provision.)

---

## 2. List registered workflow skills

```bash
python -m framework.cli.kb_cli skill-list
```

Expected output (after laptop-init):

```
Name                                     Persona         Status     Triggers
------------------------------------------------------------------------------------------
incident_summary                         ops_eng         draft      on_request
release_brief                            pm              draft      on_request
```

---

## 3. Run a starter workflow skill

### 3a. Ops Eng incident summary (Markdown output)

```bash
python -m framework.cli.kb_cli workflow-run ops_eng.incident_summary \
    --inputs '{"incident_id": "INC-EXAMPLE-001"}' \
    --show-data
```

Expected:
- Reads fixture data from `framework/_dev_fixtures/incidents/INC-EXAMPLE-001.json`
- Synthesizes a structured Markdown summary (Root_Cause / Resolution / Severity / etc.)
- Writes to `~/.kbf/outputs/incident-summary-INC-EXAMPLE-001.md`
- Prints the rendered data + delivery URL

```bash
$ open ~/.kbf/outputs/incident-summary-INC-EXAMPLE-001.md
```

You should see a clean Markdown summary of the example incident.

### 3b. PM release brief (DOCX output)

```bash
python -m framework.cli.kb_cli workflow-run pm.release_brief \
    --inputs '{"release_id": "25.01"}' \
    --show-data
```

Expected:
- Reads `framework/_dev_fixtures/releases/REL-25.01.json`
- Renders a DOCX brief
- Writes to `~/.kbf/outputs/release-brief-25.01.docx`

```bash
$ open ~/.kbf/outputs/release-brief-25.01.docx
```

(If you don't have `python-docx` installed, you'll get a stub-bytes file — install via `pip install python-docx` and re-run.)

---

## 4. Author a new workflow skill via skill-builder

```bash
python -m framework.cli.kb_cli skill-builder \
    --intent-file framework/_dev_fixtures/skill_builder_intents/example_workflow.yaml \
    --dry-run
```

This shows what the skill builder would synthesize WITHOUT writing files. Drop `--dry-run` to actually commit the artifacts.

The example intent file describes a TPM weekly exec-review skill. The skill builder will:
1. Infer required fields from the example outcome (week_id, rag_status, top_milestones, blockers, exec_asks)
2. Detect that none of them exist in any current KB → "new extraction needed"
3. Synthesize:
   - JSON-Schema for the new extraction at `framework/parsers/schemas/tpm/weekly_project_status_ppt/v1.json`
   - Persona-builder diff for `framework/persona_builders/tpm.yaml`
   - Workflow skill at `framework/workflow_skills/tpm/weekly_project_status_ppt.yaml`
   - Synthesis mapping
   - Gold-set seeds

Drop `--dry-run` to commit the artifacts.

---

## 5. Author your own intent file

Create `~/my_intent.yaml`:

```yaml
persona: ops_eng
task_description: "Generate a weekly digest of unresolved incidents per service"

sources:
  - kind: jira
    jql: 'project IN (OPS, P2T) AND status != "Resolved"'

example_outcome:
  kind: structured
  fields:
    week_id: "2026-W19"
    unresolved_count: 12
    by_service:
      auth-service: 3
      payments: 5
      customer-events: 4
    longest_open_days: 21

trigger:
  on_request: true
  inputs:
    - { name: week_id, type: string }

output_format: markdown

delivery:
  kind: filesystem
  path: "~/.kbf/outputs/unresolved-incidents-{week_id}.md"
```

Then synthesize:

```bash
python -m framework.cli.kb_cli skill-builder --intent-file ~/my_intent.yaml --dry-run
```

Inspect what would be created. If you like it, drop `--dry-run`.

---

## 6. What happens when you provision real infra

The framework is designed so that provisioning lights up paths without code changes:

| Component | Today (laptop) | After provisioning |
|---|---|---|
| Secrets | `~/.kbf/secrets.yaml` | OCI Vault (set `KBF_SECRETS_BACKEND=vault`) |
| Content store | FilestoreContentStore (JSON files, lexical search) | IncidentVectorStore (Oracle 23ai ADB, real vector similarity) |
| LLM | Stub (templated) | OCI GenAI Inference (per ADR-014) — set `KBF_LLM_PROVIDER=oci_genai` and provide endpoint URL |
| Adapters | Read fixture JSON files | Real Confluence/Jira REST or MCP |
| Output delivery (filesystem) | Local path | Could swap to OCI Object Storage (`delivery.kind: oci_object_storage`) |
| Email delivery | Filesystem outbox | OCI Email Delivery |

You change config, not code.

---

## 7. Troubleshooting

**"No module named 'framework'"** — run from project root, e.g. `cd /Users/sravansunkaranam/github/Knowledgebase` first.

**"unknown workflow skill: X"** — run `kb-cli skill-list` to see registered skills.

**"unresolved:openai-api-key"** — laptop mode uses stub LLM by default; this is expected. If you set `KBF_LLM_PROVIDER=openai_direct` you need a real key in `~/.kbf/secrets.yaml`.

**Stub PPTX/DOCX bytes in output** — you need `pip install python-pptx python-docx` for real rendering.

**Old artifact in output** — delete from `~/.kbf/outputs/` and re-run; deliverer overwrites by default.

---

## 8. What persona teams can do tomorrow

Following the user's question — *"can I use this tomorrow for ops and PM?"* — yes, with these caveats:

1. The starter `ops_eng.incident_summary` and `pm.release_brief` skills are real and runnable on fixture data
2. To demo on real data, replace fixtures with actual JSON exports from your Jira/Confluence (until adapters are wired in Phase 1+)
3. To author new skills, use `kb-cli skill-builder --intent-file` (conversational mode is Phase 3 polish)
4. To get production-quality LLM synthesis, provide an OCI GenAI URL (per ADR-014) or OpenAI key — laptop mode runs with stubs

Tomorrow's session can iterate on:
- Refining the starter skills' synthesis mappings (look at `framework/synthesis/mappings/`)
- Authoring 1-2 new workflow skills from intent files
- Showing the rendered output to PM/Ops Eng leads to validate the format

---

## 9. References

- PDD V2: [`docs/wiki/pdd/PDD-Knowledge-Builder-Framework-v2.md`](../pdd/PDD-Knowledge-Builder-Framework-v2.md)
- AIRA comparison: [`docs/wiki/aira-comparison.md`](../aira-comparison.md)
- Code-access story: [`docs/wiki/code-access-story.md`](../code-access-story.md)
- Onboarding workbooks: [`docs/wiki/onboarding/`](../onboarding/)
- ADRs: [`docs/wiki/adr/`](../adr/)
- Autonomous run log: [`pmo/AUTONOMOUS-RUN-2026-05-09.md`](../../../pmo/AUTONOMOUS-RUN-2026-05-09.md)
