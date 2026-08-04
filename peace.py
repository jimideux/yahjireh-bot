import asyncio, sys, time, json, aiohttp
sys.path.insert(0, "/root/trading")
from love import config
import journal
from exchange.blofin import BloFinClient, round_price, _headers, LIVE_TRADING_ENABLED
from signals import get_atr_binance
from joy import send
import ownership

# ── Trailing profit lock ─────────────────────────────────────────────────────
_profit_highs = {}  # tracks highest profit seen per position

# inst_id -> {"id": tpsl_id, "trigger": float, "checked": ts}
# In-memory only. A restart re-verifies against the exchange on the
# next cycle, which is the correct source of truth anyway.
_armed_stops = {}
STOP_RECHECK_SECONDS = 60

def get_trail_lock(high_watermark):
    """Swing Sniper trail: breakeven at +$20, then trail ~$15-18 behind peak.
    Lets trending winners run to $50-100 instead of banking $6.50."""
    if high_watermark >= 100: return high_watermark - 18
    if high_watermark >= 70:  return high_watermark - 16
    if high_watermark >= 50:  return high_watermark - 15
    if high_watermark >= 35:  return 20.0   # peak $35 -> lock $20
    if high_watermark >= 20:  return 0.0    # peak $20 -> breakeven (risk-free)
    return None  # not yet at target — normal SL applies

async def ensure_stop(client, inst_id, side_is_long, sl_price, margin_mode):
    """Guarantee a resting stop exists on the exchange for this position.

    Returns True if a stop is known to be resting, False otherwise.
    Never raises -- a failure here must not take down the exit engine,
    because peace.py's own trailing logic is still protecting the
    position while the process lives.
    """
    now = time.time()
    cached = _armed_stops.get(inst_id)
    if cached and (now - cached["checked"]) < STOP_RECHECK_SECONDS:
        return True
    try:
        resting = await client.get_tpsl_orders(inst_id)
    except Exception as e:
        print(f"  stop check failed {inst_id}: {e}")
        return False
    if resting:
        tid = resting[0].get("tpslId") or resting[0].get("algoId", "")
        _armed_stops[inst_id] = {"id": tid, "trigger": sl_price,
                                 "checked": now}
        return True
    side = "long" if side_is_long else "short"
    try:
        res = await client.place_tpsl(inst_id, side, sl_price,
                                      margin_mode=margin_mode)
    except Exception as e:
        print(f"  stop arm error {inst_id}: {e}")
        return False
    if res == "DRYRUN":
        print(f"  \U0001F6AB DRY-RUN: {inst_id} has NO exchange stop "
              f"(would arm @ ${sl_price:.4f})")
        return False
    if res:
        _armed_stops[inst_id] = {"id": res, "trigger": sl_price,
                                 "checked": now}
        await send(f"\U0001F6E1 <b>Stop Armed</b>\n"
                   f"\U0001F4CC {inst_id}\n"
                   f"\U0001F6D1 Trigger: ${sl_price:.4f}")
        return True
    print(f"  \u26A0\uFE0F  {inst_id} UNPROTECTED - stop could not be armed")
    return False

async def place_tp_order(inst_id, side, price, size, margin_mode, tick):
    if not LIVE_TRADING_ENABLED:
        print(f"  🚫 DRY-RUN peace: would place TP {inst_id} {side} @ {price} — BLOCKED")
        return "DRYRUN"
    body = {
        "instId":       inst_id,
        "marginMode":   margin_mode,
        "positionSide": "net",
        "side":         side,
        "orderType":    "limit",
        "price":        round_price(price, tick),
        "size":         str(int(size)) if size == int(size) else str(round(size, 8)),
        "reduceOnly":   "true"
    }
    bs   = json.dumps(body)
    hdrs = _headers("POST", "/api/v1/trade/order", bs)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://openapi.blofin.com/api/v1/trade/order",
                data=bs, headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                d    = await r.json()
                rows = d.get("data") or [{}]
                row  = rows[0] if rows else {}
                oid  = row.get("orderId", "")
                # Per-order code lives in data[0], NOT at the top
                # level. Top-level "0" only means the request was
                # well-formed -- the order itself can still fail.
                row_code = str(row.get("code", "0"))
                if oid and row_code == "0":
                    return True
                print(f"  TP rejected {inst_id}: code={row_code} "
                      f"msg={row.get('msg') or d.get('msg')}")
                return False
    except Exception as e:
        print(f"  TP order error {inst_id}: {e}")
        return False

