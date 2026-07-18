#!/usr/bin/env python3
"""Group order session store.

One session per iMessage group chat. In-memory dict guarded by a lock,
write-through to data/sessions.json so a restart doesn't lose the demo.
"""

import json
import os
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "sessions.json")

_lock = threading.Lock()
_sessions = {}          # group_id -> session dict
_loaded = False


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load():
    global _sessions, _loaded
    if _loaded:
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            _sessions = json.load(f)
    except (OSError, json.JSONDecodeError):
        _sessions = {}
    _loaded = True


def _save():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_sessions, f, indent=2)
    os.replace(tmp, DATA_FILE)


def new_session(group_id):
    return {
        "id": group_id,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "open",            # open -> ordered -> settled | cancelled
        "store_name": None,
        "participants": {},          # sender -> {name, items: [{name, quantity}], paid}
        "order_result": None,        # dd bridge response after checkout
        "charge": None,              # ramp charge record
        "receipts": [],
    }


def get(group_id):
    """Fetch (or create) the open session for a group chat."""
    with _lock:
        _load()
        sess = _sessions.get(group_id)
        if sess is None or sess["status"] in ("settled", "cancelled"):
            sess = new_session(group_id)
            _sessions[group_id] = sess
            _save()
        return sess


def peek(group_id):
    with _lock:
        _load()
        return _sessions.get(group_id)


def update(group_id, mutate):
    """Apply mutate(session) atomically and persist. Returns the session."""
    with _lock:
        _load()
        sess = _sessions.get(group_id)
        if sess is None:
            sess = new_session(group_id)
            _sessions[group_id] = sess
        mutate(sess)
        sess["updated_at"] = _now()
        _save()
        return sess


def all_sessions():
    with _lock:
        _load()
        return list(_sessions.values())


def find_receipt(receipt_id):
    with _lock:
        _load()
        for sess in _sessions.values():
            for r in sess.get("receipts") or []:
                if r["id"] == receipt_id:
                    return sess, r
    return None, None


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
