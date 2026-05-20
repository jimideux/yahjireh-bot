import asyncio, json, os, sys, time, re
from datetime import datetime, timezone
import aiohttp
sys.path.insert(0,"/root/trading")
from love import config
from exchange.blofin import BloFinClient
from joy import send

CONFIG_FILE = "/root/trading/brain_config.json"
STATE_FILE  = "/root/trading/brain_state.json"

DEFAULT_CONFIG = {
    "stale_order_hours":      24,
    "stale_atr_distance":     3.0,
    "volume_drop_threshold":  0.25,
    "volume_spike_threshold": 3.0,
    "min_volume_24h_usd":     500000,
    "aged_position_hours":    24,
    "morning_brief_hour_utc": 8,
    "brain_interval_s":       300,
    "quality_interval_s":     3600,
    "health_interval_s":      21600
}

def load_cfg():
    if not os.path.exists(CONFIG_FILE):
        json.dump(DEFAULT_CONFIG, open(CONFIG_FILE,"w"), indent=2)
    try: return {**DEFAULT_CONFIG, **json.load(open(CONFIG_FILE))}
    except: return DEFAULT_CONFIG

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except: pass
    return {"paused":{},"counters":{"dupes_canceled":0,"stale_canceled":0},
            "last_quality_ts":0,"last_health_ts":0,
            "last_morning_brief_date":"","quality_fails":{}}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    json.dump(state, open(tmp,"w"), indent=2)
    os.replace(tmp, STATE_FILE)

