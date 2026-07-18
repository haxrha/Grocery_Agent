#!/usr/bin/env python3
"""Group-order bot backend.

Sits between the iMessage relay and the DoorDash CLI bridge (server.py):
teammates text a bot in a group chat; the relay POSTs each message here;
an LLM parses it; items aggregate per group; on "checkout" the combined
list goes to DoorDash on one company Ramp card and everyone gets an
itemized receipt with a Venmo link.

API (JSON):
  POST /api/message                  {sender, group_id, text, name?} -> {reply, session}
  GET  /api/groups                   all sessions (frontend feed)
  GET  /api/groups/<id>              one session incl. receipts
  POST /api/groups/<id>/checkout     force checkout (body: {store_name?, store_id?})
  POST /api/groups/<id>/settle       {sender} mark a share as paid
  GET  /api/receipts/<rcpt-id>       receipt JSON
  GET  /receipts/<rcpt-id>           printable HTML receipt
  GET  /api/health                   subsystem status

Run: python agent_server.py [port]   (default 8766, binds 127.0.0.1)
Env:  MOCK_DD=1 forces mock DoorDash resolution (auto when dd-cli missing).
"""

import json
import os
import re
import shutil
import sys
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import llm
import payments
import store
from server import DDError, place_bulk_order

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MOCK_DD = os.environ.get("MOCK_DD") == "1" or not (
    shutil.which("dd-cli") or shutil.which("dd-cli.exe"))


# ------------------------------------------------------------------ helpers

def norm(name):
    return re.sub(r"\s+", " ", name.strip().lower())


def aggregate_items(session):
    """Merge everyone's items into one order list (sum duplicate names)."""
    merged = {}
    for person in session["participants"].values():
        for it in person["items"]:
            key = norm(it["name"])
            if key in merged:
                merged[key]["quantity"] += it["quantity"]
            else:
                merged[key] = {"name": it["name"], "quantity": it["quantity"]}
    return list(merged.values())


def mock_resolve(items, store_name):
    """Deterministic fake DoorDash resolution so the demo runs without dd-cli."""
    resolved = [{
        "name": it["name"].title(),
        "quantity": it["quantity"],
        "price": round(1.50 + (zlib.crc32(norm(it["name"]).encode()) % 900) / 100, 2),
        "unit": None,
    } for it in items]
    return 200, {
        "ok": True,
        "mock": True,
        "store_name": store_name or "FreshMart (mock)",
        "store_id": "0",
        "cart_uuid": store.new_id("mockcart"),
        "resolved": resolved,
        "notes": "MOCK MODE — dd-cli not on PATH; prices are fake",
        "item_errors": [],
        "delivery_address": "123 Demo St",
    }


def do_checkout(group_id, store_name=None, store_id=None):
    """Aggregate -> DoorDash cart -> Ramp charge -> receipts. Returns (status, payload)."""
    session = store.get(group_id)
    items = aggregate_items(session)
    if not items:
        return 400, {"ok": False, "error": "Nobody has added any items yet."}

    body = {"items": items}
    if store_id:
        body["store_id"] = store_id
    if store_name or session.get("store_name"):
        body["store_name"] = store_name or session["store_name"]

    if MOCK_DD:
        http_status, result = mock_resolve(items, body.get("store_name"))
    else:
        http_status, result = place_bulk_order(body)
    if not result.get("ok"):
        return http_status, {"ok": False, "error": result.get("error"),
                             "requested": items}

    receipts = payments.build_receipts(session, result.get("resolved"))
    charge = payments.charge(payments.order_total(receipts),
                             memo=f"Group order {group_id}")

    def apply(sess):
        sess["status"] = "ordered"
        sess["store_name"] = result.get("store_name")
        sess["order_result"] = result
        sess["charge"] = charge
        sess["receipts"] = receipts
    session = store.update(group_id, apply)
    return 200, {"ok": True, "session": session}


# ------------------------------------------------------------------ bot logic

HELP_TEXT = ("🛒 I collect the group's order. Text me things like:\n"
             "• \"2 burritos and a diet coke\" — add food\n"
             "• \"drop my coke\" — remove\n• \"status\" — what we have\n"
             "• \"order from Chipotle\" — pick the store\n"
             "• \"send it\" — I place ONE DoorDash order on the company Ramp "
             "card and text everyone their share\n• \"paid\" — after you Venmo")


