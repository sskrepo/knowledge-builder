#!/usr/bin/env bash
# Bootstrap OCI Vault with all required Knowledge Builder Framework secrets.
# Run once per environment. Idempotent — only prompts for missing secrets.
#
# Usage:  ./bootstrap-vault.sh --env dev|staging|prod
#
# Reads the required-secrets manifest emitted by check-config.py and walks
# the user through populating each entry. Skips entries that already exist.

set -euo pipefail

ENV="${1:-dev}"
shift || true

if [[ "$ENV" =~ ^(--env=)?(dev|staging|prod)$ ]]; then
  ENV="${ENV#--env=}"
else
  echo "Usage: $0 [--env=]dev|staging|prod" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "▶ Bootstrapping Vault for env=$ENV"
echo "  repo: $REPO_ROOT"
echo

# 1. Generate manifest of required secrets from current configs
MANIFEST="/tmp/kbf-vault-required-${ENV}.txt"
python3 framework/scripts/check-config.py --env "$ENV" --emit-secrets-manifest > "$MANIFEST"

if [[ ! -s "$MANIFEST" ]]; then
  echo "❌ check-config.py produced no manifest. Aborting." >&2
  exit 1
fi

echo "Required secrets:"
nl -w2 -s'  ' "$MANIFEST"
echo

# 2. Load Vault OCID from env config
VAULT_OCID="$(yq -r '.vault.vault_ocid' framework/config/$ENV.yaml)"
KEY_OCID="$(yq -r '.vault.master_key_ocid' framework/config/$ENV.yaml)"
COMPARTMENT="$(yq -r '.vault.compartment_ocid' framework/config/$ENV.yaml)"

if [[ "$VAULT_OCID" == "ocid1.vault.oc1.iad.YOUR_"* ]]; then
  echo "❌ Vault OCID is still a placeholder in framework/config/$ENV.yaml — fill in real OCIDs first." >&2
  exit 1
fi

# 3. For each required secret, check if it exists; if not, prompt for value and create
while IFS= read -r ref; do
  [[ -z "$ref" ]] && continue
  SECRET_NAME="${ref#vault://kb/}"
  echo "─── $SECRET_NAME ───────────────────────────────"

  EXISTING="$(oci vault secret list \
    --compartment-id "$COMPARTMENT" \
    --query "data[?\"secret-name\"=='$SECRET_NAME'] | [0].id" \
    --raw-output 2>/dev/null || echo "")"

  if [[ -n "$EXISTING" && "$EXISTING" != "null" ]]; then
    echo "  ✓ already exists ($EXISTING). Skipping."
    continue
  fi

  read -r -s -p "  Enter value for $SECRET_NAME (input hidden): " VALUE
  echo
  [[ -z "$VALUE" ]] && { echo "  ⚠️  Empty value, skipping (re-run later)."; continue; }

  ENCODED="$(printf '%s' "$VALUE" | base64)"

  oci vault secret create-base64 \
    --compartment-id "$COMPARTMENT" \
    --secret-name "$SECRET_NAME" \
    --vault-id "$VAULT_OCID" \
    --key-id "$KEY_OCID" \
    --secret-content-content "$ENCODED" \
    --secret-content-stage CURRENT > /dev/null

  echo "  ✓ created."
  unset VALUE ENCODED
done < "$MANIFEST"

echo
echo "✅ Vault bootstrap complete for env=$ENV"
echo "   Re-run check-config.py to verify all secrets resolve:"
echo "     python3 framework/scripts/check-config.py --env $ENV"
