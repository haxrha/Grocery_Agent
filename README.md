# Grocery_Agent

A very basic webapp that takes bulk grocery orders and automatically puts
everything into your DoorDash cart via `dd-cli`.

## Requirements

- `dd-cli` on PATH, signed in (`dd-cli login`)
- Python 3 (stdlib only — no packages to install)

## Run

```
python3 server.py          # http://127.0.0.1:8765
python3 server.py 9000     # custom port (or PORT=9000)
```

Open the page, paste your bulk list (one item per line — `2 milk`,
`milk x2`, `0.5 ground beef` all work), optionally pick a store, and hit
**Send order to cart**. The page shows what was resolved, prices, and the
cart it landed in. Nothing is ever checked out or paid — the flow stops at
the cart; you review and submit in DoorDash (or `dd-cli order submit`).

## API (for sending bulk orders programmatically)

`POST /api/order` with either shape:

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
nearby stores.

Response: `ok`, `store_name`, `cart_uuid`, `resolved[]` (actual products +
prices), `notes` (misses / store substitutions), `item_errors[]`, and the
full `cart`. If an open cart already exists at the store, items are
**appended** to it and `appended_to_existing_cart` is `true`.

Also available: `GET /api/stores` (nearby grocery stores), `GET /api/carts`
(your open carts).

## Linq webhook (text your grocery list, it lands in your cart)

`POST /webhook/linq/<token>` accepts Linq `message.received` webhooks,
pulls the message text out of the payload's `parts[]` (tolerant of other
shapes too — `body`, `note`, nested `data`/`message` objects), parses it
as an order ("get me milk, eggs, 2 bread" works), and adds it to your
cart. The token is auto-generated into `.webhook_secret` on first run
(gitignored) and printed at startup.

Easiest way to run the whole thing publicly:

```
./start.sh     # starts server + Cloudflare quick tunnel, prints the webhook URL
```

Then register that URL with Linq:

```
./register_webhook.sh https://your-tunnel.trycloudflare.com
```

This needs your Linq API token in `.linq_api_token` (gitignored). It
deletes stale grocery-agent subscriptions from previous tunnel URLs,
creates a fresh `message.received` subscription (API fields:
`target_url`, `subscribed_events`), and saves the subscription's
signing secret into `.linq_signing_secret` — Linq issues a NEW signing
secret per subscription, so this file supports one secret per line and
the server accepts any of them. Restart `server.py` after re-registering.

Add `?store_id=...` or `?store_name=...` to the webhook URL to pin a
store.

Behavior and security:

- **Authorized users**: `.authorized_users` (gitignored) lists who may
  text orders in, one phone number (or iMessage email handle) per line,
  `#` comments allowed; matching is on the last 10 digits so formatting
  doesn't matter. Texts from anyone else are ignored. The file is read
  per request — edit it to add users, no restart needed. If the file is
  empty or missing, everyone is allowed. Caveat: if Linq's payload
  carries the sender under a field name the server doesn't recognize,
  the message is allowed through with a warning on stderr (so unknown
  payload shapes don't lock real users out) — check the first real
  delivery and tighten if needed.
- **Confirm-before-order over iMessage**: texts from a real chat are
  interpreted by Claude (headless `claude -p`, haiku, using the OAuth
  token in `.claude_oauth_token`) as order / confirm / cancel / chat.
  An order is resolved against DoorDash first, then Claude texts back
  the actual products, prices, store, and approximate total; nothing
  touches the cart until the sender replies YES. Modifications
  ("actually make it 3 milks") update the pending order; NO cancels it;
  pending confirmations expire after an hour. Confirmed orders appear
  in the web UI's "iMessage orders" section (`GET /api/orders`). If the
  Claude call fails, word-list heuristics take over. Note the server
  keeps pending confirmations in memory — a restart forgets them.
  Programmatic webhook payloads (native `{"text"|"items"}` shapes, or
  payloads without a chat id) skip confirmation and order immediately.
- Incoming texts are marked as read via Linq (`POST /v3/chats/{id}/read`)
  using the API token in `.linq_api_token`, so the sender sees the agent
  saw their message — including texts that don't trigger an order.
- Conversational texts ("thanks!", "ok", "on my way", emoji-only) are
  never ordered. Without this, "thanks!" resolves to trash bags.
- The webhook responds immediately (202) and fills the cart in the
  background — resolution takes ~a minute and webhook senders retry on
  timeout, which would double orders. Retried deliveries are deduped by
  `webhook-id`. Check results locally at `GET /api/webhook-log`.
- Through the tunnel, ONLY the webhook route (with valid token) and
  `/api/order` (with `X-Webhook-Token` header) are reachable — the UI
  and read APIs are localhost-only.
- Optionally set `LINQ_SIGNING_SECRET` (your Linq endpoint's signing
  secret, `whsec_...`) to also verify Standard-Webhooks signatures.
- Quick-tunnel URLs change on every restart of `start.sh` — re-register
  the webhook in Linq after a restart, or set up a named Cloudflare
  tunnel / reserved ngrok domain for a permanent URL.

## How it works

1. **Resolve** — free-form names → real store items:
   `dd-cli build-grocery-list` in auto mode, or `dd-cli find-items` when a
   store is pinned (build-grocery-list ignores store pins).
2. **Menu id** — from the grocery list, falling back to `dd-cli item-details`.
3. **Pre-flight** — `dd-cli cart list --store-id` to append to an existing
   open cart instead of silently making a second one.
4. **Add** — `dd-cli cart add-items` with the whole list in one call.

The server binds to 127.0.0.1 only.
