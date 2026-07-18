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

import base64
import collections
import hashlib
import hmac
import json
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
DD_TIMEOUT = 180


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
ORDERS = collections.deque(maxlen=100)  # confirmed orders, shown in the web UI


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


# --- Linq webhook support ---------------------------------------------------

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


def linq_api_token():
    token = os.environ.get("LINQ_API_TOKEN")
    if token:
        return token
    try:
        with open(os.path.join(BASE_DIR, ".linq_api_token")) as f:
            return f.read().strip()
    except OSError:
        return None


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


def execute_pending(pending):
    """Place a confirmed pending order, falling back to other stores if closed."""
    try:
        added, existing = add_to_cart(pending["store_id"], pending["menu_id"],
                                      pending["resolved"])
        return build_response(added, existing, pending["resolved"],
                              pending["notes"], pending["items"],
                              pending["store_id"], pending["store_name"],
                              pending.get("delivery_address"))
    except DDError as e:
        if not is_store_closed(e):
            raise
    tried = {pending["store_id"]}
    for alt_id, alt_name in pending.get("alt_stores", [])[:6]:
        if alt_id in tried:
            continue
        tried.add(alt_id)
        note = (f"{pending['store_name']} wasn't accepting orders, "
                f"used {alt_name} instead")
        try:
            return order_at_store(alt_id, alt_name, pending["items"], [note])
        except DDError as e:
            if is_store_closed(e):
                continue
            raise
    raise DDError("No nearby store is accepting this order right now.")


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


def compose_confirm_text(res, summary):
    """Have Claude write the confirmation iMessage; template on failure."""
    total = approx_total(res["resolved"])
    notes = "; ".join(n for n in res.get("notes") or [] if n)
    fallback = (f"Here's your DoorDash order —\n{summary}\n"
                + (f"Note: {notes}\n" if notes else "")
                + f"Approx total ${total:.2f} before fees. "
                  "Reply YES to confirm or NO to cancel.")
    try:
        out = ask_claude(
            "Write a short, friendly plain-text iMessage to a customer confirming "
            "their DoorDash grocery order BEFORE it is placed. List every item with "
            "quantity and price, name the store, give the approximate total "
            f"${total:.2f} before fees"
            + (f", and mention this note: {notes}" if notes else "")
            + ". End by asking them to reply YES to confirm or NO to cancel. "
            f"Reply ONLY with the message text.\n\nOrder:\n{summary}")
        return out.strip() or fallback
    except Exception:
        return fallback


def ack_text(result):
    items = result.get("resolved") or []
    note = f" Note: {result['notes']}." if result.get("notes") else ""
    return (f"Order confirmed — added {len(items)} item(s) to your DoorDash cart "
            f"at {result.get('store_name')}, approx ${approx_total(items):.2f} "
            f"before fees.{note} Review and check out in the DoorDash app.")


