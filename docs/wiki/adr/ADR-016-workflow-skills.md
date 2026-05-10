---
title: ADR-016 — Workflow skills (persona-owned, three trigger models)
status: accepted
created: 2026-05-09
owner: architect
tags: [adr, workflow-skills, phase-3]
related: [PDD-v2, ADR-006, ADR-007, ADR-015, ADR-017]
---

# ADR-016 — Workflow skills

## Status
Accepted (2026-05-09).

## Context
V1 had only ingestion-and-retrieval primitives. V2 introduces **workflow skills** — first-class, persona-owned skills that produce a *specific outcome* (PPT, DOCX, email, Slack message, structured answer) on a schedule, on user request, or on data event.

Workflow skills are how a persona team crystallizes a recurring task or specialized output that's beyond plain Q&A.

## Decision

### Workflow skill = persona-owned outcome producer

```yaml
workflow_skill: weekly_exec_review
persona: tpm                                     # AUTHORING owner

trigger:
  on_schedule: { cron: "0 16 * * 5", delivery: {...} }
  on_request:  { enabled: true, inputs: [...], output_format: pptx, response_mode: artifact_url }
  on_event:    { enabled: false }                 # Phase 4+

skill_card:                                       # for shim_workflows matching
  use_when: "..."
  example_invocations: ["...", "..."]
  do_not_use_for: "..."

requires_extractions:                             # link to extraction skill(s)
  - kb: tpm.weekly_project_status
    required_fields: [week_id, rag_status, top_milestones, blockers, exec_asks]

synthesis:
  template: synthesis/templates/weekly_exec_review.pptx
  slide_mapping: synthesis/mappings/weekly_exec_review.yaml

eval:
  gold_set: eval/gold_sets/tpm-weekly-exec-review.jsonl
  exit_criteria: { field_accuracy: 0.85, delivery_success_rate: 0.99 }
```

### Three trigger models

| Trigger | Mechanism | Example |
|---|---|---|
| `on_schedule` | cron expression; `framework/ingestion/scheduler.py` extended | "every Friday 4pm produce exec PPT" |
| `on_request` | registered as MCP tool; orchestrator's classifier matches | user asks "exec review for project X" |
| `on_event` | data event triggers (Phase 4) | "INC closes → produce postmortem template" |

A single skill can declare any combination. The same skill can be both autonomous (cron) and interactive (on-request).

### `on_request` skills become MCP tools

Workflow skills with `on_request.enabled: true` register automatically into the MCP tool registry at startup:

```python
# framework/workflow_runtime/skill_registry.py — psuedocode

def register_workflow_skills_as_mcp_tools(workflow_skills_dir: Path) -> dict:
    registry = {}
    for skill_yaml in workflow_skills_dir.rglob("*.yaml"):
        cfg = yaml.safe_load(skill_yaml.read_text())
        if cfg.get("trigger", {}).get("on_request", {}).get("enabled"):
            registry[cfg["workflow_skill"]] = WorkflowMCPTool(cfg)
    return registry
```

The orchestrator's intent classifier then has both retrieval tools (vector_search, search_wiki) AND workflow skills (weekly_exec_review, stuck_poddb_alert) in its tool registry. Same routing primitive; skills produce richer outputs.

### Module layout

```
framework/workflow_runtime/
├── trigger_dispatcher.py    # cron loop + event listener + on-request via MCP
├── executor.py              # source discovery → extract → retrieve → synthesize → render → deliver
├── skill_registry.py        # scans workflow_skills/*.yaml; exposes on_request as MCP tools
└── skill_suggester.py       # logs Tier-4 misses; weekly digest (Phase 4 — see ADR-018)

framework/renderers/
├── _base.py                 # Renderer Protocol
├── pptx_renderer.py         # python-pptx
├── docx_renderer.py         # python-docx / docx-js
├── email_renderer.py        # MJML / HTML
├── slack_renderer.py        # block-kit
└── markdown_renderer.py     # generic

framework/deliverers/
├── _base.py                 # Deliverer Protocol
├── object_storage.py        # OCI Object Storage
├── email.py                 # OCI Email Delivery / SMTP
├── slack.py                 # webhook
└── sync_return.py           # synchronous artifact URL response

framework/workflow_skills/
├── _template.yaml           # for skill builder to start from
├── tpm/
│   └── weekly_exec_review.yaml
├── ops_eng/
│   └── stuck_poddb_alert.yaml
└── pm/
    └── release_brief.yaml
```

### Renderer Protocol

```python
@runtime_checkable
class Renderer(Protocol):
    name: str           # "pptx" | "docx" | "email" | "slack" | "markdown"
    def render(self, data: dict, template: Path | str) -> bytes: ...
    # bytes = the rendered artifact ready to deliver
```

### Deliverer Protocol

```python
@runtime_checkable
class Deliverer(Protocol):
    name: str           # "oci_object_storage" | "email" | "slack" | "sync_return"
    def deliver(self, artifact: bytes, destination: dict) -> dict: ...
    # returns: { "status": "delivered" | "failed", "url": str|None, "error": str|None }
```

### Execution flow

```
trigger_dispatcher fires (schedule|request|event)
        ↓
executor.execute(skill_name, inputs):
  1. Load workflow skill YAML
  2. Resolve source set:
       • if `sources.procedural` → adapter.discover(steps) per ADR-011 amend
       • else → static source list from linked extraction skill
  3. Verify cached ContentItems exist for sources (from extraction skill's pipeline)
       • if cache miss → trigger fresh extraction via kb-cli ingest --paths ...
  4. Retrieve ContentItems via store.query()
  5. Apply slide_mapping → produce structured data dict per output section
  6. Synthesize via Synthesizer (gpt-4o, structured per ADR-007 amend 2)
  7. Render via Renderer registry (template + data → bytes)
  8. Deliver via Deliverer registry (bytes + destination → result)
  9. Cost telemetry written
  10. Eval gold-set entry recorded if available
```

## Considered alternatives

- **No workflow skills; rely entirely on retrieval + manual rendering**: rejected; the persona team has to wire up every cron + every renderer themselves
- **Workflow skills as agents (LLM loops)**: rejected per PDD §7 (skills-default policy). Workflow skills are *bounded* — fixed tool sequence, no autonomous decision-making mid-flight
- **Global workflow skills (not persona-owned)**: rejected; persona ownership keeps each team's prompt scope manageable and aligns workflow authoring with persona-team accountability
- **Single trigger model**: rejected; cron-only loses on-request convenience; on-request-only loses autonomous delivery

## Consequences

- New module: `framework/workflow_runtime/` (~500 LOC for v1)
- New modules: `framework/renderers/` and `framework/deliverers/` (~80-150 LOC per impl)
- `framework/workflow_skills/` directory becomes a first-class part of the framework, alongside `persona_builders/`
- MCP server registers workflow skills as tools at startup
- Eval extends to workflow output quality (per ADR-005 amend 4)
- Cost telemetry adds a new operation kind: `workflow_execute`

## References
- [PDD V2 §4 — Workflow skills](../pdd/PDD-Knowledge-Builder-Framework-v2.md)
- [ADR-007 amend 2 — Structured synthesis output schema](ADR-007-persona-context-skill.md)
- [ADR-011 amend — Adapter.discover()](ADR-011-dual-mode-source-adapters.md)
- [ADR-015 — Skill-by-demonstration onboarding](ADR-015-skill-by-demonstration.md)
- [ADR-017 — Extraction-workflow linking](ADR-017-extraction-workflow-linking.md)
