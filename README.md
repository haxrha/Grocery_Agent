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

## Group dispatch dashboard

`http://127.0.0.1:8765/dashboard` is a shared status screen (dorm TV, common
room) for group orders: live driver map on the left, deal board and batch
roster on the right, a big countdown to when the next combined order goes
out, and an event ticker along the bottom. Nobody orders from the dashboard;
orders join the batch via the API (iMessage agent, curl, whatever):

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

## How it works

1. **Resolve** — free-form names → real store items:
   `dd-cli build-grocery-list` in auto mode, or `dd-cli find-items` when a
   store is pinned (build-grocery-list ignores store pins).
2. **Menu id** — from the grocery list, falling back to `dd-cli item-details`.
3. **Pre-flight** — `dd-cli cart list --store-id` to append to an existing
   open cart instead of silently making a second one.
4. **Add** — `dd-cli cart add-items` with the whole list in one call.

The server binds to 127.0.0.1 only.