async def get_fear_greed():
    """Fetch Fear & Greed Index from alternative.me"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
                data = d["data"][0]
                value = int(data["value"])
                label = data["value_classification"]
                return value, label
    except Exception as e:
        print(f"Fear & Greed error: {e}")
    return None, None

def get_fear_greed_signal(value):
    """Returns trading bias based on Fear & Greed"""
    if value is None: return "neutral"
    if value < 30:    return "longs_only"   # Extreme Fear = bounce likely
    if value > 75:    return "shorts_only"  # Extreme Greed = correction likely
    return "neutral"

async def check_fear_greed(client, state, cfg):
    """Check Fear & Greed every hour and update state"""
    last_check = state.get("last_fg_ts", 0)
    if time.time() - last_check < 3600: return
    value, label = await get_fear_greed()
    if value is None: return
    signal = get_fear_greed_signal(value)
    prev_signal = state.get("fg_signal","neutral")
    state["fg_value"]  = value
    state["fg_label"]  = label
    state["fg_signal"] = signal
    state["last_fg_ts"] = time.time()
    emoji = "😱" if value < 30 else "🤑" if value > 75 else "😐"
    print(f"[BRAIN] Fear & Greed: {value} ({label}) → {signal}")
    # Alert on signal change
    if signal != prev_signal:
        bias = {
            "longs_only":   "⚠️ Only LONGS — market oversold, bounce likely",
            "shorts_only":  "⚠️ Only SHORTS — market overbought, correction likely",
            "neutral":      "✅ Neutral — follow EMA signals normally"
        }.get(signal,"")
        await send(
            f"{emoji} <b>Fear & Greed Update</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Score: {value}/100 — {label}\n"
            f"Signal: {bias}\n"
            f"Strategy adjusted automatically")

async def check_news(client, state):
    """Scan CoinTelegraph RSS for negative keywords"""
    last_check = state.get("last_news_ts", 0)
    if time.time() - last_check < 1800: return  # every 30 mins
    state["last_news_ts"] = time.time()
    BAD_KEYWORDS  = ["crash","hack","lawsuit","collapse",
                     "exploit","stolen","scam","liquidation","hacked","breach"]
    GOOD_KEYWORDS = ["rally","surge","breakout","bullish","approval",
                     "etf","adoption","record","all-time","rise"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://cointelegraph.com/rss",
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                text = await r.text()
        titles = re.findall(r"<title><![CDATA[(.*?)]]></title>", text)
        if not titles:
            titles = re.findall(r"<title>(.*?)</title>", text)
        titles = [t for t in titles if len(t) > 20][:15]
        bad_hits  = []
        good_hits = []
        for t in titles:
            tl = t.lower()
            if any(k in tl for k in BAD_KEYWORDS):
                bad_hits.append(t)
            elif any(k in tl for k in GOOD_KEYWORDS):
                good_hits.append(t)
        if bad_hits:
            state["news_pause_until"] = time.time() + 1800
            state["news_bias"] = "bearish"
            await send(
                f"📰 <b>Negative News Detected</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(f"• {h[:80]}" for h in bad_hits[:3]) +
                f"\n\n⏸️ New entries paused 30 mins\n"
                f"🛡️ Existing positions protected")
        elif good_hits:
            state["news_bias"] = "bullish"
            state["news_pause_until"] = 0
            await send(
                f"📰 <b>Positive News</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(f"• {h[:80]}" for h in good_hits[:3]))
        else:
            state["news_bias"] = "neutral"
            state["news_pause_until"] = 0
            print(f"[BRAIN] News: neutral")
    except Exception as e:
        print(f"News check error: {e}")

async def check_daily_drawdown(client, state):
    try:
        equity = await client.get_equity()
        now_date = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
        if state.get("daily_date") != now_date:
            state["daily_date"]     = now_date
            state["daily_start_eq"] = equity
            state["daily_stopped"]  = False
            print(f"[BRAIN] New day — start equity: ${equity:.2f}")
            return
        start_eq = state.get("daily_start_eq", equity)
        drawdown = (equity - start_eq) / start_eq * 100
        stopped  = state.get("daily_stopped", False)
        if drawdown <= -4.0 and not stopped:
            state["daily_stopped"] = True
            __import__("subprocess").run(["systemctl","stop","trend"], capture_output=True)
            await send(
                "🚨 <b>Daily Drawdown Limit Hit</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Start: ${start_eq:.2f}\n"
                f"Now:   ${equity:.2f}\n"
                f"Drop:  {drawdown:.2f}%\n\n"
                "⛔ Trend bot stopped for today\n"
                "✅ Peace.py still protecting positions\n"
                "🔄 Resumes tomorrow automatically")
            print(f"[BRAIN] DAILY STOP — drawdown {drawdown:.2f}%")
        else:
            print(f"[BRAIN] Drawdown: {drawdown:.2f}% | limit: -4% | start: ${start_eq:.2f}")
    except Exception as e:
        print(f"Drawdown check error: {e}")

async def log_trade(inst_id, side, pnl, fee):
    import json as _j, time as _t
    try:
        log_file = "/root/trading/trade_log.json"
        try: logs = _j.load(open(log_file))
        except: logs = []
        logs.append({
            "time":   _t.strftime("%Y-%m-%d %H:%M:%S"),
            "pair":   inst_id,
            "side":   side,
            "pnl":    round(pnl,4),
            "fee":    round(fee,4),
            "net":    round(pnl-fee,4),
            "result": "win" if pnl>0 else "loss" if pnl<0 else "flat"
        })
        if len(logs) > 500: logs = logs[-500:]
        _j.dump(logs, open(log_file,"w"), indent=2)
        print(f"[BRAIN] Trade logged: {inst_id} {side} pnl=${pnl:.4f} net=${pnl-fee:.4f}")
    except Exception as e:
        print(f"Trade log error: {e}")

async def monitor_and_log_fills(client, state):
    try:
        history = await client._req("GET","/api/v1/trade/orders-history",
            params={"instType":"SWAP","limit":"20"},private=True)
        if not history or not isinstance(history,list): return
        logged = state.get("logged_fills",[])
        for o in history:
            oid  = o.get("ordId") or o.get("orderId","")
            st   = o.get("state","").lower()
            if st not in ("filled","full_fill"): continue
            if not oid or oid in logged: continue
            pnl = float(o.get("pnl") or 0)
            fee = abs(float(o.get("fee") or 0))
            if pnl==0 and fee==0: continue
            logged.append(oid)
            if len(logged)>200: logged=logged[-200:]
            state["logged_fills"]=logged
            await log_trade(o.get("instId",""),o.get("side",""),pnl,fee)
    except Exception as e:
        print(f"Fill monitor error: {e}")

async def cancel_stale_orders(client, state, cfg):
    now=time.time(); cancelled=0; pairs_hit=[]
    for pair in config.active_pairs:
        try:
            orders = await client.get_pending_orders(pair)
            if not orders: continue
            price = await client.get_mark_price(pair)
            atr   = await client.get_atr(pair, period=config.atr_period, bar=config.atr_bar)
            if price<=0 or atr<=0: continue
            max_dist = atr * cfg["stale_atr_distance"]
            for o in orders:
                oid      = o.get("ordId") or o.get("orderId","")
                order_px = float(o.get("price") or 0)
                create_t = int(o.get("createTime") or o.get("cTime") or 0)
                if not oid: continue
                age_h = (now - create_t/1000)/3600 if create_t>0 else 0
                dist  = abs(price - order_px)
                if age_h > cfg["stale_order_hours"] or dist > max_dist:
                    ok = await client.cancel_order(pair, oid)
                    if ok:
                        cancelled+=1
                        state["counters"]["stale_canceled"]+=1
                        pairs_hit.append(pair)
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"Stale check error {pair}: {e}")
    if cancelled>0:
        await send(f"🧹 <b>Brain: {cancelled} stale orders cleared</b>\n"+"\n".join(set(pairs_hit)))
    else:
        print(f"[{time.strftime('%H:%M:%S')}] Brain: no stale orders")

async def cancel_dupes(client, state):
    dupes=0
    for pair in config.active_pairs:
        try:
            orders=await client.get_pending_orders(pair)
            seen={}
            for o in orders:
                oid = o.get("ordId") or o.get("orderId","")
                key = f"{o.get('side')}:{o.get('price')}:{o.get('size')}"
                if key in seen:
                    ok=await client.cancel_order(pair,oid)
                    if ok:
                        dupes+=1
                        state["counters"]["dupes_canceled"]+=1
                    await asyncio.sleep(0.1)
                else: seen[key]=oid
        except Exception as e:
            print(f"Dupe check error {pair}: {e}")
    if dupes>0:
        await send(f"🔁 <b>Brain: {dupes} duplicates cancelled</b>")

async def volume_check(client, cfg):
    for pair in config.active_pairs:
        try:
            candles=await client.get_candles(pair, bar="5m", limit=25)
            if len(candles)<25: continue
            recent=float(candles[0][5] if len(candles[0])>5 else 0)
            avg=sum(float(c[5]) for c in candles[1:25] if len(c)>5)/24
            if avg<=0: continue
            ratio=recent/avg
            if ratio>cfg["volume_spike_threshold"]:
                await send(f"🚨 <b>Brain: Volume SPIKE</b>\n📌 {pair} — {ratio:.1%} of normal\n⚡ Possible news")
            elif ratio<cfg["volume_drop_threshold"]:
                print(f"  {pair}: low volume {ratio:.1%}")
        except Exception as e:
            print(f"Volume check {pair}: {e}")

async def position_health(client, cfg):
    try:
        positions=await client.get_positions()
        now=time.time()
        for pos in positions:
            inst_id=pos.get("instId","")
            size=float(pos.get("positions") or 0)
            upnl=float(pos.get("unrealizedPnl") or 0)
            if abs(size)<0.001: continue
            orders=await client.get_pending_orders(inst_id)
            tp_orders=[o for o in orders if o.get("reduceOnly","false")=="true"]
            if not tp_orders:
                await send(
                    f"🔍 <b>Brain: Orphan Position</b>\n"
                    f"📌 {inst_id} — NO TP ORDER\n"
                    f"💰 uPnL: ${upnl:.2f}\n"
                    f"⚡ Peace.py will fix next cycle")
            create_ts=int(pos.get("cTime") or pos.get("createTime") or 0)
            if create_ts>0:
                age_h=(now-create_ts/1000)/3600
                if age_h>cfg["aged_position_hours"]:
                    await send(
                        f"⏰ <b>Brain: Aged Position</b>\n"
                        f"📌 {inst_id} open {age_h:.1f}h\n"
                        f"💰 uPnL: ${upnl:.2f}\n👀 Please review")
    except Exception as e:
        print(f"Position health error: {e}")

async def health_report(client, state):
    try:
        equity=await client.get_equity()
        positions=await client.get_positions()
        upnl=sum(float(p.get("unrealizedPnl",0)) for p in positions)
        worst=min((float(p.get("unrealizedPnl",0)) for p in positions),default=0)
        worst_pair=next((p.get("instId","") for p in positions if float(p.get("unrealizedPnl",0))==worst),"")
        paused=[(k,v["reason"]) for k,v in state.get("paused",{}).items() if time.time()<v.get("until",0)]
        emoji="🟢" if upnl>=0 else "🔴"
        await send(
            f"📋 <b>6-Hour Health Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Equity: ${equity:.2f}\n"
            f"{emoji} uPnL: ${upnl:.2f}\n"
            f"📈 Positions: {len(positions)}\n"
            f"📉 Worst: {worst_pair} ${worst:.2f}\n"
            f"🧹 Stale: {state['counters']['stale_canceled']}\n"
            f"🔁 Dupes: {state['counters']['dupes_canceled']}\n"
            f"⏸️ Paused: {len(paused)}\n"
            +("\n".join(f"  • {p[0]}: {p[1]}" for p in paused) if paused else "  None")+
            f"\n\n✅ Running 24/7 🙏")
        state["last_health_ts"]=time.time()
    except Exception as e:
        print(f"Health report error: {e}")

async def morning_brief(client, state, cfg):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_morning_brief_date")==today: return
    if datetime.now(timezone.utc).hour!=cfg["morning_brief_hour_utc"]: return
    try:
        equity=await client.get_equity()
        positions=await client.get_positions()
        upnl=sum(float(p.get("unrealizedPnl",0)) for p in positions)
        emoji="🟢" if upnl>=0 else "🔴"
        fg_val = state.get("fg_value","?")
        fg_lbl = state.get("fg_label","Unknown")
        await send(
            f"☀️ <b>Morning Brief — {today}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Equity: ${equity:.2f}\n"
            f"{emoji} uPnL: ${upnl:.2f}\n"
            f"📈 Positions: {len(positions)}\n"
            f"😱 Fear & Greed: {fg_val}/100 — {fg_lbl}\n"
            f"🧹 Stale cleared: {state['counters']['stale_canceled']}\n"
            f"🔁 Dupes cleared: {state['counters']['dupes_canceled']}\n\n"
            f"🙏 The Lord Will Provide\nHave a profitable day!")
        state["last_morning_brief_date"]=today
        state["counters"]["stale_canceled"]=0
        state["counters"]["dupes_canceled"]=0
    except Exception as e:
        print(f"Morning brief error: {e}")

async def main():
    print("Brain v3 starting...")
    await send(
        "🧠 <b>Brain v3 Active</b>\n\n"
        "✅ Stale order cleanup (24h / 3x ATR)\n"
        "✅ Duplicate prevention\n"
        "✅ Volume spike alerts\n"
        "✅ Orphan position detection\n"
        "✅ Aged position alerts\n"
        "✅ 6-hour health reports\n"
        "✅ Morning brief 8am UTC\n"
        "✅ Atomic state saves\n"
        "🙏 Capital protected")
    client=BloFinClient()
    state=load_state()
    cfg=load_cfg()
    loop=0
    while True:
        try:
            cfg=load_cfg()
            await check_daily_drawdown(client, state)
            await monitor_and_log_fills(client, state)
            await cancel_stale_orders(client, state, cfg)
            await cancel_dupes(client, state)
            await volume_check(client, cfg)
            await check_fear_greed(client, state, cfg)
            await check_news(client, state)
            await morning_brief(client, state, cfg)
            if loop%3==0:
                await position_health(client, cfg)
            if time.time()-state.get("last_health_ts",0)>cfg["health_interval_s"]:
                await health_report(client, state)
            save_state(state)
            loop+=1
        except Exception as e:
            print(f"Brain error: {e}")
        await asyncio.sleep(cfg["brain_interval_s"])

if __name__=="__main__":
    asyncio.run(main())
