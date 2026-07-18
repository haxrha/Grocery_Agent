#!/usr/bin/env python3
"""Bridge a basic web form to the DoorDash CLI.

Serves static/index.html and exposes a JSON API:

  GET  /api/stores          nearby grocery stores (for the store picker)
  GET  /api/carts           the consumer's open carts
  POST /api/order           resolve a bulk item list and add it all to the cart

POST /api/order body (either shape):
  {"text": "2 milk\neggs\nbread x3", "store_name": "Whole Foods"}
  {"items": [{"name": "milk", "quantity": 2}, ...], "store_id": "1741590"}

Run: python3 server.py [port]   (default 8765, binds 127.0.0.1 only)
"""

import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DD_TIMEOUT = 180


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
        if self.path in ("/", "/index.html"):
            self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        elif self.path == "/api/stores":
            self.handle_api(lambda: (200, run_dd("find-nearby-stores", "--max", "15")))
        elif self.path == "/api/carts":
            self.handle_api(lambda: (200, run_dd("cart", "list")))
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/order":
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        self.handle_api(lambda: place_bulk_order(body))

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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Grocery Agent listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
