import asyncio, os, sys, time, subprocess, json
sys.path.insert(0,"/root/trading")
from love import config
from exchange.blofin import BloFinClient
from joy import send
import aiohttp
from dotenv import load_dotenv
load_dotenv("/root/trading/.env")

TOKEN        = os.getenv("TELEGRAM_TOKEN","")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID","")
BOT_START    = time.time()
PAUSED       = False
FILLS_FILE   = "/root/trading/seen_fills.json"
seen_fills   = set()

def load_seen_fills():
    try:
        if os.path.exists(FILLS_FILE):
            return set(json.load(open(FILLS_FILE)))
    except: pass
    return set()

def save_seen_fills():
    try: json.dump(list(seen_fills), open(FILLS_FILE,"w"))
    except: pass

def uptime_str(start):
    secs=int(time.time()-start); d=secs//86400; h=(secs%86400)//3600; m=(secs%3600)//60
    parts=[]
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)

def run_cmd(cmd):
    try:
        r=subprocess.run(cmd,shell=True,capture_output=True,text=True,timeout=10)
        return r.stdout.strip()
    except Exception as e: return str(e)

async def get_updates(offset=0):
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                r=await s.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                    params={"offset":offset,"timeout":25},
                    timeout=aiohttp.ClientTimeout(total=35))
                d=await r.json()
                return d.get("result",[])
        except asyncio.TimeoutError: return []
        except: await asyncio.sleep(2**attempt)
    return []

