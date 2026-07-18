#!/usr/bin/env python3
"""Grocery Agent: group DoorDash ordering with an iMessage front door.

Serves static/dashboard.html and exposes a JSON API:

  GET  /api/stores          nearby grocery stores (for the store picker)
  GET  /api/carts           the consumer's open carts
  POST /api/order           resolve a bulk item list and add it all to the cart

Group-order dispatch (backs /dashboard):

  GET  /api/dashboard       full dispatch state: batch, deals, timer, driver
  POST /api/batch/join      {"person": "Sarah", "text": "2 milk\neggs"} queue items
  POST /api/deals           {"store", "title", "threshold"} post a deal
  POST /api/timer           {"minutes": 20} move the next send time

Linq iMessage agent:

  POST /webhook/linq/<token>  Linq message.received webhook. Claude interprets
                              the text, replies with the resolved order for
                              confirmation, and a YES joins the group batch.
  GET  /api/orders            confirmed iMessage orders + pending confirmations
  GET  /api/webhook-log       recent webhook deliveries and their outcomes

The batch fires automatically when the timer hits zero (BATCH_MINUTES env,
default 30). DEMO=1 seeds sample data and animates a driver on the map.

Run: python3 server.py [port]   (default 8765, binds 127.0.0.1 only;
use start.sh for the public Cloudflare tunnel + webhook URL)
"""

import base64
import collections
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
DD_TIMEOUT = 180

# Group-order dispatch config
BATCH_MINUTES = float(os.environ.get("BATCH_MINUTES", "30"))
DEMO = os.environ.get("DEMO", "") not in ("", "0")
DROP_POINT = {
    "name": os.environ.get("DROP_NAME", "John Jay Lobby, Columbia"),
    "lat": float(os.environ.get("DROP_LAT", "40.8062")),
    "lng": float(os.environ.get("DROP_LNG", "-73.9631")),
}


def load_or_create_token():
    path = os.path.join(BASE_DIR, ".webhook_secret")
    try:
        with open(path) as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


WEBHOOK_TOKEN = load_or_create_token()
WEBHOOK_LOG = collections.deque(maxlen=50)
SEEN_IDS = collections.OrderedDict()
SEEN_LOCK = threading.Lock()

# Conversational ordering state: per-chat orders awaiting a YES over iMessage.
PENDING_ORDERS = {}
PENDING_LOCK = threading.Lock()
PENDING_TTL = 3600  # a pending confirmation goes stale after an hour
ORDERS = collections.deque(maxlen=100)  # confirmed iMessage orders


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


# --- Group-order dispatch state ---------------------------------------------

