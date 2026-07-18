# Grocery_Agent

# we built a group-ordering agent you text like a roommate. Introducing, hungry henry!

35% of your order on DoorDash could just be delivery fees alone and most DoorDash orders happen during lunch or dinner anyways. So to get a group order, instead of sending links, sharing carts, and figuring out who to zelle and how much - Hungry Henry just lives inside your iMessage group chat and does the searching, ordering and splitting the bill for you!

Why in iMessage? Hungry Henry doesn't need other users to have a DoorDash account or app and users don't need to do a lengthy search, all they need to do is send one iMessage. 

Hungry Henry will also automatically find the best deal nearest to the batch order after just hearing a suggestion of what you want. You can also talk to Hungry Henry in your DMs directly if you need help figuring out what works best for you and he'll help you out!

Hungry Henry also makes reciepts for the bill which was split so users can upload it to their ramp accounts if needed :)


You can also put henry in "waiting mode" and in the family group chat when you wanna add something to the family grocery list you can just @henry - and when someone is going to grab groceries - they can use the hungry henry website to see a updated grocery list! So no more keeping track of who wants what specific brand or amount of something. 



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

Group chats: add the agent's number to an iMessage group, then any
already-authorized user sends one "@henry" mention — the chat
self-provisions (`.authorized_chats`, gitignored), members are
harvested into `.authorized_users` via `GET /v3/chats/{id}` so they can
also DM, and everyone in the group can order from then on. In groups
Henry is silent unless mentioned: only messages containing "@henry"
(or "@hungry henry") get processed — everything else is ignored and
not even marked read. A bare YES/NO still works for a member with a
pending confirmation. Henry is always active — there is no mute.
Each member has their own pending order; confirmations in the group
are addressed "For iMessage …1234". Adding someone to the group in Messages is all
it takes to onboard them.

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

## Group-order bot with Ramp splits (`agent_server.py`)

A second backend for the payments half: teammates text a bot in a group
chat, an LLM parses each message, the group's items aggregate into ONE
DoorDash order fronted by a company card, and everyone pays their share
through a **Stripe Checkout** link on an itemized receipt (auto-marked
paid). The actual DoorDash charge rides the card saved in the DoorDash
account dd-cli is signed into — Stripe test cards can't pay real
merchants. (`ramp.py`, the earlier Ramp-card variant, is kept in the tree
in case we switch back.)

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
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_…`). Each receipt becomes a Stripe Checkout link and the server auto-marks it paid. Without it: Venmo links + manual "paid" texts. |
| `STRIPE_ISSUING=1` | Also create a virtual "company card" via Stripe Issuing (test mode) and capture a simulated charge for the order total |
| `BASE_URL` | Public base for Checkout success links, default `http://127.0.0.1:8766` |
| `VENMO_HANDLE` | Fallback pay links when Stripe is off (the card owner's handle) |
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
fees. With `STRIPE_SECRET_KEY` set, every receipt carries a Stripe Checkout
link (test mode: pay with card `4242 4242 4242 4242`, any future expiry,
any CVC) and the server polls the Checkout Session to flip receipts to
paid automatically — no webhook/tunnel needed.
