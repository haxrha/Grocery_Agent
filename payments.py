#!/usr/bin/env python3
"""Stripe payment + split/receipt logic for group orders.

Money flows in two halves:

  1. The actual DoorDash order is charged to whatever card is saved in the
     DoorDash account dd-cli is signed into (a real personal card for the
     live demo — Stripe Issuing test cards can't pay real merchants, and
     live Issuing needs sales approval).
  2. Everyone pays their share back through a Stripe Checkout link on their
     receipt. The backend polls the Checkout Session and marks receipts
     paid automatically. Works in test mode with card 4242 4242 4242 4242.

Optional flourish: with STRIPE_ISSUING=1 (test key + Issuing enabled in the
sandbox), a virtual "company card" is created via the Issuing API and a
simulated authorization for the order total is captured against it, so the
demo shows a real card object being charged.

Everything is raw Stripe REST via urllib (stdlib only, no SDK version
drift). Without STRIPE_SECRET_KEY the module degrades to a mock card and
Venmo links so the flow still demos offline.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import store

STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_ISSUING = os.environ.get("STRIPE_ISSUING") == "1"
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8766")  # for success links

VENMO_HANDLE = os.environ.get("VENMO_HANDLE", "your-venmo")
TAX_RATE = float(os.environ.get("TAX_RATE", "0.08875"))
DELIVERY_FEE = float(os.environ.get("DELIVERY_FEE", "4.99"))
SERVICE_FEE = float(os.environ.get("SERVICE_FEE", "2.50"))

ISSUING_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "stripe_issuing.json")


def is_live():
    return bool(STRIPE_KEY)


def mode():
    if STRIPE_KEY.startswith("sk_test"):
        return "stripe-test"
    if STRIPE_KEY.startswith("sk_live") or STRIPE_KEY.startswith("rk_live"):
        return "stripe-live"
    return "stripe" if STRIPE_KEY else "mock"


class PayError(Exception):
    pass


# ------------------------------------------------------------- stripe REST

def _flatten(params, prefix=""):
    """dict -> Stripe's form encoding: {"a": {"b": 1}} -> a[b]=1, lists by index."""
    pairs = []
    if isinstance(params, dict):
        for k, v in params.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            pairs.extend(_flatten(v, key))
    elif isinstance(params, list):
        for i, v in enumerate(params):
            pairs.extend(_flatten(v, f"{prefix}[{i}]"))
    else:
        pairs.append((prefix, str(params)))
    return pairs


