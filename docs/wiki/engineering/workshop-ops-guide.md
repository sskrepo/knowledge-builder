---
title: "Workshop Operations Guide — Running the Framework + Natural Language Gold-Set Interface"
created: 2026-05-10
owner: architect
tags: [engineering, workshop, gold-set, operations, v2]
status: current
---

# Workshop Operations Guide

This document covers two things:

1. **How to run the application** — end-to-end setup for the facilitator's laptop
2. **How to stand up the natural language gold-set feeder** — so persona teams can populate eval gold sets during workshops without touching JSONL files

---

## Part 1: Running the Application

### 1.1 Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.9+ | macOS ships 3.9; 3.12 recommended |
| PyYAML | any | `pip3 install pyyaml` |
| python-pptx | optional | For real PPTX rendering (otherwise stub bytes) |
| python-docx | optional | For real DOCX rendering |
| Git | any | For the repo |

No Oracle ADB, no OCI Vault, no OpenAI key, no Confluence/Jira tokens. Everything runs in **filestore + stub LLM** mode.

### 1.2 One-Time Setup (5 minutes)

```bash
# 1. Clone and enter the repo
cd /Users/sravansunkaranam/github/Knowledgebase
# If using a worktree:
# cd .claude/worktrees/agitated-villani-eeed95

# 2. Install dependencies
pip3 install pyyaml python-pptx python-docx

# 3. Bootstrap local dev environment
python3 -m framework.cli.kb_cli laptop-init
```

This creates `~/.kbf/` with:
```
~/.kbf/
├── secrets.yaml       # Placeholder secrets (mode 600)
├── store/             # Filestore content store
├── outputs/           # Workflow skill outputs land here
├── outbox/            # Email delivery archive
└── slack-outbox/      # Slack delivery archive
```

### 1.3 Shell Environment

Add to `~/.zshrc` or `~/.bashrc` (or export in the terminal for the workshop):

```bash
export KBF_ENV=dev
export KBF_SECRETS_BACKEND=local
export KBF_SECRETS_FILE=$HOME/.kbf/secrets.yaml
export KBF_STORE_BACKEND=filestore
export KBF_STORE_ROOT=$HOME/.kbf/store
export KBF_LLM_PROVIDER=stub
```

Verify:

```bash
python3 -m framework.cli.kb_cli skill-list
```

Expected:
```
Name                                     Persona         Status     Triggers
------------------------------------------------------------------------------------------
incident_summary                         ops_eng         draft      on_request
release_brief                            pm              draft      on_request
weekly_exec_review                       tpm             draft      on_request,on_schedule
```

### 1.4 Command Reference

| Command | What it does | When to use |
|---|---|---|
| `kb-cli laptop-init` | Bootstrap `~/.kbf/` directories and secrets template | Once, at setup |
| `kb-cli skill-list` | Show all registered workflow skills | Verify setup, demo |
| `kb-cli workflow-run <persona>.<skill> --inputs '{...}'` | Execute a workflow skill and produce output | Demo, validation |
| `kb-cli skill-builder --persona <p>` | Interactive skill authoring (conversational) | Workshop Activity 3 |
| `kb-cli skill-builder --intent-file <yaml> [--dry-run]` | Batch skill authoring from intent file | Pre-workshop prep |
| `kb-cli gold-feed --persona <p> [--skill <s>]` | Interactive gold-set feeding (NL interface) | Workshop gold-set session |
| `kb-cli code-wiki-build` | Build structural code wiki index | Developer onboarding |
| `kb-cli validate <persona-builder.yaml>` | Lint a persona builder config | Before promote |
| `kb-cli promote <persona-builder.yaml> --validate-links` | Promote to production with ADR-017 link check | After eval gate passes |

### 1.5 Running a Demo (10 minutes)

The facilitator should run these live to show what the framework produces:

```bash
# 1. Incident summary (Markdown)
python3 -m framework.cli.kb_cli workflow-run ops_eng.incident_summary \
    --inputs '{"incident_id": "INC-EXAMPLE-001"}' --show-data

# Open the output
open ~/.kbf/outputs/incident-summary-INC-EXAMPLE-001.md

# 2. Release brief (DOCX)
python3 -m framework.cli.kb_cli workflow-run pm.release_brief \
    --inputs '{"release_id": "25.01"}' --show-data

open ~/.kbf/outputs/release-brief-25.01.docx

# 3. Weekly exec review (PPTX)
python3 -m framework.cli.kb_cli workflow-run tpm.weekly_exec_review \
    --inputs '{"project": "all"}' --show-data

open ~/.kbf/outputs/weekly-exec-review-all.pptx
```