async def handle_command(text, client):
    global PAUSED
    cmd = text.split()[0].lower().strip()

    if cmd=="/status":
        import json as _j
        equity=await client.get_equity()
        positions=await client.get_positions()
        upnl=sum(float(p.get("unrealizedPnl",0)) for p in positions)
        up=uptime_str(BOT_START)
        sign="+" if upnl>=0 else ""
        trend_active=run_cmd("systemctl is-active trend 2>/dev/null")
        peace_active=run_cmd("systemctl is-active peace 2>/dev/null")
        brain_active=run_cmd("systemctl is-active brain 2>/dev/null")
        try:
            bl=_j.load(open("/root/trading/baseline.json"))
            start_eq=bl.get("start_equity",equity)
            start_time=bl.get("start_time","unknown")
        except: start_eq=equity; start_time="unknown"
        realized=round(equity-start_eq,2)
        r_sign="+" if realized>=0 else ""
        wins=0; losses=0; total_fees=0.0; total_pnl=0.0
        try:
            history=await client._req("GET","/api/v1/trade/orders-history",
                params={"instType":"SWAP","limit":"100"},private=True)
            if history and isinstance(history,list):
                for o in history:
                    if o.get("state","") not in ("filled","full_fill"): continue
                    pnl=float(o.get("pnl") or 0)
                    fee=abs(float(o.get("fee") or 0))
                    total_fees+=fee
                    if pnl>0: wins+=1; total_pnl+=pnl
                    elif pnl<0: losses+=1; total_pnl+=pnl
        except: pass
        total_trades=wins+losses
        win_rate=round(wins/total_trades*100,1) if total_trades>0 else 0
        net_pnl=round(total_pnl-total_fees,2)
        emoji="🟢" if realized>=0 else "🔴"
        await send(
            f"📊 <b>Bot Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Uptime: {up}\n"
            f"💰 Equity: ${equity:.2f}\n"
            f"📈 Positions: {len(positions)}\n"
            f"💹 Open uPnL: {sign}${upnl:.2f}\n\n"
            f"📈 <b>vs Baseline</b>\n"
            f"📅 Since: {start_time}\n"
            f"{emoji} Realized: {r_sign}${realized}\n"
            f"💎 Total: ${round(realized+upnl,2)}\n\n"
            f"🏆 <b>Trade Stats</b>\n"
            f"✅ Wins: {wins}\n"
            f"❌ Losses: {losses}\n"
            f"📊 Win Rate: {win_rate}%\n"
            f"💸 Fees: ${round(total_fees,4)}\n"
            f"💰 Net PnL: ${net_pnl}\n\n"
            f"🤖 <b>Services</b>\n"
            f"  Trend: {'✅' if trend_active=='active' else '❌'}\n"
            f"  Peace: {'✅' if peace_active=='active' else '❌'}\n"
            f"  Brain: {'✅' if brain_active=='active' else '❌'}\n"
            f"  Mode: {'⏸️ PAUSED' if PAUSED else '▶️ RUNNING'}")


    elif cmd=="/balance":
        eq=await client.get_equity()
        await send(f"💰 <b>Balance</b>\n${eq:.2f} USDT")

    elif cmd=="/positions":
        positions=await client.get_positions()
        if not positions:
            await send("📊 No open positions"); return
        msg="📊 <b>Open Positions</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for p in positions:
            inst=p.get("instId",""); size=float(p.get("positions",0))
            upnl=float(p.get("unrealizedPnl",0)); entry=float(p.get("averagePrice",0))
            mark=float(p.get("markPrice",0))
            d="LONG" if size>0 else "SHORT"
            e="🟢" if upnl>=0 else "🔴"
            s="+" if upnl>=0 else ""
            mm=p.get("marginMode","cross")
            msg+=f"{e} <b>{inst}</b> {d} [{mm}]\n   Entry: ${entry:.4f} | Mark: ${mark:.4f}\n   uPnL: {s}${upnl:.2f}\n\n"
        await send(msg)

    elif cmd=="/closest":
        all_orders=[]
        for pair in config.active_pairs:
            try:
                orders=await client.get_pending_orders(pair)
                price=await client.get_mark_price(pair)
                if price<=0: continue
                for o in orders:
                    px=float(o.get("price") or 0)
                    if px<=0: continue
                    all_orders.append({"pair":pair,"side":o.get("side",""),
                        "price":px,"mark":price,"dist":abs(price-px)/price*100})
            except: pass
        all_orders.sort(key=lambda x: x["dist"])
        if not all_orders:
            await send("📋 No pending orders"); return
        msg="🎯 <b>Closest Orders</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i,o in enumerate(all_orders[:10]):
            filled=int((1-min(o["dist"]/2,1))*10)
            bar="█"*filled+"░"*(10-filled)
            emoji="🟢" if o["side"]=="buy" else "🔴"
            msg+=f"{i+1}. {emoji} <b>{o['pair']}</b> {o['side'].upper()}\n   ${o['price']:.4f} | {o['dist']:.2f}% [{bar}]\n"
        await send(msg)

    elif cmd=="/pnl":
        await send(
            f"📈 <b>Session PnL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Running: {uptime_str(BOT_START)}\n"
            f"💡 Check Telegram alerts for WIN/LOSS history")

    elif cmd=="/pairs":
        await send("📊 <b>Active Pairs</b>\n" + "\n".join(f"• {p}" for p in config.active_pairs))

    elif cmd=="/grid":
        status=run_cmd("systemctl is-active wealth 2>/dev/null || systemctl is-active trading 2>/dev/null")
        await send(f"📊 <b>Grid Bot</b>\n Status: {'✅ Active' if status=='active' else '❌ Inactive'}\n Leverage: {config.max_leverage}x\n Notional: ${config.max_notional_usd}/order\n Pairs: {len(config.active_pairs)}")

    elif cmd=="/trend":
        status=run_cmd("systemctl is-active trend 2>/dev/null")
        state={}
        try:
            if os.path.exists("/root/trading/trend_state.json"):
                state=json.load(open("/root/trading/trend_state.json"))
        except: pass
        trades=state.get("open_trades",[])
        await send(
            f"📈 <b>Trend Bot</b>\n"
            f"Status: {'✅ Active' if status=='active' else '❌ Inactive'}\n"
            f"Open trades: {len(trades)}/{config.trend_max_slots}\n"
            f"Leverage: {config.trend_leverage}x isolated\n"
            f"Margin: {int(config.trend_margin_pct*100)}% per trade")

    elif cmd=="/pause":
        PAUSED=True
        open("/root/trading/PAUSED","w").write("paused")
        await send("⏸️ <b>Trading Paused</b>\nNo new grids or trend entries.\nSend /resume to restart.")

    elif cmd=="/resume":
        PAUSED=False
        try: os.remove("/root/trading/PAUSED")
        except: pass
        await send("▶️ <b>Trading Resumed</b>")

    elif cmd=="/restart":
        await send("🔄 <b>Restarting bots...</b>")
        run_cmd("systemctl restart wealth peace brain trend 2>/dev/null || systemctl restart trading peace brain trend 2>/dev/null")
        await asyncio.sleep(5)
        await send("✅ <b>Bots Restarted</b>")

    elif cmd=="/stop":
        await send("🛑 <b>Stopping all bots...</b>")
        run_cmd("systemctl stop wealth trading peace brain trend 2>/dev/null")
        await send("⛔ <b>All Bots Stopped</b>\nSend /restart to resume.")

    elif cmd=="/help":
        await send(
            "🤖 <b>Commands</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>INFO</b>\n"
            "/status — Full bot status\n"
            "/balance — Current equity\n"
            "/positions — Open positions\n"
            "/closest — Orders near fill\n"
            "/pnl — Session PnL\n"
            "/pairs — Active pairs\n"
            "/grid — Grid bot status\n"
            "/trend — Trend bot status\n\n"
            "⚙️ <b>CONTROL</b>\n"
            "/pause — Pause all trading\n"
            "/resume — Resume trading\n"
            "/restart — Restart all bots\n"
            "/stop — Stop all bots\n\n"
            "/help — This message\n\n"
            "🔔 WIN/LOSS alerts automatic\n"
            "🙏 The Lord Will Provide")

    else:
        await send(f"❓ Unknown: {cmd}\nSend /help")

