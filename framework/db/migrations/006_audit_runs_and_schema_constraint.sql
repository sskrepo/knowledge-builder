-- Migration 006: KBF_AUDIT_RUNS table + extraction_schema constraint fix
-- Implements ADR-023: kbf_ops persona and reviewSkillSession MCP tool.
--
-- Changes:
--   1. ALTER KB_SHIM.KBF_SKILL_ARTIFACTS — extend chk_ksa_artifact_type to
--      include 'extraction_schema' (5th artifact type added in this milestone).
--      Required only if migration-005 was already applied without it.
--      Idempotent: drops old constraint (ORA-02443 ignored), adds new one
--      (ORA-02264 ignored if already correct).
--
--   2. CREATE KB_SHIM.KBF_AUDIT_RUNS — history of reviewSkillSession runs.
--
-- All statements idempotent (ORA-00955/ORA-02264/ORA-02443 suppressed).

-- ---------------------------------------------------------------------------
-- 1. Extend chk_ksa_artifact_type to include 'extraction_schema'
-- ---------------------------------------------------------------------------

-- Drop the old 4-type constraint (ignore if already dropped / never existed)
BEGIN
    EXECUTE IMMEDIATE
        'ALTER TABLE KB_SHIM.KBF_SKILL_ARTIFACTS DROP CONSTRAINT chk_ksa_artifact_type';
EXCEPTION
    WHEN OTHERS THEN
        -- ORA-02443: Cannot drop constraint - nonexistent constraint
        IF SQLCODE != -2443 THEN RAISE; END IF;
END;
/

-- Add the new 5-type constraint (ignore if already exists with correct name)
BEGIN
    EXECUTE IMMEDIATE '
        ALTER TABLE KB_SHIM.KBF_SKILL_ARTIFACTS
        ADD CONSTRAINT chk_ksa_artifact_type CHECK (
            artifact_type IN (
                ''workflow_skill'',
                ''persona_builder_delta'',
                ''eval_extraction'',
                ''eval_workflow'',
                ''extraction_schema''
            )
        )
    ';
EXCEPTION
    WHEN OTHERS THEN
        -- ORA-02264: name already used by existing constraint
        IF SQLCODE != -2264 THEN RAISE; END IF;
END;
/

-- ---------------------------------------------------------------------------
-- 2. KBF_AUDIT_RUNS — dedup + history of reviewSkillSession audit runs
-- ---------------------------------------------------------------------------

BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE KB_SHIM.KBF_AUDIT_RUNS (
            review_id       VARCHAR2(64)    NOT NULL,
            synth_id        VARCHAR2(64)    NOT NULL,
            run_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP,
            depth           VARCHAR2(16)    DEFAULT ''full'',
            overall_score   NUMBER(4,1),
            recommendation  VARCHAR2(32),
            bugs_filed      NUMBER(4)       DEFAULT 0,
            triggered_by    VARCHAR2(128),
            report_json     CLOB,
            CONSTRAINT pk_kbf_audit_runs PRIMARY KEY (review_id),
            CONSTRAINT chk_kar_depth CHECK (
                depth IN (''structural'', ''semantic'', ''full'')
            ),
            CONSTRAINT chk_kar_recommendation CHECK (
                recommendation IS NULL OR
                recommendation IN (''do_not_promote'', ''promote_with_fixes'', ''promote'')
            )
        )
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_audit_synth
        ON KB_SHIM.KBF_AUDIT_RUNS (synth_id, run_at DESC)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_audit_recommendation
        ON KB_SHIM.KBF_AUDIT_RUNS (recommendation, run_at DESC)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/
