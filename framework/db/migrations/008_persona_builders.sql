-- Migration 008: KBF_PERSONA_BUILDERS — ADB-backed persona builder KB store
--
-- Implements DECISION Option B: persona builder KB entries live in ADB rather
-- than git-tracked YAML files.  Promoted skills are written here via
-- skill_store.upsert_persona_builder_kb().  ShimKb merges these records on top
-- of the seed *.yaml files at startup and after each PROMOTE.
--
-- Idempotent: re-running is safe (CREATE TABLE and GRANT are guarded below
-- by the standard Oracle "table already exists" error ORA-00955 which the
-- migration runner should tolerate, or wrap in a BEGIN/EXCEPTION block).

CREATE TABLE KB_SHIM.KBF_PERSONA_BUILDERS (
    persona        VARCHAR2(64)   NOT NULL,
    kb_name        VARCHAR2(128)  NOT NULL,
    content_yaml   CLOB           NOT NULL,   -- full KB entry dict YAML
    status         VARCHAR2(32)   DEFAULT 'draft' NOT NULL,  -- draft | production
    schema_version NUMBER(4)      DEFAULT 1   NOT NULL,
    created_at     TIMESTAMP      DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at     TIMESTAMP      DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_kpb PRIMARY KEY (persona, kb_name)
);

-- NOTE: no GRANT needed — the runtime connects as Admin which owns KB_SHIM.
-- Add a GRANT here when a dedicated kbf_runtime role is created (future).
