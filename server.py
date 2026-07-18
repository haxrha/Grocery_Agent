#!/usr/bin/env python3
"""Bridge a basic web form to the DoorDash CLI.

Serves static/index.html + static/dashboard.html and exposes a JSON API:

  GET  /api/stores          nearby grocery stores (for the store picker)
  GET  /api/carts           the consumer's open carts
  POST /api/order           resolve a bulk item list and add it all to the cart

Group-order dispatch (backs /dashboard):

  GET  /api/dashboard       full dispatch state: batch, deals, timer, driver
  POST /api/batch/join      {"person": "Sarah", "text": "2 milk\neggs"} queue items
  POST /api/deals           {"store", "title", "threshold"} post a deal
  POST /api/timer           {"minutes": 20} move the next send time

The batch fires automatically when the timer hits zero (BATCH_MINUTES env,
default 30). DEMO=1 seeds sample data and animates a driver on the map.

POST /api/order body (either shape):
  {"text": "2 milk\neggs\nbread x3", "store_name": "Whole Foods"}
  {"items": [{"name": "milk", "quantity": 2}, ...], "store_id": "1741590"}

Run: python3 server.py [port]   (default 8765, binds 127.0.0.1 only)
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DD_TIMEOUT = 180

# Group-order dispatch config
BATCH_MINUTES = float(os.environ.get("BATCH_MINUTES", "30"))
DEMO = os.environ.get("DEMO", "") not in ("", "0")
DROP_POINT = {
    "name": os.environ.get("DROP_NAME", "John Jay Lobby, Columbia"),
    "lat": float(os.environ.get("DROP_LAT", "40.8062")),
    "lng": float(os.environ.get("DROP_LNG", "-73.9631")),
}


class DDError(Exception):
    pass


def run_dd(*args):
    """Run a dd-cli command with --json-output and return its structured payload."""
    cmd = ["dd-cli", "--json-output", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DD_TIMEOUT)
    except FileNotFoundError:
        raise DDError("dd-cli not found on PATH")
    except subprocess.TimeoutExpired:
        raise DDError(f"dd-cli timed out after {DD_TIMEOUT}s: {' '.join(args)}")
    out = proc.stdout.strip()
    if not out:
        err = proc.stderr.strip()
        err = re.sub(r"^Error:\s*", "", err) or f"dd-cli exited {proc.returncode}"
        raise DDError(err[:500])
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        raise DDError(f"dd-cli returned non-JSON output: {out[:500]}")
    data = envelope.get("structuredContent")
    if data is None:
        try:
            data = json.loads(envelope["content"][0]["text"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise DDError(f"unrecognized dd-cli response shape: {out[:500]}")
    if envelope.get("isError"):
        raise DDError(data.get("message") or "dd-cli reported an error")
    return data


# "2x milk", "2 x milk" / "2 milk", "0.5 ground beef" / "milk x2" / "milk, 2"
LINE_PATTERNS = [
    re.compile(r"^(?P<qty>\d+(?:\.\d+)?)\s*[xX]\s+(?P<name>.+)$"),
    re.compile(r"^(?P<qty>\d+(?:\.\d+)?)\s+(?P<name>.+)$"),
    re.compile(r"^(?P<name>.+?)\s*[xX]\s*(?P<qty>\d+(?:\.\d+)?)$"),
    re.compile(r"^(?P<name>.+?)\s*,\s*(?P<qty>\d+(?:\.\d+)?)$"),
]


def parse_quantity(raw):
    qty = float(raw)
    return int(qty) if qty == int(qty) else qty


def parse_order_text(text):
    """Parse one-item-per-line bulk text into [{name, quantity}]."""
    items = []
    for line in text.splitlines():
        line = line.strip().strip("-•*").strip()
        if not line:
            continue
        for pattern in LINE_PATTERNS:
            m = pattern.match(line)
            if m:
                items.append({"name": m.group("name").strip(),
                              "quantity": parse_quantity(m.group("qty"))})
                break
        else:
            items.append({"name": line, "quantity": 1})
    return items


def normalize_items(body):
    if body.get("items"):
        items = []
        for entry in body["items"]:
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            qty = entry.get("quantity", 1)
            if not isinstance(qty, (int, float)) or qty <= 0:
                qty = 1
            items.append({"name": name, "quantity": qty})
        return items
    if body.get("text"):
        return parse_order_text(str(body["text"]))
    return []


def is_store_closed(msg):
    msg = str(msg).lower()
    return "not available for ordering" in msg or "store may be closed" in msg


def clean_qty(qty):
    qty = float(qty)
    return int(qty) if qty.is_integer() else qty


def get_menu_id(store_id, item_id):
    details = run_dd("item-details", "--store-id", str(store_id),
                     "--item-id", str(item_id))
    menu_id = details.get("menu_id")
    if not menu_id:
        raise DDError(f"could not determine menu_id for store {store_id}")
    return menu_id


def resolve_via_find_items(store_id, items):
    """Resolve requested items at ONE specific store. Returns (resolved, missing)."""
    args = ["find-items", "--store-id", str(store_id)]
    for it in items:
        args += ["-q", it["name"]]
    results = run_dd(*args).get("results") or {}
    resolved, missing = [], []
    for it in items:
        matches = results.get(it["name"]) or []
        if not matches:
            missing.append(it["name"])
            continue
        top = matches[0]
        resolved.append({"id": top["item_id"],
                         "name": top.get("item_name") or it["name"],
                         "quantity": it["quantity"],
                         "price": top.get("main_price"),
                         "store_id": str(store_id),
                         "measurement_unit": None})
    return resolved, missing


def add_to_cart(store_id, menu_id, resolved):
    """Append resolved items to the open cart at this store (or a new one)."""
    existing_uuid = None
    carts = run_dd("cart", "list", "--store-id", str(store_id))
    for cart in carts.get("carts") or []:
        if str(cart.get("store_id")) == str(store_id):
            existing_uuid = cart.get("cart_uuid")
            break

    cart_items = [{"item_id": str(it["id"]), "item_name": it["name"],
                   "quantity": clean_qty(it.get("quantity", 1))} for it in resolved]
    add_args = ["cart", "add-items", "--store-id", str(store_id),
                "--menu-id", str(menu_id), "--items-json", json.dumps(cart_items)]
    if existing_uuid:
        add_args += ["--cart-uuid", existing_uuid]
    return run_dd(*add_args), existing_uuid


def build_response(added, existing_uuid, resolved, notes, requested,
                   store_id, store_name, delivery_address=None):
    result = {
        "ok": bool(added.get("success")) or bool(added.get("cart_uuid")),
        "store_name": (added.get("cart") or {}).get("store_name") or store_name,
        "store_id": str(store_id),
        "cart_uuid": added.get("cart_uuid") or existing_uuid,
        "appended_to_existing_cart": bool(existing_uuid),
        "requested": requested,
        "resolved": [{"name": it["name"], "quantity": clean_qty(it.get("quantity", 1)),
                      "price": it.get("price"),
                      "unit": it.get("measurement_unit")} for it in resolved],
        "notes": "; ".join(n for n in notes if n) or None,
        "item_errors": added.get("item_errors") or [],
        "cart": added.get("cart"),
        "delivery_address": delivery_address,
    }
    if not result["ok"]:
        result["error"] = added.get("message") or "cart add-items failed"
        return 502, result
    return 200, result


def order_at_store(store_id, store_name, items, extra_notes=()):
    """find-items path: resolve and add at one explicit store."""
    resolved, missing = resolve_via_find_items(store_id, items)
    if not resolved:
        raise DDError(f"no requested items matched at {store_name or store_id}")
    menu_id = get_menu_id(store_id, resolved[0]["id"])
    added, existing_uuid = add_to_cart(store_id, menu_id, resolved)
    notes = list(extra_notes)
    if missing:
        notes.append("couldn't find: " + ", ".join(missing))
    return build_response(added, existing_uuid, resolved, notes, items,
                          store_id, store_name)


def place_bulk_order(body):
    """Resolve a bulk item list and add everything to the DoorDash cart."""
    items = normalize_items(body)
    if not items:
        return 400, {"ok": False, "error": "No items given. Send {\"text\": ...} "
                     "or {\"items\": [{\"name\", \"quantity\"}]}."}

    # Explicit store: resolve directly there (build-grocery-list ignores pins).
    if body.get("store_id"):
        return order_at_store(str(body["store_id"]), body.get("store_name"), items)

    # Auto mode: let build-grocery-list pick the store and resolve the list.
    resolve_args = ["build-grocery-list", "--items-json", json.dumps(items)]
    if body.get("store_name"):
        resolve_args += ["--desired-mx-name", str(body["store_name"])]
    grocery_list = run_dd(*resolve_args)
    resolved = grocery_list.get("items") or []
    if not grocery_list.get("success") or not resolved:
        return 502, {"ok": False, "error": grocery_list.get("message")
                     or "No items could be resolved.", "requested": items}

    store_id = str(resolved[0]["store_id"])
    delivery_address = grocery_list.get("delivery_address")
    try:
        menu_id = grocery_list.get("menu_id") or get_menu_id(store_id, resolved[0]["id"])
        added, existing_uuid = add_to_cart(store_id, menu_id, resolved)
        return build_response(added, existing_uuid, resolved,
                              [grocery_list.get("message")], items, store_id,
                              grocery_list.get("store_name"), delivery_address)
    except DDError as e:
        if not is_store_closed(e):
            raise
        first_error = str(e)

    # The auto-picked store isn't accepting orders — try other nearby stores.
    tried = {store_id}
    for store in (grocery_list.get("available_stores") or [])[:6]:
        alt_id = str(store.get("store_id"))
        if alt_id in tried:
            continue
        tried.add(alt_id)
        note = (f"{grocery_list.get('store_name')} wasn't accepting orders, "
                f"used {store.get('name')} instead")
        try:
            status, result = order_at_store(alt_id, store.get("name"), items, [note])
            result["delivery_address"] = delivery_address
            return status, result
        except DDError as e:
            if is_store_closed(e):
                continue
            raise
    return 502, {"ok": False, "requested": items,
                 "error": f"No nearby store is accepting this order right now. "
                          f"First store said: {first_error}"}


# ---------------------------------------------------------------------------
# Group-order dispatch: shared batch, deal board, timer, dashboard state
# ---------------------------------------------------------------------------

STATE_LOCK = threading.Lock()
STATE = {
    "next_fire_at": 0.0,
    "batch": [],       # [{person, items: [{name, quantity, est?}], joined_at}]
    "deals": [],       # [{store, title, threshold, posted_by}]
    "events": [],      # [{at, text}] newest first
    "last_order": None,
}

DURABLE_KEYS = ("next_fire_at", "batch", "deals", "events", "last_order")


def load_state():
    if DEMO:
        return
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
        for key in DURABLE_KEYS:
            if key in saved:
                STATE[key] = saved[key]
    except (OSError, json.JSONDecodeError):
        pass


def save_state():
    """Persist durable state. Caller must hold STATE_LOCK."""
    if DEMO:
        return
    try:
        with open(STATE_PATH, "w") as f:
            json.dump({k: STATE[k] for k in DURABLE_KEYS}, f)
    except OSError:
        pass


def push_event(text):
    with STATE_LOCK:
        STATE["events"].insert(0, {"at": time.time(), "text": str(text)[:160]})
        STATE["events"] = STATE["events"][:30]
        save_state()


def batch_totals():
    """Caller must hold STATE_LOCK."""
    people = len(STATE["batch"])
    count = 0
    est = 0.0
    priced = False
    for entry in STATE["batch"]:
        for it in entry["items"]:
            qty = float(it.get("quantity", 1))
            count += qty
            if isinstance(it.get("est"), (int, float)):
                priced = True
                est += it["est"] * qty
    return {"people": people, "items": clean_qty(count),
            "est_subtotal": round(est, 2) if priced else None}


def batch_join(body):
    person = str(body.get("person") or body.get("name") or "someone").strip()[:40] or "someone"
    items = []
    for entry in body.get("items") or []:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        qty = entry.get("quantity", 1)
        if not isinstance(qty, (int, float)) or qty <= 0:
            qty = 1
        item = {"name": name, "quantity": qty}
        est = entry.get("est")
        if isinstance(est, (int, float)) and est > 0:
            item["est"] = est
        items.append(item)
    if not items and body.get("text"):
        items = parse_order_text(str(body["text"]))
    if not items:
        return 400, {"ok": False, "error": "No items given. Send {\"person\", \"text\"} "
                     "or {\"person\", \"items\": [{\"name\", \"quantity\"}]}."}
    with STATE_LOCK:
        STATE["batch"].append({"person": person, "items": items, "joined_at": time.time()})
        save_state()
        totals = batch_totals()
    plural = "s" if len(items) != 1 else ""
    push_event(f"{person} joined the batch with {len(items)} item{plural}")
    return 200, {"ok": True, **totals}


def add_deal(body):
    title = str(body.get("title", "")).strip()
    if not title:
        return 400, {"ok": False, "error": "deal needs a title"}
    store = str(body.get("store", "")).strip() or None
    threshold = body.get("threshold")
    if not isinstance(threshold, (int, float)) or threshold <= 0:
        threshold = None
    with STATE_LOCK:
        STATE["deals"].insert(0, {"store": store, "title": title, "threshold": threshold,
                                  "posted_by": str(body.get("posted_by", "")).strip() or None})
        STATE["deals"] = STATE["deals"][:8]
        save_state()
    push_event(f"New deal: {title}" + (f" at {store}" if store else ""))
    return 200, {"ok": True}


def set_timer(body):
    minutes = body.get("minutes")
    if not isinstance(minutes, (int, float)) or minutes <= 0 or minutes > 24 * 60:
        return 400, {"ok": False, "error": "minutes must be between 0 and 1440"}
    with STATE_LOCK:
        STATE["next_fire_at"] = time.time() + minutes * 60
        save_state()
    push_event(f"Next order moved to T-{clean_qty(minutes)} min")
    return 200, {"ok": True, "next_fire_at": STATE["next_fire_at"]}


def fire_batch():
    """Merge everyone's items and send the combined order to the cart."""
    with STATE_LOCK:
        entries = list(STATE["batch"])
        deals = list(STATE["deals"])
    merged = {}
    for entry in entries:
        for it in entry["items"]:
            key = it["name"].strip().lower()
            slot = merged.setdefault(key, {"name": it["name"], "quantity": 0})
            slot["quantity"] += it["quantity"]
    body = {"items": [{"name": v["name"], "quantity": clean_qty(v["quantity"])}
                      for v in merged.values()]}
    if deals and deals[0].get("store"):
        body["store_name"] = deals[0]["store"]
    push_event(f"Timer hit zero, sending {len(body['items'])} items for {len(entries)} people")
    try:
        _, result = place_bulk_order(body)
    except DDError as e:
        result = {"ok": False, "error": str(e)}
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    with STATE_LOCK:
        STATE["last_order"] = result
        if result.get("ok"):
            STATE["batch"] = []
        save_state()
    if result.get("ok"):
        push_event(f"Order sent to the {result.get('store_name')} cart, review and check out")
    else:
        push_event(f"Order failed: {result.get('error')} (batch kept)")


