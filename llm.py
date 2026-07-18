#!/usr/bin/env python3
"""Parse casual iMessage food-order texts into structured intents.

Resolution chain, best first:
  1. Anthropic SDK (needs ANTHROPIC_API_KEY or an `ant auth login` profile)
  2. `claude -p` subprocess (Claude Code CLI, already signed in on dev boxes)
  3. Regex fallback (quantity patterns only, no real NLU)

parse_message(text) -> {"intent", "items": [{"name", "quantity"}],
                        "store_name", "reply_hint", "parser"}
"""

import json
import os
import re
import shutil
import subprocess

MODEL = os.environ.get("PARSE_MODEL", "claude-opus-4-8")
CLI_TIMEOUT = int(os.environ.get("PARSE_CLI_TIMEOUT", "60"))

INTENTS = ["order", "remove", "status", "checkout", "cancel",
           "pay", "set_store", "help", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "number"},
                },
                "required": ["name", "quantity"],
                "additionalProperties": False,
            },
        },
        "store_name": {"type": ["string", "null"]},
        "reply_hint": {"type": ["string", "null"]},
    },
    "required": ["intent", "items", "store_name", "reply_hint"],
    "additionalProperties": False,
}

SYSTEM = """You parse short, casual group-chat texts sent to a food-ordering bot.
People text things like "2 burritos and a diet coke", "im good, just fries",
"actually drop my coke", "what do we have so far?", "send it", "order from chipotle",
"i venmoed you". Classify the intent and extract food items.

Intents:
- order: adding/requesting food items (extract every item with a quantity, default 1)
- remove: removing items they previously asked for (extract the items to remove)
- status: asking what the group order currently contains
- checkout: telling the bot to place the order now ("send it", "order it", "we're ready")
- cancel: cancel the whole group order
- pay: saying they paid / venmoed / settled up
- set_store: naming a store or restaurant to order from (set store_name; if items are
  also mentioned, prefer intent=order and still set store_name)
- help: asking how the bot works
- other: anything else (greetings, chatter)

Rules:
- quantities: copy leading numbers EXACTLY ("2 burritos" -> quantity 2, "fries x2" ->
  quantity 2); "a"/"an" = 1, "a couple" = 2, "a few" = 3. Fractions allowed (0.5 lb).
  Example: "2 chicken burritos and a diet coke" ->
  [{"name": "chicken burrito", "quantity": 2}, {"name": "diet coke", "quantity": 1}]
- Keep item names short and generic ("diet coke", "chicken burrito"), no adjectives
  like "yummy". Never invent items that were not mentioned.
- store_name: only when a store/restaurant is explicitly named, else null.
- reply_hint: null, or one short cheerful sentence ONLY if something needs
  clarifying (e.g. ambiguous quantity). Do not chat.
Return only the JSON object."""