async def close_market(client, inst_id, size, reason, margin_mode):
    try:
        _pos = await client.get_positions()
        for _p in _pos:
            if _p.get("instId")==inst_id:
                _u = float(_p.get("unrealizedPnl",0) or 0)
                _mk = float(_p.get("markPrice",0) or 0)
                journal.close_trade(inst_id, _mk, reason, _u,
                    fees=abs(float(_p.get("notional",0) or 0))*0.0012)
                break
    except Exception as _e: print(f"journal close err: {_e}")
    if not LIVE_TRADING_ENABLED:
        print(f"  🚫 DRY-RUN peace: would CLOSE {inst_id} ({reason}) — BLOCKED")
        return "DRYRUN"
    try:
        await client.cancel_all_tpsl(inst_id)
        await client.cancel_all_orders(inst_id)
        _armed_stops.pop(inst_id, None)
        await asyncio.sleep(0.3)
        side = "sell" if size > 0 else "buy"
        abs_size = abs(size)
        body = {
            "instId":       inst_id,
            "marginMode":   margin_mode,
            "positionSide": "net",
            "side":         side,
            "orderType":    "market",
            "size":         str(int(abs_size)) if abs_size == int(abs_size) else str(round(abs_size,8)),
            "reduceOnly":   "true"
        }
        bs   = json.dumps(body)
        hdrs = _headers("POST", "/api/v1/trade/order", bs)
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://openapi.blofin.com/api/v1/trade/order",
                data=bs, headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                d   = await r.json()
                oid = d.get("data",[{}])[0].get("orderId","") if d.get("data") else ""
                if oid:
                    print(f"  ✅ Market closed {inst_id}: {reason}")
                    await send(
                        f"🛑 <b>Position Closed</b>\n"
                        f"📌 {inst_id}\n"
                        f"💸 Reason: {reason}")
                    return True
    except Exception as e:
        print(f"  Market close error {inst_id}: {e}")
    return False

def get_trailing_sl(entry, mark, tp_price, is_long):
    tp_dist  = (tp_price - entry) if is_long else (entry - tp_price)
    progress = ((mark - entry) / tp_dist) if is_long else ((entry - mark) / tp_dist)
    progress = max(0.0, min(1.0, progress))
    if is_long:
        if progress >= 0.90:
            c = entry * 1.010
            if c < mark: return c, "trail-90%"
        if progress >= 0.75:
            c = entry * 1.005
            if c < mark: return c, "trail-75%"
        if progress >= 0.50:
            if entry < mark: return entry, "trail-50%"
        return entry * (1 - config.atr_sl_mult * 0.005), "original"
    else:
        if progress >= 0.90:
            c = entry * (1 - 0.010)
            if c > mark: return c, "trail-90%"
        if progress >= 0.75:
            c = entry * (1 - 0.005)
            if c > mark: return c, "trail-75%"
        if progress >= 0.50:
            if entry > mark: return entry, "trail-50%"
        return entry * (1 + config.atr_sl_mult * 0.005), "original"