async def monitor_fills(client):
    while True:
        try:
            orders=await client._req("GET","/api/v1/trade/orders-history",
                params={"instType":"SWAP","limit":"20"},private=True)
            if orders and isinstance(orders,list):
                for o in orders:
                    oid=o.get("ordId") or o.get("orderId","")
                    state=o.get("state","").lower()
                    if state not in ("filled","full_fill"): continue
                    if not oid or oid in seen_fills: continue
                    seen_fills.add(oid)
                    save_seen_fills()
                    pnl=float(o.get("pnl") or 0)
                    fee=abs(float(o.get("fee") or 0))
                    net=pnl-fee
                    inst=o.get("instId",""); side=o.get("side","")
                    if pnl>0:
                        await send(
                            f"🏆 <b>WIN</b>\n"
                            f"📌 {inst} {side.upper()}\n"
                            f"💰 Gross: +${pnl:.4f}\n"
                            f"💸 Fee: -${fee:.4f}\n"
                            f"✅ Net: +${net:.4f}")
                    elif pnl<0:
                        await send(
                            f"❌ <b>LOSS</b>\n"
                            f"📌 {inst} {side.upper()}\n"
                            f"💰 Gross: ${pnl:.4f}\n"
                            f"💸 Fee: -${fee:.4f}\n"
                            f"🔴 Net: ${net:.4f}")
        except Exception as e:
            print(f"Fill monitor error: {e}")
        await asyncio.sleep(30)

async def main():
    global seen_fills
    print("Kindness starting...")
    client=BloFinClient()

    # Pre-seed fills — prevents duplicate alerts on restart
    seen_fills=load_seen_fills()
    try:
        recent=await client._req("GET","/api/v1/trade/orders-history",
            params={"instType":"SWAP","limit":"100"},private=True)
        if recent and isinstance(recent,list):
            for o in recent:
                oid=o.get("ordId") or o.get("orderId","")
                if oid: seen_fills.add(oid)
            save_seen_fills()
            print(f"Pre-seeded {len(seen_fills)} fills")
    except Exception as e:
        print(f"Pre-seed error: {e}")

    asyncio.create_task(monitor_fills(client))
    offset=0; errors=0
    while True:
        try:
            updates=await get_updates(offset)
            errors=0
            for u in updates:
                offset=u["update_id"]+1
                msg=u.get("message",{}); text=msg.get("text","")
                chat=str(msg.get("chat",{}).get("id",""))
                if text.startswith("/") and chat==CHAT_ID:
                    print(f"CMD: {text}")
                    try: await handle_command(text, client)
                    except Exception as e:
                        print(f"Command error {text}: {e}")
                        await send(f"⚠️ Error: {e}")
        except Exception as e:
            errors+=1
            print(f"Loop error: {e}")
            await asyncio.sleep(min(30, 2**errors))
            continue
        await asyncio.sleep(1)

if __name__=="__main__":
    asyncio.run(main())