def items_phrase(items):
    return ", ".join(f"{it['quantity']}× {it['name']}" for it in items)


def group_summary(session):
    people = {s: p for s, p in session["participants"].items() if p["items"]}
    if not people:
        return "Nothing in the group order yet."
    lines = [f"• {p.get('name') or s}: {items_phrase(p['items'])}"
             for s, p in people.items()]
    total_items = sum(it["quantity"] for p in people.values() for it in p["items"])
    head = f"📋 {len(people)} people, {total_items} items"
    if session.get("store_name"):
        head += f" → {session['store_name']}"
    return head + "\n" + "\n".join(lines) + "\nText \"send it\" to place the order."


def handle_message(body):
    sender = str(body.get("sender") or "").strip()
    group_id = str(body.get("group_id") or "default").strip()
    text = str(body.get("text") or "")
    display = str(body.get("name") or "").strip() or None
    if not sender:
        return 400, {"ok": False, "error": "missing 'sender'"}

    parsed = llm.parse_message(text)
    intent = parsed["intent"]
    session = store.get(group_id)

    if session["status"] == "ordered" and intent in ("order", "remove", "checkout"):
        reply = ("This group's order was already placed"
                 f" at {session.get('store_name')}. Start a new one by texting"
                 " items after everyone settles, or \"cancel\" it.")
        return 200, {"ok": True, "reply": reply, "parsed": parsed,
                     "session": session}

    if intent == "order":
        def add(sess):
            person = sess["participants"].setdefault(
                sender, {"name": display, "items": [], "paid": False})
            if display:
                person["name"] = display
            for it in parsed["items"]:
                for have in person["items"]:
                    if norm(have["name"]) == norm(it["name"]):
                        have["quantity"] += it["quantity"]
                        break
                else:
                    person["items"].append(dict(it))
            if parsed["store_name"]:
                sess["store_name"] = parsed["store_name"]
        session = store.update(group_id, add)
        who = display or sender
        n_people = sum(1 for p in session["participants"].values() if p["items"])
        reply = (f"Got it, {who} — {items_phrase(parsed['items'])} added. "
                 f"{n_people} {'person is' if n_people == 1 else 'people are'} in. "
                 "\"status\" to review, \"send it\" to order.")
        if parsed["store_name"]:
            reply += f" (Store set to {parsed['store_name']}.)"

    elif intent == "remove":
        removed = []
        def rm(sess):
            person = sess["participants"].get(sender)
            if not person:
                return
            if parsed["items"]:
                wanted = {norm(it["name"]) for it in parsed["items"]}
                keep = []
                for it in person["items"]:
                    if any(w in norm(it["name"]) or norm(it["name"]) in w for w in wanted):
                        removed.append(it["name"])
                    else:
                        keep.append(it)
                person["items"] = keep
            else:
                removed.extend(it["name"] for it in person["items"])
                person["items"] = []
        session = store.update(group_id, rm)
        reply = (f"Removed {', '.join(removed)}." if removed
                 else "Couldn't find that in your list — text \"status\" to see it.")

    elif intent == "status":
        reply = group_summary(session)

    elif intent == "set_store":
        session = store.update(group_id, lambda s: s.update(
            store_name=parsed["store_name"]))
        reply = f"Store set to {parsed['store_name']}. Keep the orders coming!"

    elif intent == "checkout":
        status_code, result = do_checkout(group_id, parsed["store_name"])
        if not result.get("ok"):
            return 200, {"ok": True, "parsed": parsed, "session": session,
                         "reply": f"⚠️ Couldn't place the order: {result.get('error')}"}
        session = result["session"]
        r = session["order_result"]
        lines = [f"✅ Order placed at {r.get('store_name')} on the company card "
                 f"({session['charge']['card']}). Total ${session['charge']['amount']:.2f}."]
        for rcpt in session["receipts"]:
            lines.append(f"• {rcpt['name']}: ${rcpt['total']:.2f} → {rcpt['pay_url']}")
        if r.get("notes"):
            lines.append(f"ℹ️ {r['notes']}")
        reply = "\n".join(lines)

    elif intent == "cancel":
        session = store.update(group_id, lambda s: s.update(status="cancelled"))
        reply = "Order cancelled. Text me items whenever you're hungry again."

    elif intent == "pay":
        def pay(sess):
            person = sess["participants"].get(sender)
            if person:
                person["paid"] = True
            for rcpt in sess.get("receipts") or []:
                if rcpt["sender"] == sender:
                    rcpt["paid"] = True
            if sess["status"] == "ordered" and sess.get("receipts") and \
                    all(rc["paid"] for rc in sess["receipts"]):
                sess["status"] = "settled"
        session = store.update(group_id, pay)
        reply = "💸 Marked you as paid — thanks!"
        if session["status"] == "settled":
            reply += " Everyone's settled up. 🎉"

    elif intent == "help":
        reply = HELP_TEXT

    else:
        reply = parsed.get("reply_hint") or \
            "Not sure what you meant — text \"help\" for what I can do."

    return 200, {"ok": True, "reply": reply, "parsed": parsed, "session": session}


