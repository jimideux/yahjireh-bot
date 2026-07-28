import asyncio, sys, time, json, os
sys.path.insert(0, "/root/trading")
from love import config
from exchange.blofin import BloFinClient, round_price, _headers, LIVE_TRADING_ENABLED
from joy import send
from declog import log_decision
import aiohttp

STATE_FILE = "/root/trading/trend_state.json"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except: pass
    return {"open_trades": [], "cooldowns": {}}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    json.dump(state, open(tmp,"w"), indent=2)
    os.replace(tmp, STATE_FILE)

async def get_signal(client, pair):
    try:
        from signals import get_signal as _get_signal
        return await _get_signal(pair,
            ema_short=config.ema_short,
            ema_long=config.ema_long,
            vol_min=config.trend_vol_min,
            price_zone=config.ema_threshold,
            interval="1h")
    except Exception as e:
        print(f"Signal error {pair}: {e}")
    return None

async def set_leverage(client, pair, leverage):
    try:
        await client._req("POST", "/api/v1/account/set-leverage",
            body={"instId": pair, "leverage": str(leverage), "marginMode": "isolated"},
            private=True)
        return True
    except Exception as e:
        print(f"Leverage error {pair}: {e}")
        return False

async def execute_entry(client, signal, equity):
    pair      = signal["pair"]
    direction = signal["direction"]
    price     = await client.get_mark_price(pair)
    if price <= 0: return None
    margin    = round(equity * config.trend_margin_pct, 2)
    notional  = margin * config.trend_leverage
    inst      = await client.get_instrument(pair)
    cv        = float(inst.get("contractValue","1") or 1)
    lot       = float(inst.get("lotSize","1") or 1)
    min_s     = float(inst.get("minSize","1") or 1)
    tick      = float(inst.get("tickSize","0.01") or 0.01)
    raw       = notional / (price * cv)
    if lot > 0: raw = (raw // lot) * lot
    contracts = max(raw, min_s)
    size_str  = str(int(contracts)) if contracts == int(contracts) else str(round(contracts,8))
    if direction == "long":
        entry_px = price * (1 - config.trend_entry_offset)
        side     = "buy"
    else:
        entry_px = price * (1 + config.trend_entry_offset)
        side     = "sell"
    await set_leverage(client, pair, config.trend_leverage)
    await asyncio.sleep(0.5)
    body = {
        "instId":       pair,
        "marginMode":   "isolated",
        "positionSide": "net",
        "side":         side,
        "orderType":    "limit",
        "price":        round_price(entry_px, tick),
        "size":         size_str,
    }
    bs   = json.dumps(body)
    if not LIVE_TRADING_ENABLED:
        print(f"  🚫 DRY-RUN trend: would place ENTRY order — BLOCKED")
        return "DRYRUN"
    hdrs = _headers("POST", "/api/v1/trade/order", bs)
    async with aiohttp.ClientSession() as s:
        async with s.post("https://openapi.blofin.com/api/v1/trade/order",
            data=bs, headers=hdrs, timeout=aiohttp.ClientTimeout(total=10)) as r:
            d   = await r.json()
            oid = d.get("data",[{}])[0].get("orderId","") if d.get("data") else ""
            if oid:
                print(f"  ✅ Trend entry: {pair} {direction} @ {entry_px:.4f} size={size_str}")
                return {"pair": pair, "direction": direction,
                        "entry": round(entry_px,6), "margin": margin,
                        "notional": round(notional,2), "order_id": oid,
                        "opened_at": time.time()}
            else:
                msg = d.get("data",[{}])[0].get("msg","") if d.get("data") else str(d)
                print(f"  ❌ Trend entry failed {pair}: {msg}")
    return None

async def monitor(client, state):
    if not state["open_trades"]: return
    positions  = await client.get_positions()
    pos_pairs  = {p.get("instId"): p for p in positions}
    still_open = []
    for trade in state["open_trades"]:
        pair = trade["pair"]
        if pair in pos_pairs:
            upnl = float(pos_pairs[pair].get("unrealizedPnl",0))
            mark = float(pos_pairs[pair].get("markPrice",0))
            sign = "+" if upnl>=0 else ""
            print(f"  [TREND] {pair} {trade['direction'].upper()} uPnL={sign}${upnl:.2f} mark=${mark:.4f}")
            still_open.append(trade)
        else:
            orders = await client.get_pending_orders(pair)
            pending = [o for o in orders if o.get("orderId")==trade.get("order_id") or o.get("reduceOnly","false")=="false"]
            if pending:
                print(f"  [TREND] {pair} entry pending...")
                still_open.append(trade)
            else:
                state["cooldowns"][pair] = time.time() + config.cooldown_after_close_s
                print(f"  [TREND] {pair} closed — cooldown set")
                await send(f"📊 <b>Trend Trade Closed</b>\n📌 {pair} {trade['direction'].upper()}\n💰 Check BloFin for P&L")
    state["open_trades"] = still_open

async def scan(client, state):
    positions  = await client.get_positions()
    # Only count isolated margin positions as trend slots
    isolated   = [p for p in positions if p.get("marginMode","cross")=="isolated"]
    live_pairs = {p.get("instId") for p in isolated}
    open_pairs = [t["pair"] for t in state["open_trades"]]
    slots      = config.trend_max_slots - len(live_pairs)
    if slots <= 0:
        print(f"  [TREND] {len(live_pairs)} slots full")
        return
    equity = await client.get_equity()
    print(f"[TREND] Scan | equity=${equity:.2f} | slots={config.trend_max_slots-len(live_pairs)}")
    new_count = 0
    for pair in config.active_pairs:
        if slots <= 0 or new_count >= config.max_new_entries_per_scan: break
        if pair in live_pairs or pair in open_pairs: continue
        if time.time() < state["cooldowns"].get(pair, 0):
            rem = int((state["cooldowns"][pair] - time.time())/60)
            print(f"  [TREND] {pair}: cooldown {rem}min")
            continue
        signal = await get_signal(client, pair)
        if not signal:
            print(f"  [TREND] {pair}: no signal")
            continue
        direction = signal["direction"].upper()
        emoji     = "📈" if signal["direction"]=="long" else "📉"
        log_decision(pair, signal["direction"], signal.get("price"),
                     {"ema20": signal.get("ema20"), "ema50": signal.get("ema50"),
                      "dist_pct": signal.get("dist_pct"), "vol_ratio": signal.get("vol_ratio"),
                      "equity": round(equity,2)})
        margin    = round(equity * config.trend_margin_pct, 2)
        await send(
            f"🎯 <b>Trend Signal!</b>\n"
            f"{emoji} <b>{pair}</b> {direction}\n"
            f"💲 Price: ${signal['price']:.4f}\n"
            f"📊 EMA20: {signal['ema20']} | EMA50: {signal['ema50']}\n"
            f"📏 Dist: {signal['dist_pct']}% | Vol: {signal['vol_ratio']}x\n"
            f"💰 Margin: ${margin} @ {config.trend_leverage}x\n"
            f"⚡ Executing...")
        trade = await execute_entry(client, signal, equity)
        if trade:
            state["open_trades"].append(trade)
            slots -= 1
            new_count += 1
            await send(
                f"✅ <b>Trend Entry Placed!</b>\n"
                f"📌 {pair} {direction}\n"
                f"📈 Entry: ${trade['entry']:.4f}\n"
                f"💰 Margin: ${trade['margin']:.2f} @ {config.trend_leverage}x\n"
                f"🛡️ Peace.py sets TP/SL automatically")
        else:
            await send(f"❌ Entry failed: {pair}")

async def main():
    print("Trend Bot starting...")
    state  = load_state()
    client = BloFinClient()
    equity = await client.get_equity()
    await send(
        f"📈 <b>Trend Bot Active</b>\n\n"
        f"📊 Pairs: {', '.join(config.active_pairs)}\n"
        f"⚡ Leverage: {config.trend_leverage}x isolated\n"
        f"💰 Margin: {int(config.trend_margin_pct*100)}% per trade\n"
        f"🔢 Max slots: {config.trend_max_slots}\n"
        f"📊 Signal: 1H EMA{config.ema_short}/EMA{config.ema_long}\n"
        f"🛡️ Peace.py handles TP/SL\n"
        f"⏱ Scan: {config.scan_interval}s\n\n"
        f"🙏 The Lord Will Provide")
    while True:
        try:
            await monitor(client, state)
            await scan(client, state)
            save_state(state)
        except Exception as e:
            print(f"Trend error: {e}")
            await asyncio.sleep(10)
        await asyncio.sleep(config.scan_interval)

if __name__ == "__main__":
    asyncio.run(main())
