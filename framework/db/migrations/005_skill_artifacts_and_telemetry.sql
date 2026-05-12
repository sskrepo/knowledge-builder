-- Migration 005: skill artifacts and telemetry tables
-- Implements DECISION-006 Option A: Oracle ADB as durable store for
-- skill authoring artifacts and operational telemetry.
--
-- Tables created:
--   KB_SHIM.KBF_SKILL_ARTIFACTS  — synthesized skill files (4 per session)
--   KB_SHIM.KBF_ERROR_LOG        — server-side error records
--   KB_SHIM.KBF_BUG_REPORTS      — user-reported bug records (reportBug tool)
--   KB_SHIM.KBF_COST_LOG         — LLM token usage telemetry
--
-- All statements wrapped in BEGIN..EXCEPTION blocks so re-runs are safe (idempotent).
-- ORA-00955 = "name is already used by an existing object"

-- ---------------------------------------------------------------------------
-- KBF_SKILL_ARTIFACTS
-- ---------------------------------------------------------------------------

BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE KB_SHIM.KBF_SKILL_ARTIFACTS (
            artifact_id     VARCHAR2(300)   NOT NULL,
            synth_id        VARCHAR2(200)   NOT NULL,
            persona         VARCHAR2(100)   NOT NULL,
            skill_name      VARCHAR2(200)   NOT NULL,
            artifact_type   VARCHAR2(50)    NOT NULL,
            rel_path        VARCHAR2(500)   NOT NULL,
            content         CLOB            NOT NULL,
            status          VARCHAR2(20)    DEFAULT ''draft'' NOT NULL,
            created_at      TIMESTAMP       DEFAULT SYSTIMESTAMP NOT NULL,
            updated_at      TIMESTAMP       DEFAULT SYSTIMESTAMP NOT NULL,
            CONSTRAINT pk_kbf_skill_artifacts PRIMARY KEY (artifact_id),
            CONSTRAINT chk_ksa_artifact_type CHECK (
                artifact_type IN (
                    ''workflow_skill'',
                    ''persona_builder_delta'',
                    ''eval_extraction'',
                    ''eval_workflow''
                )
            ),
            CONSTRAINT chk_ksa_status CHECK (
                status IN (''draft'', ''promoted'', ''archived'')
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
        CREATE INDEX idx_ksa_synth_id
        ON KB_SHIM.KBF_SKILL_ARTIFACTS (synth_id)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_ksa_persona_skill
        ON KB_SHIM.KBF_SKILL_ARTIFACTS (persona, skill_name)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ---------------------------------------------------------------------------
-- KBF_ERROR_LOG
-- ---------------------------------------------------------------------------

BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE KB_SHIM.KBF_ERROR_LOG (
            error_id        VARCHAR2(100)   DEFAULT SYS_GUID() NOT NULL,
            request_id      VARCHAR2(200),
            timestamp_utc   TIMESTAMP       DEFAULT SYSTIMESTAMP NOT NULL,
            tool            VARCHAR2(100),
            error_type      VARCHAR2(200),
            message         CLOB,
            stack_trace     CLOB,
            extra_json      CLOB,
            CONSTRAINT pk_kbf_error_log PRIMARY KEY (error_id)
        )
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kel_timestamp
        ON KB_SHIM.KBF_ERROR_LOG (timestamp_utc)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kel_tool
        ON KB_SHIM.KBF_ERROR_LOG (tool)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ---------------------------------------------------------------------------
-- KBF_BUG_REPORTS
-- ---------------------------------------------------------------------------

BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE KB_SHIM.KBF_BUG_REPORTS (
            bug_id          VARCHAR2(100)   DEFAULT SYS_GUID() NOT NULL,
            request_id      VARCHAR2(200),
            queue_id        VARCHAR2(200),
            timestamp_utc   TIMESTAMP       DEFAULT SYSTIMESTAMP NOT NULL,
            tool            VARCHAR2(100),
            description     CLOB,
            extra_json      CLOB,
            CONSTRAINT pk_kbf_bug_reports PRIMARY KEY (bug_id)
        )
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kbr_timestamp
        ON KB_SHIM.KBF_BUG_REPORTS (timestamp_utc)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kbr_queue_id
        ON KB_SHIM.KBF_BUG_REPORTS (queue_id)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ---------------------------------------------------------------------------
-- KBF_COST_LOG
-- ---------------------------------------------------------------------------

BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE KB_SHIM.KBF_COST_LOG (
            cost_id         VARCHAR2(100)   DEFAULT SYS_GUID() NOT NULL,
            timestamp_utc   TIMESTAMP       DEFAULT SYSTIMESTAMP NOT NULL,
            persona         VARCHAR2(100),
            operation       VARCHAR2(100),
            skill_name      VARCHAR2(200),
            prompt_tokens   NUMBER(10)      DEFAULT 0 NOT NULL,
            completion_tokens NUMBER(10)    DEFAULT 0 NOT NULL,
            total_tokens    NUMBER(10)      DEFAULT 0 NOT NULL,
            CONSTRAINT pk_kbf_cost_log PRIMARY KEY (cost_id)
        )
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kcl_timestamp
        ON KB_SHIM.KBF_COST_LOG (timestamp_utc)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kcl_persona
        ON KB_SHIM.KBF_COST_LOG (persona)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE '
        CREATE INDEX idx_kcl_operation
        ON KB_SHIM.KBF_COST_LOG (operation)
    ';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
END;
/
