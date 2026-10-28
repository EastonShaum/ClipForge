#!/usr/bin/env bash
# Stop hook: ping Discord when Claude finishes a turn and is waiting on Easton.
# The webhook URL comes from the project's .env (CF_DISCORD_WEBHOOK_URL), never from settings.
set -euo pipefail
cd "$(dirname "$0")/../.."
url=$(grep -m1 '^CF_DISCORD_WEBHOOK_URL=' .env | cut -d= -f2- | tr -d '"'"'"'\r')
[ -n "$url" ] || exit 0
input=$(cat)
[ "$(jq -r '.stop_hook_active // false' <<<"$input")" = "true" ] && exit 0
msg=$(jq -r '.last_assistant_message // empty' <<<"$input")
if [ -z "$msg" ]; then
  t=$(jq -r '.transcript_path // empty' <<<"$input")
  [ -f "$t" ] && msg=$(tac "$t" | jq -rs 'map(select(.type=="assistant") | [.message.content[]? | select(.type=="text") | .text] | join("\n") | select(length>0)) | first // ""')
fi
msg=$(printf '%s' "$msg" | head -c 350)
jq -n --arg m "$msg" '{content: ("🤖 **Claude is done and waiting for you** (ClipForge)\n>>> " + (if $m == "" then "(no summary)" else $m end))}' |
  curl -fsS -m 10 -H 'Content-Type: application/json' -d @- "$url" >/dev/null
