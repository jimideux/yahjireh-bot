import asyncio, sys, time, json, os
sys.path.insert(0, "/root/trading")
from love import config
from exchange.blofin import BloFinClient, round_price
from signals import get_atr_binance, get_ema_binance, calc_ema
from joy import send

COOLDOWN_FILE = "/root/trading/cooldowns.json"

def load_cooldowns():
    try:
        if os.path.exists(COOLDOWN_FILE):
            return json.load(open(COOLDOWN_FILE))
    except: pass
    return {}

def save_cooldowns(cd):
    try: json.dump(cd, open(COOLDOWN_FILE,"w"))
    except: pass

def is_on_cooldown(cooldowns, pair):
    return time.time() < cooldowns.get(pair, 0)

def set_cooldown(cooldowns, pair):
    cooldowns[pair] = time.time() + config.cooldown_after_close_s
    save_cooldowns(cooldowns)

def calc_ema(closes, period):
    if len(closes) < period: return 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema

async def passes_filters(client, pair):
    try:
        # Volume filter
        ticker = await client.get_ticker(pair)
        vol24h = float(ticker.get("volCurrency24h") or 0)
        if vol24h < config.min_volume_24h_usd:
            print(f"  {pair}: low volume ${vol24h/1e6:.1f}M < ${config.min_volume_24h_usd/1e6:.1f}M")
            return False

        price = await client.get_mark_price(pair)
        if price <= 0: return False

        # ATR from Binance
        atr = await get_atr_binance(pair, period=config.atr_period, interval=config.atr_bar)
        if atr <= 0:
            print(f"  {pair}: Binance ATR unavailable")
            return False
        atr_pct = atr / price
        if atr_pct < config.min_atr_pct:
            print(f"  {pair}: ATR too low {atr_pct:.4f} < {config.min_atr_pct}")
            return False

        # EMA zone from Binance
        ema50 = await get_ema_binance(pair, period=50, interval=config.atr_bar)
        if ema50 <= 0:
            print(f"  {pair}: Binance EMA unavailable")
            return False
        dist = abs(price - ema50) / ema50
        if dist > config.ema_threshold:
            print(f"  {pair}: price too far from EMA50 {dist:.3f} > {config.ema_threshold}")
            return False

        return True
    except Exception as e:
        print(f"  {pair}: filter error {e}")
        return False