async def check_position(client, pos):
    inst_id     = pos.get("instId","")
    size        = float(pos.get("positions") or 0)
    entry       = float(pos.get("averagePrice") or 0)
    mark        = float(pos.get("markPrice") or entry)
    upnl        = float(pos.get("unrealizedPnl") or 0)
    margin_mode = pos.get("marginMode","cross")
    if abs(size) < 0.001 or entry <= 0: return

    is_long  = size > 0
    is_short = size < 0

    # Get ATR for TP/SL calculation
    try:
        atr = await get_atr_binance(inst_id, period=config.atr_period, interval=config.atr_bar)
        if atr <= 0: atr = entry * 0.005
    except:
        atr = entry * 0.005

    # Calculate TP and SL
    tp_dist = max(atr * config.atr_tp_mult, entry * config.min_tp_pct)
    sl_dist = atr * config.atr_sl_mult

    if is_long:
        tp_price = entry + tp_dist
        sl_price = entry - sl_dist
    else:
        tp_price = entry - tp_dist
        sl_price = entry + sl_dist

    # Trailing profit lock
    global _profit_highs
    prev_high = _profit_highs.get(inst_id, 0.0)
    if upnl > prev_high:
        _profit_highs[inst_id] = upnl
        prev_high = upnl
    try: journal.update_path(inst_id, upnl)
    except Exception: pass
    lock = get_trail_lock(prev_high)
    if lock is not None:
        if upnl > 0 and upnl <= lock:
            msg = inst_id + " trail lock hit! Closing at +$" + str(round(upnl,2))
            print("[PEACE] " + msg)
            await send("🏆 <b>" + msg + "</b>")
            await close_market(client, inst_id, size, "Trail lock $" + str(lock), margin_mode)
            _profit_highs.pop(inst_id, None)
            return
        else:
            print("[PEACE] " + inst_id + " trailing | high=$" + str(round(prev_high,2)) + " lock=$" + str(lock) + " now=$" + str(round(upnl,2)))

    # Hard loss stop
    if upnl < -config.max_loss_usd:
        await close_market(client, inst_id, size, f"Hard stop ${upnl:.2f}", margin_mode)
        return

    # Trailing SL
    dynamic_sl, sl_type = get_trailing_sl(entry, mark, tp_price, is_long)
    sl_hit = (is_long and mark <= dynamic_sl) or (is_short and mark >= dynamic_sl)
    if sl_hit:
        await close_market(client, inst_id, size, f"{sl_type} SL @ ${mark:.4f}", margin_mode)
        return

    if sl_type != "original":
        print(f"  {inst_id} [{sl_type}] sl=${dynamic_sl:.4f} mark=${mark:.4f} uPnL=${upnl:.2f}")

    # Arm a resting stop on the exchange. This is the backstop for
    # process death; the trailing logic above handles the live case.
    await ensure_stop(client, inst_id, is_long, sl_price, margin_mode)

    # Check and place TP order
    orders    = await client.get_pending_orders(inst_id)
    tp_orders = [o for o in orders if o.get("reduceOnly","false") == "true"]

    if not tp_orders:
        try:
            inst = await client.get_instrument(inst_id)
            tick = float(inst.get("tickSize","0.01") or 0.01)
        except:
            tick = 0.01
        tp_side  = "sell" if is_long else "buy"
        abs_size = abs(size)
        ok = await place_tp_order(inst_id, tp_side, tp_price, abs_size, margin_mode, tick)
        if ok == "DRYRUN":
            print(f"  \U0001F6AB DRY-RUN: no TP exists for {inst_id} "
                  f"(would be ${tp_price:.4f})")
        elif ok is True:
            print(f"  ✅ TP placed: {inst_id} @ ${tp_price:.4f} (ATR×{config.atr_tp_mult})")
            await send(
                f"🎯 <b>TP Set</b>\n"
                f"📌 {inst_id} {'LONG' if is_long else 'SHORT'}\n"
                f"📈 Entry: ${entry:.4f}\n"
                f"🎯 TP: ${tp_price:.4f}\n"
                f"🛑 SL: ${sl_price:.4f}")
        else:
            print(f"  ❌ TP failed: {inst_id} — will retry")
    else:
        existing_tp = tp_orders[0].get("price","?")
        print(f"  {inst_id} TP@${existing_tp} uPnL=${upnl:.2f}")

async def main():
    print("Peace v3 starting...")
    await send(
        f"🛡️ <b>Peace v3 Active</b>\n\n"
        f"🎯 TP: {config.atr_tp_mult}x ATR (min {config.min_tp_pct*100}%)\n"
        f"🛑 SL: {config.atr_sl_mult}x ATR\n"
        f"💸 Hard stop: ${config.max_loss_usd}/position\n"
        f"🌍 Global stop: ${config.max_total_open_loss_usd}\n"
        f"📈 Trailing stop: 50/75/90% of TP\n"
        f"✅ Cross + isolated margin supported\n"
        f"⏱ Check every {config.watch_interval}s\n"
        f"🙏 All positions protected")
    client = BloFinClient()
    while True:
        try:
            all_positions = await client.get_positions()
            positions = [p for p in all_positions
                         if ownership.is_owned(p.get("instId", ""))]
            skipped = [p.get("instId", "?") for p in all_positions
                       if not ownership.is_owned(p.get("instId", ""))]
            if skipped:
                print(f"[PEACE] ignoring unowned: {', '.join(skipped)}")
            total_upnl = sum(float(p.get("unrealizedPnl",0)) for p in positions)

            # Global stop
            if total_upnl < -config.max_total_open_loss_usd:
                await send(
                    f"🚨 <b>GLOBAL STOP</b>\n"
                    f"Total loss ${total_upnl:.2f} > ${config.max_total_open_loss_usd}\n"
                    f"Closing ALL positions")
                for p in positions:
                    sz = float(p.get("positions",0))
                    if abs(sz) >= 0.001:
                        mm = p.get("marginMode","cross")
                        await close_market(client, p.get("instId",""), sz, "GLOBAL STOP", mm)
                        await asyncio.sleep(0.5)
            else:
                if positions:
                    print(f"[{time.strftime('%H:%M:%S')}] {len(positions)} positions | uPnL: ${total_upnl:.2f}")
                    for pos in positions:
                        await check_position(client, pos)
                        await asyncio.sleep(0.3)
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] No positions")
        except Exception as e:
            print(f"Peace error: {e}")
        await asyncio.sleep(config.watch_interval)

if __name__ == "__main__":
    asyncio.run(main())
