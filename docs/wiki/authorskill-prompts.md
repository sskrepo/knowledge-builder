---
title: authorSkill — Full Prompt Dump
source: framework/skill_builder/conversation.py, review.py, synthesize_schema.py
compiled_at: 2026-05-15
owner: architect
tags: [skill-builder, prompts, adr-028]
status: current
---

# authorSkill — Full Prompt Dump (Living Reference)

This document is the authoritative inventory of every LLM prompt used anywhere
in the authorSkill flow. It is produced as part of ADR-028 (Item 1) and must be
kept current when any prompt changes.

For each prompt the entry records:
- The Python constant name and source file:line
- The `.format(...)` variables injected at call time (= the "personalization surface")
- Whether those variables cause the *instructions* to differ by persona, or only
  supply data the LLM reasons over
- The call site

---

## 1. `_ANALYZE_ARTIFACT_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 119  
**Path in flow:** Legacy pre-ADR-027 path only (ANALYZE_ARTIFACT state). New
sessions never reach this prompt; it runs only for in-flight sessions that were
started before ADR-027 deployed.

**Full prompt template:**
```
You are a Knowledge Builder Framework schema engineer. An artifact has been parsed
and its structural sections identified.

Persona: {persona}
Intent: "{intent}"
Artifact type: {artifact_type}

Sections / slides found in the artifact:
{field_contexts}

For EACH field listed above, decide:
1. The most appropriate JSON Schema type ("string", "integer", "boolean", "array").
   Use "array" only for genuinely multi-valued list fields. Default to "string".
2. A precise 1–2 sentence extraction instruction that tells the LLM parser exactly
   what content to look for and how to format the output.

Return ONLY a JSON object mapping field_name → object with "type" and "description":
{
  "schedule_health": {
    "type": "string",
    "description": "RAG status (Red/Amber/Green) ..."
  }
}
```

**Format kwargs at call time:**
| Variable | Source | Personalises instructions? |
|---|---|---|
| `persona` | `self._data.persona` (e.g. `"tpm"`) | NO — inserted as a label string only; the instructions themselves are identical for every persona |
| `intent` | `self._data.intent_description` | NO — user data, not instruction-shaping |
| `artifact_type` | artifact extension (e.g. `"pptx"`) | NO — used as a label |
| `field_contexts` | section/slide titles extracted from artifact | NO — user data |

**Verdict:** Static template. `persona` is a bare string label; no conditional instruction branches, no persona-specific examples, no persona-specific heuristics.

---

## 2. `_CAPTURE_INTENT_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 150  
**Path in flow:** ADR-027 CAPTURE_INTENT state — called in `_advance_to_capture_intent()`, line 816.

**Full prompt template:**
```
You are a Knowledge Builder Framework assistant. Parse the user's intent into a
normalised goal object so downstream design steps have a structured representation
to work from.

Persona: {persona}
Raw intent: "{intent}"

Return ONLY a JSON object with these keys:
{
  "output_kind": "pptx | docx | markdown | email | slack",
  "audience": "exec | team | ops | all",
  "cadence": "weekly | monthly | on_request | daily",
  "scope_domains": ["domain1", "domain2"],
  "success_criteria": ["criterion1", "criterion2"],
  "ambiguities": ["anything unclear that the user should confirm"]
}

Rules:
- "output_kind": infer from words like "PPT", "deck", "slide", "document", "report", "email"
- "scope_domains": extract project/service names (e.g. "26ai", "FA DB", "OCIFACP")
- "success_criteria": infer from phrases like "one slide", "real data", "exec-ready"
- "ambiguities": list anything genuinely unclear; empty list if intent is clear
- Keep all string values concise (< 80 chars each)
```

**Format kwargs at call time** (`_advance_to_capture_intent`, line 816):
```python
prompt = _CAPTURE_INTENT_PROMPT.format(
    persona=persona,       # e.g. "tpm"
    intent=intent,         # e.g. "Produce a weekly PPT for exec review"
)
```

| Variable | Source | Personalises instructions? |
|---|---|---|
| `persona` | `self._data.persona` | NO — label only; the output_kind/audience/cadence inference rules are identical for TPM and ops_eng |
| `intent` | `self._data.intent_description` | NO — user data |