async def open_grid(client, pair, cooldowns):
    try:
        price = await client.get_mark_price(pair)
        if price <= 0: return False
        atr = await get_atr_binance(pair, period=config.atr_period, interval=config.atr_bar)
        if atr <= 0:
            atr = price * 0.005  # fallback 0.5%

        spacing   = max(atr * config.atr_spacing_mult, price * config.min_spacing_pct)
        notional  = config.max_notional_usd
        inst      = await client.get_instrument(pair)
        cv        = float(inst.get("contractValue","1") or 1)
        lot       = float(inst.get("lotSize","1") or 1)
        min_s     = float(inst.get("minSize","1") or 1)
        tick      = float(inst.get("tickSize","0.01") or 0.01)
        raw       = notional / (price * cv)
        if lot > 0: raw = (raw // lot) * lot
        contracts = max(raw, min_s)
        if contracts < min_s: return False

        placed_buy=0; placed_sell=0
        levels    = config.neutral_grid_levels
        size_str  = str(int(contracts)) if contracts==int(contracts) else str(round(contracts,8))

        async def place_grid_order(side, px):
            import json as _json, aiohttp as _aio
            from exchange.blofin import _headers as _hdrs
            body = {"instId":pair,"marginMode":"cross","positionSide":"net",
                    "side":side,"orderType":"limit","price":str(px),"size":size_str}
            bs = _json.dumps(body)
            hdrs = _hdrs("POST","/api/v1/trade/order",bs)
            try:
                async with _aio.ClientSession() as s:
                    async with s.post("https://openapi.blofin.com/api/v1/trade/order",
                        data=bs,headers=hdrs,timeout=_aio.ClientTimeout(total=10)) as r:
                        d = await r.json()
                        data = d.get("data",[{}])
                        oid = data[0].get("orderId","") if data else ""
                        if oid:
                            print(f"  Order: {pair} {side} @ {px} x{size_str} (limit)")
                            return True
                        else:
                            msg = data[0].get("msg","") if data else str(d)
                            print(f"  Failed {pair} {side}: {msg}")
                            return False
            except Exception as e:
                print(f"  Order error {pair}: {e}")
                return False

        # Buy orders below price
        for i in range(1, levels+1):
            bp = round_price(price - (spacing * i), tick)
            if bp <= 0: break
            if bp < price * 0.99: break
            if await place_grid_order("buy", bp): placed_buy += 1
            await asyncio.sleep(0.2)

        # Sell orders above price
        for i in range(1, levels+1):
            sp = round_price(price + (spacing * i), tick)
            if sp > price * 1.01: break
            if await place_grid_order("sell", sp): placed_sell += 1
            await asyncio.sleep(0.2)

        if placed_buy + placed_sell > 0:
            print(f"  {pair}: grid opened {placed_buy}B + {placed_sell}S @ ${price:.4f}")
            await send(
                f"📊 <b>Grid Opened</b>\n"
                f"📌 {pair} | NEUTRAL\n"
                f"💲 Price: ${price:.4f} | ATR: ${atr:.4f}\n"
                f"📏 Spacing: ${spacing:.4f}\n"
                f"📋 {placed_buy} buys + {placed_sell} sells\n"
                f"💵 ${notional}/order @ {config.max_leverage}x")
            return True
    except Exception as e:
        print(f"  {pair}: grid error {e}")
    return False

async def scan(client, cooldowns):
    print(f"\n[GRID] Scan @ {time.strftime('%H:%M:%S')}")
    equity = await client.get_equity()
    print(f"  Equity: ${equity:.2f}")

    if equity < config.initial_capital * (1 - config.max_dd_pct):
        print("  ⚠️ Drawdown limit hit — pausing grid")
        return

    positions  = await client.get_positions()
    pos_map    = {p.get("instId"): float(p.get("positions",0)) for p in positions if float(p.get("positions",0)) != 0}
    active     = len(pos_map)
    new_count  = 0

    # Cancel opposite entries if position exists
    for inst_id, size in pos_map.items():
        if size > 0:
            orders = await client.get_pending_orders(inst_id)
            for o in orders:
                if o.get("side","").lower()=="sell" and o.get("reduceOnly","false")=="false":
                    oid = o.get("ordId") or o.get("orderId","")
                    if oid: await client.cancel_order(inst_id, oid)
        elif size < 0:
            orders = await client.get_pending_orders(inst_id)
            for o in orders:
                if o.get("side","").lower()=="buy" and o.get("reduceOnly","false")=="false":
                    oid = o.get("ordId") or o.get("orderId","")
                    if oid: await client.cancel_order(inst_id, oid)

    # Check brain pauses
    brain_state = {}
    try:
        if os.path.exists("/root/trading/brain_state.json"):
            brain_state = json.load(open("/root/trading/brain_state.json"))
    except: pass

    for pair in config.active_pairs:
        if active >= config.max_open_pairs: break
        if new_count >= config.max_new_entries_per_scan: break
        if pair in pos_map: continue
        if is_on_cooldown(cooldowns, pair):
            remaining = int((cooldowns[pair] - time.time()) / 60)
            print(f"  {pair}: cooldown {remaining}min")
            continue

        paused = brain_state.get("paused", {})
        if pair in paused and time.time() < paused[pair].get("until", 0):
            print(f"  {pair}: paused by brain")
            continue

        orders = await client.get_pending_orders(pair)
        entry_orders = [o for o in orders if o.get("reduceOnly","false")=="false"]
        if len(entry_orders) >= config.neutral_grid_levels:
            print(f"  {pair}: already has {len(entry_orders)} orders")
            continue

        if not await passes_filters(client, pair):
            continue

        print(f"  {pair}: opening grid")
        if await open_grid(client, pair, cooldowns):
            active += 1
            new_count += 1
        await asyncio.sleep(1)

async def main():
    print("Grid Bot starting...")
    await send(
        f"📊 <b>Grid Bot Active</b>\n\n"
        f"📌 Pairs: {', '.join(config.active_pairs)}\n"
        f"⚡ Leverage: {config.max_leverage}x cross\n"
        f"💵 Notional: ${config.max_notional_usd}/order\n"
        f"📏 Spacing: {config.atr_spacing_mult}x ATR\n"
        f"🔢 Levels: {config.neutral_grid_levels} neutral\n"
        f"🎯 TP: {config.atr_tp_mult}x ATR | 🛑 SL: {config.atr_sl_mult}x ATR\n"
        f"⏱ Scan: {config.scan_interval}s\n"
        f"🙏 The Lord Will Provide")
    client    = BloFinClient()
    cooldowns = load_cooldowns()
    while True:
        try:
            await scan(client, cooldowns)
        except Exception as e:
            print(f"Grid error: {e}")
        await asyncio.sleep(config.scan_interval)

if __name__ == "__main__":
    asyncio.run(main())
