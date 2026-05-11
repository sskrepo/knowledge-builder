---
title: ADR-020 — Codex CLI as MCP Transport for Laptop Mode
status: accepted
created: 2026-05-11
owner: architect
deciders: user
tags: [adr, adapters, ingestion, auth, laptop, mcp]
related: [ADR-011, ADR-010]
---

# ADR-020 — Codex CLI as MCP Transport for Laptop Mode

## Status
Accepted (2026-05-11).

## Context

ADR-011 defined two adapter transport modes: `native` (direct REST) and `mcp` (HTTP POST to an MCP endpoint with a service bearer token). The `mcp` mode assumes a stable, service-addressable HTTP endpoint and a service auth token stored in Vault.

The user's org currently provides Confluence and Jira MCP access exclusively through the **OpenAI Codex CLI** (`@openai/codex` v0.130.0), which:

- Is installed per-user on laptops (`npx @openai/codex`)
- Holds individual OAuth credentials at `~/.codex/auth.json`
- Manages MCP server registrations at `~/.codex/config.toml` via `codex mcp add`
- Exposes those MCP servers to any process Codex spawns or to a stdio MCP proxy (`codex mcp-server`)

There is no service auth token or service-addressable HTTP endpoint available yet. This makes `mode: mcp` non-functional on laptops. On the remote OCI VM (once service tokens are provisioned), `mode: mcp` remains the correct choice.

The framework needs a third transport mode — `codex_cli` — that routes through the Codex CLI layer, using the individual user's OAuth credentials. This mode is **laptop-only**; it must never be deployed to remote environments.

### Discovery facts

| Property | Value |
|---|---|
| Binary | `npx @openai/codex` (or `codex` if installed globally) |
| Version confirmed | 0.130.0 |
| Auth store | `~/.codex/auth.json` (OAuth tokens, refreshed automatically) |
| MCP config | `~/.codex/config.toml` (MCP server spawn commands registered by `codex mcp add`) |
| Non-interactive exec | `codex exec "<prompt>" --json` — emits JSONL event stream on stdout |
| Structured output | `codex exec "<prompt>" --json --output-schema <file>` — constrains final result |
| Stdio MCP proxy | `codex mcp-server` — exposes Codex itself as a stdio MCP server |

The user registers Confluence and Jira MCP servers by running `codex mcp add` once; Codex stores the spawn command in `~/.codex/config.toml`. After that, when Codex runs, it spawns those servers as child processes and brokers tool calls through them.

## Decision

Add `mode: codex_cli` as a third transport option for Confluence and Jira adapters, using **Option B: direct MCP stdio subprocess** (described below). The key insight is that `codex mcp add` populates `~/.codex/config.toml` with the exact spawn command for each MCP server. The framework reads that config and spawns the same subprocess directly — bypassing Codex itself for individual tool calls, while relying on Codex to have the MCP server configured.

### Why not Option A — `codex exec` with structured prompts

`codex exec` interposes an LLM between the framework and the MCP tool. The framework would issue a natural-language prompt like "call confluence.list_pages for space KEY and return the raw result," Codex would interpret it, issue the tool call internally, and return synthesized output as JSONL. Problems:

- An LLM intermediary on every ingestion tool call is expensive (tokens) and slow (LLM latency on each page fetch).
- The output is LLM-shaped, not MCP-tool-shaped. Reliable JSON extraction from JSONL requires fragile parsing heuristics.
- Codex may rewrite tool responses, dropping fields the framework needs for normalization.
- Cost telemetry becomes inaccurate — LLM tokens consumed by transport bleed into knowledge-builder cost accounting.

Rejected.

### Why not Option C — Hybrid auth bootstrap

Using `codex exec` purely for an initial OAuth flow, then caching tokens for direct calls, is not viable: the Confluence/Jira MCP servers spawned by Codex use their own auth mechanism (which Codex provisions via `codex mcp add` configuration — often environment variables or per-server config blocks). Those credentials live in the server's spawn environment, managed by Codex, not in a token the framework can extract and reuse independently.

Rejected.

### Option B — Direct MCP stdio subprocess (chosen)

When `codex mcp add` registers a server, it writes a spawn command into `~/.codex/config.toml`, e.g.:

```toml
[[mcpServers]]
name = "confluence"
command = "npx"
args  = ["-y", "@company/confluence-mcp-server"]
env   = { CONFLUENCE_URL = "https://confluence.mycompany.internal", CONFLUENCE_TOKEN = "..." }

[[mcpServers]]
name = "jira"
command = "npx"
args  = ["-y", "@company/jira-mcp-server"]
env   = { JIRA_URL = "https://jira.mycompany.internal", JIRA_TOKEN = "..." }
```

The framework's `codex_cli` transport:

1. **Reads** `~/.codex/config.toml` (or the path in `codex_cli.config_path` config) to find the spawn command for the named server.
2. **Spawns** that process as a subprocess with the configured `command`, `args`, and `env`.
3. **Speaks MCP JSON-RPC over the subprocess's stdio** — the same wire protocol the MCP spec defines (`initialize` → `tools/list` → `tools/call`).
4. **Keeps the subprocess alive** across multiple tool calls within a single adapter session (process-per-session, not process-per-call).
5. **Shuts down** the subprocess cleanly on adapter teardown.

This is deterministic, fast, and token-free. Auth is handled by the MCP server itself, configured at `codex mcp add` time — the framework does not need to know the credential type or value.

### Adapter code location

Following the ADR-011 directory convention, the `codex_cli` transport is a third sibling module:

```
framework/adapters/confluence/
├── __init__.py          # factory: native | mcp | codex_cli
├── _base.py             # ConfluenceAdapterBase
├── native.py            # REST — unchanged
├── mcp.py               # HTTP MCP — unchanged
├── codex_cli.py         # NEW: stdio MCP subprocess transport
└── shared.py            # auth helpers, normalize — unchanged

framework/adapters/jira/
├── __init__.py          # factory: native | mcp | codex_cli
├── _base.py             # JiraAdapterBase
├── native.py            # unchanged
├── mcp.py               # unchanged
├── codex_cli.py         # NEW
└── shared.py            # unchanged
```

The factory in each `__init__.py` gains a third branch:

```python
elif mode == "codex_cli":
    return ConfluenceCodexCliAdapter(adapter_config["codex_cli"])
```

### CodexCliAdapter implementation contract

```python
class ConfluenceCodexCliAdapter:
    name = "confluence:codex_cli"
    kind = "confluence"
    mode = "codex_cli"

    def __init__(self, cfg: dict):
        self.server_name   = cfg["server_name"]       # name as in config.toml [[mcpServers]]
        self.config_path   = cfg.get("config_path", "~/.codex/config.toml")
        self.tool_map      = cfg["tool_map"]           # same as mcp mode
        self.required_caps = cfg.get("required_capabilities", [])
        self.timeout_s     = cfg.get("timeout_seconds", 60)
        self.max_retries   = cfg.get("max_retries", 2)
        self._proc: subprocess.Popen | None = None
        self._seq = 0

    def _spawn(self) -> None:
        """Read config.toml, find server_name entry, spawn subprocess."""
        ...

    def _rpc(self, method: str, params: dict) -> dict:
        """Write JSON-RPC request to proc.stdin; read response from proc.stdout."""
        ...

    def healthcheck(self) -> HealthReport: ...
    def list(self, q: SourceQuery) -> Iterable[RawItemRef]: ...
    def fetch(self, ref: RawItemRef) -> RawItem: ...
    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]: ...
    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]: ...

    def close(self) -> None:
        """Terminate subprocess, flush buffers."""
        ...
```

`_rpc()` follows standard MCP JSON-RPC framing: newline-delimited JSON messages, `id` field for request/response correlation. The `initialize` handshake is performed once in `_spawn()`.

The `normalize()` method is **shared with `mcp.py`** via `shared.py` — the MCP tool response shape is identical regardless of whether the tool was called over HTTP or stdio.

### Config schema — `codex_cli` stanza

Added to `framework/config/adapters/confluence.yaml` and `jira.yaml`:

```yaml
# framework/config/adapters/confluence.yaml
adapter: confluence
mode: native | mcp | codex_cli     # add codex_cli as valid value

codex_cli:
  server_name: confluence           # matches [[mcpServers]] name in ~/.codex/config.toml
  config_path: ~/.codex/config.toml # default; override if Codex installed non-standard
  tool_map:                         # same structure as mcp.tool_map
    list_pages_in_space: confluence.list_pages
    get_page_by_id: confluence.get_page
    search: confluence.search
    list_attachments: confluence.list_attachments
  required_capabilities: [list_pages_in_space, get_page_by_id]
  timeout_seconds: 60
  max_retries: 2
  poll_interval_seconds: 300        # no webhook support in codex_cli mode
```

```yaml
# framework/config/adapters/jira.yaml
codex_cli:
  server_name: jira
  config_path: ~/.codex/config.toml
  tool_map:
    search_issues: jira.search
    get_issue: jira.get_issue
    list_comments: jira.list_comments
    list_attachments: jira.list_attachments
  required_capabilities: [search_issues, get_issue]
  timeout_seconds: 60
  max_retries: 2
  poll_interval_seconds: 300
```

### Startup capability probe

The same startup probe from ADR-011 applies. `CodexCliAdapter.healthcheck()` calls `tools/list` over the stdio subprocess and validates required capabilities. If Codex is not installed, config.toml is absent, or the named server is not registered, healthcheck fails fast with a human-readable message (`codex not installed`, `server 'confluence' not in ~/.codex/config.toml`, etc.).