def _stripe(method, path, params=None):
    if not STRIPE_KEY:
        raise PayError("STRIPE_SECRET_KEY not set")
    url = "https://api.stripe.com" + path
    body = None
    if params:
        encoded = urllib.parse.urlencode(_flatten(params))
        if method == "GET":
            url += "?" + encoded
        else:
            body = encoded.encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {STRIPE_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise PayError(f"Stripe {e.code} on {path}: {detail}")
    except urllib.error.URLError as e:
        raise PayError(f"Stripe unreachable: {e.reason}")


# ------------------------------------------------------------- company card

def _issuing_card():
    """Create (once) and cache a virtual company card via Stripe Issuing."""
    try:
        with open(ISSUING_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    holder = _stripe("POST", "/v1/issuing/cardholders", {
        "name": "Group Order Bot", "type": "individual",
        "email": "bot@example.com",
        "billing": {"address": {"line1": "1 Hacker Way", "city": "New York",
                                "state": "NY", "postal_code": "10001",
                                "country": "US"}},
    })
    card = _stripe("POST", "/v1/issuing/cards", {
        "cardholder": holder["id"], "currency": "usd",
        "type": "virtual", "status": "active",
    })
    info = {"card_id": card["id"], "last4": card.get("last4", "????")}
    os.makedirs(os.path.dirname(ISSUING_CACHE), exist_ok=True)
    with open(ISSUING_CACHE, "w", encoding="utf-8") as f:
        json.dump(info, f)
    return info


def card_label():
    if is_live() and STRIPE_ISSUING:
        try:
            return f"Company Card (Stripe Issuing ····{_issuing_card()['last4']})"
        except PayError as e:
            print(f"[payments] Issuing unavailable ({e}); using mock card label")
    return "Company Card ····4242 (saved in the DoorDash account)"


def charge(amount, memo):
    """Record the company-card charge for this order.

    The real charge happens on the card saved in DoorDash; this record is
    what the frontend shows. With STRIPE_ISSUING=1 on a test key, a
    simulated authorization for the amount is captured on the virtual card.
    """
    record = {
        "id": store.new_id("chg"),
        "card": card_label(),
        "amount": round(amount, 2),
        "memo": memo,
        "provider": mode(),
        "status": "pending_settlement",
        "stripe_authorization_id": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if is_live() and STRIPE_ISSUING and STRIPE_KEY.startswith("sk_test"):
        try:
            card = _issuing_card()
            auth = _stripe("POST", "/v1/test_helpers/issuing/authorizations", {
                "card": card["card_id"],
                "amount": int(round(amount * 100)),
                "merchant_data": {"name": "DOORDASH"},
            })
            _stripe("POST",
                    f"/v1/test_helpers/issuing/authorizations/{auth['id']}/capture")
            record["stripe_authorization_id"] = auth["id"]
            record["status"] = "captured"
        except PayError as e:
            print(f"[payments] simulated Issuing charge failed: {e}")
    return record


# ------------------------------------------------------------- pay links

def _checkout_link(receipt):
    """Stripe Checkout Session for one person's share -> (url, session_id)."""
    session = _stripe("POST", "/v1/checkout/sessions", {
        "mode": "payment",
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": int(round(receipt["total"] * 100)),
                "product_data": {
                    "name": f"{receipt['name']}'s share — group order {receipt['group_id']}",
                },
            },
        }],
        "metadata": {"receipt_id": receipt["id"], "group_id": receipt["group_id"],
                     "sender": receipt["sender"]},
        "success_url": f"{BASE_URL}/receipts/{receipt['id']}?paid=1",
        "cancel_url": f"{BASE_URL}/receipts/{receipt['id']}",
    })
    return session["url"], session["id"]


def _venmo_link(receipt):
    note = urllib.parse.quote(
        f"Group order {receipt['group_id']} — {receipt['name']}")
    return (f"https://account.venmo.com/pay?recipients={VENMO_HANDLE}"
            f"&amount={receipt['total']}&note={note}")


def refresh_receipts(session):
    """Poll unpaid Stripe Checkout Sessions and flip receipts to paid.
    Mutates the session dict in place (call inside store.update)."""
    if not is_live():
        return
    for rcpt in session.get("receipts") or []:
        if rcpt.get("paid") or not rcpt.get("stripe_session_id"):
            continue
        try:
            cs = _stripe("GET", f"/v1/checkout/sessions/{rcpt['stripe_session_id']}")
        except PayError:
            continue
        if cs.get("payment_status") == "paid":
            rcpt["paid"] = True
            person = session["participants"].get(rcpt["sender"])
            if person:
                person["paid"] = True
    if session.get("status") == "ordered" and session.get("receipts") and \
            all(r["paid"] for r in session["receipts"]):
        session["status"] = "settled"


# ------------------------------------------------------------- splits & receipts

def _price_lookup(resolved):
    out = []
    for it in resolved or []:
        try:
            price = float(it.get("price"))
        except (TypeError, ValueError):
            continue
        out.append((str(it.get("name", "")).lower(), price))
    return out


def _unit_price(name, priced):
    import difflib
    name = name.lower()
    for rname, price in priced:
        if name in rname or rname in name:
            return price
    names = [r for r, _ in priced]
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.5)
    if close:
        return dict(priced)[close[0]]
    return None