# ------------------------------------------------------------------ HTTP layer

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def fresh_receipt(self, receipt_id):
        """Find a receipt, refreshing its Stripe payment status first."""
        sess, rcpt = store.find_receipt(receipt_id)
        if rcpt and not rcpt["paid"]:
            sess = store.update(sess["id"], payments.refresh_receipts)
            rcpt = next(r for r in sess["receipts"] if r["id"] == receipt_id)
        return sess, rcpt

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(STATIC_DIR, "agent.html"), encoding="utf-8") as f:
                    self.send_html(f.read())
            except OSError:
                self.send_json(404, {"ok": False, "error": "agent.html missing"})
        elif path == "/api/health":
            self.send_json(200, {
                "ok": True,
                "doordash": "mock (dd-cli not on PATH)" if MOCK_DD else "dd-cli",
                "llm": llm.parser_status(),
                "payments": payments.status(),
            })
        elif path == "/api/groups":
            self.send_json(200, {"ok": True, "groups": store.all_sessions()})
        elif path.startswith("/api/groups/"):
            group_id = urllib.parse.unquote(path.split("/")[3])
            sess = store.peek(group_id)
            if sess:
                if sess.get("receipts"):
                    sess = store.update(group_id, payments.refresh_receipts)
                self.send_json(200, {"ok": True, "session": sess})
            else:
                self.send_json(404, {"ok": False, "error": "no such group"})
        elif path.startswith("/api/receipts/"):
            sess, rcpt = self.fresh_receipt(path.split("/")[3])
            if rcpt:
                self.send_json(200, {"ok": True, "receipt": rcpt})
            else:
                self.send_json(404, {"ok": False, "error": "no such receipt"})
        elif path.startswith("/receipts/"):
            sess, rcpt = self.fresh_receipt(path.split("/")[2])
            if rcpt:
                self.send_html(payments.receipt_html(sess, rcpt))
            else:
                self.send_json(404, {"ok": False, "error": "no such receipt"})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            body = self.read_body()
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        try:
            if path == "/api/message":
                status, payload = handle_message(body)
            elif path.startswith("/api/groups/") and path.endswith("/checkout"):
                status, payload = do_checkout(urllib.parse.unquote(path.split("/")[3]),
                                              body.get("store_name"),
                                              body.get("store_id"))
            elif path.startswith("/api/groups/") and path.endswith("/settle"):
                group_id = urllib.parse.unquote(path.split("/")[3])
                status, payload = handle_message(
                    {"sender": body.get("sender"), "group_id": group_id,
                     "text": "paid"})
            else:
                status, payload = 404, {"ok": False, "error": "not found"}
        except DDError as e:
            status, payload = 502, {"ok": False, "error": str(e)}
        except Exception as e:  # keep the server alive
            status, payload = 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.send_json(status, payload)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("AGENT_PORT", 8766))
    print(f"Group-order bot on http://127.0.0.1:{port}"
          f"  (DoorDash: {'MOCK' if MOCK_DD else 'dd-cli'}, "
          f"payments: {payments.mode()})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
