#!/usr/bin/env bash
# ============================================================
# adb-connect.sh — Oracle ADB bastion tunnel + wallet setup
#
# Creates an OCI Bastion port-forwarding session to the ADB
# private endpoint, opens an SSH tunnel on localhost:LOCAL_PORT,
# downloads + patches the wallet, and prints connect info.
#
# Re-run any time the tunnel drops or the bastion session expires
# (sessions last 3 hours; OCI token lasts ~1 hour and auto-refreshes).
#
# Confirmed working setup (2026-05-12):
#   Profile:   adpcpprod
#   ADB:       aira_genai_agent_db_Sravan  (LAMOBL31WHYAI5KW)
#   ADB IP:    100.200.232.160  port 1522
#   Bastion:   Bastion202604212202
#   VCN:       same subnet — bastion and ADB co-located in aira-adb-vcn
#
# Usage:
#   ./framework/scripts/adb-connect.sh            # defaults below
#   ./framework/scripts/adb-connect.sh --check    # is tunnel alive?
#   ./framework/scripts/adb-connect.sh --wallet-only  # repatch wallet, no bastion
#   LOCAL_PORT=1523 ./adb-connect.sh              # different local port
# ============================================================
set -euo pipefail

# ---- Config (override via env vars) --------------------------
OCI_PROFILE="${OCI_PROFILE:-adpcpprod}"
BASTION_OCID="${BASTION_OCID:-ocid1.bastion.oc1.eu-frankfurt-1.amaaaaaa3gzug4ya2cww7za3tsy5z4dt4an7kwwcwmgurpe57epzu4yuajlq}"
ADB_OCID="${ADB_OCID:-ocid1.autonomousdatabase.oc1.eu-frankfurt-1.antheljr3gzug4yajqfqgt7xhuw4ycqruhoj3cr34gaox43wlslugwfrm5kq}"
ADB_PRIVATE_IP="${ADB_PRIVATE_IP:-100.200.232.160}"
ADB_PORT="${ADB_PORT:-1522}"
LOCAL_PORT="${LOCAL_PORT:-1522}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
WALLET_DIR="${WALLET_DIR:-$HOME/.adb/wallet_aira_genai_agent_db_Sravan}"
WALLET_PASSWORD="${WALLET_PASSWORD:-}"          # set via: export WALLET_PASSWORD=...
SESSION_TTL="${SESSION_TTL:-10800}"          # 3 hours
BASTION_HOST="host.bastion.eu-frankfurt-1.oci.oraclecloud.com"
# ----------------------------------------------------------------

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[adb-connect]${NC} $*"; }
warn() { echo -e "${YELLOW}[adb-connect]${NC} $*"; }
err()  { echo -e "${RED}[adb-connect] ERROR${NC} $*" >&2; }

WALLET_ONLY=false
CHECK_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --wallet-only) WALLET_ONLY=true ;;
    --check)       CHECK_ONLY=true ;;
    --port=*)      LOCAL_PORT="${arg#*=}" ;;
  esac
done

# ---- --check mode ----
if $CHECK_ONLY; then
  if nc -z localhost "$LOCAL_PORT" 2>/dev/null; then
    log "Tunnel UP on localhost:$LOCAL_PORT  (pid=$(cat /tmp/adb-tunnel.pid 2>/dev/null || echo '?'))"
    exit 0
  else
    err "Tunnel DOWN on localhost:$LOCAL_PORT"
    exit 1
  fi
fi

# ---- Validate OCI session ----
log "Validating OCI session (profile: $OCI_PROFILE)..."
VALID=$(oci session validate --profile "$OCI_PROFILE" 2>&1 || true)
if echo "$VALID" | grep -q "expired\|invalid"; then
  err "OCI session expired. Run:"
  err "  oci session authenticate --profile $OCI_PROFILE --region eu-frankfurt-1"
  exit 1
fi
log "$VALID"

# ---- Wallet password — required, never hardcoded ----
if [[ -z "$WALLET_PASSWORD" ]]; then
  read -r -s -p "Wallet password (export WALLET_PASSWORD to skip this prompt): " WALLET_PASSWORD
  echo ""
fi
if [[ -z "$WALLET_PASSWORD" ]]; then
  err "WALLET_PASSWORD is required. Set it with: export WALLET_PASSWORD=<pw>"
  exit 1
