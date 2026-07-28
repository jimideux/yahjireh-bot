#!/usr/bin/env python3
"""YahJireh Telegram — full command console + alerts."""
import os
os.environ["ENV_FILE"] = "/root/trading/.env.live"
import asyncio, sys, time, json, subprocess
sys.path.insert(0, "/root/trading")
import aiohttp
from exchange.blofin import BloFinClient, LIVE_TRADING_ENABLED
from dotenv import load_dotenv
load_dotenv("/root/trading/.env.live")

TOKEN = os.getenv("TELEGRAM_TOKEN","")
CHAT  = str(os.getenv("TELEGRAM_CHAT_ID",""))
PAUSE_FILE = "/root/trading/PAUSED"
PY = "/root/trading/.venv/bin/python3"

async def send(s, text):
    if not TOKEN: print(f"[MSG] {text}"); return
    try:
        await s.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id":CHAT,"text":text,"parse_mode":"HTML"},
            timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e: print(f"tg send error: {e}")

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()

async def cmd_status(s, c):
    eq = await c.get_equity()
    t = sh("systemctl is-active sniper-trend"); p = sh("systemctl is-active sniper-peace")
    paused = "PAUSED" if os.path.exists(PAUSE_FILE) else "active"
    mode = "LIVE 🔴" if LIVE_TRADING_ENABLED else "DRY-RUN 🟢"
    last = sh("journalctl -u sniper-trend -n 30 --no-pager -o cat | grep Scan | tail -1")
    return (f"🤖 <b>Status</b>\nMode: {mode}\nEntries: {paused}\n"
            f"trend: {t} | peace: {p}\n💰 ${eq:,.2f}\n<code>{last[:100]}</code>")

async def cmd_server(s, c):
    up = sh("uptime -p"); load = sh("uptime | awk -F'load average:' '{print $2}'")
    mem = sh("free -m | awk 'NR==2{printf \"%s/%sMB (%.0f%%)\", $3,$2,$3*100/$2}'")
    disk = sh("df -h / | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}'")
    boot = sh("uptime -s")
    svcs = ""
    for u in ["sniper-trend","sniper-peace","tg"]:
        st = sh(f"systemctl is-active {u}")
        svcs += f"  {'🟢' if st=='active' else '🔴'} {u}: {st}\n"
    return (f"🖥 <b>Server</b>\n⏱ Up: {up}\n🔄 Booted: {boot}\n"
            f"📊 Load:{load}\n🧠 RAM: {mem}\n💾 Disk: {disk}\n\n<b>Services</b>\n{svcs}")

async def cmd_balance(s, c):
    eq = await c.get_equity()
    return f"💰 <b>Balance</b>\nEquity: ${eq:,.2f}"

async def cmd_positions(s, c):
    pos = [p for p in await c.get_positions() if abs(float(p.get('positions',0) or 0))>0]
    if not pos: return "📭 No open positions"
    out = "📊 <b>Positions</b>\n"
    for p in pos:
        u = float(p.get('unrealizedPnl',0) or 0)
        out += (f"{'🟢' if u>=0 else '🔴'} {p['instId']}\n"
                f"   {p.get('positions')} @ {p.get('averagePrice')}\n   uPnL: ${u:+.2f}\n")
    return out

async def cmd_pnl(s, c):
    d = await c._req('GET','/api/v1/account/positions-history',params={'limit':'50'},private=True)
    if not d: return "No trade history"
    pnls=[float(x.get('realizedPnl',0) or 0) for x in d]
    fees=sum(abs(float(x.get('fee',0) or 0)) for x in d)
    w=[p for p in pnls if p>0]
    return (f"📈 <b>P&L</b> (last {len(pnls)})\nNet: ${sum(pnls):+,.2f}\n"
            f"Wins: {len(w)}/{len(pnls)} ({len(w)/len(pnls)*100:.0f}%)\nFees: ${fees:,.2f}")

async def cmd_decisions(s, c):
    try:
        from declog import read_all
        recs = read_all()
        if not recs: return "📋 No dry-run decisions logged yet"
        out = f"📋 <b>Decisions</b>: {len(recs)} total\n"
        for r in recs[-8:]:
            out += f"{r['time'][5:16]} {r['pair']} {r['direction'].upper()} @{r.get('price')}\n"
        return out
    except Exception as e: return f"declog error: {e}"

async def cmd_status(s, c):
    eq = await c.get_equity()
    t = sh("systemctl is-active sniper-trend"); p = sh("systemctl is-active sniper-peace")
    paused = "PAUSED" if os.path.exists(PAUSE_FILE) else "active"
    mode = "LIVE" if LIVE_TRADING_ENABLED else "DRY-RUN"
    return (f"🤖 <b>Status</b>\nMode: {mode}\nEntries: {paused}\n"
            f"trend: {t} | peace: {p}\n💰 ${eq:,.2f}")

async def cmd_server(s, c):
    up = sh("uptime -p")
    boot = sh("uptime -s")
    load = sh("cat /proc/loadavg | cut -d' ' -f1-3")
    mem = sh("free -h | grep Mem | awk '{print $3\"/\"$2}'")
    disk = sh("df -h / | tail -1 | awk '{print $3\"/\"$2\" \"$5}'")
    svcs = ""
    for u in ["sniper-trend","sniper-peace","tg"]:
        st = sh("systemctl is-active " + u)
        svcs += ("🟢 " if st=="active" else "🔴 ") + u + ": " + st + "\n"
    return (f"🖥 <b>Server</b>\n⏱ {up}\n🔄 Booted: {boot}\n"
            f"📊 Load: {load}\n🧠 RAM: {mem}\n💾 Disk: {disk}\n\n{svcs}")

