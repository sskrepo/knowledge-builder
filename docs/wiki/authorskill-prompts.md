---
title: authorSkill — Full Prompt Dump (GENERATED — DO NOT HAND-EDIT)
source: framework/config/prompts/*.yaml
generator: python -m framework.tools.prompt_lab docs
generated_at: 2026-05-16T18:51:24+00:00
owner: architect
tags: [skill-builder, prompts, adr-030]
status: generated
---

# authorSkill — Full Prompt Dump

> **GENERATED — DO NOT HAND-EDIT.**
> Source: `framework/config/prompts/*.yaml`
> Generated at: `2026-05-16T18:51:24+00:00`
> Regenerate with: `python -m framework.tools.prompt_lab docs`

## Prompt Index

- [analyze_artifact](#analyze_artifact) — DEPRECATED (legacy ADR-026 path). Decide JSON Schema types and extraction instructions from parsed artifact sections. Used only by in-flight sessions on the pre-ADR-027 state machine.
- [capture_intent](#capture_intent) — Parse the user's raw intent into a normalised goal object for downstream design steps.
- [clarify](#clarify) — Turn message template for the CLARIFY state — emits conversational prose, not an LLM call.
- [configure_sources](#configure_sources) — Propose the most likely source descriptors given the user's intent and available adapters.
- [description_synthesis](#description_synthesis) — Write precise extraction instructions for each field in an extraction schema.
- [design_skill](#design_skill) — Design a complete skill from intent, source capability inventory, artifact layout, and reusable KB cards.
- [eval_judge](#eval_judge) — Faithfulness judge: determine whether an extracted field value is grounded in the source snippet.
- [executor_extract](#executor_extract) — Production render-time extraction prompt for /api/v1/ask workflow output. Extracts structured fields from Confluence pages using the committed skill schema.
- [failure_classifier](#failure_classifier) **[LOCKED]** — Diagnose WHY a produced artifact does not match the reference; return a structured failure_class label for S6 routing.
- [inspect_sources](#inspect_sources) — Review sample content from a source and produce a capability inventory with confidence levels.
- [review_design_replan](#review_design_replan) — Return a diff object for user-requested modifications to the current skill design.
- [review_extract](#review_extract) — Preview extraction: extract structured fields from a source document using the committed schema.

---

## analyze_artifact

> Legacy — retained for in-flight session continuity only. New sessions use design_skill instead. Will be removed when all pre-ADR-027 in-flight sessions have completed.

| Field | Value |
|---|---|
| id | `analyze_artifact` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `2048` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `persona, intent, artifact_type, field_contexts` |

**Template:**

```
You are a Knowledge Builder Framework schema engineer. An artifact has been parsed \
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
{{
  "schedule_health": {{
    "type": "string",
    "description": "RAG status (Red/Amber/Green) for the schedule, with a 1–2 sentence \
justification citing specific milestone dates or blockers from the slide."
  }}
}}
```

---

## capture_intent

| Field | Value |
|---|---|
| id | `capture_intent` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `1024` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `persona, intent, persona_key_fields` |

**Persona overlays:** architect, developer, eng_mgr, kbf_ops, ops_eng, ops_mgr, pm, service_owner, tpm

**Template:**

```
You are a Knowledge Builder Framework assistant. Parse the user's intent into a
normalised goal object so downstream design steps have a structured representation
to work from.

Persona: {persona}
Raw intent: "{intent}"

Persona guidance: This persona's canonical output always includes these fields —
{persona_key_fields}
Use this list as a starting point for understanding what dimensions matter most.

Return ONLY a JSON object with these keys:
{{
  "output_kind": "pptx | docx | markdown | email | slack",
  "audience": "exec | team | ops | all",
  "cadence": "weekly | monthly | on_request | daily",
  "scope_domains": ["domain1", "domain2"],
  "success_criteria": ["criterion1", "criterion2"],
  "blocking_ambiguities": [
    "questions whose answers would change the schema structure, output kind, or source selection"
  ],
  "nice_to_know_ambiguities": [
    "questions that can be assumed; proceed and flag for user awareness"
  ]
}}

Rules:
- "output_kind": infer from words like "PPT", "deck", "slide", "document", "report", "email"
- "scope_domains": extract project/service names (e.g. "26ai", "FA DB", "OCIFACP")
- "success_criteria": infer from phrases like "one slide", "real data", "exec-ready"
- "blocking_ambiguities": ONLY include if the answer would materially change the schema
  structure, the source selection, or the output format. Example: "which Confluence space
  — FAAAS or 26AI-LEGACY?" is blocking. "which day of the week?" is nice_to_know.
- "nice_to_know_ambiguities": advisory items; proceed with stated assumption, flag for user.
- Keep all string values concise (< 80 chars each)
```

---

## clarify

> This is a turn message template, not an LLM call. The call site uses spec.text only; it does not call llm.chat. Included here so all user-facing string templates live in one place (ADR-030 goal).

| Field | Value |
|---|---|
| id | `clarify` |
| version | `1.0` |
| model | `none` |
| max_tokens | `None` |
| response_format | `none` |
| locked | `False` |
| required_vars | `question` |

**Template:**

```
Before I proceed, I need to clarify one thing:

{question}

Please provide a specific answer. I will use your response to design the skill correctly.
(You can type 'skip' to proceed with my best assumption — but be aware this may affect
the quality of the extraction schema.)
```

---

## configure_sources

| Field | Value |
|---|---|
| id | `configure_sources` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `1024` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `persona, normalised_intent, adapter_list, intent_text` |

**Template:**

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
  {{
    "kind": "confluence",
    "pages": ["20030556732"],
    "rationale": "26ai project status page explicitly mentioned in intent"
  }}
]

Rules:
- Extract all page IDs or URLs from the intent text — these are high-confidence.
- Propose additional sources only when the adapter list makes them available AND
  the intent clearly implies them.
- Do not invent sources not supported by the adapter list.
- Return an empty array [] if no confident source can be proposed.
```

---

## description_synthesis

| Field | Value |
|---|---|
| id | `description_synthesis` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `2048` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `artifact_type, persona, intent, field_contexts` |

**Template:**

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
Example: {{"schedule_health": "RAG status (Red/Amber/Green) for the schedule, with a 1-2 sentence justification citing specific milestone dates or blockers."}}
```

---

## design_skill

| Field | Value |
|---|---|
| id | `design_skill` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `4096` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `persona, normalised_intent, source_capability, artifact_layout, existing_kb_cards, persona_key_fields, persona_extraction_style, persona_few_shot_example` |

**Persona overlays:** architect, developer, eng_mgr, kbf_ops, ops_eng, ops_mgr, pm, service_owner, tpm

**Template:**

```
You are a Knowledge Builder Framework skill architect. Design a complete skill from
the user's intent, the source capability inventory, the artifact layout, and the
existing reusable KB cards.

Persona: {persona}
Normalised intent: {normalised_intent}
Source capability inventory: {source_capability}
Artifact layout hint (may be null): {artifact_layout}
Existing reusable KB cards: {existing_kb_cards}

Persona guidance — canonical key fields for this persona:
{persona_key_fields}

Persona extraction style:
{persona_extraction_style}

Worked example of a well-designed field for this persona:
{persona_few_shot_example}

Produce a single JSON design object:
{{
  "schema": {{
    "title": "skill_name",
    "properties": {{
      "field_name": {{
        "type": "string|array|integer|boolean",
        "description": "precise 1-2 sentence extraction instruction",
        "maxLength": 500
      }}
    }},
    "required": ["field1", "field2"]
  }},
  "source_bindings": {{
    "field_name": ["source_id1"]
  }},
  "workflow_shape": {{
    "output_format": "pptx|docx|markdown|email|slack",
    "layout": "weekly_exec_review_v1 | default",
    "trigger": {{"on_request": true, "schedule": "cron_or_null"}},
    "retriever": "search_wiki"
  }},
  "reuse_plan": {{
    "covered": {{"field": "existing_kb_name"}},
    "gaps": ["field1", "field2"]
  }},
  "unsupportable_fields": [
    {{"field": "field_name", "reason": "why no source can provide this"}}
  ],
  "blocking_questions": [
    "questions whose answers would change the schema structure — must resolve before review"
  ],
  "open_questions": [
    "cosmetic questions that can be noted without blocking — show at REVIEW_DESIGN"
  ]
}}

Rules:
- Include fields that at least one source can support with confidence "high", "medium",
  or "synthesisable". Do NOT exclude synthesisable fields — they are extractable.
- For fields with confidence="synthesisable": the extraction instruction MUST explicitly
  state "Derive this value by [aggregating / combining / summarising] the following
  content: [specific source element, e.g. WBS table rows flagged as blocked]."
  A synthesisable field with no aggregation instruction in its description is a prompt
  defect — it will cause the LLM to fail or hallucinate at extraction time.
- Fields with confidence="low" or genuinely "missing" from all sources: exclude them
  or place them in "unsupportable_fields" with a clear reason.
- Source bindings must reference source IDs from the capability inventory.
- Reuse plan covers must reference real KB cards from "existing_kb_cards".
- If artifact layout is provided, align the output_format and layout accordingly.
- Choose "weekly_exec_review_v1" layout only for exec-review PPTX skills.
- "required" list should contain only fields critical to the skill's purpose.
- maxLength: 200 for IDs/statuses, 500 for summaries, 2000 for detailed content.
- "blocking_questions": ONLY include questions where the answer would change the schema
  structure, field types, or source bindings. These trigger CLARIFY before REVIEW_DESIGN.
- "open_questions": advisory items — show at REVIEW_DESIGN but do not block.
- Persona key fields above are strong hints — always attempt to include them if the
  source capability inventory can support them.
```

---

## eval_judge

| Field | Value |
|---|---|
| id | `eval_judge` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `256` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `field_name, field_description, extracted_value, source_snippet` |

**Template:**

```
You are a Knowledge Builder Framework faithfulness judge. Determine whether an
extracted field value is faithfully grounded in the source document snippet.

Field: {field_name}
Extraction instruction: {field_description}
Extracted value: {extracted_value}
Source snippet: {source_snippet}

Return ONLY a JSON object:
{{
  "faithful": true | false,
  "confidence": "high | medium | low",
  "reason": "1 sentence explanation"
}}

Rules:
- "faithful" = true if the extracted value is directly supported by the source snippet.
- "faithful" = false if the extracted value contains information NOT present in the snippet.
- Paraphrasing is acceptable; exact wording is not required.
- If the extracted value is empty/null and the field is optional, mark faithful=true.
- Base the judgment ONLY on the source snippet provided — do not use outside knowledge.
```

---

## executor_extract

> Template uses {field_lines} (pre-joined with newlines by caller), {user_request} (the user's input string), {snippet} (source text truncated to 24000 chars). These map directly to the f-string variables in the original executor.py _llm_extract_fields method.

| Field | Value |
|---|---|
| id | `executor_extract` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `4096` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `field_lines, user_request, snippet` |

**Template:**

```
You are extracting structured fields from a Confluence/wiki page to populate an executive-review presentation. Return a single JSON object with EXACTLY these keys (use empty string "" or empty list [] when a field is genuinely absent — do not invent data):

{field_lines}

User request: {user_request}

=== Source document ===
{snippet}
=== End source ===

Respond with ONLY the JSON object, no prose, no markdown fences.
```

---

## failure_classifier

> Gate-locked. Anti-bias guard (CRITICAL REASONING RULE) prevents SOURCE_COVERAGE misclassification when synthesisable evidence exists. This text is the validated result of the gate test — changing it without re-passing the gate invalidates the S6 routing guarantee.

| Field | Value |
|---|---|
| id | `failure_classifier` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `512` |
| response_format | `json_object` |
| locked | `True` |
| checksum | `sha256:aef837cdde856fe83039f19fff816a101fe886187a7ce6f741a39eaab71c1d1f` |
| required_vars | `normalised_intent, schema_properties, capability_inventory, gap_report, missing_sections, thin_sections` |

**Template:**

```
You are a Knowledge Builder Framework failure-class classifier. Your job is to
diagnose WHY a produced artifact does not match the reference, and return a
structured, auditable classification so the framework can route the user to the
correct fix state.

=== INPUTS ===

Normalised intent:
{normalised_intent}

Current schema (fields the skill was designed to extract):
{schema_properties}

Source capability inventory (what each source can provide, with confidence levels):
{capability_inventory}

Comparator gap report:
{gap_report}

Missing sections (present in reference, absent from produced artifact):
{missing_sections}

Thin sections (present in produced artifact but far less content than reference):
{thin_sections}

=== FAILURE CLASSES ===

Choose exactly ONE failure class from this list:

- MISSING_FIELDS: The missing/thin sections are absent because the SCHEMA never
  included fields for them. The source capability inventory shows these fields
  ARE available (confidence=high, medium, or synthesisable) — the data exists
  in the source but the schema never asked for it.

- THIN_FIELDS: The fields ARE in the schema, but their extraction instructions
  produce thin or empty output. The source capability inventory shows the content
  IS available (confidence=synthesisable is common here — the LLM must aggregate
  across multiple rows/items but the instruction does not say so).

- WRONG_LAYOUT: The required sections/fields are present in the schema and the
  source, but the output artifact has wrong ordering, missing slide structure,
  wrong column arrangement, or incorrect section grouping.

- SOURCE_COVERAGE: The missing sections correspond to fields that the source
  capability inventory marks as confidence=missing or confidence=low with reason
  "content genuinely absent from source". The content does NOT exist in the
  source pages in any form — not even as synthesisable fragments.

- WRONG_SOURCE: A different source page or Confluence space likely has the
  required content. The current source pages are the wrong ones — the content
  exists somewhere else, not in the currently configured sources.

- UNSUPPORTABLE: The missing content cannot be derived from any configured source
  at all, even with synthesis. Human judgment is required. No automated fix will
  help.

=== CRITICAL REASONING RULE (anti-bias guard) ===

Before choosing SOURCE_COVERAGE, you MUST verify: does the source capability
inventory show confidence=synthesisable for ANY field related to the missing
section? If YES, the content IS present in the source — it just requires synthesis
(aggregation/combination) from scattered elements. In that case the correct class
is MISSING_FIELDS (the schema never asked for the synthesis) or THIN_FIELDS
(the schema asked for it but the instruction did not specify the synthesis logic).

"No verbatim labelled row for X" does NOT mean the source lacks X. If the
capability inventory shows synthesisable evidence (e.g. WBS table rows with
status/notes/risk data), the content EXISTS. The failure is in the schema or
extraction instruction — NOT in the source coverage.

Only choose SOURCE_COVERAGE if the capability inventory explicitly shows
confidence=missing or confidence=low with a reason stating the content is
genuinely absent (e.g. "source page has no risk section", "no milestone dates
found anywhere in the page").

=== REQUIRED OUTPUT ===

Return ONLY a valid JSON object with exactly these fields:

{{
  "failure_class": "MISSING_FIELDS|THIN_FIELDS|WRONG_LAYOUT|SOURCE_COVERAGE|WRONG_SOURCE|UNSUPPORTABLE",
  "confidence": "high|medium|low",
  "evidence": "Concrete reasoning (2-4 sentences). MUST reference specific fields
               from the capability inventory and schema. For example: 'The capability
               inventory shows risks (confidence=synthesisable, evidence=WBS rows with
               blocked/at-risk notes). The schema has no risks field. Therefore the fix
               is to add the field to the schema, not add more source pages.'",
  "alternative_class": "the second most likely failure class",
  "why_not_alternative": "1-2 sentences explaining why the alternative class is
                          ruled out. MUST cite the capability inventory evidence
                          that makes the alternative implausible."
}}

Rules:
- failure_class and confidence are REQUIRED — do not omit them.
- evidence and why_not_alternative are REQUIRED and must cite specific inventory entries.
- If confidence=low, you are uncertain — note the ambiguity in evidence.
- Do NOT choose SOURCE_COVERAGE unless the capability inventory explicitly confirms
  the content is absent from all sources (confidence=missing with clear reason).
- The routing map (code, not your choice) will translate your failure_class to a
  target state. Your job is diagnosis, not routing.
```

---

## inspect_sources

| Field | Value |
|---|---|
| id | `inspect_sources` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `2048` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `source_id, persona, normalised_intent, sample_content` |

**Template:**

```
You are a Knowledge Builder Framework source analyst. Review the sample content
fetched from a source and produce a capability inventory.

Source ID: {source_id}
Persona: {persona}
Intent: {normalised_intent}

Sample content (up to 3 pages):
{sample_content}

Return ONLY a JSON object:
{{
  "source_id": "{source_id}",
  "available_fields": [
    {{"field": "snake_case_name", "type": "string|array|integer",
      "confidence": "high|medium|synthesisable|low",
      "evidence": "quote or location from sample (< 100 chars)"}}
  ],
  "missing_fields": [
    {{"field": "field_the_intent_might_want",
      "reason": "why this content cannot supply it"}}
  ],
  "suggested_fields": [
    {{"field": "snake_case_name", "type": "string|array|integer",
      "reason": "why this is consistently present and useful"}}
  ],
  "summary": "2-3 sentence overview of what this source contains"
}}

Confidence taxonomy for available_fields:
- "high": the field value is present as an explicitly labelled element in the source
  (e.g. a named table cell, a heading with its content, a structured field).
- "medium": the field value is clearly inferrable from the source with minimal
  interpretation (e.g. a status implied by a colour-coded row, a date in a nearby cell).
- "synthesisable": the field VALUE must be DERIVED by aggregating or combining content
  that IS present in the source but does not appear as a single labelled element.
  Examples: "risks" synthesised from WBS table status cells marked "blocked" or
  "at risk"; "next_steps" synthesised from open action items across multiple rows.
  Use this level when the source has the raw ingredients but the LLM must aggregate
  them — do NOT classify these as "low" or "missing".
- "low": the field is marginally mentioned or ambiguous; treat as advisory only.

Rules:
- "available_fields": ONLY fields clearly extractable or synthesisable from the sample.
- "suggested_fields": fields present in the sample that the intent might have missed.
- "missing_fields": fields the intent implies but the source clearly CANNOT provide
  even via synthesis (the raw content is genuinely absent).
- Base ALL findings on the sample content — do not invent.
```

---

## review_design_replan

| Field | Value |
|---|---|
| id | `review_design_replan` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `2048` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `current_design, edit_request, updated_source_capability` |

**Template:**

```
You are a Knowledge Builder Framework skill architect. The user wants to modify the
current skill design. Return ONLY the changes needed as a diff object.

Current design: {current_design}
User edit request: "{edit_request}"
Updated source capability (if sources changed): {updated_source_capability}

Return ONLY a JSON diff object with keys matching what changed:
{{
  "schema_add": {{"field_name": {{"type": "...", "description": "...", "maxLength": 500}}}},
  "schema_remove": ["field_to_remove"],
  "schema_update": {{"field_name": {{"description": "updated instruction"}}}},
  "source_bindings_add": {{"new_field": ["source_id"]}},
  "source_bindings_remove": ["field_to_unbind"],
  "workflow_shape_update": {{"layout": "new_layout"}},
  "reuse_plan_update": {{"covered": {{}}, "gaps": ["field1"]}},
  "open_questions": ["any new questions from the edit"]
}}

Rules:
- Only include keys that actually change — omit unchanged sections.
- If the edit request is trivial (rename, description change), return only the
  affected field in schema_update.
- If new sources must be added (the edit implies data not in the inventory),
  add an open_question noting the source gap.
```

---

## review_extract

| Field | Value |
|---|---|
| id | `review_extract` |
| version | `1.0` |
| model | `synthesis` |
| max_tokens | `4096` |
| response_format | `json_object` |
| locked | `False` |
| required_vars | `field_lines, text` |

**Template:**

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

---

## Persona Overlays

Source: `framework/config/prompts/persona_overlays.yaml`

### architect

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Technical precision above all. Do not simplify or rephrase technical terms — the audience is senior engineers and architects who will implement from this document. Rationale must reference the framework's core principles (spec §2): polyglot storage, deterministic extraction, LLM-in-ingestion vs LLM-in-retrieval separation, etc. Alternatives must be specific — "Option B" with a name, not vague. Consequences should separate positive from negative explicitly. Reversibility is a required field for every decision. Affected-components should reference the spec §5 component map names (core/, adapters/, parsers/, stores/, retrievers/, orchestrator/). Cadence: per decision event, not periodic.
```

**`persona_few_shot_example`:**

```
Field name: "alternatives_rejected"
Type: array of objects
Description: >
  From the ADR Confluence page or the ADR markdown document, extract each
  alternative design option that was explicitly considered and rejected. For
  each: (1) the option name/label as written, (2) a 1-2 sentence summary of
  what it would have done, and (3) the stated rejection reason. If the ADR
  uses "Option A / B / C" labelling, preserve those labels. If rejection
  reasons are implicit (e.g. listed under "Cons"), synthesise a concise
  rejection reason from the cons list. Do not fabricate options.
Example extracted value:
  - option: "Option B — Qdrant as primary vector store"
    summary: "Use Qdrant instead of pgvector as the vector storage backend."
    rejection_reason: >
      Adds operational complexity (separate service to manage); pgvector on
      ADB provides sufficient performance for the v1 scale target
      (<10M chunks); Qdrant migration path deferred to ADR-002 escalation
      trigger.
  - option: "Option C — Pinecone managed service"
    summary: "Use Pinecone's managed vector index."
    rejection_reason: >
      External SaaS dependency conflicts with the OCI-first mandate; no
      on-prem / private-cloud deployment path available.
```

**`persona_key_fields`:**

```
decision_summary, rationale, alternatives_rejected, consequences, affected_components, open_questions
```

---

### developer

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Developer-to-developer communication: assume the reader is a senior engineer who will read the source. Be technically precise — use actual class names, method names, and file paths. Public interface should list the most important entry points, not every method. Known limitations must be honest: if a path is stub-only (e.g. no ADB, no real embeddings), say so explicitly. Test coverage notes should be actionable: "X is not tested; add Y test when Z is provisioned." Change log entries should reference commit SHAs or PR numbers if available in the source. Cadence: per-module, updated on significant change.
```

**`persona_few_shot_example`:**

```
Field name: "known_limitations"
Type: array of strings
Description: >
  From the module's docstring, inline TODO comments, or the engineering wiki
  page for this component, extract every documented limitation or known gap.
  Include: (1) stub-only code paths that require external provisioning (ADB,
  Vault, OpenAI) to activate, (2) scale assumptions baked into the
  implementation (e.g. "designed for <10K chunks per space"), and (3) any
  explicit "NOT IMPLEMENTED" or "deferred to vN" markers. Synthesise from
  comments if no dedicated section exists. Do not include aspirational TODOs
  that are not current limitations.
Example extracted value:
  - "FilestoreSessionStore uses Jaccard overlap (lexical) not vector similarity;
     will miss semantically-equivalent-but-lexically-different intents until
     ADB + HNSW index is provisioned."
  - "OCI bastion auto-reconnect is laptop-mode only (KBF_ENV=laptop guard).
     Production environments must handle SSH tunnel expiry via their own
     infrastructure keep-alive."
  - "_EXTRACT_MAX_TOKENS is capped at 4096; schemas with >40 fields may still
     hit the ceiling on models with smaller context windows."
```

**`persona_key_fields`:**

```
component_summary, public_interface, dependencies, known_limitations, test_coverage_notes, change_log
```

---

### eng_mgr

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Engineering-management lens: delivery cadence, team capacity, and risk. Avoid product-feature language (that is the PM's domain). Sprint health should quantify (e.g. "7/10 story points delivered by day 5 of 10"). Team capacity numbers should be headcount-accurate. Tech-debt items should link to the Jira ticket or Confluence page if available. Dependency blockers must name the blocking team and the expected unblock date. Shipped highlights should be written for a non-engineering audience: describe the user-facing or operational impact, not the implementation mechanism. Cadence: weekly sprint rhythm.
```

**`persona_few_shot_example`:**

```
Field name: "tech_debt_items"
Type: array of objects
Description: >
  Extract active tech-debt items that the engineering team is spending time on
  this sprint. Source: Jira filter for the team's board filtered by label
  "tech-debt" or epic "Platform Health". For each item: (1) Jira ticket key,
  (2) one-line description of the debt and its impact, (3) estimated effort
  remaining (story points or days), and (4) whether it is blocking feature
  work. If the team's Confluence sprint page has a "Tech Debt" section, extract
  from there preferentially. Do not include closed/done items.
Example extracted value:
  - ticket: "FAAAS-1423"
    description: >
      AdbSessionStore pool-attached path has zero unit test coverage — any
      real-backend regression ships undetected.
    effort_remaining: "3 points"
    blocks_feature_work: true
  - ticket: "FAAAS-1387"
    description: >
      Bastion SSH tunnel reconnect logic does not surface partial-failure
      state to the operator — silent retry masking real connectivity issues.
    effort_remaining: "2 points"
    blocks_feature_work: false
```

**`persona_key_fields`:**

```
sprint_health, delivery_risk, team_capacity, tech_debt_items, dependency_blockers, shipped_highlights
```

---

### kbf_ops

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
KBF operations lens: this persona reviews authorSkill sessions for quality and diagnoses extraction failures. Be diagnostic and precise. Schema coverage must be a fraction with numerator and denominator (e.g. "7/9 fields extracted"). Faithfulness score must identify which specific fields failed and why (e.g. "orm_status: extracted value not grounded in source snippet — LLM hallucinated RAG colour"). Prompt gap findings should name the specific prompt constant (e.g. `_DESIGN_SKILL_PROMPT`) and the line of reasoning that failed. Recommended actions must be implementation-ready: "Update field description for 'blocking_issues' to specify that blockers should be synthesised from WBS table status cells, not only from explicitly labelled 'Blockers' sections." Cadence: per reviewed session.
```

**`persona_few_shot_example`:**

```
Field name: "prompt_gap_findings"
Type: array of objects
Description: >
  From the session review data (eval JSONL, session transcript, or reviewSkillSession
  output), identify specific gaps in the prompts or schema that caused extraction
  failures. For each finding: (1) the affected prompt or schema element, (2) the
  observed failure (what the LLM did instead of what was expected), (3) the likely
  cause (e.g. ambiguous instruction, missing confidence level, no few-shot example),
  and (4) a severity rating (high / medium / low based on impact on artifact quality).
  Ground each finding in observed evidence from the session — do not speculate beyond
  what the session data shows.
Example extracted value:
  - element: "_DESIGN_SKILL_PROMPT — synthesisable field inclusion rule"
    failure: >
      'risks' and 'next_steps' were excluded from the generated schema because
      INSPECT_SOURCES returned confidence=medium for WBS table content, and the
      prompt's inclusion rule only allowed high/medium verbatim fields. The WBS
      table content was present but required synthesis across multiple rows.
    likely_cause: >
      No 'synthesisable' confidence level existed at INSPECT_SOURCES time;
      the prompt rule treated synthesisable content the same as absent content.
    severity: "high"
  - element: "_CAPTURE_INTENT_PROMPT — ambiguity classification"
    failure: >
      'which Confluence space?' was listed as an ambiguity but classified as
      nice_to_know; user typed 'ok' and the state advanced without resolution.
      The resulting schema used the wrong space (FA-LEGACY instead of FAAAS).
    likely_cause: >
      Prompt did not distinguish blocking_ambiguities (prevent correct schema
      design) from nice_to_know (can proceed with assumption). All ambiguities
      were treated as advisory.
    severity: "high"
```

**`persona_key_fields`:**

```
session_quality_summary, schema_coverage, faithfulness_score, prompt_gap_findings, recommended_actions, session_metadata
```

---

### ops_eng

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Operational precision: exact timestamps, exact service names (use the canonical Fusion service name, not informal nicknames), and exact metrics (MTTR in minutes/hours, pods affected as a count). Root cause must be stated as a causal chain if known: "X happened because Y which caused Z." If root cause is still under investigation, state that explicitly — do not guess. Action items must be time-bound and assigned. Severity must match the incident management taxonomy (do not invent your own scale). Affected-services should list the Oracle Fusion module (e.g. Financials Cloud, HCM, SCM) plus the infrastructure layer (OCI region, ADB tier). Cadence: per-incident, not periodic.
```

**`persona_few_shot_example`:**

```
Field name: "root_cause"
Type: string
Description: >
  Extract the confirmed or probable root cause of the incident from the Jira
  incident ticket or the post-mortem Confluence page. If a formal RCA section
  exists, use it verbatim (truncated to 500 chars if necessary). If no formal
  RCA exists, synthesise from the "What happened" and "Timeline" sections:
  identify the initiating event, the propagation mechanism, and the failure
  mode. If root cause is listed as "TBD" or "under investigation", return
  that phrase verbatim rather than guessing. Prefix confirmed root causes with
  "[Confirmed]" and probable root causes with "[Probable]".
Example extracted value: >
  [Confirmed] OCI Compute quota exhaustion in eu-frankfurt-1 prevented the
  auto-scaling group from launching replacement pods during the 26ai patching
  window. The pod count fell below the minimum healthy threshold, triggering
  Fusion Financials read-only mode for 47 minutes. Root cause was an approved
  quota increase request that had not yet been applied when the patching
  window opened.
```

**`persona_key_fields`:**

```
incident_summary, severity, affected_services, mttr, root_cause, action_items
```

---

### ops_mgr

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Operations-management lens: fleet-wide aggregates, not per-incident detail. SLA compliance must cite the target (e.g. "99.9% availability target") and the actual achieved value with the measurement period. Top incidents should be 1-2 sentence summaries with severity and customer-impact scope — not full RCAs. Capacity outlook should quantify: "N new pods requested; provisioning lead time M weeks." Team load should include on-call count and any toil- reduction wins. Improvement priorities should be outcome-oriented: "Reduce P2 MTTR from 4h to 2h by Q3." Cadence: monthly or quarterly.
```

**`persona_few_shot_example`:**

```
Field name: "sla_compliance"
Type: object
Description: >
  Extract SLA compliance metrics for the reporting period from the ops
  manager's Confluence summary page or the SLA dashboard export. Required
  sub-fields: (1) reporting_period (e.g. "April 2026"), (2)
  availability_target (e.g. "99.9%"), (3) availability_actual (e.g. "99.87%"),
  (4) p1_mttr_target_hours, (5) p1_mttr_actual_hours, (6) sla_met (boolean).
  If the source has multiple customer tiers (Gold/Silver/Bronze SLA), extract
  the worst-performing tier and note the tier name. If any sub-field is not
  available in the source, set it to null — do not fabricate.
Example extracted value:
  reporting_period: "April 2026"
  availability_target: "99.9%"
  availability_actual: "99.87%"
  p1_mttr_target_hours: 4
  p1_mttr_actual_hours: 5.2
  sla_met: false
  note: "Gold tier — one P1 incident (INC-2026-0412) exceeded MTTR target by 1.2h"
```

**`persona_key_fields`:**

```
service_health_summary, top_incidents, sla_compliance, capacity_outlook, team_load, improvement_priorities
```

---

### pm

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Product-management lens: focus on user value, release readiness, and risk. Avoid engineering-implementation detail (commits, build IDs, test counts) unless they directly affect release readiness. Release status should be a single human-readable phrase. Known risks should be ranked by probability × impact. Go/no-go criteria must be boolean-checkable (each criterion is either met, not met, or in progress). Customer-impact statements should be written in the voice of a release note: "Customers will now be able to..." Cadence: per-release cycle (not weekly). Use present tense for current status, future tense for upcoming.
```

**`persona_few_shot_example`:**

```
Field name: "go_nogo_criteria"
Type: array of objects
Description: >
  Extract each formal go/no-go criterion for the 25.01 Fusion Applications
  quarterly release. For each criterion: (1) the criterion text as stated in
  the release checklist Confluence page, (2) current status (met / not_met /
  in_progress), and (3) owner team. If the source lists criteria under a
  "Release Readiness" or "Exit Gates" heading, use those verbatim. If no
  formal list exists, infer from action items tagged "release blocker" in Jira.
  Do not fabricate criteria.
Example extracted value:
  - criterion: "All P0/P1 Jira issues resolved or deferred with PM sign-off"
    status: "in_progress"
    owner: "PM"
  - criterion: "Security pen-test sign-off received from Cloud Security"
    status: "met"
    owner: "Cloud Security"
  - criterion: "Customer-facing release notes reviewed and approved by Docs"
    status: "not_met"
    owner: "Docs Team"
```

**`persona_key_fields`:**

```
release_scope, release_status, known_risks, go_nogo_criteria, stakeholder_asks, customer_impact
```

---

### service_owner

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Service-ownership lens: the reader is either a new on-call engineer learning the service or a stakeholder asking "who owns X and is it healthy?" Be unambiguous about ownership (named team and individual). Operational status should include a last-updated timestamp if available. Dependencies must distinguish between hard dependencies (service fails without them) and soft dependencies (degraded but functional). Known risks should each have a status (open / mitigated / accepted). Runbook links should be real Confluence or wiki URLs — do not fabricate. Cadence: per-service page, updated on change.
```

**`persona_few_shot_example`:**

```
Field name: "dependencies"
Type: array of objects
Description: >
  From the service's Confluence page or the service catalogue entry, extract
  all upstream dependencies. For each: (1) the dependency name (service or
  infrastructure), (2) whether it is a hard dependency (service fails without
  it) or soft (degraded mode), (3) the owner team of the dependency, and (4)
  a brief description of what capability it provides. If the page lists
  dependencies in a table, extract from the table. If they are embedded in
  prose, synthesise from sentences like "X relies on Y for Z." Do not include
  this service itself as a dependency.
Example extracted value:
  - name: "Oracle 23ai Autonomous Database (ADB)"
    dependency_type: "hard"
    owner_team: "OCI Database Platform"
    provides: "Session persistence, skill artifact storage, vector index"
  - name: "OCI Vault"
    dependency_type: "hard"
    owner_team: "Cloud Security"
    provides: "API credentials, encryption keys for ADB wallet"
  - name: "Confluence (Atlassian Cloud)"
    dependency_type: "soft"
    owner_team: "IT / Platform Tools"
    provides: "Knowledge source ingestion; framework degrades to cached content if unavailable"
```

**`persona_key_fields`:**

```
service_description, operational_status, owner_team, dependencies, known_risks, runbook_links
```

---

### tpm

**Applies to:** capture_intent, design_skill

**`persona_extraction_style`:**

```
Use exec-safe language throughout — no jargon, no internal code names without expansion, no ambiguous acronyms. RAG status (Red/Amber/Green) must always be accompanied by a 1-sentence justification. Risks should be stated as "X is at risk because Y; mitigation is Z." Next steps must be time-bound (by [date] or "this sprint"). Keep field values concise: the audience is a VP or above reading a weekly status deck in under 2 minutes. Aggregation: when multiple Confluence pages or Jira epics cover the same program, synthesise a single programme-level view — do not dump per-project details. Cadence: weekly. Avoid passive voice.
```

**`persona_few_shot_example`:**

```
Field name: "blocking_issues"
Type: array of objects
Description: >
  List every open blocker for the 26ai FA DB upgrade program. For each blocker,
  extract: (1) a one-line description of the issue, (2) the owner team or named
  individual, (3) the target resolution date if stated, and (4) whether it is
  on the critical path. Derive blockers from the "Blockers / Risks" section of
  the weekly ops Confluence page; if none is labelled explicitly, synthesise
  from action items flagged as "blocked" or "waiting on". If no blockers exist,
  return an empty array — do not fabricate entries.
Example extracted value:
  - description: "26ai pod patching blocked on OCI Compute quota increase request"
    owner: "Infra Ops"
    target_resolution: "2026-05-22"
    critical_path: true
  - description: "DR drill sign-off pending Legal review of RTO SLA amendment"
    owner: "TPM - S. Kumar"
    target_resolution: "2026-05-29"
    critical_path: false
```

**`persona_key_fields`:**

```
orm_status, rag_summary, schedule_health, blocking_issues, next_steps, exec_asks
```

---