fi

OCI="oci --profile $OCI_PROFILE --auth security_token"

if ! $WALLET_ONLY; then
  # ---- Kill stale tunnel process (port may or may not be bound) ----
  STALE=$(lsof -ti tcp:"$LOCAL_PORT" 2>/dev/null || true)
  if [[ -n "$STALE" ]]; then
    warn "Killing stale process on port $LOCAL_PORT: $STALE"
    kill -9 $STALE 2>/dev/null || true
    sleep 1
  fi

  # ---- Reuse existing bastion session if still ACTIVE (fast path) ----
  # The SSH process can die while the OCI session lives on for its full 3-hour TTL.
  # Re-opening SSH against the existing session takes ~3s vs 60-90s for a new session.
  SESSION_ID=""
  if [[ -f /tmp/adb-session.id ]]; then
    EXISTING_ID=$(cat /tmp/adb-session.id)
    EXISTING_STATE=$($OCI bastion session get \
      --session-id "$EXISTING_ID" \
      --query 'data."lifecycle-state"' \
      --raw-output 2>/dev/null || echo "UNKNOWN")
    if [[ "$EXISTING_STATE" == "ACTIVE" ]]; then
      log "Reusing existing ACTIVE bastion session (skipping 60-90s provisioning)"
      SESSION_ID="$EXISTING_ID"
    else
      warn "Existing session state=$EXISTING_STATE — will create a new one"
    fi
  fi

  if [[ -z "$SESSION_ID" ]]; then
    # ---- Create new bastion session ----
    log "Creating bastion port-forwarding session..."
    SESSION_JSON=$($OCI bastion session create-port-forwarding \
      --bastion-id "$BASTION_OCID" \
      --display-name "kbf-adb-$(date +%Y%m%d-%H%M%S)" \
      --key-type PUB \
      --ssh-public-key-file "${SSH_KEY}.pub" \
      --target-private-ip "$ADB_PRIVATE_IP" \
      --target-port "$ADB_PORT" \
      --session-ttl "$SESSION_TTL" 2>&1)

    SESSION_ID=$(echo "$SESSION_JSON" | python3 -c \
      "import sys,json; print(json.load(sys.stdin)['data']['id'])" 2>/dev/null)

    if [[ -z "$SESSION_ID" ]]; then
      err "Failed to create session. Response: $SESSION_JSON"
      exit 1
    fi
    echo "$SESSION_ID" > /tmp/adb-session.id
    log "New session: $SESSION_ID"
  fi

  # ---- Wait for ACTIVE (new sessions only — reused ACTIVE sessions skip this) ----
  WAIT_STATE=$($OCI bastion session get --session-id "$SESSION_ID" \
    --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "UNKNOWN")
  if [[ "$WAIT_STATE" != "ACTIVE" ]]; then
    log "Waiting for session to become ACTIVE (new session)..."
    for i in $(seq 1 18); do
      STATE=$($OCI bastion session get --session-id "$SESSION_ID" \
        --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "UNKNOWN")
      if [[ "$STATE" == "ACTIVE" ]]; then
        log "ACTIVE after $((i*10))s"
        break
      fi
      [[ "$STATE" == "FAILED" || "$STATE" == "DELETED" ]] && { err "Session $STATE"; exit 1; }
      echo -n "  ${STATE} (${i}0s)... "
      sleep 10
      echo ""
    done
  else
    log "Session already ACTIVE — opening SSH tunnel immediately"
  fi

  # ---- Open SSH tunnel ----
  log "Opening tunnel: localhost:$LOCAL_PORT → $ADB_PRIVATE_IP:$ADB_PORT"
  ssh -i "$SSH_KEY" \
    -N \
    -L "${LOCAL_PORT}:${ADB_PRIVATE_IP}:${ADB_PORT}" \
    -p 22 \
    "${SESSION_ID}@${BASTION_HOST}" \
    -o StrictHostKeyChecking=no \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes &

  TUNNEL_PID=$!
  echo "$TUNNEL_PID" > /tmp/adb-tunnel.pid
  sleep 3

  # Wait up to 30s for the port to open (generous budget for new sessions with network latency)
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if nc -z localhost "$LOCAL_PORT" 2>/dev/null; then
      log "✓ Tunnel listening on localhost:$LOCAL_PORT (pid=$TUNNEL_PID)"
      break
    fi
    # Check if SSH process already died (connection refused at bastion end)
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      err "SSH process exited unexpectedly — session may have expired. Re-run without --skip-migrate to create a new session."
      exit 1
    fi
    warn "Port not open yet ($attempt/10)..."
    sleep 3
  done

  # Final hard check before continuing
  if ! nc -z localhost "$LOCAL_PORT" 2>/dev/null; then
    err "Tunnel failed to open on localhost:$LOCAL_PORT after 30s"
    err "SSH pid=$TUNNEL_PID, session=$SESSION_ID"
    err "Try: rm /tmp/adb-session.id && ./framework/scripts/adb-connect.sh"
    exit 1
  fi
