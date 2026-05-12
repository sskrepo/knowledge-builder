-- kb_shim schema — Oracle 23ai Autonomous Database
-- Owner: PDD V3 §16 (deployment interaction layer), ADR-003 (Store Protocol)
-- Used by: framework/deploy/session/adb_store.py
-- Run via: kb-cli migrate --schema kb_shim --env laptop
--
-- This script:
--   1. Creates the KB_SHIM schema (user) if it does not exist.
--   2. Grants minimum required privileges.
--   3. Creates the author_skill_sessions table + indexes.
-- Re-running is fully idempotent (ORA-01920 and ORA-00955 are swallowed).

-- =========================================================================
-- Schema (user) — KB_SHIM
-- -------------------------------------------------------------------------
-- In Oracle a "schema" is a user. Because the app-side connection pool runs
-- as ADMIN, any DML against kb_shim.* must be issued from an ADMIN session
-- (ADMIN has DBA, so it can access any schema's objects directly).
-- =========================================================================
BEGIN
  EXECUTE IMMEDIATE 'CREATE USER kb_shim IDENTIFIED BY "KBFshim2024!" QUOTA UNLIMITED ON DATA';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -1920 THEN RAISE; END IF; -- ORA-01920: user already exists — idempotent
END;
/

GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO kb_shim;

-- =========================================================================
-- Table: author_skill_sessions
-- One row per authorSkill conversation session (V3 Track D-3).
--
-- Column notes:
--   synth_id     — UUID assigned by the API layer, primary key
--   user_id      — bearer-token identity (or "anon" in dev)
--   persona      — e.g. "ops_eng", "pm", "tpm"
--   skill_name   — e.g. "incident_summary" (nullable until skill is chosen)
--   intent       — free-text intent description (large; CLOB)
--   state        — conversation state machine state name
--   session_data — full session dict serialised as JSON (CLOB)
--   status       — lifecycle: in_progress | completed | abandoned | expired
-- =========================================================================
CREATE TABLE kb_shim.author_skill_sessions (
  synth_id     VARCHAR2(128)               NOT NULL,
  user_id      VARCHAR2(128)               NOT NULL,
  persona      VARCHAR2(64),
  skill_name   VARCHAR2(256),
  intent       CLOB,
  state        VARCHAR2(64),
  session_data CLOB  CHECK (session_data IS JSON),
  created_at   TIMESTAMP WITH TIME ZONE    DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at   TIMESTAMP WITH TIME ZONE    DEFAULT SYSTIMESTAMP NOT NULL,
  expires_at   TIMESTAMP WITH TIME ZONE,
  status       VARCHAR2(32)                DEFAULT 'in_progress' NOT NULL,
  --
  CONSTRAINT pk_author_skill_sessions PRIMARY KEY (synth_id),
  CONSTRAINT chk_ass_status CHECK (status IN ('in_progress','completed','abandoned','expired'))
);

-- Index: list-by-user ordered by recency — used by _SQL_LIST in adb_store.py
CREATE INDEX idx_ass_user_updated ON kb_shim.author_skill_sessions (user_id, updated_at DESC);

-- Index: bulk-expire sweep — used by _SQL_EXPIRE_STALE in adb_store.py
CREATE INDEX idx_ass_status_expires ON kb_shim.author_skill_sessions (status, expires_at);
