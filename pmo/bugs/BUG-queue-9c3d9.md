---
queue_id: BUG-queue-9c3d9
source: user_report
tool: uploadArtifact
filed_at: 2026-05-13T06:22:49
status: open
---

# BUG-queue-9c3d9

**Tool**: `uploadArtifact` | **Filed**: 2026-05-13 | **Status**: open

uploadArtifact + ANALYZE_ARTIFACT analyzer is too limited for image-only pptx files and silently dis…

<details>
<summary>Full details</summary>

**Description**:
uploadArtifact + ANALYZE_ARTIFACT analyzer is too limited for image-only pptx files and silently disables auto-rich-description for subsequently-added fields. Repro: (1) extracted slide 15 of a reference exec-status PDF using poppler (pdftoppm) into a JPG, wrapped the JPG in a single-slide pptx via pptxgenjs (sizing: contain), uploaded via uploadArtifact (synthId synth-tpm-9d3b6233, artifactId art-c665fcc6, sizeBytes 134811, filename faaas-slide15-reference.pptx). (2) Fed 'artifact:<filename> id:<artifactId>' to authorSkill at ANALYZE_ARTIFACT. (3) Analyzer returned only 4 placeholder fields: title, section_a, section_b, section_c — no OCR, no actual extraction of the slide's content (Scope, Provisioning/Lifecycle Status, Next Steps, Key Milestones, ORM, Risk/Mitigation labels were all clearly visible in the embedded image). (4) Worse, replacing those 4 stub fields with a real 22-field list at REVIEW_FIELDS resulted in all 22 going to REVIEW_SCHEMA as stub descriptions ('Field X — refine description'), with the banner: "22 field(s) were added after the artifact analysis — their descriptions were synthesised from context and may need more refinement than the rest." So the artifact upload simultaneously (a) failed to provide useful schema seed content and (b) disabled the auto-rich-description path for fields added afterward. Net effect: artifact upload makes things strictly worse than not uploading. Recommendations: (1) when a pptx contains primarily image content, run OCR on the embedded images before classifying as 'no extractable structure'; (2) decouple "added after artifact analysis" from "needs manual refinement" — if the prior analysis produced only stubs, treat newly-added fields the same as if no artifact had been provided, and run the regular auto-rich-description path; (3) document the analyzer's known limitations (text-only PPTX, OCR off) in the uploadArtifact description.

**Triggering input**:
```json
{
  "affected_session": "synth-tpm-9d3b6233",
  "artifactId": "art-c665fcc6",
  "filename": "faaas-slide15-reference.pptx",
  "sizeBytes": 134811,
  "analyzer_output_fields": [
    "title",
    "section_a",
    "section_b",
    "section_c"
  ],
  "ground_truth_in_slide": [
    "Scope",
    "Provisioning and Lifecycle Status (with Assumptions, Status bullets, Next Steps subsections)",
    "Key Milestones (right rail)",
    "ORM",
    "Risk / Mitigation",
    "FAAASPMO-1190 (JIRA key in header)"
  ],
  "side_effect": "all 22 user-added fields came back as stubs at REVIEW_SCHEMA with 'added after artifact analysis' warning"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: uploadArtifact-analyzer-limited-for-image-pptx

</details>
