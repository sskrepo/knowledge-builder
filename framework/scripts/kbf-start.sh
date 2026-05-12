#!/usr/bin/env bash
# ============================================================
# kbf-start.sh — Start the Knowledge Builder Framework on laptop
#
# Does everything in one shot:
#   1. Load secrets from ~/.kbf/secrets.env (create it once — see below)
#   2. Validate prerequisites
#   3. Ensure OCI session token is valid (refresh or prompt to re-auth)
#   4. Ensure OCI token LaunchAgent is running (auto-refresh every 4 min)
#   5. Ensure ADB bastion tunnel is alive (or create a new one)
#   6. Start the KBF MCP server on port 8080
#   7. Health-check and print the curl commands to interact with it
#
# ---- One-time setup ----------------------------------------------------------
# Create ~/.kbf/secrets.env with your secrets (never commit this file):
#
#   mkdir -p ~/.kbf
#   cat > ~/.kbf/secrets.env <<'EOF'
#   KBF_ADB_ADMIN_PASSWORD=23AiOnCallAgentTest
#   WALLET_PASSWORD=<your-wallet-zip-password>
#   EOF
#   chmod 600 ~/.kbf/secrets.env
#
# Then just run:
#   bash framework/scripts/kbf-start.sh
# ============================================================
set -euo pipefail

# ── Colours ─────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GRN}[kbf]${NC} $*"; }
warn() { echo -e "${YLW}[kbf]${NC} $*"; }
err()  { echo -e "${RED}[kbf] ERROR${NC} $*" >&2; }
hdr()  { echo -e "\n${CYN}${BLD}── $* ──${NC}"; }

# ── Flag parsing ─────────────────────────────────────────────
# --migrate        run kb-cli migrate --schema all before starting the server
# --skip-migrate   (default) skip migration — use on subsequent startups
RUN_MIGRATE=false
for _arg in "$@"; do
    case "$_arg" in
        --migrate)      RUN_MIGRATE=true  ;;
        --skip-migrate) RUN_MIGRATE=false ;;
    esac
done

# ── Resolve repo root (works whether run from repo root or scripts/) ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"
log "Repo root: $REPO_ROOT"

# ── Configurable ─────────────────────────────────────────────
PORT="${KBF_PORT:-8080}"
OCI_PROFILE="${OCI_PROFILE:-adpcpprod}"
OCI_REGION="${OCI_REGION:-eu-frankfurt-1}"
PYTHON="${KBF_PYTHON:-/Library/Developer/CommandLineTools/usr/bin/python3}"
SECRETS_FILE="${HOME}/.kbf/secrets.env"
LAUNCHAGENT_LABEL="com.kbf.oci-token-refresh"
LAUNCHAGENT_PLIST="${HOME}/Library/LaunchAgents/${LAUNCHAGENT_LABEL}.plist"
LOG_FILE="${HOME}/.kbf/kbf-server.log"

# ============================================================
# STEP 1 — Load secrets
# ============================================================
hdr "Step 1 · Secrets"

if [[ -f "$SECRETS_FILE" ]]; then
    log "Loading secrets from $SECRETS_FILE"
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
else
    warn "No secrets file found at $SECRETS_FILE"
    warn "Create it once with:"
    warn "  mkdir -p ~/.kbf"
    warn "  echo 'KBF_ADB_ADMIN_PASSWORD=<password>' >> ~/.kbf/secrets.env"
    warn "  echo 'WALLET_PASSWORD=<wallet-zip-password>' >> ~/.kbf/secrets.env"
    warn "  chmod 600 ~/.kbf/secrets.env"
fi