STATE_LOCK = threading.Lock()
STATE = {
    "next_fire_at": 0.0,
    "batch": [],       # [{person, items: [{name, quantity, est?}], joined_at, chat_id?}]
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
    entry = {"person": person, "items": items, "joined_at": time.time()}
    if body.get("chat_id"):  # iMessage joiners get notified when the batch fires
        entry["chat_id"] = str(body["chat_id"])
    with STATE_LOCK:
        STATE["batch"].append(entry)
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
    # Text the iMessage joiners how their run went.
    for chat_id in {e.get("chat_id") for e in entries if e.get("chat_id")}:
        if result.get("ok"):
            send_linq_message(chat_id,
                              f"Group order sent — the {result.get('store_name')} cart "
                              f"has everyone's items. Pickup at {DROP_POINT['name']}.")
        else:
            send_linq_message(chat_id,
                              f"Group order hit a snag: {result.get('error')} "
                              "Your items are still in the batch for the next run.")


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


def imessage_snapshot():
    """Pending confirmations + recent confirmed iMessage orders for the UI."""
    now = time.time()
    with PENDING_LOCK:
        awaiting = [{"person": batch_person(p.get("sender")),
                     "summary": p["summary"],
                     "age_seconds": int(now - p["created"])}
                    for p in PENDING_ORDERS.values()
                    if now - p["created"] <= PENDING_TTL]
    return {"awaiting": awaiting, "recent": list(ORDERS)[:6]}


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
    payload["imessage"] = imessage_snapshot()
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


# --- Linq iMessage agent -----------------------------------------------------

CHITCHAT_WORDS = {
    "thanks", "thank", "you", "thx", "ty", "ok", "okay", "k", "kk", "cool",
    "great", "awesome", "perfect", "nice", "good", "yes", "yeah", "yep", "no",
    "nope", "hi", "hello", "hey", "yo", "sup", "bye", "goodbye", "later",
    "lol", "haha", "hahaha", "omg", "pls", "please", "sure", "np", "welcome",
    "morning", "night", "gn", "gm", "u", "it", "got", "sounds", "see", "soon",
    "the", "that", "this", "a", "so", "and", "will", "do", "done", "all",
    "much", "very", "really", "appreciate", "appreciated", "i", "i'm", "im",
    "my", "your", "for", "man", "bro", "dude", "one", "sec", "min", "wait",
    "on", "way", "here", "there", "love", "what", "when", "where", "is",
    "be", "are", "was", "how", "why", "who", "can", "could", "would",
    "should", "of", "in", "at", "to", "we", "they", "me", "us", "not",
}


def looks_like_chitchat(text):
    """True for courtesy/conversational texts that must not become orders."""
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return True  # emoji- or punctuation-only message
    return all(w in CHITCHAT_WORDS for w in words)


def message_to_order_text(text):
    """Turn a chat message into one-item-per-line order text."""
    text = re.sub(r"^\s*(?:please\s+)?(?:order|buy|get(?:\s+me)?|add)\b[:,]?\s*",
                  "", text.strip(), flags=re.I)
    if "\n" not in text and re.search(r"[,;]", text):
        parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
        # "milk, 2" is quantity syntax for one item — only split real lists
        if parts and not any(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
            text = "\n".join(parts)
    return text


TEXT_KEYS = ("text", "body", "content", "note", "notes", "description")
CONTAINER_KEYS = ("message", "data", "payload", "event", "object", "conversation")


def find_message_text(obj, depth=0):
    """Best-effort hunt for the human message text in an arbitrary payload."""
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, str):
        return obj.strip() or None
    if isinstance(obj, dict):
        parts = obj.get("parts")
        if isinstance(parts, list):  # Linq messages carry text in parts[]
            texts = []
            for p in parts:
                if isinstance(p, dict):
                    if p.get("type") not in (None, "text"):
                        continue
                    t = (p.get("text") or p.get("value")  # Linq uses "value"
                         or p.get("body") or p.get("content"))
                    if isinstance(t, str) and t.strip():
                        texts.append(t.strip())
                elif isinstance(p, str) and p.strip():
                    texts.append(p.strip())
            if texts:
                return "\n".join(texts)
        for k in TEXT_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for k in CONTAINER_KEYS:
            if k in obj:
                t = find_message_text(obj[k], depth + 1)
                if t:
                    return t
    if isinstance(obj, list):
        for v in obj:
            t = find_message_text(v, depth + 1)
            if t:
                return t
    return None


def extract_order_payload(payload):
    """Map an arbitrary webhook payload onto a place_bulk_order() body."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("items") or payload.get("text"):
        return dict(payload)  # already in our native shape
    body = {}
    text = find_message_text(payload)
    if text:
        body["text"] = message_to_order_text(text)
    for k in ("store_id", "store_name"):
        if isinstance(payload.get(k), (str, int)):
            body[k] = str(payload[k])
    return body


def normalize_phone(value):
    """Comparable form: last 10 digits for phone numbers, lowercase otherwise
    (iMessage senders can also be email handles)."""
    value = str(value).strip().lower()
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 10:
        return digits[-10:]
    return value


def load_authorized_users():
    """Allowed sender numbers/handles, one per line. Empty/missing = allow all."""
    users = set()
    try:
        with open(os.path.join(BASE_DIR, ".authorized_users")) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    users.add(normalize_phone(line))
    except OSError:
        pass
    return users


def agent_line():
    """The agent's own Linq phone number (normalized), from .agent_number."""
    try:
        with open(os.path.join(BASE_DIR, ".agent_number")) as f:
            value = f.read().strip()
        return normalize_phone(value) if value else None
    except OSError:
        return None


def load_authorized_chats():
    """Group chats whose members may all use the agent, one chat id per line."""
    chats = set()
    try:
        with open(os.path.join(BASE_DIR, ".authorized_chats")) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    chats.add(line)
    except OSError:
        pass
    return chats


def authorize_chat(chat_id, note=""):
    """Add a group chat to the allowlist. Returns True if newly added."""
    if str(chat_id) in load_authorized_chats():
        return False
    path = os.path.join(BASE_DIR, ".authorized_chats")
    with open(path, "a") as f:
        f.write(f"{chat_id}" + (f"  # {note}" if note else "") + "\n")
    os.chmod(path, 0o600)
    return True


def fetch_chat_handles(chat_id):
    """Participant handles for a chat via GET /v3/chats/{id} (empty on failure)."""
    token = linq_api_token()
    if not token:
        return []
    req = urllib.request.Request(
        f"https://api.linqapp.com/v3/chats/{urllib.parse.quote(str(chat_id))}",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        sys.stderr.write(f"chat lookup failed for {chat_id}: {e}\n")
        return []
    handles = data.get("handles") or (data.get("chat") or {}).get("handles") or []
    return [str(h["handle"]) for h in handles
            if isinstance(h, dict) and not h.get("is_me")
            and str(h.get("handle") or "").strip()]


def harvest_group_members(chat_id):
    """Fold a group chat's members into .authorized_users so DMs work too."""
    known = load_authorized_users()
    added = [h for h in fetch_chat_handles(chat_id)
             if normalize_phone(h) not in known]
    if added:
        path = os.path.join(BASE_DIR, ".authorized_users")
        with open(path, "a") as f:
            for handle in added:
                f.write(f"{handle}  # auto: member of group {chat_id}\n")
        os.chmod(path, 0o600)
    return added


# In group chats Henry only answers when addressed ("henry 2 milk, eggs").
GROUP_TRIGGER = re.compile(r"^\s*@?(?:hungry\s+)?henry\b[\s,:!.-]*", re.I)


def pending_key(chat_id, sender, is_group):
    """Group chats track one pending order per member, DMs one per chat."""
    if is_group and sender:
        return f"{chat_id}|{normalize_phone(sender)}"
    return str(chat_id)


SENDER_KEYS = ("sender_handle", "from", "sender", "sender_number",
               "from_number", "phone_number", "phone", "handle", "address",
               "participant")


def find_sender(obj, depth=0):
    """Best-effort hunt for the sender's phone number/handle in a payload."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    for k in SENDER_KEYS:
        v = obj.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
        if isinstance(v, dict):
            if v.get("is_me") is True:  # that's our own Linq number
                continue
            for kk in ("phone_number", "number", "handle", "address", "id"):
                vv = v.get(kk)
                if isinstance(vv, (str, int)) and str(vv).strip():
                    return str(vv)
    for k in CONTAINER_KEYS:
        if isinstance(obj.get(k), dict):
            found = find_sender(obj[k], depth + 1)
            if found:
                return found
    return None


def batch_person(sender):
    """Roster-friendly label for an iMessage sender (masked number)."""
    digits = re.sub(r"\D", "", str(sender or ""))
    return f"iMessage …{digits[-4:]}" if len(digits) >= 4 else "iMessage friend"


def find_chat_dict(obj, depth=0):
    """The chat object ({id, is_group, ...}) in a webhook payload, if any."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    for k in ("chat", "conversation"):
        v = obj.get(k)
        if isinstance(v, dict) and str(v.get("id") or "").strip():
            return v
    for k in CONTAINER_KEYS:
        if isinstance(obj.get(k), dict):
            found = find_chat_dict(obj[k], depth + 1)
            if found:
                return found
    return None


CHAT_ID_KEYS = ("chat_id", "chatId", "conversation_id", "conversationId")


def find_chat_id(obj, depth=0):
    """Best-effort hunt for the chat/conversation id in a webhook payload."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    for k in CHAT_ID_KEYS:
        v = obj.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    for k in ("chat", "conversation"):
        v = obj.get(k)
        if isinstance(v, dict) and str(v.get("id") or "").strip():
            return str(v["id"])
    for k in CONTAINER_KEYS:
        if isinstance(obj.get(k), dict):
            found = find_chat_id(obj[k], depth + 1)
            if found:
                return found
    return None


def linq_api_token():
    token = os.environ.get("LINQ_API_TOKEN")
    if token:
        return token
    try:
        with open(os.path.join(BASE_DIR, ".linq_api_token")) as f:
            return f.read().strip()
    except OSError:
        return None


def send_read_receipt(chat_id):
    """Mark the Linq chat read so the sender sees the agent saw their text."""
    token = linq_api_token()
    if not token or not chat_id:
        return False
    req = urllib.request.Request(
        f"https://api.linqapp.com/v3/chats/{urllib.parse.quote(str(chat_id))}/read",
        method="POST", data=b"",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"read receipt failed for chat {chat_id}: HTTP {e.code}\n")
    except Exception as e:
        sys.stderr.write(f"read receipt failed for chat {chat_id}: {e}\n")
    return False


def send_linq_message(chat_id, text):
    """Reply into the Linq chat (iMessage/SMS)."""
    token = linq_api_token()
    if not token or not chat_id:
        return False
    body = json.dumps(
        {"message": {"parts": [{"type": "text", "value": text}]}}).encode()
    req = urllib.request.Request(
        f"https://api.linqapp.com/v3/chats/{urllib.parse.quote(str(chat_id))}/messages",
        method="POST", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"linq send failed for chat {chat_id}: HTTP {e.code}\n")
    except Exception as e:
        sys.stderr.write(f"linq send failed for chat {chat_id}: {e}\n")
    return False


def claude_oauth_token():
    try:
        with open(os.path.join(BASE_DIR, ".claude_oauth_token")) as f:
            return f.read().strip()
    except OSError:
        return None


def ask_claude(prompt, timeout=90):
    """One headless Claude call; returns the text reply."""
    token = claude_oauth_token()
    if not token:
        raise DDError("no Claude token in .claude_oauth_token")
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=token)
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.run(["claude", "-p", prompt, "--model", "haiku"],
                          capture_output=True, text=True, timeout=timeout, env=env)
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise DDError(f"claude call failed: {proc.stderr.strip()[:200]}")
    return out


def claude_interpret(text, pending_summary):
    """Classify an incoming text: order / confirm / cancel / chat (+ items)."""
    pending_note = (f"The customer has a PENDING order awaiting their confirmation:\n"
                    f"{pending_summary}\n" if pending_summary
                    else "There is no pending order.\n")
    prompt = f"""You are the message classifier for a grocery-ordering agent that people text over iMessage.
{pending_note}The customer just texted:
\"\"\"{text}\"\"\"

Reply ONLY with a JSON object, no other text:
{{"intent": "order" | "confirm" | "cancel" | "chat", "items": [{{"name": "...", "quantity": 1}}]}}

Rules:
- "order": the message contains a grocery/shopping list or asks to buy items. Extract every item with quantity (default 1; decimals allowed for weight items like meat or produce by the pound). Do not invent items.
- Quantity means packages/units as sold in a store, NOT individual pieces: "a dozen eggs" is ONE carton (quantity 1), "two dozen eggs" is quantity 2, "a 6-pack of soda" is quantity 1.
- "confirm": ONLY when a pending order exists and this message agrees to it (yes / yep / confirm / sounds good / thumbs up).
- "cancel": ONLY when a pending order exists and this message declines or cancels it.
- "chat": greetings, thanks, questions, or anything that is not an instruction to buy things.
- If the message changes or adds to the list while an order is pending, use intent "order" with the full UPDATED list: the pending order's items with this message's changes applied (item names in plain form, e.g. "milk" not "Tuscan Dairy Farms Whole Milk (1 qt)").
- If the message replaces the order outright ("scratch that, just get X"), use intent "order" with only the new items."""
    out = ask_claude(prompt)
    m = re.search(r"\{.*\}", out, re.S)
    data = json.loads(m.group(0)) if m else None
    if not data or data.get("intent") not in ("order", "confirm", "cancel", "chat"):
        raise DDError("uninterpretable claude output")
    return data


CONFIRM_WORDS = {"yes", "yep", "yeah", "y", "confirm", "confirmed", "sure",
                 "ok", "okay", "sounds", "good", "do", "it", "place", "go"}
CANCEL_WORDS = {"no", "nope", "nah", "cancel", "nevermind", "stop", "dont", "don't"}


def heuristic_interpret(text, has_pending):
    """Fallback when the Claude call fails."""
    words = set(re.findall(r"[a-z']+", text.lower()))
    if has_pending:
        if words and words <= CONFIRM_WORDS:
            return {"intent": "confirm"}
        if words & CANCEL_WORDS:
            return {"intent": "cancel"}
    cleaned = message_to_order_text(text)
    if looks_like_chitchat(cleaned):
        return {"intent": "chat"}
    return {"intent": "order", "items": parse_order_text(cleaned)}


def resolve_bulk(body, items):
    """Resolve items to store products WITHOUT touching the cart."""
    if body.get("store_id"):
        store_id = str(body["store_id"])
        resolved, missing = resolve_via_find_items(store_id, items)
        if not resolved:
            raise DDError(f"no requested items matched at store {store_id}")
        return {"store_id": store_id,
                "store_name": body.get("store_name") or f"store {store_id}",
                "menu_id": get_menu_id(store_id, resolved[0]["id"]),
                "resolved": resolved,
                "notes": ["couldn't find: " + ", ".join(missing)] if missing else [],
                "alt_stores": [], "delivery_address": None}
    args = ["build-grocery-list", "--items-json", json.dumps(items)]
    if body.get("store_name"):
        args += ["--desired-mx-name", str(body["store_name"])]
    gl = run_dd(*args)
    resolved = gl.get("items") or []
    if not gl.get("success") or not resolved:
        raise DDError(gl.get("message") or "No items could be resolved.")
    store_id = str(resolved[0]["store_id"])
    # keep partial-match notes ("couldn't find: X"), drop success boilerplate
    msg = gl.get("message") or ""
    return {"store_id": store_id, "store_name": gl.get("store_name"),
            "menu_id": gl.get("menu_id") or get_menu_id(store_id, resolved[0]["id"]),
            "resolved": resolved,
            "notes": [msg] if msg and not msg.startswith("Successfully") else [],
            "alt_stores": [(str(s.get("store_id")), s.get("name"))
                           for s in (gl.get("available_stores") or [])],
            "delivery_address": gl.get("delivery_address")}


def preview_items(resolved):
    return [{"name": it["name"], "quantity": clean_qty(it.get("quantity", 1)),
             "price": it.get("price"), "unit": it.get("measurement_unit")}
            for it in resolved]


def approx_total(resolved):
    return sum((it.get("price") or 0) * float(it.get("quantity") or 1)
               for it in resolved)


def summarize_resolved(res):
    lines = []
    for it in res["resolved"]:
        q = clean_qty(it.get("quantity", 1))
        unit = f" {it['measurement_unit']}" if it.get("measurement_unit") else "x"
        price = f" (${it['price']})" if it.get("price") is not None else ""
        lines.append(f"- {q}{unit} {it['name']}{price}")
    return f"From {res['store_name']}:\n" + "\n".join(lines)


def minutes_to_fire():
    with STATE_LOCK:
        nf = STATE["next_fire_at"]
    if not nf:
        return None
    return max(1, round((nf - time.time()) / 60))


def compose_confirm_text(res, summary):
    """Have Claude write the confirmation iMessage; template on failure."""
    total = approx_total(res["resolved"])
    mins = minutes_to_fire()
    when = f"in about {mins} minutes" if mins else "soon"
    notes = "; ".join(n for n in res.get("notes") or [] if n)
    fallback = (f"Here's what I found —\n{summary}\n"
                + (f"Note: {notes}\n" if notes else "")
                + f"Est ${total:.2f} before fees. The next group run goes out "
                  f"{when} (drop: {DROP_POINT['name']}). "
                  "Reply YES to hop on or NO to cancel.")
    try:
        out = ask_claude(
            "Write a short, friendly plain-text iMessage to a customer confirming "
            "their grocery order BEFORE it joins a shared group DoorDash run. List "
            "every item with quantity and price, name the store, give the "
            f"approximate total ${total:.2f} before fees, and say the group order "
            f"goes out {when} with pickup at {DROP_POINT['name']}"
            + (f", and mention this note: {notes}" if notes else "")
            + ". End by asking them to reply YES to confirm or NO to cancel. "
            f"Reply ONLY with the message text.\n\nOrder:\n{summary}")
        return out.strip() or fallback
    except Exception:
        return fallback


def handle_conversation(chat_id, text, sender, params, entry, is_group=False):
    """Background worker: interpret a text, reply, and manage the confirm loop."""
    try:
        now = time.time()
        pkey = pending_key(chat_id, sender, is_group)
        with PENDING_LOCK:
            pending = PENDING_ORDERS.get(pkey)
            if pending and now - pending["created"] > PENDING_TTL:
                del PENDING_ORDERS[pkey]
                pending = None
        try:
            intent = claude_interpret(text, pending["summary"] if pending else None)
        except Exception as e:
            sys.stderr.write(f"claude interpret failed ({e}); using heuristics\n")
            intent = heuristic_interpret(text, bool(pending))
        kind = intent.get("intent")
        entry["intent"] = kind

        if kind == "confirm" and pending:
            person = batch_person(pending.get("sender") or sender)
            _, result = batch_join({"person": person,
                                    "items": pending["batch_items"],
                                    "chat_id": chat_id})
            with PENDING_LOCK:
                PENDING_ORDERS.pop(pkey, None)
            if result.get("ok"):
                est = approx_total(pending["resolved"])
                ORDERS.appendleft({
                    "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "person": person,
                    "items": pending["batch_items"],
                    "est_total": round(est, 2),
                    "store_hint": pending.get("store_name"),
                    "status": "joined_batch",
                })
                mins = minutes_to_fire()
                when = f"in ~{mins} min" if mins else "soon"
                entry["status"] = "done"
                entry["result"] = {"ok": True, "joined_batch": True,
                                   "person": person, "est_total": round(est, 2)}
                who = f"{person} is" if is_group else "You're"
                send_linq_message(chat_id,
                                  f"{who} on the run — {len(pending['batch_items'])} "
                                  f"item(s), est ${est:.2f}. The group order goes out "
                                  f"{when} (pickup: {DROP_POINT['name']}). "
                                  "I'll text here when it's sent.")
            else:
                entry["status"] = "failed"
                entry["result"] = result
                send_linq_message(chat_id, "Sorry — couldn't add you to the run: "
                                  f"{result.get('error')}")
        elif kind == "cancel" and pending:
            with PENDING_LOCK:
                PENDING_ORDERS.pop(pkey, None)
            entry["status"] = "cancelled"
            person = batch_person(pending.get("sender") or sender)
            send_linq_message(chat_id,
                              (f"Cancelled {person}'s order. " if is_group
                               else "No problem — cancelled that order. ")
                              + "Text me a new list anytime.")
        elif kind == "order":
            items = normalize_items({"items": intent.get("items") or []}) \
                or normalize_items({"text": message_to_order_text(text)})
            if not items:
                entry["status"] = "ignored"
                return
            entry["requested"] = items
            entry["status"] = "resolving"
            body = {k: params[k][0] for k in ("store_id", "store_name")
                    if params.get(k)}
            res = resolve_bulk(body, items)
            # batch entries keep the short requested names; est comes from
            # the resolution when it lines up 1:1
            if len(res["resolved"]) == len(items):
                batch_items = []
                for i, it in enumerate(items):
                    b = {"name": it["name"], "quantity": clean_qty(it["quantity"])}
                    price = res["resolved"][i].get("price")
                    if isinstance(price, (int, float)) and price > 0:
                        b["est"] = price
                    batch_items.append(b)
            else:
                batch_items = [{"name": it["name"][:40],
                                "quantity": clean_qty(it.get("quantity", 1)),
                                **({"est": it["price"]}
                                   if isinstance(it.get("price"), (int, float))
                                   and it["price"] > 0 else {})}
                               for it in res["resolved"]]
            summary = summarize_resolved(res)
            confirm_msg = compose_confirm_text(res, summary)
            if is_group:
                confirm_msg = f"For {batch_person(sender)}:\n{confirm_msg}"
            with PENDING_LOCK:
                PENDING_ORDERS[pkey] = {**res, "items": items,
                                        "batch_items": batch_items,
                                        "sender": sender,
                                        "created": time.time(),
                                        "summary": summary}
            sent = send_linq_message(chat_id, confirm_msg)
            push_event(f"{batch_person(sender)} texted an order, awaiting YES")
            entry["status"] = "awaiting_confirmation"
            entry["result"] = {"ok": True, "store_name": res["store_name"],
                               "resolved": preview_items(res["resolved"]),
                               "confirmation_sent": sent,
                               "confirmation_message": confirm_msg}
        else:  # chat
            entry["status"] = "ignored"
    except Exception as e:
        entry["status"] = "failed"
        entry["result"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            send_linq_message(chat_id, "Sorry, something went wrong with that — "
                              "mind texting your list again?")
        except Exception:
            pass


def linq_signing_secrets():
    """All configured signing secrets — Linq issues one per subscription."""
    found = []
    if os.environ.get("LINQ_SIGNING_SECRET"):
        found.append(os.environ["LINQ_SIGNING_SECRET"])
    try:
        with open(os.path.join(BASE_DIR, ".linq_signing_secret")) as f:
            found += [line.strip() for line in f if line.strip()]
    except OSError:
        pass
    return found


def verify_linq_signature(headers, raw_body):
    """Standard-Webhooks HMAC check; enforced only when a secret is configured."""
    secrets_list = linq_signing_secrets()
    if not secrets_list:
        return True  # the URL path token is the gate
    msg_id = headers.get("webhook-id") or ""
    ts = headers.get("webhook-timestamp") or ""
    signed = f"{msg_id}.{ts}.".encode() + raw_body
    sig_header = (headers.get("webhook-signature")
                  or headers.get("X-Webhook-Signature") or "")
    for secret in secrets_list:
        key = secret[6:] if secret.startswith("whsec_") else secret
        try:
            key_bytes = base64.b64decode(key + "=" * (-len(key) % 4))
        except Exception:
            key_bytes = key.encode()
        expected = base64.b64encode(
            hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()
        if any(hmac.compare_digest(c.partition(",")[2], expected)
               for c in sig_header.split()):
            return True
    return False


def already_seen(event_id):
    """Dedup webhook retries (senders retry when we were slow once)."""
    if not event_id:
        return False
    with SEEN_LOCK:
        if event_id in SEEN_IDS:
            return True
        SEEN_IDS[event_id] = True
        while len(SEEN_IDS) > 500:
            SEEN_IDS.popitem(last=False)
    return False


def process_webhook_order(body, entry):
    """Background worker for programmatic (non-chat) webhook payloads."""
    try:
        _, result = place_bulk_order(body)
        entry["status"] = "done" if result.get("ok") else "failed"
        entry["result"] = result
    except DDError as e:
        entry["status"] = "failed"
        entry["result"] = {"ok": False, "error": str(e)}
    except Exception as e:
        entry["status"] = "failed"
        entry["result"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


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

    def is_tunneled(self):
        """True when the request arrived through the public Cloudflare tunnel."""
        return bool(self.headers.get("Cf-Connecting-Ip") or self.headers.get("Cf-Ray"))

    def header_token_ok(self):
        return hmac.compare_digest(self.headers.get("X-Webhook-Token", ""),
                                   WEBHOOK_TOKEN)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        if self.is_tunneled():  # everything except the webhook is local-only
            self.send_json(404, {"ok": False, "error": "not found"})
        elif self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
            self.send_file(os.path.join(STATIC_DIR, "dashboard.html"),
                           "text/html; charset=utf-8")
        elif self.path == "/api/dashboard":
            self.handle_api(lambda: (200, dashboard_payload()))
        elif self.path == "/api/stores":
            self.handle_api(lambda: (200, run_dd("find-nearby-stores", "--max", "15")))
        elif self.path == "/api/carts":
            self.handle_api(lambda: (200, run_dd("cart", "list")))
        elif self.path == "/api/webhook-log":
            self.send_json(200, {"ok": True, "log": list(WEBHOOK_LOG)})
        elif self.path == "/api/orders":
            snap = imessage_snapshot()
            self.send_json(200, {"ok": True, "orders": list(ORDERS),
                                 "awaiting_confirmation": snap["awaiting"]})
        elif self.path.startswith("/assets/"):
            rel = os.path.normpath(self.path.split("?", 1)[0].lstrip("/"))
            path = os.path.join(STATIC_DIR, rel)
            if rel.startswith("assets" + os.sep) and \
                    os.path.abspath(path).startswith(STATIC_DIR + os.sep):
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
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if path.startswith("/webhook/linq"):
            self.handle_linq_webhook(path, params)
            return
        if self.is_tunneled():
            # Through the tunnel only /api/order is reachable, token-gated;
            # batch/deals/timer stay local-only.
            if path.rstrip("/") != "/api/order":
                self.send_json(404, {"ok": False, "error": "not found"})
                return
            if not self.header_token_ok():
                self.send_json(403, {"ok": False,
                                     "error": "missing or bad X-Webhook-Token"})
                return
        route = self.POST_ROUTES.get(path.rstrip("/") or path)
        if route is None:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            body = json.loads(self.read_body() or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        self.handle_api(lambda: route(body))

    def handle_linq_webhook(self, path, params):
        token = path[len("/webhook/linq"):].strip("/") or \
            self.headers.get("X-Webhook-Token", "")
        if not hmac.compare_digest(token, WEBHOOK_TOKEN):
            self.send_json(403, {"ok": False, "error": "bad webhook token"})
            return
        raw = self.read_body()
        if not verify_linq_signature(self.headers, raw):
            self.send_json(401, {"ok": False, "error": "bad webhook signature"})
            return
        event = (self.headers.get("webhook-event")
                 or self.headers.get("X-Webhook-Event") or "")
        if event and "received" not in event:
            self.send_json(200, {"ok": True, "ignored": f"event '{event}'"})
            return
        if already_seen(self.headers.get("webhook-id")
                        or self.headers.get("X-Webhook-ID")):
            self.send_json(200, {"ok": True, "ignored": "duplicate delivery"})
            return
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"ok": False, "error": "invalid JSON body"})
            return

        try:  # capture raw deliveries so unknown payload shapes can be mapped
            with open(os.path.join(BASE_DIR, ".last_deliveries.jsonl"), "a") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                    "event": event, "payload": payload}) + "\n")
        except OSError:
            pass

        authorized = load_authorized_users()
        sender = find_sender(payload)
        chat = find_chat_dict(payload)
        chat_id = str(chat["id"]) if chat else find_chat_id(payload)
        is_group = bool(chat and chat.get("is_group"))

        # Guard: only handle messages on the agent's own Linq line. The
        # account carries other lines (business traffic) — never touch those.
        agent_number = agent_line()
        owner = ((chat or {}).get("owner_handle") or {}).get("handle")
        if agent_number and owner and normalize_phone(owner) != agent_number:
            self.send_json(200, {"ok": True,
                                 "ignored": "message is for a different line"})
            return
        sender_known = bool(sender) and (not authorized
                                         or normalize_phone(sender) in authorized)

        if is_group:
            # Membership in a provisioned group chat IS the authorization.
            if chat_id not in load_authorized_chats():
                if sender_known:
                    # An authorized user speaking in a new group provisions it.
                    if authorize_chat(chat_id,
                                      f"provisioned by {batch_person(sender)}"):
                        added = harvest_group_members(chat_id)
                        push_event(f"Group chat provisioned by {batch_person(sender)}"
                                   + (f", {len(added)} members authorized"
                                      if added else ""))
                        threading.Thread(target=send_linq_message, args=(
                            chat_id,
                            "Hungry Henry here — this group can now order "
                            "groceries. Start a message with \"henry\" + your "
                            "list (e.g. \"henry 2 milk, eggs\") and I'll confirm "
                            "before it joins the group run."), daemon=True).start()
                else:
                    self.send_json(200, {"ok": True,
                                         "ignored": "group chat not provisioned"})
                    return
        else:
            if authorized and sender and normalize_phone(sender) not in authorized:
                self.send_json(200, {"ok": True,
                                     "ignored": "sender not an authorized user"})
                return
            if authorized and not sender:
                # Unknown payload shape — let it through so real users aren't
                # locked out, but flag it so the field name can be added.
                sys.stderr.write("warning: allowlist active but no sender field "
                                 "found in webhook payload — allowing\n")

        if chat_id:
            threading.Thread(target=send_read_receipt, args=(chat_id,),
                             daemon=True).start()

        # Conversational path: a real chat we can reply into gets the
        # Claude-driven confirm loop; a YES joins the group batch.
        raw_text = find_message_text(payload)
        is_native = bool(payload.get("items") or payload.get("text"))
        if not is_native and chat_id and raw_text:
            text = raw_text
            if is_group:
                m = GROUP_TRIGGER.match(text)
                with PENDING_LOCK:
                    has_pending = pending_key(chat_id, sender, True) in PENDING_ORDERS
                if m:
                    text = text[m.end():].strip() or text
                elif not has_pending:
                    # Group banter not addressed to Henry (and no pending
                    # order whose YES/NO this could be) stays untouched.
                    self.send_json(200, {"ok": True, "marked_read": True,
                                         "ignored": "not addressed to henry"})
                    return
            entry = {"received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                     "chat_id": chat_id, "message": raw_text,
                     "group": is_group, "status": "interpreting", "result": None}
            WEBHOOK_LOG.appendleft(entry)
            threading.Thread(target=handle_conversation,
                             args=(chat_id, text, sender, params, entry, is_group),
                             daemon=True).start()
            self.send_json(202, {"ok": True, "marked_read": True,
                                 "note": "interpreting message; any order will be "
                                         "confirmed over iMessage before it joins "
                                         "the group run"})
            return

        body = extract_order_payload(payload)
        if body.get("text") and looks_like_chitchat(body["text"]):
            self.send_json(200, {"ok": True, "marked_read": bool(chat_id),
                                 "ignored": "message doesn't look like an order"})
            return
        for k in ("store_id", "store_name"):  # query params override payload
            if params.get(k):
                body[k] = params[k][0]
        items = normalize_items(body)
        if not items:
            self.send_json(200, {"ok": True, "marked_read": bool(chat_id),
                                 "ignored": "no order items found in message"})
            return

        # Programmatic payloads order immediately in the background —
        # webhook senders retry on timeout, which would double the order.
        entry = {"received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "requested": items, "status": "processing", "result": None}
        WEBHOOK_LOG.appendleft(entry)
        threading.Thread(target=process_webhook_order, args=(body, entry),
                         daemon=True).start()
        self.send_json(202, {"ok": True, "accepted": items,
                             "note": "adding to your DoorDash cart in the background; "
                                     "see GET /api/webhook-log (local only) for the result"})

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
    print(f"Webhook path (append to your public tunnel URL): /webhook/linq/{WEBHOOK_TOKEN}")
    server.serve_forever()


if __name__ == "__main__":
    main()
