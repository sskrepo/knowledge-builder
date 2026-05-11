---
title: ADR-019 — Bastion auto-reconnect for Oracle ADB in laptop mode
status: accepted
created: 2026-05-11
owner: architect
tags: [adr, infra, oracle, laptop-mode, phase-1]
related: [ADR-001, ADR-010, ADR-012]
---

# ADR-019 — Bastion auto-reconnect for Oracle ADB in laptop mode

## Status
Accepted (2026-05-11).

## Context
When running in laptop mode (`KBF_DEPLOYMENT_MODE=laptop`), the framework connects to a remote Oracle 23ai ADB instance through an OCI Bastion managed-SSH port-forwarding session. These bastion sessions have a hard maximum lifetime of 3 hours enforced by OCI. When the tunnel expires, every ADB operation fails with one of:

- `ORA-12541: TNS:no listener` — TCP port no longer listening
- `ORA-12170: TNS:connect timeout` — port open but DB unreachable
- `socket.error: [Errno 61] Connection refused` — tunnel process died
- `DPY-6005: cannot connect to database` — oracledb Python driver wrapping the above

The current behaviour: the process crashes or hangs; the user must manually create a new bastion session via the OCI Console or CLI, extract the SSH command, start the tunnel, and restart the framework process. This is a friction point during local development and persona authoring workshops.

In production mode (OCI Compute VM in the same VCN as ADB), there is no bastion; the VM connects to ADB directly over the private subnet. The reconnect problem is exclusively a laptop-mode concern. Production gets standard exponential-backoff retries only.

OCI CLI (`/opt/homebrew/bin/oci`, v3.79.0) is already installed on developer laptops as part of the standard toolkit and is authenticated via `~/.oci/config`. The `oci bastion session create-managed-ssh` command can programmatically create a new port-forwarding session and emit the SSH tunnel command.

## Decision

Introduce a **connection-retry wrapper** around every ADB operation (pool acquire). The wrapper has two branches controlled by `KBF_DEPLOYMENT_MODE`:

### Branch A — laptop mode (auto-reconnect)

```
ADB operation fails
        │
        ▼
Is error an ADB connectivity error?  ──No──▶  re-raise immediately
        │ Yes
        ▼
reconnect_attempt += 1
        │
        ├─ reconnect_attempt > 3  ──▶  raise BastionReconnectError (give up)
        │
        ▼
oci bastion session create-managed-ssh
   --bastion-id $BASTION_OCID
   --target-private-ip $ADB_HOST
   --target-port $ADB_PORT
   --session-ttl 10800  (3 h, the OCI maximum)
        │
        ▼
Parse SSH command from OCI CLI JSON output
        │
        ▼
Kill previous tunnel subprocess (if tracked)
Start new SSH tunnel subprocess (background)
        │
        ▼
Port-check loop: poll LOCAL_TUNNEL_PORT until open
   timeout: 30 s / interval: 1 s
        │
        ▼
SELECT 1 FROM DUAL via a fresh connection
        │
        ├─ success ──▶  reset reconnect_attempt = 0 ; retry original operation
        │
        └─ failure ──▶  loop back to reconnect_attempt check
```

### Branch B — non-laptop mode (exponential backoff only)

For `KBF_DEPLOYMENT_MODE` values other than `laptop` (including production and staging), the wrapper applies standard exponential backoff:

```
ADB operation fails
        │
        ▼
Is error an ADB connectivity error?  ──No──▶  re-raise immediately
        │ Yes
        ▼
attempt += 1  (max 5 attempts)
wait: 2^attempt seconds (cap 30 s)
        │
        ├─ attempt > 5  ──▶  re-raise
        │
        └─ retry original operation
```

The assumption is that in production the tunnel is managed externally (private subnet routing, security list rules) and transient failures should self-heal without operator intervention.

### Error classification

The following errors trigger the reconnect path. Everything else is re-raised immediately.

| Error class | Signal |
|---|---|
| `ORA-12541` | TNS:no listener — port closed |
| `ORA-12170` | TNS:connect timeout |
| `ORA-12560` | TNS:protocol adapter error (tunnel dead mid-session) |
| `DPY-6005` | oracledb: cannot connect |
| `socket.error` ECONNREFUSED / ETIMEDOUT | kernel-level TCP failure |

Application-level Oracle errors (`ORA-0001` constraint violation, `ORA-1400` NOT NULL, etc.) are never caught by this wrapper — they propagate to the caller.

### Configuration

A new `bastion` section in each env YAML (per ADR-010 configuration plane):

```yaml
# framework/config/dev.yaml  (laptop-mode additions)
deployment_mode: laptop      # laptop | vm | container

bastion:
  bastion_ocid: ocid1.bastion.oc1.iad.YOUR_BASTION_OCID
  target_db_host: 10.0.1.50    # ADB private IP (from OCI Console)
  target_db_port: 1521
  local_tunnel_port: 15211     # localhost port for the SSH tunnel
  ssh_key_path: ~/.ssh/oci_bastion_key   # key authorized for the bastion
  session_ttl_seconds: 10800   # 3 h — OCI maximum
  oci_cli_path: /opt/homebrew/bin/oci    # override if installed elsewhere
  connect_timeout_seconds: 30  # how long to wait for tunnel port to open
  max_reconnect_attempts: 3    # give up after this many tunnel re-creates

adb:
  # existing fields unchanged
  service_name: kbf_dev_high
  wallet_path: /opt/kb/wallet/dev/
  # ... etc.
```

