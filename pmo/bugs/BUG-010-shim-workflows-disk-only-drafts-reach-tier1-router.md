# BUG-010: ShimWorkflows disk-only — draft skills reach Tier-1 router, silent wrong output type

**Queue ID**: BUG-queue-2ad9a
**Status**: FIXED
**Severity**: HIGH (silent wrong output — .eml request returned .pptx artifact from a different skill)
**Session**: 2026-05-16 hardening session
**Filed**: 2026-05-16
**Fixed in**: 8c2bec1 — `framework/orchestrator/shim_workflows.py` (`all_cards()`), `framework/deploy/mcp_server.py`, `framework/deploy/skill_store/adb.py`, `framework/deploy/skill_store/filestore.py`, `framework/skill_builder/synthesize_workflow.py` (`_build_skill_card`)

---

## Symptom

User promoted skill `tpm.project_tracking_weekly_stakeholder_status_email` (output type: `.eml`) and called `askKnowledgeBase` to generate a weekly stakeholder email. The Tier-1 router returned `artifact_path = ~/.kbf/outputs/26ai_confluence_pptx.pptx` — a completely different skill's artifact — and the answer stated "output_eml had no support". A stricter call with `functionalArea` set fell back to `no_answer`. No error surface — the system appeared to succeed.

---

## Root Cause

`ShimWorkflows.all_cards()` was disk-only: it read all YAML files from `framework/workflow_skills/` with no filtering by ADB promotion status. Draft skills (on-disk authoring artifacts never promoted to ADB) were indistinguishable from promoted skills. The Tier-1 LLM classifier received both promoted and draft skill cards and picked the closest match — which happened to be the `.pptx` draft whose card text didn't mention the output format. Additionally, `_build_skill_card` in `synthesize_workflow.py` truncated `example_invocations[0]` to 100 characters and omitted the output format token, making `.eml` and `.pptx` skill cards nearly identical to the classifier.

This contrasts with `ShimKb`, which has been ADB-aware since ADR-015 Option B. `ShimWorkflows` never received that treatment.

---

## Fix

Two fixes in commit 8c2bec1:

**Fix 1 — ADB-aware ShimWorkflows (mirrors ShimKb pattern):**
- `ShimWorkflows.__init__` gains `skill_store=None` param.
- `all_cards()` now queries `skill_store.list_promoted_workflow_skills()` when wired — only promoted/production skills reach the classifier.
- `all_cards_including_draft()` added for tooling/CLI (never used by classifier).
- Laptop mode (no store): serves all cards with explicit INFO log — no silent degradation.
- Store failure: WARNING + returns empty list — no drafts reach the classifier while store recovers.
- New abstract method `list_promoted_workflow_skills()` on `SkillStore` ABC; implemented in `AdbSkillStore` (SQL: `KBF_SKILL_ARTIFACTS WHERE artifact_type='workflow_skill' AND status IN ('promoted','production')`) and `FilestoreSkillStore` (reads `~/.kbf/workflow_promotions/`).
- `mcp_server.py`: `ShimWorkflows` wired with `skill_store=app.state.skill_store`.

**Fix 2 — Differentiated skill card text:**
- `synthesize_workflow.py` `_build_skill_card`: `example_invocations[0]` now `task[:300] + " Output: {output_format}."` (was `task[:100]`, no format token). `use_when` also carries output format. Classifier can now distinguish `.eml` from `.pptx` skills by card text alone.

ADB (`KBF_SKILL_ARTIFACTS`) is the single source of truth for workflow promotion. Disk YAML is authoring artifact only; never written at runtime.

---

## How Found

User `reportBug` → BUG-queue-2ad9a (2026-05-16T18:49). User observed `.eml` request returning `.pptx` artifact and filed via MCP `reportBug` tool. Architect performed root-cause analysis (ShimWorkflows disk-only vs ShimKb ADB-aware) during investigation of the promotion status gap.

---

## Related

- ADR-015: ShimKb ADB-aware promotion (the pattern ShimWorkflows now follows)
- ADR-016: Workflow skills contract (amended by this fix: disk YAML is authoring artifact only)
- BUG-queue-990fe (BUG-011): silent wrong-page — separate root cause, same session
- BUG-012: ADR-032 space_allow_list wrong value — downstream fix enabled by this fix
