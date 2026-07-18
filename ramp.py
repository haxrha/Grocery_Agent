#!/usr/bin/env python3
"""Ramp card payment + split/receipt logic for group orders.

The DoorDash order is paid on ONE company Ramp card; everyone else settles
their share back via Venmo. This module:

  - talks to the Ramp developer API (client-credentials OAuth) when
    RAMP_CLIENT_ID / RAMP_CLIENT_SECRET are set (RAMP_ENV=demo|prod,
    default demo sandbox),
  - records a charge against the chosen card (Ramp transactions are created
    by the card network, not the API, so the record starts as
    "pending_settlement" and match_transaction() can attach the real
    transaction id once it appears),
  - computes per-person splits and generates itemized receipts with
    Venmo deep links,
  - falls back to a clearly-labeled mock card when no credentials exist,
    so the whole flow demos offline.

Stdlib only (urllib), no extra deps.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import store

RAMP_ENV = os.environ.get("RAMP_ENV", "demo")
BASE = "https://api.ramp.com" if RAMP_ENV == "prod" else "https://demo-api.ramp.com"
CLIENT_ID = os.environ.get("RAMP_CLIENT_ID")
CLIENT_SECRET = os.environ.get("RAMP_CLIENT_SECRET")
CARD_ID = os.environ.get("RAMP_CARD_ID")           # optional pin, else first card
SCOPES = os.environ.get("RAMP_SCOPES", "cards:read transactions:read")

VENMO_HANDLE = os.environ.get("VENMO_HANDLE", "your-venmo")
TAX_RATE = float(os.environ.get("TAX_RATE", "0.08875"))
DELIVERY_FEE = float(os.environ.get("DELIVERY_FEE", "4.99"))
SERVICE_FEE = float(os.environ.get("SERVICE_FEE", "2.50"))

_token = {"value": None, "expires": 0}


def is_live():
    return bool(CLIENT_ID and CLIENT_SECRET)


class RampError(Exception):
    pass


def _request(method, path, data=None, auth=None, form=False):
    url = BASE + path
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RampError(f"Ramp API {e.code} on {path}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise RampError(f"Ramp API unreachable: {e.reason}")


def get_token():
    if not is_live():
        raise RampError("RAMP_CLIENT_ID / RAMP_CLIENT_SECRET not set")
    if _token["value"] and _token["expires"] > time.time() + 60:
        return _token["value"]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data = _request("POST", "/developer/v1/token",
                    {"grant_type": "client_credentials", "scope": SCOPES},
                    auth=f"Basic {basic}", form=True)
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 3600))
    return _token["value"]


def _bearer():
    return f"Bearer {get_token()}"


def get_card():
    """The company card that pays for the group order."""
    if not is_live():
        return {"id": "mock-card", "display_name": "Team Lunch Card (mock)",
                "last_four": "4629", "mock": True}
    cards = _request("GET", "/developer/v1/cards", auth=_bearer()).get("data") or []
    if CARD_ID:
        for c in cards:
            if c.get("id") == CARD_ID:
                return c
        raise RampError(f"RAMP_CARD_ID {CARD_ID} not found")
    for c in cards:
        if c.get("state") in (None, "ACTIVE"):
            return c
    raise RampError("no active Ramp cards on this account")


def charge(amount, memo):
    """Record the card charge for this order.

    Real Ramp transactions post from the card network after DoorDash
    settles, so this returns a pending record tied to the card; call
    match_transaction() later to attach the real transaction id.
    """
    card = get_card()
    return {
        "id": store.new_id("chg"),
        "card_id": card.get("id"),
        "card": f"{card.get('display_name', 'Ramp card')} ····{card.get('last_four', '????')}",
        "amount": round(amount, 2),
        "memo": memo,
        "env": "mock" if card.get("mock") else RAMP_ENV,
        "status": "pending_settlement",
        "ramp_transaction_id": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def match_transaction(charge_record):
    """Best-effort: find the settled Ramp transaction matching this charge."""
    if not is_live() or charge_record.get("ramp_transaction_id"):
        return charge_record
    txns = _request("GET", "/developer/v1/transactions?limit=50",
                    auth=_bearer()).get("data") or []
    for t in txns:
        if abs(float(t.get("amount", 0)) - charge_record["amount"]) < 0.01:
            charge_record["ramp_transaction_id"] = t.get("id")
            charge_record["status"] = "settled"
            break
    return charge_record


# ------------------------------------------------------------- splits & receipts

def _price_lookup(resolved):
    """[(lowercased resolved name, unit price)] with junk filtered out."""
    out = []
    for it in resolved or []:
        try:
            price = float(it.get("price"))
        except (TypeError, ValueError):
            continue
        out.append((str(it.get("name", "")).lower(), price))
    return out


def _unit_price(name, priced):
    """Match a requested item to a resolved product price (fuzzy)."""
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


def build_receipts(session, resolved, charge_record):
    """Per-person itemized receipts. Item prices come from the resolved
    DoorDash products; tax is proportional; delivery+service split equally."""
    priced = _price_lookup(resolved)
    people = [(sender, p) for sender, p in session["participants"].items() if p["items"]]
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
        note = urllib.parse.quote(f"Group order {session['id']} — {person.get('name') or sender}")
        receipts.append({
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
            "charged_to": charge_record["card"],
            "venmo_url": (f"https://account.venmo.com/pay?recipients={VENMO_HANDLE}"
                          f"&amount={total}&note={note}"),
            "paid": False,
        })
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
    paid = ("<p style='color:#2e7d32;font-weight:700'>PAID ✓</p>" if receipt["paid"]
            else f"<p><a href='{esc(receipt['venmo_url'])}'>Pay ${receipt['total']:.2f} on Venmo →</a></p>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Receipt {esc(receipt['id'])}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:420px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse}}td{{padding:.2rem 0}}
.tot td{{border-top:1px solid #999;font-weight:700;padding-top:.4rem}}
.muted{{color:#777;font-size:.85rem}}</style></head><body>
<h2>🧾 {esc(receipt['name'])}'s share</h2>
<p class="muted">Group order <b>{esc(session['id'])}</b> · {esc(session.get('store_name') or 'store TBD')}<br>
Paid upfront on {esc(receipt['charged_to'])}</p>
<table>{rows}
<tr><td colspan="2">Subtotal</td><td style="text-align:right">${receipt['subtotal']:.2f}</td></tr>
<tr><td colspan="2">Tax</td><td style="text-align:right">${receipt['tax']:.2f}</td></tr>
<tr><td colspan="2">Delivery + service (split)</td><td style="text-align:right">${receipt['fees_share']:.2f}</td></tr>
<tr class="tot"><td colspan="2">Your total</td><td style="text-align:right">${receipt['total']:.2f}</td></tr>
</table>{paid}
<p class="muted">Receipt {esc(receipt['id'])} · generated by Grocery Agent</p>
</body></html>"""


def status():
    return {"mode": "live" if is_live() else "mock", "env": RAMP_ENV,
            "base": BASE, "card_pinned": bool(CARD_ID),
            "venmo_handle": VENMO_HANDLE}