fi  # end !WALLET_ONLY

# ---- Download wallet (idempotent) ----
if [[ ! -f "$WALLET_DIR/tnsnames.ora" ]]; then
  log "Downloading ADB wallet to $WALLET_DIR..."
  mkdir -p "$WALLET_DIR"
  $OCI db autonomous-database generate-wallet \
    --autonomous-database-id "$ADB_OCID" \
    --password "$WALLET_PASSWORD" \
    --file /tmp/adb-wallet-$$.zip 2>&1
  unzip -q -o /tmp/adb-wallet-$$.zip -d "$WALLET_DIR"
  rm -f /tmp/adb-wallet-$$.zip
  log "Wallet downloaded"
else
  log "Wallet already at $WALLET_DIR"
fi

# ---- Patch tnsnames.ora: private FQDN → localhost ----
if ! grep -q "host=localhost" "$WALLET_DIR/tnsnames.ora" 2>/dev/null; then
  [[ ! -f "$WALLET_DIR/tnsnames.ora.orig" ]] && \
    cp "$WALLET_DIR/tnsnames.ora" "$WALLET_DIR/tnsnames.ora.orig"
  # Replace whatever host is in the tnsnames with localhost
  sed -i '' 's/host=[^))]*/host=localhost/g' "$WALLET_DIR/tnsnames.ora"
  log "Patched tnsnames.ora → localhost"
fi

# ---- Patch sqlnet.ora: set wallet DIRECTORY ----
if ! grep -q "$WALLET_DIR" "$WALLET_DIR/sqlnet.ora" 2>/dev/null; then
  [[ ! -f "$WALLET_DIR/sqlnet.ora.orig" ]] && \
    cp "$WALLET_DIR/sqlnet.ora" "$WALLET_DIR/sqlnet.ora.orig"
  sed -i '' "s|DIRECTORY=\"[^\"]*\"|DIRECTORY=\"$WALLET_DIR\"|g" "$WALLET_DIR/sqlnet.ora"
  log "Patched sqlnet.ora DIRECTORY → $WALLET_DIR"
fi

# ---- Available TNS aliases ----
ALIASES=$(grep -oE '^[a-z0-9_]+' "$WALLET_DIR/tnsnames.ora" | tr '\n' '  ')

# ---- Print summary ----
echo ""
log "============================================================"
log " ADB TUNNEL READY"
log "============================================================"
log " DB:       aira_genai_agent_db_Sravan (LAMOBL31WHYAI5KW)"
log " Tunnel:   localhost:$LOCAL_PORT → $ADB_PRIVATE_IP:$ADB_PORT"
if ! $WALLET_ONLY; then
  log " Tunnel PID: $TUNNEL_PID  (kill: kill $TUNNEL_PID)"
fi
log " Wallet:   $WALLET_DIR"
log " Aliases:  $ALIASES"
log ""
log " python-oracledb:"
log "   import oracledb"
log "   conn = oracledb.connect("
log "       user='ADMIN', password='<pw>',"
log "       dsn='lamobl31whyai5kw_low',"
log "       config_dir='$WALLET_DIR',"
log "       wallet_location='$WALLET_DIR',"
log "       wallet_password=os.environ['WALLET_PASSWORD'],"
log "   )"
log ""
log " Check tunnel:  ./framework/scripts/adb-connect.sh --check"
log " Close tunnel:  kill \$(cat /tmp/adb-tunnel.pid)"
log " Re-open:       ./framework/scripts/adb-connect.sh"
log "============================================================"