def handle_conversation(chat_id, text, params, entry):
    """Background worker: interpret a text, reply, and manage the confirm loop."""
    try:
        now = time.time()
        with PENDING_LOCK:
            pending = PENDING_ORDERS.get(chat_id)
            if pending and now - pending["created"] > PENDING_TTL:
                del PENDING_ORDERS[chat_id]
                pending = None
        try:
            intent = claude_interpret(text, pending["summary"] if pending else None)
        except Exception as e:
            sys.stderr.write(f"claude interpret failed ({e}); using heuristics\n")
            intent = heuristic_interpret(text, bool(pending))
        kind = intent.get("intent")
        entry["intent"] = kind

        if kind == "confirm" and pending:
            entry["status"] = "ordering"
            _, result = execute_pending(pending)
            with PENDING_LOCK:
                PENDING_ORDERS.pop(chat_id, None)
            entry["status"] = "done" if result.get("ok") else "failed"
            entry["result"] = result
            if result.get("ok"):
                ORDERS.appendleft({
                    "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "chat_id": chat_id,
                    "store_name": result.get("store_name"),
                    "cart_uuid": result.get("cart_uuid"),
                    "resolved": result.get("resolved"),
                    "notes": result.get("notes"),
                    "appended_to_existing_cart": result.get("appended_to_existing_cart"),
                })
                send_linq_message(chat_id, ack_text(result))
            else:
                send_linq_message(chat_id, "Sorry — couldn't place the order: "
                                  f"{result.get('error')}")
        elif kind == "cancel" and pending:
            with PENDING_LOCK:
                PENDING_ORDERS.pop(chat_id, None)
            entry["status"] = "cancelled"
            send_linq_message(chat_id, "No problem — cancelled that order. "
                              "Text me a new list anytime.")
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
            summary = summarize_resolved(res)
            confirm_msg = compose_confirm_text(res, summary)
            with PENDING_LOCK:
                PENDING_ORDERS[chat_id] = {**res, "items": items,
                                           "created": time.time(),
                                           "summary": summary}
            sent = send_linq_message(chat_id, confirm_msg)
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
        elif self.path in ("/", "/index.html"):
            self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        elif self.path == "/api/stores":
            self.handle_api(lambda: (200, run_dd("find-nearby-stores", "--max", "15")))
        elif self.path == "/api/carts":
            self.handle_api(lambda: (200, run_dd("cart", "list")))
        elif self.path == "/api/webhook-log":
            self.send_json(200, {"ok": True, "log": list(WEBHOOK_LOG)})
        elif self.path == "/api/orders":
            now = time.time()
            with PENDING_LOCK:
                pending = [{"chat_id": cid, "store_name": p["store_name"],
                            "summary": p["summary"],
                            "age_seconds": int(now - p["created"])}
                           for cid, p in PENDING_ORDERS.items()
                           if now - p["created"] <= PENDING_TTL]
            self.send_json(200, {"ok": True, "orders": list(ORDERS),
                                 "awaiting_confirmation": pending})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if path.rstrip("/") == "/api/order":
            if self.is_tunneled() and not self.header_token_ok():
                self.send_json(403, {"ok": False, "error": "missing or bad X-Webhook-Token"})
                return
            try:
                body = json.loads(self.read_body() or b"{}")
            except json.JSONDecodeError:
                self.send_json(400, {"ok": False, "error": "invalid JSON body"})
                return
            self.handle_api(lambda: place_bulk_order(body))
        elif path.startswith("/webhook/linq"):
            self.handle_linq_webhook(path, params)
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

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
        if authorized and sender and normalize_phone(sender) not in authorized:
            self.send_json(200, {"ok": True,
                                 "ignored": "sender not an authorized user"})
            return
        if authorized and not sender:
            # Unknown payload shape — let it through so real users aren't
            # locked out, but flag it so the field name can be added.
            sys.stderr.write("warning: allowlist active but no sender field "
                             "found in webhook payload — allowing\n")

        chat_id = find_chat_id(payload)
        if chat_id:
            threading.Thread(target=send_read_receipt, args=(chat_id,),
                             daemon=True).start()

        # Conversational path: a real chat we can reply into gets the
        # Claude-driven confirm loop instead of ordering immediately.
        raw_text = find_message_text(payload)
        is_native = bool(payload.get("items") or payload.get("text"))
        if not is_native and chat_id and raw_text:
            entry = {"received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                     "chat_id": chat_id, "message": raw_text,
                     "status": "interpreting", "result": None}
            WEBHOOK_LOG.appendleft(entry)
            threading.Thread(target=handle_conversation,
                             args=(chat_id, raw_text, params, entry),
                             daemon=True).start()
            self.send_json(202, {"ok": True, "marked_read": True,
                                 "note": "interpreting message; any order will be "
                                         "confirmed over iMessage before it is placed"})
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

        # Respond immediately — resolution takes ~a minute and webhook
        # senders retry on timeout, which would double the order.
        entry = {"received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "requested": items, "status": "processing", "result": None}
        WEBHOOK_LOG.appendleft(entry)
        threading.Thread(target=process_webhook_order, args=(body, entry),
                         daemon=True).start()
        self.send_json(202, {"ok": True, "accepted": items,
                             "marked_read": bool(chat_id),
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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Grocery Agent listening on http://127.0.0.1:{port}")
    print(f"Webhook path (append to your public tunnel URL): /webhook/linq/{WEBHOOK_TOKEN}")
    server.serve_forever()


if __name__ == "__main__":
    main()
