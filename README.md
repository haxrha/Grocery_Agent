# Grocery_Agent

Group DoorDash ordering with an iMessage front door: text your grocery
list to the agent, confirm the resolved order over iMessage, and your
items join the next shared group run — all visible on a dispatch
dashboard. Backed by `dd-cli`.

## Requirements

- `dd-cli` on PATH, signed in (`dd-cli login`)
- Python 3 (stdlib only — no packages to install)

## Run

```
python3 server.py          # http://127.0.0.1:8765
python3 server.py 9000     # custom port (or PORT=9000)
./start.sh                 # server + Cloudflare tunnel, prints the webhook URL
```

## Group dispatch dashboard

`http://127.0.0.1:8765/dashboard` is a shared status screen (dorm TV, common
room) for group orders: live driver map on the left, deal board and batch
roster on the right, a big countdown to when the next combined order goes
out, and an event ticker along the bottom. Nobody orders from the dashboard;
orders join the batch via iMessage or the API:

```
curl -X POST http://127.0.0.1:8765/api/batch/join -H 'Content-Type: application/json' \
  -d '{"person": "Sarah", "text": "2 oat milk\neggs"}'

curl -X POST http://127.0.0.1:8765/api/deals -H 'Content-Type: application/json' \
  -d '{"store": "Whole Foods", "title": "$20 off $75+", "threshold": 75}'

curl -X POST http://127.0.0.1:8765/api/timer -H 'Content-Type: application/json' \
  -d '{"minutes": 20}'
```

When the timer hits zero the server merges everyone's items into one list and
sends it through the normal order flow to a single drop point (one address,
one driver; people pick up there and settle up). `BATCH_MINUTES` sets the
cycle (default 30). Batch/deal state persists in `state.json` (gitignored).
Item entries may carry an `est` price so the deal board can show progress
toward thresholds ("$3.89 to go").

Run `DEMO=1 python3 server.py` to see it populated: sample deals, a seeded
batch, and an animated driver looping to the drop point. Demo mode never
calls dd-cli or touches `state.json`.

## iMessage agent (Linq webhook)

`POST /webhook/linq/<token>` accepts Linq `message.received` webhooks.
The flow for a real text:

1. The sender is checked against `.authorized_users` and the chat is
   marked read (`POST /v3/chats/{id}/read`).
2. Claude (headless `claude -p`, haiku, OAuth token in
   `.claude_oauth_token`) classifies the text: order / confirm / cancel
   / chat. Chitchat ("thanks!", "on my way") never becomes an order.
3. An order is resolved against DoorDash **without touching the cart**,
   and the agent texts back the actual products, prices, and estimated
   total, plus when the next group run goes out.
4. On YES, the items **join the group batch** (roster shows
   "iMessage …1234"), with resolved prices feeding the deal board. On
   NO the order is cancelled; "actually make it 3 milks" updates the
   pending order. Pending confirmations expire after an hour and live
   in memory (a restart forgets them).
5. When the batch fires, every iMessage participant gets a text with
   the result. Confirmed orders are listed at `GET /api/orders` and on
   the dashboard.

Programmatic webhook payloads (native `{"text"|"items"}` shapes, or
payloads without a chat id) skip the batch and order immediately into
the cart, in the background (webhook senders retry on timeout; retried
deliveries are deduped by `webhook-id`). Check `GET /api/webhook-log`.

Setup:

```
./start.sh                                           # prints the public webhook URL
./register_webhook.sh https://your-tunnel.trycloudflare.com
```

`register_webhook.sh` needs your Linq API token in `.linq_api_token`
(gitignored). It deletes stale grocery-agent subscriptions from previous
tunnel URLs, creates a fresh `message.received` subscription (API
fields: `target_url`, `subscribed_events`), and saves the subscription's
signing secret into `.linq_signing_secret` — Linq issues a NEW signing
secret per subscription, so that file holds one secret per line and the
server accepts any of them. Restart `server.py` after re-registering.
Quick-tunnel URLs change on every `start.sh` restart; use a named
Cloudflare tunnel for a permanent URL.