**Verdict:** Static template. The LLM is not told "for a TPM, pay special attention to X" vs "for ops_eng, pay attention to Y". The `ambiguities` list is the only mechanism for surfacing unclear requirements — but the prompt has no instruction to treat ambiguities as blocking, and `_handle_capture_intent` lets `"ok"` bypass them silently (line 891).

---

## 3. `_CONFIGURE_SOURCES_SUGGEST_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 176  
**Path in flow:** ADR-027 CONFIGURE_SOURCES state — called in `_advance_to_configure_sources_v2()`, line 924.

**Full prompt template:**
```
You are a Knowledge Builder Framework source advisor. Given the user's intent and the
persona's declared adapters, propose the most likely source descriptors.

Persona: {persona}
Normalised intent: {normalised_intent}
Available adapters: {adapter_list}
Intent text (original): "{intent_text}"

Return ONLY a JSON array of source descriptor objects. Each object must include:
- "kind": "confluence" | "jira" | "git" | "adb"
- For confluence: optionally "pages" (list of page IDs or URLs), "space", "labels"
- For jira: "jql" string
- "rationale": why this source is likely to contain the required data (1 sentence)

Example:
[
  {
    "kind": "confluence",
    "pages": ["20030556732"],
    "rationale": "26ai project status page explicitly mentioned in intent"
  }
]

Rules:
- Extract all page IDs or URLs from the intent text — these are high-confidence.
- Propose additional sources only when the adapter list makes them available AND
  the intent clearly implies them.
- Do not invent sources not supported by the adapter list.
- Return an empty array [] if no confident source can be proposed.
```

**Format kwargs at call time** (`_advance_to_configure_sources_v2`, line 924):
```python
prompt = _CONFIGURE_SOURCES_SUGGEST_PROMPT.format(
    persona=self._data.persona,
    normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
    adapter_list=json.dumps(adapter_list, indent=2),
    intent_text=self._data.intent_description,
)
```

| Variable | Source | Personalises instructions? |
|---|---|---|
| `persona` | `self._data.persona` | NO — label only |
| `normalised_intent` | LLM output from CAPTURE_INTENT | NO — data |
| `adapter_list` | read from persona YAML `knowledge_bases[*].sources[*].kind` via `_get_persona_adapters()` | PARTIALLY — the available adapter list differs per persona YAML, so the LLM is told "confluence is available" vs "confluence + jira". But the *instruction wording* is identical. |
| `intent_text` | raw user text | NO — data |

**Verdict:** Mostly static template. `adapter_list` is the closest thing to persona-shaping: if the persona's YAML declares only `confluence`, the LLM won't propose Jira sources. But this is data-driven filtering, not instruction-level differentiation (e.g., no "TPMs should prioritise Jira roadmap queries" vs "ops_eng should prioritise incident Confluence spaces").

---

## 4. `_INSPECT_SOURCES_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 208  
**Path in flow:** ADR-027 INSPECT_SOURCES state — called in `_run_inspect_sources()` per source, line 1117.

**Full prompt template:**
```
You are a Knowledge Builder Framework source analyst. Review the sample content
fetched from a source and produce a capability inventory.

Source ID: {source_id}
Persona: {persona}
Intent: {normalised_intent}

Sample content (up to 3 pages):
{sample_content}

Return ONLY a JSON object:
{
  "source_id": "{source_id}",
  "available_fields": [
    {"field": "snake_case_name", "type": "string|array|integer",
      "confidence": "high|medium|low",
      "evidence": "quote or location from sample (< 100 chars)"}
  ],
  "missing_fields": [
    {"field": "field_the_intent_might_want",
      "reason": "why this content cannot supply it"}
  ],
  "suggested_fields": [
    {"field": "snake_case_name", "type": "string|array|integer",
      "reason": "why this is consistently present and useful"}
  ],
  "summary": "2-3 sentence overview of what this source contains"
}

Rules:
- "available_fields": ONLY fields clearly extractable from the sample content.
- "suggested_fields": fields present in the sample that the intent might have missed.
- "missing_fields": fields the intent implies but the source clearly cannot provide.
- Base ALL findings on the sample content — do not invent.
```

