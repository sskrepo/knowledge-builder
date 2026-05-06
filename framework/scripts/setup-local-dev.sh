#!/usr/bin/env bash
# Set up the framework for laptop dev — no OCI Vault required.
#
# Creates ~/.kbf/secrets.yaml from the example template and reminds you to fill
# it in. Sets up env-var defaults for local mode.
#
# Run once after cloning the repo:
#   ./framework/scripts/setup-local-dev.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="$REPO_ROOT/framework/.secrets.local.yaml.example"
TARGET_DIR="$HOME/.kbf"
TARGET="$TARGET_DIR/secrets.yaml"

echo "▶ KBF local-dev setup"
echo

# 1. Create ~/.kbf directory
mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"

# 2. Create secrets file from template if absent
if [[ -f "$TARGET" ]]; then
  echo "✓ $TARGET already exists; not overwriting."
else
  cp "$TEMPLATE" "$TARGET"
  chmod 600 "$TARGET"
  echo "✓ created $TARGET (mode 600)"
fi

# 3. Suggest env vars to add to your shell profile
cat <<EOF

▶ Add the following to your shell profile (~/.zshrc or ~/.bashrc):

    # Knowledge Builder Framework — laptop dev
    export KBF_ENV=dev
    export KBF_SECRETS_BACKEND=local
    export KBF_SECRETS_FILE=\$HOME/.kbf/secrets.yaml

    # If running framework in 'openai_direct' mode (vs OCI GenAI Inference):
    export KBF_LLM_PROVIDER=openai_direct

    # If your OCI auth uses a config_file profile:
    export OCI_AUTH_METHOD=config_file
    export OCI_CONFIG_PROFILE=DEFAULT

▶ Edit secrets:

    \$EDITOR $TARGET

  Required for Phase 1 incident KB run on laptop:
    - adb-admin-dev (when ADB is provisioned)
    - kb-incidents-rw-dev
    - openai-api-key (if openai_direct mode)
    - jira-readonly (for ingest)
    - confluence-readonly (optional Phase 1)

▶ Verify:

    python3 framework/scripts/check-config.py --env dev

▶ Then proceed with the dev guide:

    open docs/wiki/engineering/dev-guide.md

EOF