def timer_loop():
    while True:
        time.sleep(5)
        with STATE_LOCK:
            due = STATE["next_fire_at"] and time.time() >= STATE["next_fire_at"]
            has_items = bool(STATE["batch"])
        if not due:
            continue
        if DEMO:
            push_event("Demo mode: timer hit zero, the order would go out now")
        elif has_items:
            fire_batch()
        else:
            push_event("Timer hit zero with an empty batch, rolling over")
        with STATE_LOCK:
            STATE["next_fire_at"] = time.time() + BATCH_MINUTES * 60
            save_state()


# Demo driver: loops store -> drop point so the map has something to show.
DEMO_STORE = {"name": "Whole Foods Market, 125th St", "lat": 40.8090, "lng": -73.9482}
DEMO_DRIVER_LOOP = 240.0


def demo_route():
    return [
        {"lat": DEMO_STORE["lat"], "lng": DEMO_STORE["lng"]},
        {"lat": 40.8098, "lng": -73.9531},
        {"lat": 40.8135, "lng": -73.9585},
        {"lat": 40.8110, "lng": -73.9603},
        {"lat": 40.8087, "lng": -73.9620},
        {"lat": DROP_POINT["lat"], "lng": DROP_POINT["lng"]},
    ]


def driver_snapshot():
    if not DEMO:
        return {"active": False}
    route = demo_route()
    t = (time.time() % DEMO_DRIVER_LOOP) / DEMO_DRIVER_LOOP
    if t < 0.2:
        phase, pos, eta = "SHOPPING", route[0], 9
    elif t < 0.9:
        p = (t - 0.2) / 0.7
        seg = p * (len(route) - 1)
        i = min(int(seg), len(route) - 2)
        f = seg - i
        pos = {"lat": route[i]["lat"] + (route[i + 1]["lat"] - route[i]["lat"]) * f,
               "lng": route[i]["lng"] + (route[i + 1]["lng"] - route[i]["lng"]) * f}
        phase, eta = "EN ROUTE", max(1, round((1 - p) * 8))
    else:
        phase, pos, eta = "AT DROP", route[-1], 0
    return {"active": True, "name": "Marcus D.", "phase": phase, "eta_min": eta,
            "lat": pos["lat"], "lng": pos["lng"], "store": DEMO_STORE["name"],
            "route": [[r["lat"], r["lng"]] for r in route]}