async def cmd_balance(s, c):
    eq = await c.get_equity()
    return f"💰 Equity: ${eq:,.2f}"

async def cmd_positions(s, c):
    pos = [p for p in await c.get_positions() if abs(float(p.get('positions',0) or 0))>0]
    if not pos: return "📭 No open positions"
    out = "📊 <b>Positions</b>\n"
    for p in pos:
        u = float(p.get('unrealizedPnl',0) or 0)
        out += f"{'🟢' if u>=0 else '🔴'} {p['instId']} {p.get('positions')} @ {p.get('averagePrice')} | ${u:+.2f}\n"
    return out

async def cmd_pnl(s, c):
    d = await c._req('GET','/api/v1/account/positions-history',params={'limit':'50'},private=True)
    if not d: return "No trade history"
    pnls=[float(x.get('realizedPnl',0) or 0) for x in d]
    fees=sum(abs(float(x.get('fee',0) or 0)) for x in d)
    w=[p for p in pnls if p>0]
    return (f"📈 <b>P&L</b> ({len(pnls)} trades)\nNet: ${sum(pnls):+,.2f}\n"
            f"Wins: {len(w)}/{len(pnls)}\nFees: ${fees:,.2f}")

async def cmd_decisions(s, c):
    from declog import read_all
    recs = read_all()
    if not recs: return "📋 No decisions logged yet"
    out = f"📋 {len(recs)} decisions\n"
    for r in recs[-8:]:
        out += f"{r['time'][5:16]} {r['pair']} {r['direction'].upper()}\n"
    return out

async def cmd_pause(s, c):
    open(PAUSE_FILE,'w').write("1")
    return "PAUSED - no new entries. /resume to restart."

async def cmd_resume(s, c):
    if os.path.exists(PAUSE_FILE): os.remove(PAUSE_FILE)
    return "RESUMED - entries enabled."

async def cmd_restart(s, c):
    sh("systemctl restart sniper-trend sniper-peace")
    await asyncio.sleep(3)
    return "Restarted: " + sh("systemctl is-active sniper-trend")

async def cmd_signal(s, c):
    return "Next scan in ~30s. Check /status."

async def cmd_stop(s, c):
    sh("systemctl stop sniper-trend sniper-peace")
    return "Bot stopped. Positions untouched."

async def cmd_panic(s, c):
    await send(s, "PANIC - closing all...")
    out = sh(PY + " /root/trading/PANIC.py 2>&1")
    sh("systemctl stop sniper-trend sniper-peace")
    return "PANIC DONE\n" + (out[-300:] if out else "(no PANIC.py found)")

HELP = (
"🤖 <b>YAHJIREH CONTROL</b>\n"
"━━━━━━━━━━━━━━━━\n"
"📊 <b>MONITOR</b>\n"
"  /status — bot mode + health\n"
"  /positions — open trades\n"
"  /balance — equity\n"
"  /pnl — profit &amp; loss stats\n"
"  /decisions — dry-run signal log\n"
"\n🖥 <b>SERVER</b>\n"
"  /server — uptime, RAM, disk\n"
"  /restart — restart bot services\n"
"\n🎮 <b>CONTROL</b>\n"
"  /pause — halt new entries\n"
"  /resume — allow entries\n"
"  /signal — force scan\n"
"\n🚨 <b>EMERGENCY</b>\n"
"  /stop — halt bot (keeps positions)\n"
"  /panic — CLOSE ALL + halt\n"
"━━━━━━━━━━━━━━━━\n"
"🙏 The Lord Will Provide")

CMDS = {'/status':cmd_status,'/server':cmd_server,'/balance':cmd_balance,
        '/positions':cmd_positions,'/pnl':cmd_pnl,'/decisions':cmd_decisions,
        '/pause':cmd_pause,'/resume':cmd_resume,'/restart':cmd_restart,
        '/signal':cmd_signal,'/stop':cmd_stop,'/panic':cmd_panic}

async def main():
    c = BloFinClient(); offset = 0
    async with aiohttp.ClientSession() as s:
        mode = "LIVE" if LIVE_TRADING_ENABLED else "DRY-RUN"
        await send(s, "YahJireh Online\nMode: " + mode + "\n/help")
        while True:
            try:
                url = "https://api.telegram.org/bot" + TOKEN + "/getUpdates?timeout=50&offset=" + str(offset)
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    ups = (await r.json()).get('result',[])
                for u in ups:
                    offset = u['update_id']+1
                    m = u.get('message') or {}
                    if str((m.get('chat') or {}).get('id')) != CHAT: continue
                    txt = (m.get('text') or '').strip().lower().split('@')[0]
                    if not txt.startswith('/'): continue
                    print("[TG] " + txt)
                    fn = CMDS.get(txt)
                    await send(s, await fn(s, c) if fn else HELP)
            except Exception as e:
                print("tg loop: " + str(e)); await asyncio.sleep(5)

asyncio.run(main())