Each command reads fixture data from `framework/_dev_fixtures/`, runs it through the workflow runtime (source resolution → data gathering → rendering → delivery), and writes a real file.

### 1.6 Transitioning to Production

When provisioning is ready, switch env vars — no code changes:

| Env var | Laptop value | Production value |
|---|---|---|
| `KBF_SECRETS_BACKEND` | `local` | `vault` |
| `KBF_STORE_BACKEND` | `filestore` | `adb` |
| `KBF_LLM_PROVIDER` | `stub` | `oci_genai` or `openai_direct` |
| `KBF_SECRETS_FILE` | `~/.kbf/secrets.yaml` | (not used with vault) |
| `KBF_STORE_ROOT` | `~/.kbf/store` | (not used with ADB) |

Plus:
```bash
kb-cli migrate --schema kb_incidents --env dev   # Deploy DDL
kb-cli ingest framework/persona_builders/ops-eng.yaml  # Real data
kb-cli eval framework/persona_builders/ops-eng.yaml     # Eval gate
```

---

## Part 2: Natural Language Gold-Set Interface

### 2.1 The Problem

The eval harness (spec §12, ADR-005) requires gold sets — curated query/citation pairs that measure retrieval quality. The exit gate is:

- **≥25 entries** per persona
- **≥80% recall@5** on the gold set
- **≥0.85 faithfulness** on the gold set

Persona teams own these gold sets, but they shouldn't need to hand-edit JSONL. They know their domain; they know what questions they ask and where the answers live. We need a natural language interface that captures this knowledge conversationally.

### 2.2 The Gold-Set Feeder

The `gold-feed` CLI command provides a conversational interface for persona teams to populate gold sets. It works entirely in stub mode — no LLM, no database.

#### Starting a session

```bash
python3 -m framework.cli.kb_cli gold-feed --persona ops_eng
```

Or with a specific skill:
```bash
python3 -m framework.cli.kb_cli gold-feed --persona ops_eng --skill incident_summary
```

#### What the conversation looks like

```
$ python3 -m framework.cli.kb_cli gold-feed --persona ops_eng

Gold-Set Feeder for persona: ops_eng
Target: 25 entries (currently 0 in gold set)

Let's build your gold set. For each entry, I need:
  1. A question your team actually asks (natural language)
  2. Where the answer lives (source citations)
  3. Key fields that should appear in the answer (optional)

Entry 1 of 25. What question does your team ask?
> What was the root cause of the pod refresh failure last Tuesday?

Good question. Where should the answer come from?
(Jira ticket URLs, Confluence pages, runbook paths — comma-separated)
> jira://OPS-4521, confluence://OPS/Post-mortem-2026-W19

Any specific fields that must appear in the answer? (key=value pairs, or 'skip')
> severity=P1, root_cause=OOM in refresh controller, resolution=memory limit increase

Here's your entry:
  Question:  "What was the root cause of the pod refresh failure last Tuesday?"
  Citations: jira://OPS-4521, confluence://OPS/Post-mortem-2026-W19
  Fields:    severity=P1, root_cause=OOM in refresh controller
  
  OK to save? (yes / edit question / edit citations / edit fields)
> yes

✓ Entry 1 saved. (1/25 toward goal)

Entry 2 of 25. Next question? (or 'done' to finish)
> How many P1 incidents hit the auth-service this quarter?
...
```

#### When the session ends

```
✓ Session complete. 8 entries saved to:
  framework/eval/gold_sets/ops_eng.jsonl

Progress: 8/25 entries (32%). Run another session to continue:
  python3 -m framework.cli.kb_cli gold-feed --persona ops_eng

When you reach 25 entries, run:
  python3 -m framework.cli.kb_cli eval framework/persona_builders/ops-eng.yaml
```

### 2.3 Gold-Set Entry Format

Each entry in the JSONL file:

```json
{
  "id": "gs-a1b2c3d4",
  "persona": "ops_eng",
  "question": "What was the root cause of the pod refresh failure last Tuesday?",
  "expected_citations": ["jira://OPS-4521", "confluence://OPS/Post-mortem-2026-W19"],
  "expected_fields": {
    "severity": "P1",
    "root_cause": "OOM in refresh controller",
    "resolution": "memory limit increase"
  },
  "must_match_fields": ["severity", "root_cause"],
  "kb": "ops_eng.ops_incidents",
  "skill": "incident_summary",
  "notes": "",
  "added_by": "workshop",
  "added_at": "2026-05-10T14:30:00Z"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | auto | Unique hash-based ID |
| `persona` | string | yes | Persona identifier |
| `question` | string | yes | Natural language query the persona would ask |
| `expected_citations` | string[] | yes | Source URLs/paths that should appear in the answer |
| `expected_fields` | object | no | Key-value pairs that should be in the answer |
| `must_match_fields` | string[] | auto | Subset of fields used for field-accuracy scoring |
| `kb` | string | auto | Knowledge base identifier (persona.kb_name) |
| `skill` | string | no | Associated workflow skill name |
| `notes` | string | no | Free-text notes from the contributor |
| `added_by` | string | auto | "workshop" or contributor identifier |
| `added_at` | string | auto | ISO 8601 timestamp |

### 2.4 How the Gold Set Connects to Eval

```
┌─────────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Workshop session    │     │  gold_sets/           │     │  Eval runner     │
│  (gold-feed CLI)     │────▶│  ops_eng.jsonl        │────▶│  (kb-cli eval)   │
│                      │     │  pm.jsonl             │     │                  │
│  Persona teams type  │     │  tpm.jsonl            │     │  recall@5        │
│  questions + citations│    │                       │     │  faithfulness    │
└─────────────────────┘     └──────────────────────┘     │  latency         │
                                                          │  cost            │
                                                          └─────────────────┘
                                                                   │
                                                          ┌────────▼────────┐
                                                          │  Exit gate:      │
                                                          │  recall ≥ 0.80   │
                                                          │  faith  ≥ 0.85   │
                                                          │  25+ entries     │
                                                          └─────────────────┘
```

The eval runner (`framework/eval/runner.py`) iterates every entry in the gold set:
1. Sends the `question` to the context builder
2. Checks if `expected_citations` appear in the retrieved results (recall@k)
3. Checks if `expected_fields` appear in the synthesized answer (field accuracy)
4. Measures latency and token cost

### 2.5 Workshop Session Plan for Gold-Set Feeding

**When:** After the skill-authoring workshop (Activity 3 in the workshop guide). Can be same day or a follow-up session.

**Duration:** 30-45 minutes for 10-15 entries. Schedule two sessions to reach 25.

**Who attends:**
- Facilitator (runs the CLI)
- 2-3 domain experts from the persona team (provide the questions)
- Schema owner (validates citations)

**Agenda:**

| Time | Activity | Notes |
|---|---|---|
| 0-5 min | Setup — confirm env vars, show current gold set count | `kb-cli gold-feed --persona ops_eng` shows progress |
| 5-10 min | Demo — facilitator types 2 example entries live | Shows the conversational flow |
| 10-35 min | Feeding — domain experts dictate questions, facilitator types | Aim for 10-15 entries per session |
| 35-40 min | Review — `cat framework/eval/gold_sets/ops_eng.jsonl \| python3 -m json.tool` | Quick sanity check on the entries |
| 40-45 min | Wrap-up — show progress, schedule follow-up if < 25 entries | Assign schema owner to add remaining entries async |

**Facilitator tips:**
- Ask "What question do you ask every Monday?" — recurring questions make the best gold entries
- Ask "Where do you go to find the answer?" — this surfaces the citations naturally
- Don't worry about exact field values — the eval will be run against real data later
- If the team gives vague citations like "somewhere in Confluence", press for space key + page title
- One good entry with precise citations beats three vague ones

### 2.6 Per-Persona Question Prompts

Use these prompts to help persona teams think of gold-set questions:

**Ops Engineering:**
- "What incidents hit [service] in the last [N] weeks?"
- "What's the root cause pattern for [failure type]?"
- "Show me the runbook for [procedure]."
- "What was the resolution for [incident ID]?"
- "Which pods are affected by [issue]?"
- "What's the blast radius of [change]?"

**PM:**
- "What shipped in release [version]?"
- "What's the status of [feature] across teams?"
- "Which requirements changed since last review?"
- "What's blocking the [milestone] deadline?"

**TPM:**
- "Give me the weekly ops summary for [project]."
- "What's the executive review deck for this week?"
- "Which projects are red/amber and why?"
- "What decisions are pending across all projects?"

**Architect:**
- "What ADRs are relevant to [component]?"
- "What's the interface contract for [service]?"
- "Which modules depend on [library]?"

### 2.7 Checking Progress

At any time, check how many entries exist:

```bash
# Count entries in a gold set
wc -l framework/eval/gold_sets/ops_eng.jsonl

