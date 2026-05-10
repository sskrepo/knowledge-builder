---
title: ADR-017 — Extraction–workflow linking contract
status: accepted
created: 2026-05-09
owner: architect
tags: [adr, workflow-skills, extraction, linking, phase-3]
related: [ADR-004, ADR-015, ADR-016]
---

# ADR-017 — Extraction–workflow linking contract

## Status
Accepted (2026-05-09).

## Context
V2 introduces workflow skills (ADR-016) that consume from extraction skills (ADR-004's `knowledge_bases`). The relationship is N:M:
- A workflow skill may consume from multiple extraction skills
- An extraction skill may feed multiple workflow skills

Without an explicit contract, drift is inevitable: a workflow skill expects a field that nobody is extracting, or an extraction skill stops emitting a field a workflow depends on. Both should be detected at promote-time, not in production.

## Decision

### Two declarations + one validation

**Workflow skill declares what it requires:**

```yaml
# workflow_skills/{persona}/{name}.yaml
requires_extractions:
  - kb: tpm.weekly_project_status
    required_fields: [week_id, rag_status, top_milestones, blockers, exec_asks]
  - kb: ops_eng.ops_incidents
    required_fields: [incident_id, severity, root_cause_summary]
    optional_fields: [resolution_summary]
```

**Extraction skill declares what it provides:**

```yaml
# persona_builders/{persona}.yaml — KB entry
knowledge_bases:
  - name: weekly_project_status
    kind: wiki
    extraction_schema: parsers/schemas/tpm/weekly-project-status/v1.json
    provides_fields:
      - week_id
      - rag_status
      - top_milestones
      - blockers
      - exec_asks
      - source_page_url
      - last_updated
```

**Promote-time validation** (in `kb-cli promote`):

```python
def validate_workflow_links(workflow_yaml: Path) -> list[str]:
    """Returns list of error messages; empty means valid."""
    cfg = yaml.safe_load(workflow_yaml.read_text())
    errors = []
    workflow_persona = cfg["persona"]

    for req in cfg.get("requires_extractions", []):
        kb_name = req["kb"]                           # e.g. "tpm.weekly_project_status"
        required = set(req.get("required_fields", []))

        # 1. Find the KB
        kb_entry = find_kb_entry(kb_name)
        if not kb_entry:
            errors.append(f"workflow references unknown KB: {kb_name}")
            continue

        # 2. Field coverage check
        provided = set(kb_entry.get("provides_fields", []))
        missing = required - provided
        if missing:
            errors.append(
                f"workflow requires fields not provided by {kb_name}: {missing}. "
                f"Either add to the extraction schema or remove from required_fields."
            )

        # 3. ACL read-scope check (per ADR-007 amend 6)
        visibility = kb_entry.get("metadata_defaults", {}).get("persona_visibility", [])
        if workflow_persona not in visibility:
            errors.append(
                f"workflow's persona '{workflow_persona}' is not in "
                f"{kb_name}'s persona_visibility {visibility}. "
                f"Either request access from {kb_entry['_owning_persona']} or rescope."
            )

    return errors
```

### Three classes of links — what skill builder synthesizes

| Class | When | What skill builder does |
|---|---|---|
| **Reuse-only** | All required fields are already in existing extraction skills (ACL-permitted) | Creates only the workflow skill; links to existing KBs |
| **New-extraction** | No existing extraction provides the fields | Creates a new extraction skill (KB entry + JSON-Schema + extraction gold) AND the workflow skill, linked |
| **Mixed** | Some fields reused; some new | Creates a new extraction skill for gap fields; workflow links to BOTH the new and the existing KBs |

The skill builder's `reuse_detector.py` makes this decision automatically (per ADR-015):

```python
detection = detect_reuse(
    required_fields=["week_id", "rag_status", "incident_id", "severity"],
    persona="tpm",                       # workflow's owning persona
)
# → covered: { "incident_id": "ops_eng.ops_incidents",
#              "severity":    "ops_eng.ops_incidents" }
# → gaps:    [ "week_id", "rag_status" ]
# → action:  Create new tpm.weekly_project_status for gaps;
#            workflow links to both ops_eng.ops_incidents (reused) and
#            tpm.weekly_project_status (new)
```

### Field naming — controlled vocabulary

To make linking work at scale, field names should follow a convention:

- **Snake_case identifiers** (e.g., `week_id`, `root_cause_summary`)
- **Stable across versions** (renaming is a breaking change → schema_version bump + reingest)
- **Avoid persona-specific prefixes when the field is universal** (use `incident_id`, not `aira_incident_id`)
- **Use persona-specific prefixes when the field is persona-specific** (e.g., `pm_priority_score` vs `tpm_program_status`)

The skill builder enforces this by suggesting field names during synthesis and warning on collisions.

### Backward compatibility on field changes

When an extraction schema's `provides_fields` changes:

| Change | Impact | Action |
|---|---|---|
| Add a new field | Additive; safe | Bump schema_version patch; no re-ingest needed |
| Remove a field | Breaking — workflows depending on it stop working | (1) deprecation period: keep field but mark with `deprecated: true`; (2) bump schema_version; (3) re-ingest impacted KB; (4) `kb-cli validate-all` flags affected workflows; (5) workflow owners update or skill builder re-synthesizes |
| Rename a field | Equivalent to remove + add | Same as above |

`kb-cli validate-all` runs after any extraction-schema change in CI to catch dangling workflow references.

## Considered alternatives

- **Implicit linking by field-name match**: rejected; brittle; no validation; no ACL check
- **Workflow embeds its own extraction**: rejected; defeats reuse + multiplies cost
- **Strict typed field schema (e.g., shared protobuf-like definitions)**: rejected for v1; JSON-Schema descriptions per field are sufficient. May revisit if schema drift becomes a problem at scale

## Consequences

- `provides_fields` becomes a required key on every persona-builder KB entry
- `requires_extractions` becomes a required key on every workflow skill (empty list allowed for stateless transformation skills)
- `kb-cli promote` runs the validation; blocks on errors with clear remediation
- `kb-cli validate-all` runs in CI after any schema change
- The skill builder uses both declarations for reuse detection during authoring

## Migration

Existing persona builders (committed today) don't have `provides_fields` declared. Migration:

1. **Phase 2 backfill** — generate `provides_fields` from each KB's extraction schema (the schema's top-level required + properties keys)
2. **Phase 3 enforcement** — `kb-cli promote` requires `provides_fields` for any KB referenced by a workflow skill
3. **Phase 4 lint** — every persona builder must declare `provides_fields`

## References
- [PDD V2 §5 — Extraction–workflow linking](../pdd/PDD-Knowledge-Builder-Framework-v2.md)
- [ADR-004 — Persona-builder config schema](ADR-004-persona-builder-config.md)
- [ADR-015 — Skill-by-demonstration onboarding](ADR-015-skill-by-demonstration.md)
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md)
