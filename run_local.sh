#!/bin/bash
# Runs the job crawler on this Mac (interim, while GitHub Actions is locked).
# Secrets come from the macOS Keychain, service "job-radar-il":
#   security add-generic-password -U -s job-radar-il -a TELEGRAM_BOT_TOKEN -w
#   security add-generic-password -U -s job-radar-il -a TELEGRAM_CHAT_ID -w
# Claude: uses the local Claude Code login (claude auth login).
set -u
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOCK="/tmp/job-radar-il.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') previous run still active, skipping"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

kc() { security find-generic-password -s job-radar-il -a "$1" -w 2>/dev/null; }
TELEGRAM_BOT_TOKEN="$(kc TELEGRAM_BOT_TOKEN)"
TELEGRAM_CHAT_ID="$(kc TELEGRAM_CHAT_ID)"
export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
  echo "$(date '+%F %T') Telegram secrets missing in Keychain"
  exit 1
fi

export AI_ENGINE=claude CLAUDE_MODEL=sonnet SCAN_MODE=fast PYTHONUNBUFFERED=1
echo "===== $(date '+%F %T') local run ====="
.venv/bin/python job_crawler.py
echo "===== $(date '+%F %T') done (exit $?) ====="