`staging.yaml` and `prod.yaml` omit the `bastion` section entirely (or set `deployment_mode: vm`). The wrapper checks `deployment_mode` at startup and sets its strategy once; no per-call branching cost.

All `bastion.*` values are non-sensitive (OCIDs, IPs, paths). No Vault reference needed. The SSH private key path points to a file on the developer's machine; the file is never read by the framework — it is passed as an argument to the `ssh` process.

### Module placement

```
framework/
└── core/
    └── adb_pool.py          # NEW — connection pool factory + retry wrapper
        ├── AdbPoolConfig    # dataclass parsed from config
        ├── BastionReconnector   # creates OCI bastion sessions, manages tunnel subprocess
        ├── RetryWrapper     # wraps pool.acquire(); dispatches to reconnector or backoff
        └── BastionReconnectError  # raised after max_reconnect_attempts exceeded
```

`adb_pool.py` owns the tunnel subprocess handle (a `subprocess.Popen` object stored in module-level state). An `atexit` handler kills the tunnel on normal process exit. On `SIGTERM`/`SIGINT`, the same cleanup fires through Python's signal-to-exception path.

### Health check

After tunnel establishment, the wrapper executes a lightweight probe before declaring the connection healthy:

```python
with pool.acquire() as conn:
    conn.execute("SELECT 1 FROM DUAL")
```

This confirms the wallet is valid, the service name resolves, and the DB schema user can authenticate — ruling out partial tunnel success (port open, DB not yet accepting).

### Observability

Every reconnect attempt logs a structured event at `WARNING` level:

```json
{
  "event": "bastion_reconnect_attempt",
  "attempt": 1,
  "max_attempts": 3,
  "tunnel_port": 15211,
  "bastion_ocid": "ocid1.bastion.oc1.iad...",
  "elapsed_seconds": 8.3
}
```

A successful reconnect logs at `INFO`. A final failure logs at `ERROR` with the full exception chain before raising `BastionReconnectError`.

## Considered alternatives

- **Manual reconnect only (status quo)**: rejected. The 3-hour expiry is a known, predictable event; automating it removes the primary friction point in persona authoring workshops and daily development.
- **Wrap the SSH command in a shell script that loops forever**: rejected. An external shell loop cannot coordinate with the Python process on pool state, error classification, or structured logging. It also cannot distinguish a legitimate DB error from a tunnel failure.
- **OCI Bastion API directly (Python SDK, no CLI)**: viable but adds the `oci-sdk` Python package as a dependency the framework does not otherwise need. The CLI is already installed and authenticated on every developer machine. Revisit if the CLI proves unreliable.
- **Always-on tunnel via `autossh`**: rejected for v1. `autossh` would need to be installed separately, configured as a launchd service, and managed outside the framework process. The in-process approach keeps the reconnect behaviour observable and testable alongside the framework itself.
- **VPN / private DNS in laptop mode**: legitimate long-term alternative (an OCI FastConnect or VPN gateway eliminates bastion entirely). Deferred — it requires network infrastructure changes outside the framework's scope. The reconnect wrapper is a cheap, local fix that works today.

## Consequences

- New module: `framework/core/adb_pool.py` (~150 LOC + tests)
- New config section: `bastion` in `dev.yaml`; `_schema.json` extended with optional `bastion` object and required `deployment_mode` field
- `framework/scripts/check-config.py` extended: in laptop mode, probe `oci bastion get --bastion-id $OCID` to verify the OCID is valid and the CLI can reach OCI before the first connection attempt
- `framework/scripts/bootstrap-vault.sh` unchanged — no new Vault entries; bastion config is non-sensitive
- Integration tests: a test fixture stubs `BastionReconnector.create_session()` and verifies that a simulated ORA-12541 triggers a reconnect cycle and retries the original operation
- Dev onboarding: `engineering/laptop-quickstart.md` gains a `bastion` config snippet and a one-time `~/.oci/config` setup step
- Production behaviour: unchanged. `deployment_mode: vm` skips all bastion code paths

## References
- [ADR-001 — Tech-stack baseline](ADR-001-tech-stack-baseline.md) — Oracle 23ai ADB as the converged store
- [ADR-010 — Configuration plane](ADR-010-configuration-plane.md) — env YAML structure, validation tooling
- [ADR-012 — In-DB embedding via DBMS_VECTOR](ADR-012-in-db-embedding.md) — context on ADB connection patterns
- [engineering/laptop-quickstart.md](../engineering/laptop-quickstart.md) — where the bastion config instructions land
- OCI Bastion documentation: https://docs.oracle.com/en-us/iaas/Content/Bastion/Concepts/bastionoverview.htm
- OCI CLI `bastion session create-managed-ssh`: https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/bastion/session/create-managed-ssh.html
