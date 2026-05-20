import os, aiohttp
from dotenv import load_dotenv
load_dotenv("/root/trading/.env")
TOKEN   = os.getenv("TELEGRAM_TOKEN","")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")

async def send(text):
    if not TOKEN or not CHAT_ID:
        print(f"[MSG] {text}"); return False
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML"},
                timeout=aiohttp.ClientTimeout(total=10))
            return r.status==200
    except Exception as e:
        print(f"Telegram error: {e}"); return False