def build_receipts(session, resolved):
    """Per-person itemized receipts with pay links. Item prices come from
    the resolved DoorDash products; tax is proportional; delivery+service
    split equally."""
    priced = _price_lookup(resolved)
    label = card_label()
    people = [(s, p) for s, p in session["participants"].items() if p["items"]]
    n = max(len(people), 1)
    fixed_share = round((DELIVERY_FEE + SERVICE_FEE) / n, 2)

    receipts = []
    for sender, person in people:
        lines, subtotal, unpriced = [], 0.0, []
        for it in person["items"]:
            unit = _unit_price(it["name"], priced)
            line_total = round(unit * it["quantity"], 2) if unit is not None else None
            if line_total is None:
                unpriced.append(it["name"])
            else:
                subtotal += line_total
            lines.append({"name": it["name"], "quantity": it["quantity"],
                          "unit_price": unit, "line_total": line_total})
        tax = round(subtotal * TAX_RATE, 2)
        total = round(subtotal + tax + fixed_share, 2)
        rcpt = {
            "id": store.new_id("rcpt"),
            "group_id": session["id"],
            "sender": sender,
            "name": person.get("name") or sender,
            "items": lines,
            "subtotal": round(subtotal, 2),
            "tax": tax,
            "fees_share": fixed_share,
            "total": total,
            "unpriced_items": unpriced,
            "charged_to": label,
            "pay_method": "mock",
            "pay_url": None,
            "stripe_session_id": None,
            "paid": False,
        }
        if is_live():
            try:
                rcpt["pay_url"], rcpt["stripe_session_id"] = _checkout_link(rcpt)
                rcpt["pay_method"] = "stripe"
            except PayError as e:
                print(f"[payments] checkout link failed ({e}); using Venmo fallback")
        if not rcpt["pay_url"]:
            rcpt["pay_url"] = _venmo_link(rcpt)
            rcpt["pay_method"] = "venmo"
        receipts.append(rcpt)
    return receipts


def order_total(receipts):
    return round(sum(r["total"] for r in receipts), 2)


def receipt_html(session, receipt):
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    rows = "".join(
        f"<tr><td>{esc(l['quantity'])}×</td><td>{esc(l['name'])}</td>"
        f"<td style='text-align:right'>{'$%.2f' % l['line_total'] if l['line_total'] is not None else '—'}</td></tr>"
        for l in receipt["items"])
    pay_word = "card" if receipt["pay_method"] == "stripe" else "Venmo"
    paid = ("<p style='color:#2e7d32;font-weight:700'>PAID ✓</p>" if receipt["paid"]
            else f"<p><a href='{esc(receipt['pay_url'])}' style='display:inline-block;"
                 f"background:#635bff;color:#fff;padding:.6rem 1.2rem;border-radius:8px;"
                 f"text-decoration:none;font-weight:700'>Pay ${receipt['total']:.2f} by {pay_word} →</a></p>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Receipt {esc(receipt['id'])}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:420px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse}}td{{padding:.2rem 0}}
.tot td{{border-top:1px solid #999;font-weight:700;padding-top:.4rem}}
.muted{{color:#777;font-size:.85rem}}</style></head><body>
<h2>🧾 {esc(receipt['name'])}'s share</h2>
<p class="muted">Group order <b>{esc(session['id'])}</b> · {esc(session.get('store_name') or 'store TBD')}<br>
Fronted by {esc(receipt['charged_to'])}</p>
<table>{rows}
<tr><td colspan="2">Subtotal</td><td style="text-align:right">${receipt['subtotal']:.2f}</td></tr>
<tr><td colspan="2">Tax</td><td style="text-align:right">${receipt['tax']:.2f}</td></tr>
<tr><td colspan="2">Delivery + service (split)</td><td style="text-align:right">${receipt['fees_share']:.2f}</td></tr>
<tr class="tot"><td colspan="2">Your total</td><td style="text-align:right">${receipt['total']:.2f}</td></tr>
</table>{paid}
<p class="muted">Receipt {esc(receipt['id'])} · generated by Grocery Agent</p>
</body></html>"""


def status():
    return {"mode": mode(), "issuing": STRIPE_ISSUING and is_live(),
            "venmo_fallback": VENMO_HANDLE, "base_url": BASE_URL}
