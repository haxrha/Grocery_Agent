#!/bin/bash
# (Re)register the Linq webhook subscription for the current tunnel URL.
# Deletes stale grocery-agent subscriptions (old tunnel URLs), creates a
# fresh one, and saves its signing secret. Leaves unrelated subscriptions
# (other apps/infrastructure) alone.
#
# Usage: ./register_webhook.sh https://your-tunnel.trycloudflare.com
set -euo pipefail
cd "$(dirname "$0")"
BASE="${1:?usage: $0 https://your-tunnel-url}"
API=$(cat .linq_api_token)
TOKEN=$(cat .webhook_secret)
TARGET="${BASE%/}/webhook/linq/$TOKEN"

curl -s https://api.linqapp.com/v3/webhook-subscriptions \
  -H "Authorization: Bearer $API" |
python3 -c "
import json, sys
d = json.load(sys.stdin)
subs = d if isinstance(d, list) else d.get('data') or d.get('subscriptions') or [d]
for s in subs:
    if '/webhook/linq' in (s.get('target_url') or ''):
        print(s['id'])
" | while read -r sid; do
  curl -s -X DELETE "https://api.linqapp.com/v3/webhook-subscriptions/$sid" \
    -H "Authorization: Bearer $API" -o /dev/null
  echo "deleted stale subscription $sid"
done

curl -s -X POST https://api.linqapp.com/v3/webhook-subscriptions \
  -H "Authorization: Bearer $API" -H "Content-Type: application/json" \
  -d "{\"target_url\": \"$TARGET\", \"subscribed_events\": [\"message.received\"]}" |
python3 -c "
import json, sys
s = json.load(sys.stdin)
if not s.get('id'):
    print('registration failed:', json.dumps(s)); sys.exit(1)
print('registered:', s['target_url'])
sec = s.get('signing_secret')
if sec:
    try:
        lines = [l.strip() for l in open('.linq_signing_secret') if l.strip()]
    except OSError:
        lines = []
    if sec not in lines:
        with open('.linq_signing_secret', 'w') as f:
            f.write('\n'.join([sec] + lines) + '\n')
        print('new signing secret saved — restart server.py to pick it up')
"
