# BUG-014: Path B routing self-test — case-sensitive skill_name comparison fails on LLM capitalisation drift

**Queue ID**: BUG-queue-decision013
**ADB Bug ID**: BUG-6ee82f64
**Status**: FIXED
**Severity**: MEDIUM (intermittent EVAL Path B failure when LLM returns mixed-case skill slug)
**Session**: 2026-05-19 authorSkill PROMOTE mission
**Filed**: 2026-05-19
**Fixed in**: 38d4b71 — `framework/skill_builder/conversation.py` (`_run_eval`)

---

## Symptom

EVAL Path B routing self-test produced:

```
POSITIVE FAIL [HIGH]: 'Build a new pptx containing the Kiwi Project update slide...'
  → faaaS_kiwi_project_pptx tier 1
```

The resolved_skill_name `faaaS_kiwi_project_pptx` (capital S) did not equal the expected `faaas_kiwi_project_pptx` (all lowercase), causing a false EVAL failure and PROMOTE block.

---

## Root Cause

The routing self-test comparison in `_run_eval` was:

```python
passed = (classification.tier == 1 and resolved_skill == skill_name)
```

`classification.workflow_skill` is the raw string returned by the LLM IntentClassifier. The LLM occasionally returns mixed-case skill_name slugs (e.g. `faaaS_...` instead of `faaas_...`). Since skill_names are always lowercase slugs by convention (`_slugify()` in conversation.py enforces this), the comparison should be case-insensitive.

The negative assertion had the same issue:
```python
passed = classification.tier != 1 or resolved_skill != skill_name
```

---

## Fix

Normalise `resolved_skill` to lowercase before comparison in both positive and negative paths:

```python
resolved_skill = classification.workflow_skill
# Normalise to lowercase: skill_names are slugs; the LLM
# may occasionally return mixed-case. Case-insensitive comparison
# avoids false failures on LLM capitalisation drift.
resolved_skill_lower = resolved_skill.lower() if resolved_skill else resolved_skill
...
passed = (classification.tier == 1 and resolved_skill_lower == skill_name)
```

---

## Impact

Without this fix, a single LLM capitalisation variant in a routing query causes EVAL Path B to fail and PROMOTE to be blocked permanently — the only workaround would be re-running EVAL repeatedly until the LLM consistently returns lowercase, which is non-deterministic.

---

## Tests

Existing test `test_decision021_pathb_intent_classifier.py` covers the Path B routing logic. The normalisation fix makes the test suite more robust against LLM output variance. No new tests added — the fix is a one-line normalisation in each comparison.