**Format kwargs at call time** (`_run_inspect_sources`, line 1117):
```python
prompt = _INSPECT_SOURCES_PROMPT.format(
    source_id=cache_key,
    persona=self._data.persona,
    normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
    sample_content=sample_content[:6000],
)
```

| Variable | Source | Personalises instructions? |
|---|---|---|
| `source_id` | cache key like `"confluence:20030556732"` | NO — label |
| `persona` | `self._data.persona` | NO — label |
| `normalised_intent` | CAPTURE_INTENT output | NO — data |
| `sample_content` | fetched Confluence page text | NO — data |

**Verdict:** Static template. The analysis instructions are persona-agnostic: a TPM source and an ops_eng source are analysed by the same rubric. A TPM might care about "ORM status" and "milestone risk"; an ops_eng might care about "MTTR" and "incident severity" — the prompt has no mechanism to weight these differently.

---

## 5. `_DESIGN_SKILL_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 245  
**Path in flow:** ADR-027 DESIGN_SKILL state — called in `_run_design_skill()`, line 1312.

**Full prompt template:**
```
You are a Knowledge Builder Framework skill architect. Design a complete skill from
the user's intent, the source capability inventory, the artifact layout, and the
existing reusable KB cards.

Persona: {persona}
Normalised intent: {normalised_intent}
Source capability inventory: {source_capability}
Artifact layout hint (may be null): {artifact_layout}
Existing reusable KB cards: {existing_kb_cards}

Produce a single JSON design object:
{
  "schema": {
    "title": "skill_name",
    "properties": {
      "field_name": {
        "type": "string|array|integer|boolean",
        "description": "precise 1-2 sentence extraction instruction",
        "maxLength": 500
      }
    },
    "required": ["field1", "field2"]
  },
  "source_bindings": {
    "field_name": ["source_id1"]
  },
  "workflow_shape": {
    "output_format": "pptx|docx|markdown|email|slack",
    "layout": "weekly_exec_review_v1 | default",
    "trigger": {"on_request": true, "schedule": "cron_or_null"},
    "retriever": "search_wiki"
  },
  "reuse_plan": {
    "covered": {"field": "existing_kb_name"},
    "gaps": ["field1", "field2"]
  },
  "unsupportable_fields": [
    {"field": "field_name", "reason": "why no source can provide this"}
  ],
  "open_questions": ["question for the user to resolve"]
}

Rules:
- Include ONLY fields that at least one source can support (confidence high or medium).
- Source bindings must reference source IDs from the capability inventory.
- Reuse plan covers must reference real KB cards from "existing_kb_cards".
- If artifact layout is provided, align the output_format and layout accordingly.
- Choose "weekly_exec_review_v1" layout only for exec-review PPTX skills.
- "required" list should contain only fields critical to the skill's purpose.
- maxLength: 200 for IDs/statuses, 500 for summaries, 2000 for detailed content.
```

**Format kwargs at call time** (`_run_design_skill`, line 1312):
```python
prompt = _DESIGN_SKILL_PROMPT.format(
    persona=self._data.persona,
    normalised_intent=json.dumps(self._data.normalised_intent, indent=2),
    source_capability=json.dumps(self._data.source_capability, indent=2),
    artifact_layout=json.dumps(self._data.artifact_layout, indent=2) if artifact_layout else "null",
    existing_kb_cards=json.dumps(cards_summary, indent=2),
)
```

| Variable | Source | Personalises instructions? |
|---|---|---|
| `persona` | `self._data.persona` | NO — label only |
| `normalised_intent` | CAPTURE_INTENT output | NO — data |
| `source_capability` | INSPECT_SOURCES output | NO — data (persona-specific content, but generic rules) |
| `artifact_layout` | parse of uploaded artifact | NO — data |
| `existing_kb_cards` | ShimKb cards visible to this persona | PARTIALLY — the card list is filtered to the persona, so reuse planning sees only that persona's KBs. But the design rules ("Include ONLY fields that at least one source can support") are persona-agnostic. |

