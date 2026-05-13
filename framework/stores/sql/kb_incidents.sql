-- kb_incidents schema — Oracle 23ai Autonomous Database
-- Owner: ADR-002 §kb_incidents · ADR-003 (Store/ContentItem mapping)
-- Used by: framework/stores/incident_vector_store.py
-- Run via: kb-cli migrate --schema kb_incidents --env dev
--
-- Naming: framework convention — schema kb_incidents, run as user KB_INCIDENTS_RW
-- (per framework/config/{env}.yaml `adb.schemas.kb_incidents`).

-- =========================================================================
-- Table: content_items
-- One row per ingested incident (or related ContentItem).
-- =========================================================================
CREATE TABLE content_items (
  id                       VARCHAR2(64)     NOT NULL,    -- sha256(source:source_id:schema_version)
  source                   VARCHAR2(32)     NOT NULL,    -- "jira" | "confluence"
  source_id                VARCHAR2(128)    NOT NULL,    -- e.g. "INC-12345"
  path                     VARCHAR2(512),
  title                    VARCHAR2(500)    NOT NULL,
  body                     CLOB,                          -- summarized body (LLM output)
  --
  -- Multi-axis dimensions (per ADR-008)
  persona                  VARCHAR2(32)     NOT NULL,    -- "ops_eng" for incident KB
  primary_axis_kind        VARCHAR2(32)     NOT NULL,    -- "functional_area"
  primary_axis_value       VARCHAR2(64),                 -- e.g. "patching"
  functional_area_all      JSON,                          -- multi-valued
  resources                JSON,                          -- e.g. ["pod","poddb"]
  services                 JSON,                          -- e.g. ["auth-service"]
  kind                     VARCHAR2(32)     NOT NULL,    -- "incident_history" | "postmortem" | ...
  --
  -- ACL placeholder (Phase 4 enforces) + ownership
  persona_visibility       JSON             NOT NULL,    -- e.g. ["ops_eng","ops_mgr","aira"]
  owner                    VARCHAR2(64)     NOT NULL,
  classification           VARCHAR2(16)     NOT NULL,    -- "internal" | "restricted" | "public"
  --
  -- Versioning (spec §10)
  source_sha               VARCHAR2(64)     NOT NULL,    -- raw source content hash at ingest
  parser_version           VARCHAR2(32)     NOT NULL,
  schema_version           NUMBER(3)        NOT NULL,
  --
  -- Time
  created_at               TIMESTAMP        DEFAULT SYSTIMESTAMP,
  updated_at               TIMESTAMP        DEFAULT SYSTIMESTAMP,
  last_reviewed            TIMESTAMP,
  --
  -- Provenance
  extracted_by             VARCHAR2(64),                  -- "jira:native" | "jira:mcp" | ...
  extraction_schema        VARCHAR2(256),
  metadata_drift           NUMBER(1)        DEFAULT 0,    -- 1 if extracted values not in shim_faaas vocab
  --
  metadata_extra           JSON,                          -- open extension
  --
  CONSTRAINT pk_content_items PRIMARY KEY (id)
);

-- B-tree indexes for the most common filters
CREATE INDEX ix_ci_persona_kind        ON content_items (persona, kind);
CREATE INDEX ix_ci_primary_axis        ON content_items (primary_axis_kind, primary_axis_value);
CREATE INDEX ix_ci_source              ON content_items (source, source_id);
CREATE INDEX ix_ci_updated_at          ON content_items (updated_at);

-- JSON-path indexes for multi-valued filter columns
CREATE INDEX ix_ci_functional_area     ON content_items (json_value(functional_area_all, '$[*]'));
CREATE INDEX ix_ci_resources           ON content_items (json_value(resources,           '$[*]'));
CREATE INDEX ix_ci_services            ON content_items (json_value(services,            '$[*]'));
CREATE INDEX ix_ci_persona_visibility  ON content_items (json_value(persona_visibility,  '$[*]'));

-- =========================================================================
-- Table: chunks
-- Vector-searchable chunks; one ContentItem -> N chunks.
-- =========================================================================
CREATE TABLE chunks (
  id                       VARCHAR2(80)     NOT NULL,    -- f"{content_id}#chunk_{ord}"
  content_id               VARCHAR2(64)     NOT NULL,
  ord                      NUMBER(5)        NOT NULL,
  text                     CLOB             NOT NULL,
  heading_path             JSON,                          -- ["Section A", "Subsection 1"]
  embedding                VECTOR(3072, FLOAT32),         -- text-embedding-3-large; 3072 dims (pinned per ADR-001)
  --
  -- Inherited / chunk-specific metadata
  metadata                 JSON,
  source_sha               VARCHAR2(64)     NOT NULL,
  parser_version           VARCHAR2(32)     NOT NULL,
  schema_version           NUMBER(3)        NOT NULL,
  created_at               TIMESTAMP        DEFAULT SYSTIMESTAMP,
  --
  CONSTRAINT pk_chunks      PRIMARY KEY (id),
  CONSTRAINT fk_chunks_ci   FOREIGN KEY (content_id) REFERENCES content_items(id)
);

