#!/bin/bash
# Start the Grocery Agent server plus a Cloudflare quick tunnel,
# and print the public webhook URL to paste into Linq.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

python3 server.py "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

cloudflared tunnel --url "http://127.0.0.1:$PORT" 2>&1 | while read -r line; do
  echo "$line"
  if [[ "$line" =~ https://[a-zA-Z0-9-]+\.trycloudflare\.com ]]; then
    echo ""
    echo "==============================================================="
    echo "  Linq webhook URL:"
    echo "  ${BASH_REMATCH[0]}/webhook/linq/$(cat .webhook_secret)"
    echo "==============================================================="
    echo ""
  fi
done
