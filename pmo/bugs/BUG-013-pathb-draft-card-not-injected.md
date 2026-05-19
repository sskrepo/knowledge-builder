# BUG-013: Path B routing self-test — draft skill card not in INGEST+ candidate set

**Queue ID**: BUG-queue-decision013
**ADB Bug ID**: BUG-dcb26cd3
**Status**: FIXED
**Severity**: HIGH (EVAL Path B always fails for new skills being authored — PROMOTE always blocked)
**Session**: 2026-05-19 authorSkill PROMOTE mission
**Filed**: 2026-05-19
**Fixed in**: 09aa33c — `framework/skill_builder/conversation.py` (`_run_eval`)

---

## Symptom

EVAL Path B routing self-test failed for skill `tpm.faaas_kiwi_project_pptx` with:
- 3/5 positive queries routed to `faaas_kiwi_project_pptx` (already-promoted disk YAML)
- The remaining 2 queries routed to NULL (no skill) instead of the draft

DECISION-021 comment in the code stated: "INGEST+ candidate set: all_cards_including_draft() includes the in-authoring skill" — but this was **false**.

---

## Root Cause

`_run_eval` built the INGEST+ candidate set with:

```python
_ingest_plus_cards = _shim.all_cards_including_draft()
```

`all_cards_including_draft()` scans YAML files from `framework/workflow_skills/` on disk only. A skill being authored is in `COMMITTED` or `EVAL` state — its on-disk YAML exists but has `status: draft`. However, the on-disk YAML contains the design from the authoring session that was committed to disk BEFORE the current session's refinements.

In this case, the on-disk `faaas_kiwi_project_pptx.yaml` had the routing_queries and skill_card from a PREVIOUS session. The CURRENT session's `design_skill_card` (with updated routing_queries, use_when, etc.) was only in memory and in ADB — not propagated to disk until promotion. The classifier tested the current session's queries against the stale disk card → misroutes.

Additionally, when no disk YAML exists at all (first-time author of a new skill), `all_cards_including_draft()` would not contain the draft at all, causing all positive queries to fail.

---

## Fix

Filter out any existing disk card with the same `(persona, skill_name)`, then inject the current session's `design_skill_card` as the draft candidate:

```python
_disk_cards_raw = _shim.all_cards_including_draft()
_ingest_plus_cards = [
    c for c in _disk_cards_raw
    if not (c.get("persona") == persona and c.get("name") == skill_name)
]
_draft_card_data = self._data.design_skill_card or {}
_draft_card_for_classifier: dict = {
    "name": skill_name,
    "persona": persona,
    "summary": _draft_card_data.get("summary", ""),
    "use_when": _draft_card_data.get("use_when", ""),
    "example_invocations": _draft_card_data.get("example_invocations", []),
    "do_not_invoke_if_phrases": _draft_card_data.get("do_not_invoke_if_phrases", []),
    "routing_queries": _draft_card_data.get("routing_queries", {}),
    "on_request": True,
    "status": "draft",
    "_cfg": {
        "skill_card": _draft_card_data,
        "source_binding": {"mode": self._data.source_binding_mode or ""},
    },
}
_ingest_plus_cards.append(_draft_card_for_classifier)
```

---

## Tests Added

`framework/tests/unit/test_strategy1b_adb_content_and_pathb_draft_card.py`:
- `TestPathBDraftCardInjection::test_draft_card_is_injected_when_no_disk_cards`
- `TestPathBDraftCardInjection::test_draft_card_replaces_duplicate_disk_card`
- `TestPathBDraftCardInjection::test_other_disk_cards_are_not_removed`
- `TestPathBDraftCardInjection::test_draft_card_carries_routing_queries`
