import asyncio, sys, time, json, os
sys.path.insert(0, "/root/trading")
from love import config
from exchange.blofin import BloFinClient, round_price, _headers, LIVE_TRADING_ENABLED
from joy import send
from declog import log_decision
import ownership
import risk
# 1.5%% of equity at risk per trade (spec maximum, chosen 2026-08-04).
# The engine is the isolated hard-constraint layer: it sizes from risk
# and can only ever deny or shrink what the strategy proposes.
_risk = risk.RiskEngine(limits=risk.RiskLimits(
    risk_pct_per_trade=0.015, risk_pct_max=0.015))
import journal
journal.init()
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

async def execute_entry(client, signal, equity, margin_override=None):
    pair      = signal["pair"]
    direction = signal["direction"]
    price     = await client.get_mark_price(pair)
    if price <= 0: return None
    # Risk-sized margin from the gate; the old pct-of-equity path
    # remains only as a fallback for direct callers.
    margin    = margin_override if margin_override is not None \
                else round(equity * config.trend_margin_pct, 2)
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
                # Claim ONLY after the exchange confirms. A claim on a
                # rejected order would make peace.py adopt whatever
                # happens to be open on this pair.
                if not ownership.claim(pair, direction, "live",
                                       entry_px, oid):
                    print(f"  \u26A0\uFE0F  {pair} claim did NOT persist - "
                          f"peace.py will not manage this position")
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
    import os as _os
    if _os.path.exists("/root/trading/PAUSED"):
        print("  [TREND] PAUSED - skipping scan")
        return
    positions  = await client.get_positions()
    released   = ownership.reconcile([p.get("instId","") for p in positions])
    if released:
        print(f"  [TREND] released closed claims: {', '.join(released)}")
    # Only BOT-OWNED positions consume trend slots. Manual trades on
    # the same account are not the bot's business and must not reduce
    # its capacity. Union with state["open_trades"] so a claim that
    # failed to persist cannot cause double-entry.
    live_pairs = {p.get("instId") for p in positions
                  if ownership.is_owned(p.get("instId",""))}
    open_pairs = [t["pair"] for t in state["open_trades"]]
    occupied   = live_pairs | set(open_pairs)
    slots      = config.trend_max_slots - len(occupied)
    if slots <= 0:
        print(f"  [TREND] {len(occupied)} slots full")
        return
    equity = await client.get_equity()
    _risk.roll_day_if_needed(equity)
    print(f"[TREND] Scan | equity=${equity:.2f} | slots={slots}")
    new_count = 0
    # Entry eligibility is NOT the same question as slot accounting.
    # Slots count only bot-owned positions, but a pair is off-limits
    # for a NEW entry if ANY position exists on it, whoever opened it:
    # BloFin runs positionSide="net", so an order on a held pair merges
    # into that position rather than creating a separate one. The merged
    # position would then be claimed and closed by peace.py with the
    # manual size included. A merge cannot be undone after the fact.
    all_open_pairs = {p.get("instId") for p in positions}
    for pair in config.active_pairs:
        if slots <= 0 or new_count >= config.max_new_entries_per_scan: break
        if pair in all_open_pairs or pair in open_pairs:
            if pair in all_open_pairs and not ownership.is_owned(pair):
                print(f"  [TREND] {pair}: position held externally - skipping")
            continue
        try:
            import sqlite3 as _sq
            _jc = _sq.connect("/root/trading/journal.db")
            _busy = _jc.execute("SELECT COUNT(*) FROM trades WHERE pair=? AND mode='dry' AND status='open'", (pair,)).fetchone()[0]
            _jc.close()
            if _busy:
                print(f"  [TREND] {pair}: dry trade open - occupied")
                continue
        except Exception: pass
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
        # ---- risk gate: the strategy proposes, the engine disposes ----
        atr = await client.get_atr(pair, period=config.atr_period,
                                   bar=config.atr_bar)
        px  = float(signal.get("price") or 0) or await client.get_mark_price(pair)
        if atr <= 0 or px <= 0:
            print(f"  [TREND] {pair}: no ATR/price - skipping")
            continue
        sl_dist = atr * config.atr_sl_mult
        tp_dist = max(atr * config.atr_tp_mult, px * config.min_tp_pct)
        if signal["direction"] == "long":
            stop_px, tgt_px = px - sl_dist, px + tp_dist
        else:
            stop_px, tgt_px = px + sl_dist, px - tp_dist
        open_pos = [{"pair": p.get("instId",""),
                     "side": "long" if float(p.get("positions",0) or 0) > 0 else "short",
                     "notional": abs(float(p.get("notional",0) or 0))}
                    for p in positions if ownership.is_owned(p.get("instId",""))]
        dec = _risk.evaluate_entry(
            pair=pair, side=signal["direction"], entry_price=px,
            stop_price=stop_px, equity=equity, target_price=tgt_px,
            atr_pct=atr / px, open_positions=open_pos)
        if not dec.allowed:
            print(f"  [TREND] {pair}: risk denied - {dec.reason} {dec.detail}")
            log_decision(pair, "denied:" + dec.reason, px,
                         {"equity": round(equity, 2)})
            continue
        for w in dec.warnings:
            print(f"  [TREND] {pair}: risk warning - {w}")
        margin    = round(dec.notional / config.trend_leverage, 2)
        await send(
            f"🎯 <b>Trend Signal!</b>\n"
            f"{emoji} <b>{pair}</b> {direction}\n"
            f"💲 Price: ${signal['price']:.4f}\n"
            f"📊 EMA20: {signal['ema20']} | EMA50: {signal['ema50']}\n"
            f"📏 Dist: {signal['dist_pct']}% | Vol: {signal['vol_ratio']}x\n"
            f"💰 Margin: ${margin} @ {config.trend_leverage}x\n"
            f"⚡ Executing...")
        trade = await execute_entry(client, signal, equity,
                                    margin_override=margin)
        if trade == "DRYRUN":
            print(f"  [DRY-RUN] {pair} {direction} logged, not traded")
            try:
                _px = signal.get("price") or 0
                _notional = margin * config.trend_leverage
                journal.open_trade("dry", pair, signal["direction"], _px,
                    _notional/_px if _px else 0, _notional, config.trend_leverage, margin,
                    sig=signal, equity=equity)
            except Exception as _e: print(f"journal err: {_e}")
            state["cooldowns"][pair] = time.time() + 3600
            _risk.record_entry()  # dry entries hit the same rate caps
            slots -= 1; new_count += 1
            continue
        if trade:
            try:
                journal.open_trade("live", pair, trade["direction"], trade["entry"],
                    trade["notional"]/trade["entry"] if trade["entry"] else 0,
                    trade["notional"], config.trend_leverage, trade["margin"],
                    sig=signal, equity=equity)
            except Exception as _e: print(f"journal err: {_e}")
            state["open_trades"].append(trade)
            _risk.record_entry()  # live
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