def _extract_json(text):
    """Pull the first {...} object out of possibly-chatty output."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in output")
    return json.loads(text[start:end + 1])


def _clean(result, parser):
    items = []
    for it in result.get("items") or []:
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        try:
            qty = float(it.get("quantity", 1))
        except (TypeError, ValueError):
            qty = 1
        if qty <= 0:
            qty = 1
        items.append({"name": name, "quantity": int(qty) if qty == int(qty) else qty})
    intent = result.get("intent")
    if intent not in INTENTS:
        intent = "order" if items else "other"
    return {
        "intent": intent,
        "items": items,
        "store_name": result.get("store_name") or None,
        "reply_hint": result.get("reply_hint") or None,
        "parser": parser,
    }


# ---------------------------------------------------------------- anthropic SDK

_client = None
_sdk_broken = False


def _parse_sdk(text):
    global _client, _sdk_broken
    if _sdk_broken:
        return None
    try:
        import anthropic
        if _client is None:
            _client = anthropic.Anthropic()
        response = _client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if response.stop_reason == "refusal":
            return None
        out = next(b.text for b in response.content if b.type == "text")
        return _clean(json.loads(out), "anthropic-sdk")
    except Exception as e:  # no key, old SDK, network — fall through quietly
        _sdk_broken = True   # don't pay a failed round-trip on every message
        print(f"[llm] SDK parser unavailable ({type(e).__name__}: {e}); falling back")
        return None


# ---------------------------------------------------------------- claude CLI

_cli_path = None
_cli_broken = False


def _parse_cli(text):
    global _cli_path, _cli_broken
    if _cli_broken:
        return None
    if _cli_path is None:
        _cli_path = shutil.which("claude") or shutil.which("claude.exe") or ""
    if not _cli_path:
        _cli_broken = True
        return None
    prompt = f"{SYSTEM}\n\nText to parse:\n{text}\n\nJSON:"
    try:
        proc = subprocess.run([_cli_path, "-p", prompt],
                              capture_output=True, text=True, timeout=CLI_TIMEOUT)
        if proc.returncode != 0 or not proc.stdout.strip():
            _cli_broken = True
            return None
        return _clean(_extract_json(proc.stdout), "claude-cli")
    except Exception as e:
        _cli_broken = True
        print(f"[llm] CLI parser unavailable ({type(e).__name__}: {e}); falling back")
        return None


# ---------------------------------------------------------------- regex fallback

CHECKOUT_RE = re.compile(r"\b(check\s*out|send it|order it|place (the )?order|we'?re ready|submit)\b", re.I)
STATUS_RE = re.compile(r"\b(status|what do we have|so far|show (the )?order|summary)\b", re.I)
CANCEL_RE = re.compile(r"\b(cancel|nevermind the order|scrap it)\b", re.I)
PAY_RE = re.compile(r"\b(venmo(ed|'d)?( you)?|paid|settled|zelle[dn]?)\b", re.I)
HELP_RE = re.compile(r"\b(help|how does this work|commands)\b", re.I)
REMOVE_RE = re.compile(r"\b(remove|drop|take off|scratch|no more)\b", re.I)
STORE_RE = re.compile(r"\b(?:order )?from\s+([A-Za-z][A-Za-z '&-]{2,30})", re.I)


def _parse_regex(text):
    from server import parse_order_text  # reuse the bridge's line parser
    intent = "order"
    if CHECKOUT_RE.search(text):
        intent = "checkout"
    elif STATUS_RE.search(text):
        intent = "status"
    elif CANCEL_RE.search(text):
        intent = "cancel"
    elif PAY_RE.search(text):
        intent = "pay"
    elif HELP_RE.search(text):
        intent = "help"
    elif REMOVE_RE.search(text):
        intent = "remove"

    store = None
    m = STORE_RE.search(text)
    if m:
        store = m.group(1).strip()

    items = []
    if intent in ("order", "remove"):
        # "2 burritos and a coke" -> lines the bridge parser understands
        chopped = re.sub(REMOVE_RE, "", text)
        chopped = re.sub(r"\b(and|plus|also|pls|please|for me|i want|i'll take|get me|can i get)\b",
                         ",", chopped, flags=re.I)
        chopped = re.sub(r"\ban?\b", "1", chopped, flags=re.I)
        lines = "\n".join(p.strip() for p in chopped.split(",") if p.strip())
        items = parse_order_text(lines)
        if not items and intent == "order":
            intent = "other"
    return _clean({"intent": intent, "items": items, "store_name": store}, "regex")


def parse_message(text):
    text = (text or "").strip()
    if not text:
        return _clean({"intent": "other", "items": []}, "regex")
    return _parse_sdk(text) or _parse_cli(text) or _parse_regex(text)


def parser_status():
    return {
        "sdk": "unavailable" if _sdk_broken else "ready",
        "cli": "unavailable" if _cli_broken else (_cli_path or "unprobed"),
        "model": MODEL,
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(parse_message(" ".join(sys.argv[1:]) or "2 burritos and a coke"), indent=2))
