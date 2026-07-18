# Grocery_Agent

Group DoorDash ordering with an iMessage front door: text your grocery
list to the agent, confirm the resolved order over iMessage, and your
items join the next shared group run — all visible on a dispatch
dashboard. Backed by `dd-cli`.

A second service, **`agent_server.py`**, adds the payments layer: one
company **Ramp card** fronts the whole group order and everyone gets an
itemized receipt with a Venmo link — see
[Group-order bot with Ramp splits](#group-order-bot-with-ramp-splits-agent_serverpy).

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

Security:

- `.authorized_users` (gitignored): one phone number or iMessage email
  handle per line, `#` comments allowed, matched on the last 10 digits.
  Read per request — edit without restarting. Empty/missing = allow all.
  If a payload hides the sender under an unknown field, the message is
  allowed with a stderr warning rather than locking real users out.
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

## Group-order bot with Ramp splits (`agent_server.py`)

A second backend for the payments half: teammates text a bot in a group
chat, an LLM parses each message, the group's items aggregate into ONE
DoorDash order paid on a company **Ramp card**, and everyone gets an
itemized receipt with a Venmo link.

```
iMessage relay ──POST /api/message──▶ agent_server.py ──▶ LLM parse (intent+items)
                                          │
             frontend ◀── /api/groups ────┤  aggregate per group chat
                                          ▼  on "send it":
                              server.py bridge (dd-cli) → DoorDash cart
                                          ▼
                              Ramp card charge + per-person receipts
                                          ▼
                              Venmo links back to the group chat
```

### Run

```
python agent_server.py           # http://127.0.0.1:8766
```

Works with **zero setup** in demo mode: if `dd-cli` isn't on PATH it fakes
DoorDash resolution (labeled MOCK), and without Ramp creds it uses a mock
card. Open http://127.0.0.1:8766 for a debug console that simulates the
group chat (with a one-click 3-person demo). Needs `pip install anthropic`
for SDK parsing; otherwise it falls back to the `claude` CLI, then regex.

Env vars (all optional):

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM message parsing via the Anthropic SDK (best). Falls back to the `claude` CLI, then regex. |
| `PARSE_MODEL` | Parser model, default `claude-opus-4-8` |
| `MOCK_DD=1` | Force mock DoorDash even if dd-cli exists |
| `RAMP_CLIENT_ID` / `RAMP_CLIENT_SECRET` | Ramp developer API (client-credentials). Without them: mock card. |
| `RAMP_ENV` | `demo` (default, sandbox) or `prod` |
| `RAMP_CARD_ID` | Pin a specific card, else first active card |
| `VENMO_HANDLE` | Recipient of everyone's shares (the card owner) |
| `TAX_RATE`, `DELIVERY_FEE`, `SERVICE_FEE` | Split math, defaults 0.08875 / 4.99 / 2.50 |
| `AGENT_PORT` | Default 8766 |

### API (for the iMessage relay + frontend)

```
POST /api/message                {"sender": "+1555…", "name": "Alex",
                                  "group_id": "chat-guid", "text": "2 burritos"}
     → {"ok", "reply", "parsed", "session"}   # send `reply` back to the chat

GET  /api/groups                 all group sessions (frontend feed)
GET  /api/groups/<id>            one session: participants, items, charge, receipts
POST /api/groups/<id>/checkout   force checkout {"store_name"?, "store_id"?}
POST /api/groups/<id>/settle     {"sender"} mark a share paid
GET  /api/receipts/<rcpt-id>     receipt JSON
GET  /receipts/<rcpt-id>         printable HTML receipt (share this link)
GET  /api/health                 {doordash, llm, ramp} status
```

Bot understands: adding items ("2 burritos and a coke"), removing ("drop my
coke"), "status", "order from Chipotle", checkout ("send it"), "cancel", and
"paid"/"venmoed you". One session per `group_id`; after checkout the session
is `ordered` until everyone pays (`settled`).

Splits: each person pays for their own items (prices from the resolved
DoorDash products) + proportional tax + an equal share of delivery/service
fees; the whole order is fronted by the Ramp card. Ramp transactions post
from the card network after settlement, so the charge record starts
`pending_settlement` and is matched to the real Ramp transaction id when it
appears.
