---
title: ADR-011 — Dual-mode source adapters (REST + MCP)
status: accepted
created: 2026-05-05
owner: architect
tags: [adr, adapters, ingestion, phase-1]
related: [ADR-003, ADR-010]
---

# ADR-011 — Dual-mode source adapters

## Status
Accepted (2026-05-05). Driven by the user requirement that Confluence and Jira each have BOTH a native (REST) adapter AND an MCP-based adapter, with a runtime mode switch.

## Context
The org has existing Confluence and Jira MCP servers. Direct REST access is also viable. Users want to be able to pick either at deploy time without code changes. The framework must provide both flavors and a switch.

## Decision

### Adapter Protocol (in `framework/adapters/_base.py`)
```python
@runtime_checkable
class Adapter(Protocol):
    name: str
    kind: str           # "confluence" | "jira" | "git" | "udap" | ...
    mode: str           # "native" | "mcp" | "read_through" | (kind-specific)

    def healthcheck(self) -> HealthReport: ...
    def list(self, source_query: SourceQuery) -> Iterable[RawItemRef]: ...
    def fetch(self, ref: RawItemRef) -> RawItem: ...
    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]: ...
```

**Invariant:** `RawItem` shape is identical across modes. Parser, store, retriever code below the adapter line never knows whether an item came from REST or MCP.

### Confluence adapter shape
```
framework/adapters/confluence/
├── __init__.py            # factory: read mode, return native or mcp instance
├── _base.py               # ConfluenceAdapterBase (shared raw_item mapping)
├── native.py              # REST API client (httpx + retry + pagination)
├── mcp.py                 # MCP client; tool-name mapped per config
└── shared.py              # auth helpers, retry, raw_item normalization
```
Same shape for Jira.

### Mode switch (per config; ADR-010)
```yaml
# config/adapters/confluence.yaml
adapter: confluence
mode: native | mcp                # the active mode

native:
  base_url: ...
  auth: { token_secret: vault://... }
  pagination: { page_size: 50 }
  rate_limit: { requests_per_minute: 120 }
  webhook: { receiver_path: ..., shared_secret: vault://... }

mcp:
  endpoint: https://confluence-mcp.org.internal/mcp
  auth: { type: bearer_token, token_secret: vault://... }
  tool_map:                       # framework op → upstream MCP tool name
    list_pages_in_space: confluence.list_pages
    get_page_by_id: confluence.get_page
    search: confluence.search
    list_attachments: confluence.list_attachments
  required_capabilities: [list_pages_in_space, get_page_by_id]
  rate_limit: { requests_per_minute: 60 }
  cache_ttl_seconds: 300
```

### Capability probe at startup
Every MCP-mode adapter calls `tools/list` on the upstream MCP at startup. Compares against `required_capabilities`. Fails fast if any are missing — service won't start in a half-functional state.

### Mode switch behavior
- `mode:` is read once at startup. Hot-reload not supported in v1 (planned Phase 4).
- Switching modes requires a config-only restart; no code changes.
- Both Vault entries should exist (e.g., both `confluence-readonly` for native and `confluence-mcp-token` for MCP) so flipping doesn't fail on a missing secret. `bootstrap-vault.sh` enforces this.

### Trade-offs (when to use which)
| | Native (REST) | MCP |
|---|---|---|
| Throughput | High (parallel pagination) | Moderate (per-page tool call latency) |
| Auth | Token in Vault | MCP token in Vault |
| Failure modes | API rate-limit, 5xx | + tool timeout, missing capability |
| Best for | Batch ingestion at scale, large backfills | Smaller corpora, policy-restricted environments where direct REST isn't allowed |
| Idempotency | Full control via content-hash | Same; opaque transport |
| Operational complexity | One more thing to monitor (REST traffic) | One more thing to monitor (upstream MCP) |

Recommended pattern: **native for backfill, MCP for steady-state** (or whatever the org's policy allows).

### Other adapters (single-mode)
- `git_adapter.py` — single mode (SSH or HTTPS clone). No MCP equivalent makes sense for a git checkout.
- `udap_adapter.py` — single mode (`read_through` JDBC); never ingested per ADR-001.
- Future adapters declare their mode set in their `_base.py`.

### Webhook intake
Both modes can receive webhooks (Confluence/Jira webhooks notify on changes). Webhook handler is `framework/ingestion/webhook_router.py` — reads `mode:` from config and routes payloads to the active adapter for change processing.

In MCP mode where the upstream MCP doesn't expose webhook configuration, the framework polls instead (interval set in `mcp.poll_interval_seconds`, default 300s).

### RawItem invariant
```python
@dataclass
class RawItem:
    kind: str                     # "confluence_page" | "jira_issue" | "git_file" | ...
    source: str                   # "confluence" | "jira" | "git" | "udap"
    source_id: str                # vendor-canonical id (page id, issue key)
    payload: dict                 # raw API response (or MCP-tool result, normalized)
    metadata: dict                # source metadata (created_at, author, labels, ...)
```
**Required normalization in MCP path:** the MCP adapter MUST translate the upstream tool's response shape into the same `payload` and `metadata` keys the native adapter produces. This translation lives in `mcp.py:normalize()`.

## Considered alternatives
- **Native only** — rejected; user has existing org MCP servers and policy may favor them.
- **MCP only (wrap upstream)** — rejected; batch ingestion at scale needs native control.
- **Two separate adapters with no shared base, two separate code paths** — rejected; doubled maintenance for the same conceptual operation.
- **Mode switch at the persona-builder level instead of adapter level** — rejected; persona teams shouldn't need to know about transport. Adapter-level is the right abstraction.

## Consequences
- Two flavors per dual-mode adapter; tests must cover both.
- Vault must hold both kinds of credentials per dual-mode adapter (even if only one is active).
- Eval gold-set runs MUST work in both modes (CI matrix in Phase 1+).
- Operational dashboards must surface mode, mode-specific failure rates, and time-since-mode-switch.

## References
- [PDD §9](../pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-003 — Core interfaces](ADR-003-core-interfaces.md)
- [ADR-010 — Configuration plane](ADR-010-configuration-plane.md)

## Amendment 1 — Adapter.discover() for procedural source resolution (2026-05-09; V2)

Workflow skills (per ADR-016) often need **procedural source discovery** — multi-step lookups like "for each project space, find the latest weekly-status page." The Adapter Protocol gains a `discover()` method:

```python
@runtime_checkable
class Adapter(Protocol):
    name: str
    kind: str
    mode: str
    def healthcheck(self) -> HealthReport: ...
    def list(self, q: SourceQuery) -> Iterable[RawItemRef]: ...
    def fetch(self, ref: RawItemRef) -> RawItem: ...
    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]: ...
    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]: ...    # NEW
```

The `recipe` is a list of step dicts:

```yaml
sources:
  procedural:
    description: "For each project, find latest weekly-status page"
    steps:
      - { op: list_spaces,   kind: confluence, pattern: "PROJECT-*" }
      - { op: for_each,      var: space }
      - { op: list_pages,    space: "{space}",
                             labels: [weekly-status, exec-summary],
                             modified_within_days: 7,
                             sort: "updated_at desc",
                             limit: 1 }
```

Each adapter implements the operations its source supports (Confluence: list_spaces, list_pages, search; Jira: list_projects, search by JQL; Git: list_repos, list_paths). Operations are validated at workflow-skill promote-time.

`discover()` returns a flat iterable of RawItemRefs; the workflow runtime then fetches each via the same `fetch()` API used by extraction.
