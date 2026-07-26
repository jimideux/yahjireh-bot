import asyncio, base64, hashlib, hmac, json, os, time, uuid
from decimal import Decimal, ROUND_HALF_UP
import aiohttp
from dotenv import load_dotenv
load_dotenv(os.getenv("ENV_FILE", "/root/trading/.env"))

IS_DEMO    = os.getenv("IS_DEMO","true").lower() == "true"
BASE_URL   = "https://demo-trading-openapi.blofin.com" if IS_DEMO else "https://openapi.blofin.com"
API_KEY    = os.getenv("BLOFIN_API_KEY","")
SECRET     = os.getenv("BLOFIN_SECRET","")
PASSPHRASE = os.getenv("BLOFIN_PASSPHRASE","")
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED","false").lower() == "true"

def _sign(ts, nonce, method, path, body=""):
    prehash = path + method.upper() + ts + nonce + body
    hex_sig = hmac.new(SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest().encode()
    return base64.b64encode(hex_sig).decode()

def _headers(method, path, body="", params=None):
    ts    = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    query = "?" + "&".join(f"{k}={v}" for k,v in params.items()) if params else ""
    sig_path = path + query
    sign_body = body if method != "GET" else ""
    return {
        "Content-Type":      "application/json",
        "ACCESS-KEY":        API_KEY,
        "ACCESS-SIGN":       _sign(ts, nonce, method, sig_path, sign_body),
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-NONCE":      nonce,
        "ACCESS-PASSPHRASE": PASSPHRASE,
    }

def round_price(price, tick):
    if tick<=0: return str(round(price,8))
    dec=len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
    px=Decimal(str(price)); tk=Decimal(str(tick))
    return f"{(px/tk).to_integral_value(rounding=ROUND_HALF_UP)*tk:.{dec}f}"

def floor_contracts(usd, price, cv, lot, min_s):
    if price<=0 or cv<=0: return 0
    raw=usd/(price*cv)
    if lot>0: raw=(raw//lot)*lot
    result=max(raw,min_s)
    # If lot allows decimals keep them, else return int
    if lot>0 and lot<1:
        decimals=len(str(lot).rstrip("0").split(".")[-1]) if "." in str(lot) else 0
        return round(result,decimals)
    return int(result)

class BloFinClient:
    def __init__(self):
        self._session=None; self._instruments={}

    async def _sess(self):
        if not self._session or self._session.closed:
            self._session=aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _req(self, method, path, params=None, body=None, private=False):
        if method == "POST" and "/trade/" in path and not LIVE_TRADING_ENABLED:
            print(f"  🚫 DRY-RUN: blocked {method} {path} (LIVE_TRADING_ENABLED=false) body={body}")
            return "DRYRUN"
        s=await self._sess(); url=BASE_URL+path
        bs=json.dumps(body) if body else ""
        hdrs=_headers(method, path, bs, params) if private else {"Content-Type":"application/json"}
        for attempt in range(3):
            try:
                async with s.request(method, url, params=params,
                    data=bs if body else None, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    d=await r.json()
                    if isinstance(d,dict):
                        code=str(d.get("code","0"))
                        if code!="0":
                            print(f"BloFin {code}: {d.get('msg')} {path}")
                            return None
                        return d.get("data")
                    return d
            except Exception as e:
                print(f"Request error attempt {attempt+1}: {e}")
                if attempt<2: await asyncio.sleep(2**attempt)
        return None

    async def get_instrument(self, inst_id):
        if inst_id in self._instruments: return self._instruments[inst_id]
        d=await self._req("GET","/api/v1/market/instruments",params={"instId":inst_id})
        if d and isinstance(d,list): self._instruments[inst_id]=d[0]; return d[0]
        return {}

    async def get_ticker(self, inst_id):
        d=await self._req("GET","/api/v1/market/tickers",params={"instId":inst_id})
        return d[0] if d and isinstance(d,list) else {}

    async def get_candles(self, inst_id, bar="1H", limit=20):
        d=await self._req("GET","/api/v1/market/candles",
            params={"instId":inst_id,"bar":bar,"limit":str(limit)})
        return d if isinstance(d,list) else []

    async def get_atr(self, inst_id, period=14, bar="1H"):
        candles=await self.get_candles(inst_id,bar=bar,limit=period+5)
        if len(candles)<2: return 0.0
        highs=[float(c[2]) for c in candles]
        lows=[float(c[3]) for c in candles]
        closes=[float(c[4]) for c in candles]
        trs=[]
        for i in range(1,len(candles)):
            trs.append(max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])))
        return sum(trs[-period:])/period if trs else 0.0

    async def get_ema(self, inst_id, period=50, bar="1H"):
        candles=await self.get_candles(inst_id,bar=bar,limit=period+10)
        if not candles: return 0.0
        closes=[float(c[4]) for c in candles]
        k=2/(period+1); ema=closes[0]
        for c in closes[1:]: ema=c*k+ema*(1-k)
        return ema

    async def get_mark_price(self, inst_id):
        t=await self.get_ticker(inst_id)
        return float(t.get("last",0) or 0)

    async def get_equity(self) -> float:
        resp=await self._req("GET","/api/v1/account/balance",private=True)
        if not resp: return 0.0
        d=resp
        if isinstance(d,dict) and "data" in d and d.get("code") is not None:
            d=d.get("data")
        if isinstance(d,list):
            if not d: return 0.0
            d=d[0]
        if isinstance(d,dict):
            te=d.get("totalEquity") or d.get("equityUsd") or d.get("equity")
            if te is not None:
                try: return float(str(te))
                except: pass
        return 0.0

    async def get_positions(self):
        d=await self._req("GET","/api/v1/account/positions",private=True)
        return d if isinstance(d,list) else []

    async def get_pending_orders(self, inst_id=None):
        params={"instType":"SWAP"}
        if inst_id: params["instId"]=inst_id
        d=await self._req("GET","/api/v1/trade/orders-pending",params=params,private=True)
        return d if isinstance(d,list) else []

    async def place_order(self, inst_id, side, price=None, size=None, reduce_only=False, order_type="limit"):
        if not LIVE_TRADING_ENABLED:
            print(f"  🚫 DRY-RUN: would {side} {size} {inst_id} @ {price or 'MARKET'} ({order_type}) — order BLOCKED (LIVE_TRADING_ENABLED=false)")
            return "DRYRUN"
        if size is None or size <= 0: return False
        inst=await self.get_instrument(inst_id)
        tick=float(inst.get("tickSize","0.01"))
        body={
            "instId":inst_id,"marginMode":"cross","positionSide":"net",
            "side":side,"orderType":order_type,"size":str(size)
        }
        if order_type=="limit":
            if price is None:
                print(f"price required for limit order {inst_id}"); return False
            body["price"]=round_price(price,tick)
        if reduce_only: body["reduceOnly"]="true"
        d=await self._req("POST","/api/v1/trade/order",body=body,private=True)
        if d and isinstance(d,list):
            item=d[0]
            order_id = item.get("orderId") or item.get("orderid","")
            item_code = str(item.get("code","0"))
            if item_code=="0" or order_id:
                px_str=round_price(price,tick) if price else "MARKET"
                print(f"  Order: {inst_id} {side} @ {px_str} x{size} ({order_type})")
                return True
            print(f"  Failed {inst_id}: {item.get(chr(109)+chr(115)+chr(103))}")
        return False

    async def cancel_order(self, inst_id, order_id):
        d=await self._req("POST","/api/v1/trade/cancel-order",
            body={"instId":inst_id,"orderId":order_id},private=True)
        return d is not None

    async def cancel_all_orders(self, inst_id):
        orders=await self.get_pending_orders(inst_id)
        count=0
        for o in orders:
            oid=o.get("ordId") or o.get("orderId","")
            if oid and await self.cancel_order(inst_id,oid): count+=1
            await asyncio.sleep(0.1)
        return count

    async def calc_contracts(self, inst_id, usd):
        inst=await self.get_instrument(inst_id)
        price=await self.get_mark_price(inst_id)
        cv=float(inst.get("contractValue","1") or 1)
        lot=float(inst.get("lotSize","1") or 1)
        min_s=float(inst.get("minSize","1") or 1)
        contracts=floor_contracts(usd,price,cv,lot,min_s)
        est=contracts*price*cv
        print(f"  [SIZE] {inst_id} contracts={contracts} est_notional=${est:.2f}")
        return contracts
