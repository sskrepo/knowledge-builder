#!/bin/bash
# Logs the assistant's last response (text + tool-use markers) verbatim to the daily conversation log.
# Reads transcript_path from Claude Code's Stop-hook stdin payload.

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

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || echo "")

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  # No transcript — still log a minimal marker so the user sees the turn happened
  {
    echo ""
    echo "## [$TIME] ASSISTANT"
    echo ""
    echo "_(transcript not available)_"
  } >> "$LOG_FILE"
  exit 0
fi

# Extract the last assistant message from transcript (JSONL format)
# Each line is a JSON record; assistant messages have type="assistant" with content array.
LAST_ASSISTANT=$(jq -s '
  map(select(.type == "assistant")) | last |
  if . == null then "" else
    (.message.content // [] | map(
      if .type == "text" then .text
      elif .type == "tool_use" then "🔧 [tool: \(.name)]"
      else empty end
    ) | join("\n\n"))
  end
' "$TRANSCRIPT" 2>/dev/null || echo "_(parse error)_")

# Strip surrounding quotes from jq output
LAST_ASSISTANT=$(echo "$LAST_ASSISTANT" | sed 's/^"//;s/"$//')

{
  echo ""
  echo "## [$TIME] ASSISTANT"
  echo ""
  echo -e "$LAST_ASSISTANT"
} >> "$LOG_FILE"

exit 0