Notes on the Linq wire format (their docs are loose; verified against
live deliveries and their Python SDK): incoming text lives in
`data.parts[].value`, the sender in `data.sender_handle.handle`, the
chat in `data.chat.id`; outgoing sends wrap as
`{"message": {"parts": [{"type": "text", "value": ...}]}}`.

Group chats: add the agent's number to an iMessage group, then any
already-authorized user texts a "henry"-addressed message once — the
chat self-provisions (`.authorized_chats`, gitignored), members are
harvested into `.authorized_users` via `GET /v3/chats/{id}` so they can
also DM, and everyone in the group can order from then on. In groups
the agent only reacts to messages starting with "henry" (or
"@henry" / "hungry henry"); a bare YES/NO still works for a member
with a pending confirmation. Each member has their own pending order;
confirmations in the group are addressed "For iMessage …1234". Adding
someone to the group in Messages is all it takes to onboard them.

Security:

- `.authorized_users` (gitignored): one phone number or iMessage email
  handle per line, `#` comments allowed, matched on the last 10 digits.
  Read per request — edit without restarting. Empty/missing = allow all.
  If a payload hides the sender under an unknown field, the message is
  allowed with a stderr warning rather than locking real users out.
- `.agent_number` (gitignored) pins the agent to its own Linq line: the
  webhook subscription is created with `phone_numbers` scoping, and the
  server additionally drops any message whose chat `owner_handle`
  isn't that number — other lines on the Linq account (e.g. business
  traffic) are never read, marked read, or replied to.
- Webhook auth: secret path token (`.webhook_secret`, auto-generated)
  plus Standard-Webhooks signature verification when a signing secret
  is configured (`.linq_signing_secret` or `LINQ_SIGNING_SECRET`).
- Through the tunnel, ONLY the webhook (path token) and `/api/order`
  (`X-Webhook-Token` header) are reachable; the dashboard, batch,
  deals, and timer APIs are localhost-only.

## Direct cart API

`POST /api/order` resolves a bulk list and puts everything straight in
the DoorDash cart (no batch, no confirmation):

```
curl -X POST http://127.0.0.1:8765/api/order -H 'Content-Type: application/json' \
  -d '{"text": "2 milk\neggs\nbread x3"}'

curl -X POST http://127.0.0.1:8765/api/order -H 'Content-Type: application/json' \
  -d '{"items": [{"name": "milk", "quantity": 2}, {"name": "eggs"}],
       "store_id": "1518391"}'
```

Optional fields: `store_id` (pin to one store), `store_name` (preference,
e.g. `"Whole Foods"`). With neither, DoorDash auto-picks a nearby store; if
that store isn't accepting orders the server automatically retries other
nearby stores. Response carries `ok`, `store_name`, `cart_uuid`,
`resolved[]` (actual products + prices), `notes`, `item_errors[]`, and the
full `cart`; items append to an existing open cart at the same store
(`appended_to_existing_cart: true`). The dashboard's "Order online" modal
uses this endpoint. Also available: `GET /api/stores`, `GET /api/carts`.

Nothing is ever checked out or paid — every flow stops at the cart; you
review and submit in DoorDash (or `dd-cli order submit`).

## How it works

1. **Resolve** — free-form names → real store items:
   `dd-cli build-grocery-list` in auto mode, or `dd-cli find-items` when a
   store is pinned (build-grocery-list ignores store pins).
2. **Menu id** — from the grocery list, falling back to `dd-cli item-details`.
3. **Pre-flight** — `dd-cli cart list --store-id` to append to an existing
   open cart instead of silently making a second one.
4. **Add** — `dd-cli cart add-items` with the whole list in one call.

The server binds to 127.0.0.1 only.