### Webhook / change detection

`codex_cli` mode has no webhook support. The Confluence and Jira MCP servers spawned by Codex do not receive inbound HTTP calls — they are spawned on demand, not running as daemons. Change detection falls back to polling (`poll_interval_seconds`), identical to the MCP mode fallback in ADR-011.

### Environment guard: laptop-only enforcement

A mandatory guard in the adapter factory prevents accidental deployment:

```python
import os
if mode == "codex_cli" and os.getenv("KBF_ENV", "dev") not in ("dev", "laptop"):
    raise RuntimeError(
        "mode: codex_cli is laptop-only. "
        "Set mode: mcp with a service token for staging/prod."
    )
```

`KBF_ENV` is `dev` or `laptop` in local configs; `staging` and `prod` configs never set it to those values.

### Integration with the Adapter Protocol (ADR-011 Amendment 1)

`codex_cli` mode satisfies the full `Adapter` Protocol including `discover()` — the workflow runtime does not know or care which transport mode was used. `discover()` in `codex_cli` mode issues `tools/call` over stdio exactly as `list()` and `fetch()` do.

### RawItem invariant holds

The `normalize()` method in `shared.py` is unchanged. MCP tool responses over stdio use the same JSON shape as MCP tool responses over HTTP. The downstream pipeline (parser → store → retriever) sees identical `RawItem` instances regardless of transport.

### Mode comparison — three transports

| | `native` | `mcp` | `codex_cli` |
|---|---|---|---|
| Transport | Direct REST HTTP | HTTP POST to MCP endpoint | MCP JSON-RPC over stdio subprocess |
| Auth | Service token (Vault) | Service bearer token (Vault) | Individual OAuth via MCP server env (Codex-managed) |
| Throughput | High (parallel pagination) | Moderate | Moderate (sequential per tool call) |
| LLM cost | None | None | None |
| Laptop viable | Yes (if REST reachable) | No (no service token yet) | Yes — designed for this |
| Remote VM viable | Yes | Yes | No — blocked by env guard |
| Webhook support | Yes | Polling fallback | Polling only |
| Failure modes | API rate-limit, 5xx | + tool timeout, missing cap | + subprocess crash, config.toml parse error, codex not installed |
| Vault dependency | Yes | Yes | No — Codex manages credentials |
| Best for | Batch backfill at scale | Steady-state remote | Laptop development, live demos |

## Consequences

- The `codex_cli` stanza must be added to `confluence.yaml` and `jira.yaml` (both ship with it commented out; laptop developers uncomment it and run `codex mcp add` once).
- `framework/adapters/confluence/codex_cli.py` and `framework/adapters/jira/codex_cli.py` must be written by Backend Dev.
- The `__init__.py` factory for each adapter gains a third branch.
- The `KBF_ENV` guard must be tested — CI must verify that `mode: codex_cli` raises in a non-laptop environment.
- Tests for `codex_cli` mode must mock `subprocess.Popen` (not spawn a real Codex process in CI). A fixture that plays back `tools/list` and `tools/call` JSON-RPC responses is sufficient.
- Vault entries are not needed for `codex_cli` mode. `bootstrap-vault.sh` must not require the Confluence/Jira MCP tokens when `mode: codex_cli`.
- Laptop quickstart guide (`engineering/laptop-quickstart.md`) must document the one-time `codex mcp add` setup step.
- Operational dashboards that surface adapter mode must handle the new value without breaking.

## Considered alternatives

- **Option A — `codex exec` as LLM intermediary:** rejected; LLM token cost and output fragility make it unsuitable for ingestion-volume tool calls. See Context section.
- **Option C — Hybrid auth bootstrap:** rejected; Codex-managed MCP server credentials are not extractable as reusable tokens. See Context section.
- **Wait for service tokens; no new mode:** rejected; blocks all laptop development and live demos until service auth is provisioned, with no known ETA.
- **Wrap `codex mcp-server` as an HTTP proxy:** Codex can run as a stdio MCP server itself (`codex mcp-server`), but bridging that stdio server to HTTP requires an additional proxy process. This adds a moving part with no advantage over spawning the upstream MCP server directly. Rejected.

## References

- [ADR-011 — Dual-mode source adapters](ADR-011-dual-mode-source-adapters.md)
- [ADR-010 — Configuration plane](ADR-010-configuration-plane.md)
- [engineering/laptop-quickstart.md](../engineering/laptop-quickstart.md) — must be updated with `codex mcp add` setup
- OpenAI Codex CLI documentation — https://github.com/openai/codex
- MCP specification (JSON-RPC over stdio) — https://modelcontextprotocol.io/specification
