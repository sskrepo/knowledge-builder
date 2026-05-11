# Resume Context — Knowledge Builder Framework

**Created:** 2026-05-11
**Purpose:** Bootstrap a new Claude Code session on a different machine. Read this file first.

---

## Quick start on new machine

```bash
# Clone the repo
git clone git@github.com:sskrepo/knowledge-builder.git Knowledgebase
cd Knowledgebase

# Verify everything is there
python3 -m pytest framework/tests/ -q --tb=short
# Expected: 574 passed (1 known failure in test_code_wiki.py::test_find_symbol_function)

# Install FastAPI + httpx if needed for deploy tests
pip3 install fastapi pyyaml httpx
```

---

## Where we are

**All code phases are complete.** Phase 0 (setup) → Phase 1 (skeleton) → Phase 2 (fleet/code wiki) → Phase 3 (workflow runtime) → V3 (deployment layer) → Laptop mode. 160+ Python files, ~16K LOC, 574 tests passing.

**No code work is blocked.** The single remaining gate is external provisioning for integration testing.

## What's built

| Layer | Key files | What it does |
|-------|-----------|-------------|
| **Adapters** | `framework/adapters/{confluence,jira}/` | Tri-mode: native (REST), mcp (HTTP), codex_cli (stdio) |
| **Parsers** | `framework/parsers/` | LLM parser with schema injection, markdown chunker |
| **Stores** | `framework/stores/` | IncidentVectorStore, FilestoreContentStore, WikiMetadataStore |
| **Retrievers** | `framework/retrievers/` | vector_search, get_incident_summary, list_sources, query_fleet, text_to_sql, find_symbol, read_code_page |
| **Orchestrator** | `framework/orchestrator/` | 4-tier intent classifier, context builder, multi-persona fanout, synthesizer |
| **Skill-builder** | `framework/skill_builder/conversation.py` | 15-state session machine (IDENTIFY_PERSONA → DONE) |
| **Workflow runtime** | `framework/workflow_runtime/` | executor, skill_registry, 3 workflow skills |
| **Deploy** | `framework/deploy/` | REST routes, MCP tools, bearer auth, session persistence, serialization, cost store |
| **Laptop mode** | `framework/core/adb_pool.py` | Bastion auto-reconnect for ADB, Codex CLI MCP transport |

## Architecture decisions (20 ADRs)

All in `docs/wiki/adr/`. Key ones for your next steps:

- **ADR-019** — Bastion auto-reconnect. When SSH tunnel to ADB expires (3h OCI limit), auto-creates new bastion session via `oci` CLI, restarts tunnel, retries. Config in `framework/config/dev.yaml` under `bastion:` section.
- **ADR-020** — Codex CLI as MCP transport. `mode: codex_cli` in adapter configs. Reads spawn command from `~/.codex/config.toml`, speaks MCP JSON-RPC over stdio subprocess. Laptop-only (KBF_ENV guard).

## Split-deployment model (your request from 2026-05-11)

The architecture supports:
- **authorSkill** runs on your company laptop (has Codex CLI with individual user auth for Confluence/Jira MCP)
- **Consumption** (askKnowledgeBase) runs on remote OCI VM (no Confluence/Jira access needed — reads from ADB)
- Both write to the **same ADB instance** — so when service auth becomes available, move authorSkill to the VM too without losing any knowledge

## What you need to set up on company laptop

### 1. OCI CLI config (`~/.oci/config`)
```ini
[DEFAULT]
user=ocid1.user.oc1..YOUR_USER_OCID
fingerprint=YOUR_KEY_FINGERPRINT
tenancy=ocid1.tenancy.oc1..YOUR_TENANCY_OCID
region=us-ashburn-1
key_file=~/.oci/oci_api_key.pem
```

### 2. Codex CLI MCP servers
```bash
# Register Confluence and Jira MCP servers in Codex
npx @openai/codex mcp add confluence -- <your-confluence-mcp-server-command>
npx @openai/codex mcp add jira -- <your-jira-mcp-server-command>

# Verify they're registered
npx @openai/codex mcp list
```

### 3. Framework config (`framework/config/dev.yaml`)
Fill in the `bastion:` section with your real values:
- `bastion_ocid` — your OCI Bastion OCID
- `target_db_host` — ADB private IP
- `local_tunnel_port` — localhost port for SSH tunnel (e.g. 15211)
- `ssh_key_path` — path to your SSH private key for bastion

### 4. Adapter configs
In `framework/config/adapters/confluence.yaml` and `jira.yaml`:
- Set `mode: codex_cli`
- Uncomment and fill the `codex_cli:` section
- Set `server_name` to match what you registered in Codex (`confluence`, `jira`)
- Update `tool_map` values to match your MCP server's actual tool names (run `POST tools/list` to discover)

### 5. Local secrets (`~/.kbf/secrets.yaml`)
```yaml
secrets:
  adb-admin-dev: <your-adb-admin-password>
  kb-incidents-rw-dev: <your-kb-incidents-password>
  # ... other schema passwords as needed
```
Then: `export KBF_SECRETS_BACKEND=local`

## What gates integration testing

Once the above is configured:
```bash
# 1. Establish bastion tunnel (framework does this automatically, but you can test manually)
oci bastion session create-managed-ssh --bastion-id <OCID> --target-private-ip <IP> --target-port 1521

# 2. Run migrations
python3 -m framework.cli.kb_cli migrate --schema kb_incidents --env dev

# 3. Ingest sample data
python3 -m framework.cli.kb_cli ingest --source confluence --space <SPACE_KEY>

# 4. Run eval
python3 -m framework.cli.kb_cli eval
# Target: recall >= 80%, faithfulness >= 0.85

# 5. Run the full server
python3 -m framework.deploy.mcp_server
```

## Key files to read when resuming

1. `CLAUDE.md` — session protocol, project rules
2. `docs/wiki/current-status.md` — where we are
3. `pmo/dashboard.md` — phase status, blockers
4. `docs/wiki/adr/ADR-019-bastion-auto-reconnect.md` — bastion config details
5. `docs/wiki/adr/ADR-020-codex-cli-mcp-transport.md` — Codex CLI transport details
6. `docs/wiki/engineering/oci-deployment-runbook.md` — full OCI deployment guide

## Repo location

- **GitHub:** `git@github.com:sskrepo/knowledge-builder.git`
- **Branch:** `main` (everything merged, up to date as of 2026-05-11)

## Next milestones

1. **Set up OCI CLI + Codex CLI on company laptop** (config only — CLIs should already be installed)
2. **Provision Oracle ADB** dev instance (if not done)
3. **Run integration tests** against real ADB + Confluence/Jira
4. **Schedule persona authoring workshops** (guide at `pmo/workshops/persona-authoring-workshop.md`)
5. **AIRA team exports 50 query/citation pairs** for gold set bootstrap
6. **Phase 4** — permissions, FA semantic graph, skill suggestion loop
