#!/usr/bin/env python3
"""
ownership.py -- position ownership registry for YahJireh.

PROBLEM
-------
BloFin's /positions endpoint returns every open position on the account with no
indication of what opened it. peace.py consequently adopts manually-opened
positions and will, once live, place take-profits on them and close them.
trend.py separately counts them against its slot budget.

There is no exchange-side fix. BloFin positions carry no clientOrderId or any
other caller-supplied field, so ownership cannot be recovered from the API. It
has to be recorded locally at the moment the bot opens a position.

DESIGN
------
A claim registry at /root/trading/bot_positions.json. trend.py claims a pair
when it opens a position; peace.py refuses to touch any pair it has not
claimed; reconcile() drops claims for pairs that are no longer open.

FAIL-CLOSED
-----------
Every failure mode resolves to "not owned":
  - registry file missing      -> nothing is owned -> peace.py manages nothing
  - registry file corrupt      -> nothing is owned
  - claim written but bot died -> reconcile() releases it on next pass

The consequence of a false negative is that a bot position goes unmanaged and
you see it in the skip log. The consequence of a false positive is the bot
closing your manual trade. Those are not symmetric, so the default is to
disown.

CONCURRENCY
-----------
trend.py and peace.py are separate processes. State is re-read from disk on
every call rather than cached, and writes go through os.replace() so a reader
never observes a partial file. A read-modify-write race can at worst drop one
claim, which fails closed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

REGISTRY_PATH = "/root/trading/bot_positions.json"
STALE_CLAIM_SECONDS = 72 * 3600  # unreconciled claims expire after 3 days

_VERSION = 1


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _empty() -> Dict[str, Any]:
    return {"version": _VERSION, "positions": {}}


def _load(path: str = REGISTRY_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r") as fh:
            d = json.load(fh)
        if not isinstance(d, dict) or "positions" not in d:
            return _empty()
        if not isinstance(d["positions"], dict):
            return _empty()
        return d
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        # Fail closed: an unreadable registry means nothing is owned.
        return _empty()


def _save(d: Dict[str, Any], path: str = REGISTRY_PATH) -> bool:
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bot_positions.", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[ownership] WARN could not persist registry: {exc}")
        return False


# ---------------------------------------------------------------------------
# public api
# ---------------------------------------------------------------------------

def claim(pair: str, direction: str = "", mode: str = "live",
          entry: Optional[float] = None, order_id: str = "",
          path: str = REGISTRY_PATH) -> bool:
    """
    Record that the bot opened `pair`. Call this AFTER the exchange confirms
    the order, never before -- a claim on an order that was rejected would make
    peace.py adopt whatever happens to be open on that pair.
    """
    pair = (pair or "").upper()
    if not pair:
        return False
    d = _load(path)
    d["positions"][pair] = {
        "claimed_at": time.time(),
        "direction": direction,
        "mode": mode,
        "entry": entry,
        "order_id": order_id,
    }
    return _save(d, path)


def release(pair: str, reason: str = "", path: str = REGISTRY_PATH) -> bool:
    pair = (pair or "").upper()
    d = _load(path)
    if pair in d["positions"]:
        del d["positions"][pair]
        return _save(d, path)
    return False


def is_owned(pair: str, path: str = REGISTRY_PATH) -> bool:
    """True only if the bot holds a live claim on this pair."""
    pair = (pair or "").upper()
    rec = _load(path)["positions"].get(pair)
    if not rec:
        return False
    if rec.get("mode") != "live":
        # Dry trades create no exchange position; a dry claim must never
        # authorise peace.py to act on a real one.
        return False
    if time.time() - float(rec.get("claimed_at", 0)) > STALE_CLAIM_SECONDS:
        return False
    return True


def owned_pairs(path: str = REGISTRY_PATH) -> List[str]:
    return [p for p in _load(path)["positions"] if is_owned(p, path)]


def get(pair: str, path: str = REGISTRY_PATH) -> Optional[Dict[str, Any]]:
    return _load(path)["positions"].get((pair or "").upper())


def reconcile(open_pairs, path: str = REGISTRY_PATH) -> List[str]:
    """
    Drop claims for pairs no longer open on the exchange. Call once per loop in
    trend.py with the instIds currently returned by get_positions().

    Returns the list of released pairs.
    """
    live = {(p or "").upper() for p in open_pairs}
    d = _load(path)
    released = []
    now = time.time()
    for pair, rec in list(d["positions"].items()):
        stale = now - float(rec.get("claimed_at", 0)) > STALE_CLAIM_SECONDS
        if pair not in live or stale:
            # Grace period: a claim written seconds ago may precede the
            # position appearing in the API. Don't release inside 60s.
            if not stale and now - float(rec.get("claimed_at", 0)) < 60:
                continue
            del d["positions"][pair]
            released.append(pair)
    if released:
        _save(d, path)
    return released


def describe(path: str = REGISTRY_PATH) -> str:
    d = _load(path)["positions"]
    if not d:
        return "no bot-owned positions"
    out = []
    for pair, rec in d.items():
        age = (time.time() - float(rec.get("claimed_at", 0))) / 60.0
        out.append(f"{pair} {rec.get('direction','?')} "
                   f"[{rec.get('mode','?')}] {age:.0f}m ago")
    return " | ".join(out)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="owntest_")
    p = os.path.join(tmpdir, "bot_positions.json")
    failures = 0

    def check(name, cond, extra=""):
        nonlocal failures
        if cond:
            print(f"  PASS  {name}")
        else:
            failures += 1
            print(f"  FAIL  {name} {extra}")

    print("\n== ownership.py self-test ==\n")

    # The scenario that matters: a manual position with no registry at all.
    check("missing registry owns nothing", not is_owned("XRP-USDT", p))
    check("owned_pairs empty on missing registry", owned_pairs(p) == [])

    claim("SUI-USDT", "long", "live", 1.23, "oid-1", path=p)
    check("claimed pair is owned", is_owned("SUI-USDT", p))
    check("manual pair still unowned", not is_owned("XRP-USDT", p))
    check("case insensitive", is_owned("sui-usdt", p))

    # A dry claim must never authorise action on a real position.
    claim("LINK-USDT", "long", "dry", 12.0, "", path=p)
    check("dry claim is not ownership", not is_owned("LINK-USDT", p))

    # Corrupt file -> fail closed.
    with open(p, "w") as fh:
        fh.write("{not valid json")
    check("corrupt registry owns nothing", not is_owned("SUI-USDT", p))
    check("corrupt registry does not raise", _load(p) == _empty())

    # Rebuild and test reconcile.
    claim("SUI-USDT", "long", "live", 1.23, "oid-1", path=p)
    claim("SOL-USDT", "short", "live", 140.0, "oid-2", path=p)
    # Backdate both past the 60s grace window.
    d = _load(p)
    for rec in d["positions"].values():
        rec["claimed_at"] = time.time() - 300
    _save(d, p)

    released = reconcile(["SUI-USDT", "XRP-USDT"], path=p)
    check("reconcile releases closed pair", released == ["SOL-USDT"], f"got {released}")
    check("reconcile keeps open pair", is_owned("SUI-USDT", p))
    check("reconcile does not adopt manual pair", not is_owned("XRP-USDT", p))

    # Fresh claim inside grace window survives a reconcile that lacks it.
    claim("ETH-USDT", "long", "live", 3000.0, "oid-3", path=p)
    released = reconcile(["SUI-USDT"], path=p)
    check("grace window protects fresh claim",
          "ETH-USDT" not in released and is_owned("ETH-USDT", p), f"got {released}")

    # Stale claim expires even while nominally open.
    d = _load(p)
    d["positions"]["SUI-USDT"]["claimed_at"] = time.time() - (STALE_CLAIM_SECONDS + 60)
    _save(d, p)
    check("stale claim is not owned", not is_owned("SUI-USDT", p))
    released = reconcile(["SUI-USDT"], path=p)
    check("reconcile drops stale claim", "SUI-USDT" in released, f"got {released}")

    release("ETH-USDT", path=p)
    check("release works", not is_owned("ETH-USDT", p))
    check("release of unknown pair is safe", release("NOPE-USDT", path=p) is False)

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n== {'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'} ==\n")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