CREATE INDEX ix_chunks_content_ord     ON chunks (content_id, ord);

-- HNSW index for fast ANN search using cosine distance.
-- ORGANIZATION INMEMORY NEIGHBOR GRAPH is MANDATORY for HNSW on Oracle 23ai —
-- omitting it raises ORA-51914 ("Missing ORGANIZATION clause when creating a
-- vector index"). The only alternative is IVF with ORGANIZATION NEIGHBOR
-- PARTITIONS, which is a different index type with different recall/latency.
--
-- On an empty table this still creates in a few seconds (the previous claim
-- of "20-60 s hang" was actually the LLMClient() init we now skip in
-- kb_cli.py:cmd_migrate; the index DDL itself is cheap on empty data).
-- See ADR-025 for the production INMEMORY pool requirements.
CREATE VECTOR INDEX ix_chunks_embedding_hnsw
  ON chunks (embedding)
  ORGANIZATION INMEMORY NEIGHBOR GRAPH
  DISTANCE COSINE
  WITH TARGET ACCURACY 95
  PARAMETERS (TYPE HNSW, NEIGHBORS 32, EFCONSTRUCTION 200);

-- =========================================================================
-- Table: edges
-- Graph edges for incident -> service -> owner -> tenant.
-- =========================================================================
CREATE TABLE edges (
  src                      VARCHAR2(256)    NOT NULL,    -- URN
  dst                      VARCHAR2(256)    NOT NULL,    -- URN
  rel                      VARCHAR2(32)     NOT NULL,    -- "owns" | "impacts" | "resolves" | "references" | ...
  metadata                 JSON,
  created_at               TIMESTAMP        DEFAULT SYSTIMESTAMP,
  --
  CONSTRAINT pk_edges      PRIMARY KEY (src, dst, rel)
);

CREATE INDEX ix_edges_dst_rel ON edges (dst, rel);

-- =========================================================================
-- Optional: cost telemetry table (one per env; lives in kb_shim normally,
-- but mirrored here as a placeholder so this DDL alone bootstraps the schema).
-- Real placement is in kb_shim per ADR-006.
-- =========================================================================
-- (intentionally not created here; see kb_shim.sql)

-- =========================================================================
-- Grants — Phase 1 sets up read/write for KB_INCIDENTS_RW; the MCP server
-- reads via a dedicated KB_INCIDENTS_RO user (Phase 1 will add).
-- =========================================================================
-- GRANT SELECT, INSERT, UPDATE, DELETE ON content_items TO KB_INCIDENTS_RW;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON chunks        TO KB_INCIDENTS_RW;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON edges         TO KB_INCIDENTS_RW;
-- GRANT SELECT                          ON content_items TO KB_INCIDENTS_RO;
-- GRANT SELECT                          ON chunks        TO KB_INCIDENTS_RO;
-- GRANT SELECT                          ON edges         TO KB_INCIDENTS_RO;

-- =========================================================================
-- ADR-012 — In-DB embedding via DBMS_VECTOR
-- One-time admin step: create OCI Vector credential
-- =========================================================================
-- BEGIN
--   DBMS_CLOUD.CREATE_CREDENTIAL(
--     credential_name => 'OCI_VECTOR_CRED',
--     user_ocid       => '<your-resource-principal>',
--     tenancy_ocid    => '<tenancy>',
--     private_key     => '<from OCI Vault>',
--     fingerprint     => '<fingerprint>'
--   );
-- END;
-- /

-- Embedding procedure for kb_incidents schema
CREATE OR REPLACE PROCEDURE batch_insert_datasets_vectors_kbi AS
  CURSOR c_rows IS
    SELECT id, text
    FROM   chunks
    WHERE  embedding IS NULL;
BEGIN
  FOR r IN c_rows LOOP
    UPDATE chunks
    SET    embedding = DBMS_VECTOR.UTL_TO_EMBEDDING(
                          r.text,
                          JSON('{
                            "provider": "OCIGenAI",
                            "credential_name": "OCI_VECTOR_CRED",
                            "url": "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com/20231130/actions/embedText",
                            "model": "openai.text-embedding-3-large"
                          }')
                       )
    WHERE  id = r.id;
  END LOOP;
  COMMIT;
END;
/