# Validate required secrets
MISSING=()
[[ -z "${KBF_ADB_ADMIN_PASSWORD:-}" ]] && MISSING+=("KBF_ADB_ADMIN_PASSWORD")
[[ -z "${WALLET_PASSWORD:-}" ]]         && MISSING+=("WALLET_PASSWORD")
if (( ${#MISSING[@]} > 0 )); then
    err "Required secrets not set: ${MISSING[*]}"
    err "Export them or add to $SECRETS_FILE"
    exit 1
fi
log "Secrets: OK (KBF_ADB_ADMIN_PASSWORD + WALLET_PASSWORD loaded)"

# ============================================================
# STEP 2 — Prerequisites
# ============================================================
hdr "Step 2 · Prerequisites"

check_cmd() {
    if command -v "$1" &>/dev/null; then
        log "  ✓ $1 ($(command -v "$1"))"
    else
        err "  ✗ $1 not found — install it first"
        exit 1
    fi
}

check_cmd oci
check_cmd ssh
check_cmd nc

if ! "$PYTHON" -c "import fastapi, uvicorn, oracledb, oci, yaml" 2>/dev/null; then
    err "Python deps missing. Run:"
    err "  pip3 install fastapi uvicorn oci oracledb pyyaml httpx"
    exit 1
fi
log "  ✓ Python deps (fastapi, uvicorn, oracledb, oci, yaml)"

# ============================================================
# STEP 3 — OCI session token
# ============================================================
hdr "Step 3 · OCI session (profile: $OCI_PROFILE)"

VALID_OUTPUT=$(oci session validate --profile "$OCI_PROFILE" 2>&1 || true)

if echo "$VALID_OUTPUT" | grep -q "Session is valid"; then
    log "$VALID_OUTPUT"

    # Try a silent refresh to push the TTL window forward
    REFRESH_OUTPUT=$(oci session refresh --profile "$OCI_PROFILE" 2>&1 || true)
    if echo "$REFRESH_OUTPUT" | grep -q "Successfully refreshed"; then
        log "Token refreshed (next ~5 min)"
    else
        warn "Refresh attempt: $REFRESH_OUTPUT"
    fi
elif echo "$VALID_OUTPUT" | grep -qi "expired\|invalid\|no longer valid"; then
    warn "OCI session expired — opening browser for re-authentication…"
    warn "(Complete the browser flow, then this script will continue)"
    oci session authenticate --profile "$OCI_PROFILE" --region "$OCI_REGION"
    log "Re-authentication complete"
else
    warn "Could not determine session state: $VALID_OUTPUT"
    warn "Attempting re-authentication…"
    oci session authenticate --profile "$OCI_PROFILE" --region "$OCI_REGION"
fi

log "OCI session: OK"

# ============================================================
# STEP 4 — OCI token LaunchAgent (auto-refresh every 4 min)
# ============================================================
hdr "Step 4 · OCI token LaunchAgent"

if [[ ! -f "$LAUNCHAGENT_PLIST" ]]; then
    warn "LaunchAgent plist not found at $LAUNCHAGENT_PLIST"
    warn "OCI token will not auto-refresh — re-run oci session authenticate manually when it expires"
else
    # Check if it's loaded and healthy
    AGENT_STATUS=$(launchctl list | grep "$LAUNCHAGENT_LABEL" || true)
    if [[ -z "$AGENT_STATUS" ]]; then
        log "Loading LaunchAgent…"
        launchctl load "$LAUNCHAGENT_PLIST"
        sleep 2
        AGENT_STATUS=$(launchctl list | grep "$LAUNCHAGENT_LABEL" || true)
    fi

    EXIT_CODE=$(echo "$AGENT_STATUS" | awk '{print $2}')
    if [[ "$EXIT_CODE" == "0" || "$EXIT_CODE" == "-" ]]; then
        log "LaunchAgent running (last exit: ${EXIT_CODE}) — token auto-refreshes every 4 min"
    else
        warn "LaunchAgent last exit code: $EXIT_CODE — reloading"
        launchctl unload "$LAUNCHAGENT_PLIST" 2>/dev/null || true
        launchctl load "$LAUNCHAGENT_PLIST"
        log "LaunchAgent reloaded"
    fi
fi

# ============================================================
# STEP 5 — ADB bastion tunnel
# ============================================================
hdr "Step 5 · ADB bastion tunnel (localhost:1522)"

if ./framework/scripts/adb-connect.sh --check 2>/dev/null; then
    log "Tunnel already alive on port 1522"
else
    log "Tunnel not running — reconnecting…"
    log "(Reuses existing OCI session if still ACTIVE — ~5 s; new session takes ~60–90 s)"
    ./framework/scripts/adb-connect.sh
fi
log "ADB tunnel: OK (localhost:1522)"

# ============================================================
# STEP 5b — DB schema migration (optional — first-run or DDL changes)
# ============================================================
hdr "Step 5b · DB migration"

if $RUN_MIGRATE; then
    log "Running migrations (--schema all --env laptop) …"
    log "(First run takes ~30 s while Oracle creates users + tables)"
    "$PYTHON" -m framework.cli.kb_cli migrate --schema all --env laptop
    log "Migration: OK"
else
    log "Skipping migration (pass --migrate on first run or after DDL changes)"
    log "  Example: bash framework/scripts/kbf-start.sh --migrate"
fi

# ============================================================
# STEP 6 — Start the KBF MCP server
# ============================================================
hdr "Step 6 · KBF MCP server (port $PORT)"

# Kill any previous instance on this port
STALE_PID=$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)
if [[ -n "$STALE_PID" ]]; then
    warn "Killing stale process on port $PORT (pid=$STALE_PID)"
    kill -9 $STALE_PID 2>/dev/null || true
    sleep 1
fi

mkdir -p "${HOME}/.kbf"

log "Starting server… log → $LOG_FILE"
log "Press Ctrl+C to stop"
echo ""
echo -e "${BLD}Server log:${NC} tail -f $LOG_FILE"
echo ""

# Export everything the server needs
export KBF_ENV=laptop
export KBF_ADB_ADMIN_PASSWORD
export WALLET_PASSWORD

# Print the interaction cheatsheet before handing off to uvicorn
cat << CHEATSHEET
${CYN}${BLD}─────────────────────────────────────────────────────────${NC}
  KBF MCP server starting on http://localhost:${PORT}
  Bearer token: dev-only-token-replace-me
${CYN}─────────────────────────────────────────────────────────${NC}

  Health check:
    curl -s http://localhost:${PORT}/healthz | python3 -m json.tool

  List MCP tools (no auth needed):
    curl -s -X POST http://localhost:${PORT}/mcp/tools/list

  Ask a question:
    curl -s -X POST http://localhost:${PORT}/api/v1/ask \\
      -H "Authorization: Bearer dev-only-token-replace-me" \\
      -H "Content-Type: application/json" \\
      -d '{"question":"What were the top P1 incidents last quarter?","persona":"ops_eng"}' \\
      | python3 -m json.tool

  Start an authorSkill session:
    curl -s -X POST http://localhost:${PORT}/api/v1/kb/authorSkill \\
      -H "Authorization: Bearer dev-only-token-replace-me" \\
      -H "Content-Type: application/json" \\
      -d '{"persona":"ops_eng","intentDescription":"Summarise on-call runbooks"}' \\
      | python3 -m json.tool

  Wire into Claude Code (.mcp.json in repo root — MCP Streamable HTTP):
    {
      "mcpServers": {
        "kbf": {
          "type": "http",
          "url": "http://localhost:${PORT}/mcp",
          "headers": { "Authorization": "Bearer dev-only-token-replace-me" }
        }
      }
    }

  Verify transport works (JSON-RPC 2.0 initialize handshake):
    curl -s -X POST http://localhost:${PORT}/mcp \\
      -H "Content-Type: application/json" \\
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \\
      | python3 -m json.tool

${CYN}─────────────────────────────────────────────────────────${NC}
CHEATSHEET

# Run uvicorn — foreground so Ctrl+C shuts everything down cleanly
exec "$PYTHON" -m uvicorn \
    framework.deploy.mcp_server:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info \
    2>&1 | tee "$LOG_FILE"
