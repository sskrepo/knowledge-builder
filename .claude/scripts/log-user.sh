#!/bin/bash
# Logs the user's prompt verbatim to the daily conversation log in Google Drive.
# Configured project name comes from .claude/scripts/.project-name (set by init-project.sh).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME=$(cat "$SCRIPT_DIR/.project-name" 2>/dev/null || basename "$(pwd)")
LOG_BASE="$HOME/Google Drive/AI Projects/Claude/Conversations/$PROJECT_NAME"
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M:%S)
LOG_FILE="$LOG_BASE/$DATE.md"

mkdir -p "$LOG_BASE"

if [ ! -f "$LOG_FILE" ]; then
  echo "# Conversation Log — $PROJECT_NAME — $DATE" > "$LOG_FILE"
  echo "" >> "$LOG_FILE"
fi

# Read JSON payload from stdin
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // .user_prompt // .message // empty' 2>/dev/null || echo "")

# Fallback: if jq fails or produces empty, dump raw input
if [ -z "$PROMPT" ]; then
  PROMPT="$INPUT"
fi

{
  echo ""
  echo "## [$TIME] USER"
  echo ""
  echo "$PROMPT"
} >> "$LOG_FILE"

# Hook contract: exit 0 to allow prompt to proceed
exit 0