def dashboard_payload():
    with STATE_LOCK:
        payload = {
            "now": time.time(),
            "next_fire_at": STATE["next_fire_at"],
            "drop_point": DROP_POINT,
            "batch": STATE["batch"],
            "batch_totals": batch_totals(),
            "deals": STATE["deals"],
            "events": STATE["events"],
            "last_order": STATE["last_order"],
            "demo": DEMO,
        }
    payload["driver"] = driver_snapshot()
    return payload


def seed_demo():
    now = time.time()
    STATE["next_fire_at"] = now + 11 * 60 + 23
    STATE["deals"] = [
        {"store": "Whole Foods", "title": "$20 off $75+ storewide", "threshold": 75,
         "posted_by": "Sarah"},
        {"store": "Safeway", "title": "40% off first grocery order", "threshold": 60,
         "posted_by": "Jake"},
        {"store": "Westside Market", "title": "Free delivery over $35", "threshold": 35,
         "posted_by": None},
    ]
    STATE["batch"] = [
        {"person": "Sarah", "joined_at": now - 1500, "items": [
            {"name": "oat milk", "quantity": 2, "est": 5.49},
            {"name": "large eggs", "quantity": 1, "est": 6.99},
            {"name": "sourdough bread", "quantity": 1, "est": 5.79}]},
        {"person": "Jake", "joined_at": now - 1100, "items": [
            {"name": "chicken thighs", "quantity": 2, "est": 8.49},
            {"name": "jasmine rice", "quantity": 1, "est": 4.29},
            {"name": "hot sauce", "quantity": 1, "est": 3.99}]},
        {"person": "Priya", "joined_at": now - 700, "items": [
            {"name": "greek yogurt", "quantity": 3, "est": 1.79},
            {"name": "bananas", "quantity": 6, "est": 0.29},
            {"name": "peanut butter", "quantity": 1, "est": 4.99}]},
        {"person": "Toby", "joined_at": now - 300, "items": [
            {"name": "cold brew concentrate", "quantity": 1, "est": 9.99}]},
    ]
    STATE["events"] = [
        {"at": now - 300, "text": "Toby joined the batch with 1 item"},
        {"at": now - 700, "text": "Priya joined the batch with 3 items"},
        {"at": now - 900, "text": "New deal: 40% off first grocery order at Safeway"},
        {"at": now - 1100, "text": "Jake joined the batch with 3 items"},
        {"at": now - 1500, "text": "Sarah joined the batch with 3 items"},
        {"at": now - 1600, "text": "New deal: $20 off $75+ storewide at Whole Foods"},
        {"at": now - 1700, "text": f"Batch opened, drop point: {DROP_POINT['name']}"},
    ]


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
            self.send_file(os.path.join(STATIC_DIR, "dashboard.html"), "text/html; charset=utf-8")
        elif self.path == "/api/dashboard":
            self.handle_api(lambda: (200, dashboard_payload()))
        elif self.path == "/api/stores":
            self.handle_api(lambda: (200, run_dd("find-nearby-stores", "--max", "15")))
        elif self.path == "/api/carts":
            self.handle_api(lambda: (200, run_dd("cart", "list")))
        elif self.path.startswith("/assets/"):
            rel = os.path.normpath(self.path.split("?", 1)[0].lstrip("/"))
            path = os.path.join(STATIC_DIR, rel)
            if rel.startswith("assets" + os.sep) and os.path.abspath(path).startswith(STATIC_DIR + os.sep):
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.send_file(path, ctype)
            else:
                self.send_json(404, {"ok": False, "error": "not found"})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    POST_ROUTES = {
        "/api/order": place_bulk_order,
        "/api/batch/join": batch_join,
        "/api/deals": add_deal,
        "/api/timer": set_timer,
    }

    def do_POST(self):
        route = self.POST_ROUTES.get(self.path)
        if route is None:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        self.handle_api(lambda: route(body))

    def handle_api(self, fn):
        try:
            status, payload = fn()
        except DDError as e:
            status, payload = 502, {"ok": False, "error": str(e)}
        except Exception as e:  # keep the server alive on unexpected failures
            status, payload = 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.send_json(status, payload)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8765))
    load_state()
    if DEMO:
        seed_demo()
    with STATE_LOCK:
        if not STATE["next_fire_at"] or STATE["next_fire_at"] < time.time():
            STATE["next_fire_at"] = time.time() + BATCH_MINUTES * 60
        save_state()
    threading.Thread(target=timer_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    mode = " (demo mode)" if DEMO else ""
    print(f"Grocery Agent listening on http://127.0.0.1:{port}{mode}")
    print(f"Dashboard: http://127.0.0.1:{port}/dashboard")
    server.serve_forever()


if __name__ == "__main__":
    main()