**Verdict:** Static template with persona-filtered data inputs. The LLM receives no persona-specific extraction heuristics, no "for TPM skills, always include ORM status and RAG summary", no few-shot examples of a good TPM vs ops_eng schema. This is the highest-leverage gap in the flow.

---

## 6. `_REVIEW_DESIGN_REPLAN_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 298  
**Path in flow:** REVIEW_DESIGN state, substantive edit path — called in `_run_design_replan()`, line 1601.

**Full prompt template:**
```
You are a Knowledge Builder Framework skill architect. The user wants to modify the
current skill design. Return ONLY the changes needed as a diff object.

Current design: {current_design}
User edit request: "{edit_request}"
Updated source capability (if sources changed): {updated_source_capability}

Return ONLY a JSON diff object with keys matching what changed:
{
  "schema_add": {"field_name": {"type": "...", "description": "...", "maxLength": 500}},
  "schema_remove": ["field_to_remove"],
  "schema_update": {"field_name": {"description": "updated instruction"}},
  "source_bindings_add": {"new_field": ["source_id"]},
  "source_bindings_remove": ["field_to_unbind"],
  "workflow_shape_update": {"layout": "new_layout"},
  "reuse_plan_update": {"covered": {}, "gaps": ["field1"]},
  "open_questions": ["any new questions from the edit"]
}

Rules:
- Only include keys that actually change — omit unchanged sections.
- If the edit request is trivial (rename, description change), return only the
  affected field in schema_update.
- If new sources must be added (the edit implies data not in the inventory),
  add an open_question noting the source gap.
```

**Format kwargs at call time** (`_run_design_replan`, line 1601):
```python
prompt = _REVIEW_DESIGN_REPLAN_PROMPT.format(
    current_design=json.dumps(self._data.design, indent=2),
    edit_request=edit_request,
    updated_source_capability=json.dumps(self._data.source_capability, indent=2),
)
```

| Variable | Source | Personalises instructions? |
|---|---|---|
| `current_design` | accumulated design dict | NO — data |
| `edit_request` | user's freetext edit | NO — data |
| `updated_source_capability` | cached INSPECT_SOURCES result | NO — data |

**Verdict:** Static template. No persona context passed at all — not even the bare `persona` label variable. A TPM replan and an ops_eng replan receive identical instructions.

---

## 7. `_EVAL_JUDGE_PROMPT`

**File:** `framework/skill_builder/conversation.py`, line 326  
**Path in flow:** EVAL state, faithfulness scoring per field — called inside the EVAL state handler.

**Full prompt template:**
```
You are a Knowledge Builder Framework faithfulness judge. Determine whether an
extracted field value is faithfully grounded in the source document snippet.

Field: {field_name}
Extraction instruction: {field_description}
Extracted value: {extracted_value}
Source snippet: {source_snippet}

Return ONLY a JSON object:
{
  "faithful": true | false,
  "confidence": "high | medium | low",
  "reason": "1 sentence explanation"
}

Rules:
- "faithful" = true if the extracted value is directly supported by the source snippet.
- "faithful" = false if the extracted value contains information NOT present in the snippet.
- Paraphrasing is acceptable; exact wording is not required.
- If the extracted value is empty/null and the field is optional, mark faithful=true.
- Base the judgment ONLY on the source snippet provided — do not use outside knowledge.
```

**Format kwargs:** field_name, field_description, extracted_value, source_snippet — all data, no persona.

**Verdict:** Static template. Correct: faithfulness is a universal, persona-agnostic judgment. This prompt is not a problem.

---

## 8. `_DESCRIPTION_SYNTHESIS_PROMPT`

**File:** `framework/skill_builder/synthesize_schema.py`, line 14  
**Path in flow:** Legacy pre-ADR-027 REVIEW_SCHEMA path, called in `_llm_synthesize_descriptions()`.  
Not called by the ADR-027 16-state machine for new sessions. Retained for in-flight pre-ADR-027 sessions.