# Pretty-print the last entry
tail -1 framework/eval/gold_sets/ops_eng.jsonl | python3 -m json.tool

# Count entries across all personas
wc -l framework/eval/gold_sets/*.jsonl
```

The gold-feed CLI also shows progress at the start of each session and after each entry.

### 2.8 After Gold Sets Are Populated

Once a persona reaches 25+ entries:

```bash
# 1. Validate the persona builder
python3 -m framework.cli.kb_cli validate framework/persona_builders/ops-eng.yaml

# 2. Run eval (requires real ADB + LLM — stub mode returns placeholder metrics)
python3 -m framework.cli.kb_cli eval framework/persona_builders/ops-eng.yaml

# 3. If gates pass, promote
python3 -m framework.cli.kb_cli promote framework/persona_builders/ops-eng.yaml --validate-links
```

---

## Part 3: Architecture of the NL Interface

### 3.1 Component Map

```
framework/
├── cli/
│   └── kb_cli.py              # gold-feed subcommand wired here
├── eval/
│   ├── gold_set_feeder.py     # GoldSetFeeder state machine
│   ├── gold_sets/             # JSONL files (one per persona)
│   │   ├── ops_eng.jsonl
│   │   ├── pm.jsonl
│   │   └── tpm.jsonl
│   ├── runner.py              # Reads gold sets, runs eval
│   └── metrics/               # recall, faithfulness, latency
└── skill_builder/
    ├── conversation.py        # Skill authoring (separate concern)
    └── gold_seed.py           # Starter entries from skill synthesis
```

### 3.2 GoldSetFeeder State Machine

```
INIT ──► ENTRY ──► CITATION ──► EXPECTED_FIELDS ──► REVIEW ──► NEXT
                                                                 │
                                                    ┌────────────┘
                                                    │
                                                    ├── user types question → ENTRY
                                                    └── user types "done" → DONE
```

- **INIT**: Loads existing gold set, shows progress toward 25-entry goal
- **ENTRY**: Accepts a natural language question
- **CITATION**: Accepts source citations (URLs, Jira keys, Confluence paths)
- **EXPECTED_FIELDS**: Accepts key=value pairs or "skip"
- **REVIEW**: Shows the assembled entry for confirmation/editing
- **NEXT**: Loops back to ENTRY or exits to DONE
- **DONE**: Writes accumulated entries to JSONL (append mode)

### 3.3 Integration Points

| Component | Integration | Notes |
|---|---|---|
| `kb-cli` | `gold-feed` subcommand | Mirrors `skill-builder` pattern |
| `eval/runner.py` | Reads `gold_sets/{persona}.jsonl` | Existing — no changes needed |
| `skill_builder/gold_seed.py` | Seeds starter entries during skill synthesis | Existing — feeds into same JSONL |
| `promote` command | Checks gold set exists and has ≥25 entries | Could be added as a pre-promote check |

### 3.4 No LLM Required

The gold-set feeder is entirely template-driven. It parses user input with regex (citation patterns, key=value pairs) and assembles JSONL entries deterministically. This means:

- Works in `KBF_LLM_PROVIDER=stub` mode
- No API costs during workshops
- No latency — instant responses
- Persona teams can run it on any laptop with Python installed

---

## Part 4: End-to-End Workshop Playbook

### 4.1 Before the Workshop (Facilitator)

```bash
# Verify setup
python3 -m framework.cli.kb_cli laptop-init
python3 -m framework.cli.kb_cli skill-list

# Run all 3 starter skills to verify outputs
python3 -m framework.cli.kb_cli workflow-run ops_eng.incident_summary \
    --inputs '{"incident_id": "INC-EXAMPLE-001"}'
python3 -m framework.cli.kb_cli workflow-run pm.release_brief \
    --inputs '{"release_id": "25.01"}'
python3 -m framework.cli.kb_cli workflow-run tpm.weekly_exec_review \
    --inputs '{"project": "all"}'

# Open outputs to verify rendering
ls ~/.kbf/outputs/
```

### 4.2 During the Workshop (90 minutes)

| Segment | Time | Facilitator runs | Persona team does |
|---|---|---|---|
| 1. Intro | 10 min | Show architecture slide from PDD | Listen, ask questions |
| 2. Demo | 15 min | Run 3 starter skills live | Watch outputs appear in real time |
| 3. Brainstorm | 15 min | Whiteboard | List recurring deliverables (sticky notes) |
| 4. Skill authoring | 20 min | `kb-cli skill-builder --persona <p>` | Provide example artifact, review fields |
| 5. Gold-set feeding | 20 min | `kb-cli gold-feed --persona <p>` | Dictate questions + citations |
| 6. Wrap-up | 10 min | Show PR diff, gold set count | Confirm schema owner, schedule follow-up |

### 4.3 After the Workshop

```bash
# Check what was produced
git status

# Review the synthesized skill
cat framework/workflow_skills/<persona>/<skill_name>.yaml

# Review the gold set
wc -l framework/eval/gold_sets/<persona>.jsonl
cat framework/eval/gold_sets/<persona>.jsonl | python3 -m json.tool

# Validate the persona builder
python3 -m framework.cli.kb_cli validate framework/persona_builders/<persona>.yaml

# Test the new skill
python3 -m framework.cli.kb_cli workflow-run <persona>.<skill_name> \
    --inputs '{}' --show-data

# Create a PR
git add -A && git commit -m "Workshop: <persona> skill + gold set"
```

### 4.4 Progress Tracking

| Milestone | How to check | Target |
|---|---|---|
| Skill authored | `kb-cli skill-list` shows the new skill | 1+ per persona |
| Gold set started | `wc -l framework/eval/gold_sets/<persona>.jsonl` | 10+ at workshop |
| Gold set complete | Same check | 25+ entries |
| Eval gate pass | `kb-cli eval framework/persona_builders/<persona>.yaml` | recall ≥ 0.80 |
| Production | `kb-cli promote ... --validate-links` | status: production |

---

## Appendix A: Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named 'framework'` | Not running from repo root | `cd /path/to/Knowledgebase` |
| `No module named 'yaml'` | PyYAML not installed | `pip3 install pyyaml` |
| `unknown workflow skill` | Skill YAML not in `workflow_skills/` | Run `kb-cli skill-list` to check |
| Stub PPTX/DOCX | Missing rendering libraries | `pip3 install python-pptx python-docx` |
| `command not found: python` | macOS uses `python3` | Use `python3 -m framework.cli.kb_cli` |
| Gold set not found by eval | Wrong file path | Must be `framework/eval/gold_sets/{persona}.jsonl` |
| Existing entries lost | Feeder uses append mode | This shouldn't happen; check file permissions |

## Appendix B: File Locations

| Artifact | Path | Owner |
|---|---|---|
| CLI entry point | `framework/cli/kb_cli.py` | dev-manager |
| Gold-set feeder | `framework/eval/gold_set_feeder.py` | architect |
| Gold sets (data) | `framework/eval/gold_sets/{persona}.jsonl` | persona teams |
| Eval runner | `framework/eval/runner.py` | qa |
| Skill-builder conversation | `framework/skill_builder/conversation.py` | architect |
| Gold-seed (from synthesis) | `framework/skill_builder/gold_seed.py` | architect |
| Workshop guide | `pmo/workshops/persona-authoring-workshop.md` | pm |
| Persona builders | `framework/persona_builders/{persona}.yaml` | persona teams |
| Workflow skills | `framework/workflow_skills/{persona}/{skill}.yaml` | persona teams |
| Dev config | `framework/config/dev.yaml` | architect |
| Laptop quickstart | `docs/wiki/engineering/laptop-quickstart.md` | dev-manager |

## Appendix C: Citation URL Conventions

| Source system | Format | Example |
|---|---|---|
| Jira | `jira://{ticket_key}` | `jira://OPS-4521` |
| Confluence | `confluence://{space}/{page_title}` | `confluence://OPS/Post-mortem-2026-W19` |
| Git | `git://{repo}/{path}` | `git://infra-ops/runbooks/pod-refresh.md` |
| Code wiki | `code://{file_path}#symbol` | `code://framework/adapters/udap_adapter.py#UdapAdapter` |
| UDAP/Fleet | `udap://{entity_type}/{id}` | `udap://pod/pod-refresh-001` |
| File | `file://{path}` | `file:///Users/.../outputs/report.md` |