**Full prompt template:**
```
You are a Knowledge Builder Framework schema engineer. Your job is to write
precise extraction instructions for each field in an extraction schema.

The schema will be used by an LLM parser to extract structured data from
{artifact_type} documents for persona "{persona}".

User intent: "{intent}"

For each field below, write a single concise extraction instruction (1-2 sentences)
that tells the LLM parser exactly what content to look for and how to format it.
Be specific — reference the section/slide title and describe the expected format.

Fields (with source location in the artifact):
{field_contexts}

Return ONLY a JSON object mapping field_name → extraction_instruction string.
```

**Format kwargs** (`_llm_synthesize_descriptions`, line 110):
```python
prompt = _DESCRIPTION_SYNTHESIS_PROMPT.format(
    artifact_type=artifact_type,   # "PowerPoint presentation" or "document"
    persona=persona,               # label only
    intent=intent,
    field_contexts="\n".join(context_lines),
)
```

| Variable | Personalises instructions? |
|---|---|
| `artifact_type` | Marginally — changes the noun ("presentation" vs "document") |
| `persona` | NO — label only |
| `intent` | NO — data |
| `field_contexts` | NO — data |

**Verdict:** Static template. Legacy path; same diagnosis as the others.

---

## 9. `_REVIEW_EXTRACT_PROMPT`

**File:** `framework/skill_builder/review.py`, line 110  
**Path in flow:** PREVIEW_EXTRACTION state (and legacy REVIEW_SCHEMA), called in `_llm_extract()`, line 156.

**Full prompt template:**
```
You are extracting structured fields from a source document to preview what an
LLM parser would produce when this schema is applied at ingest time.

Return a single JSON object with EXACTLY these field keys
(use empty string "" or empty list [] when a field is genuinely absent —
do NOT invent data that is not in the source document):

{field_lines}

=== Source document ===
{text}
=== End source ===

Respond with ONLY the JSON object, no prose, no markdown fences.
```

**Format kwargs** (`_llm_extract`, line 156):
```python
prompt = _REVIEW_EXTRACT_PROMPT.format(
    field_lines="\n".join(field_lines),   # field name + type + description per line
    text=text,                            # source document text (capped 12000 chars)
)
```

| Variable | Personalises instructions? |
|---|---|
| `field_lines` | PARTIALLY — includes per-field `description` from the schema, which was set by DESIGN_SKILL (persona-aware data, but the enclosing extraction instruction is generic) |
| `text` | NO — data |

**Verdict:** Static template. No persona context. The per-field descriptions come from the design (which is data-shaped by the source capability) but the extraction instruction format is uniform.

---

## Summary Table

| Prompt | State | Persona in format() kwargs? | Instructions differ by persona? |
|---|---|---|---|
| `_ANALYZE_ARTIFACT_PROMPT` | ANALYZE_ARTIFACT (legacy) | YES — label | NO |
| `_CAPTURE_INTENT_PROMPT` | CAPTURE_INTENT | YES — label | NO |
| `_CONFIGURE_SOURCES_SUGGEST_PROMPT` | CONFIGURE_SOURCES | YES — label + adapter list | adapter list varies; instructions static |
| `_INSPECT_SOURCES_PROMPT` | INSPECT_SOURCES | YES — label | NO |
| `_DESIGN_SKILL_PROMPT` | DESIGN_SKILL | YES — label + filtered KB cards | KB card list varies; instructions static |
| `_REVIEW_DESIGN_REPLAN_PROMPT` | REVIEW_DESIGN (replan) | NO — omitted entirely | NO |
| `_EVAL_JUDGE_PROMPT` | EVAL | NO | NO — correct by design |
| `_DESCRIPTION_SYNTHESIS_PROMPT` | REVIEW_SCHEMA (legacy) | YES — label | NO |
| `_REVIEW_EXTRACT_PROMPT` | PREVIEW_EXTRACTION | NO | NO |

**Conclusion:** Every prompt is a static template. Persona enters as a bare string label or as filtered data (adapter list, KB card list). No prompt contains persona-specific instructional branches, heuristics, or few-shot examples. The LLM receives no guidance on what a "good" TPM skill looks like vs what a "good" ops_eng skill looks like.
