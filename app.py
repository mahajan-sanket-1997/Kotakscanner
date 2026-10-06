import os
from collections import deque
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

load_dotenv()
try:
    from neo_api_client import NeoAPI
    from neo_api_client.websocket.feed import WsToken, SFeedScrip, SFeedIndex
except ImportError:
    NeoAPI = None
    WsToken = None
    SFeedScrip = None
    SFeedIndex = None

app = FastAPI(title="KotakScanner", version="0.5.0")
INDEXES = {"NIFTY 50": ("nse_cm", "Nifty 50"), "SENSEX": ("bse_cm", "SENSEX")}
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
INTERVAL_MINUTES = {"1min":1,"3min":3,"5min":5,"10min":10,"15min":15,"30min":30,"60min":60}

# Pattern detection uses the most recent 1-3 candles.
def candle_pattern(c):
    if not c: return None
    x=c[-1]; o,h,l,cl=map(float,(x["open"],x["high"],x["low"],x["close"]))
    rng=max(h-l,1e-9); body=abs(cl-o); upper=h-max(o,cl); lower=min(o,cl)-l
    if body <= rng*.10: return "Doji"
    if body <= rng*.35 and lower >= body*2 and upper <= body: return "Hammer"
    if body <= rng*.35 and upper >= body*2 and lower <= body: return "Inverted Hammer" if cl>=o else "Shooting Star"
    if len(c)>=2:
        p=c[-2]; po,pc=float(p["open"]),float(p["close"])
        if pc<po and cl>o and o<=pc and cl>=po: return "Bullish Engulfing"
        if pc>po and cl<o and o>=pc and cl<=po: return "Bearish Engulfing"
    if len(c)>=3:
        a,b=c[-3],c[-2]; ao,ac=float(a["open"]),float(a["close"]); bo,bc=float(b["open"]),float(b["close"])
        br=abs(bc-bo); brange=max(float(b["high"])-float(b["low"]),1e-9)
        if ac<ao and br<=brange*.35 and cl>o and cl>=(ao+ac)/2: return "Morning Star"
        if ac>ao and br<=brange*.35 and cl<o and cl<=(ao+ac)/2: return "Evening Star"
    return None

def market_signal(candles, is_index=False):
    """Use up to the latest 50 candles for context; return an explainable directional indication."""
    if len(candles) < 20:
        return {"direction":"NEUTRAL","confidence":0,"score":0,"pattern":None,"reason":"Need at least 20 candles for market context"}
    # Do not let the still-forming candle dominate the signal.
    closed=[c for c in candles if not c.get("forming")]
    if len(closed) < 20: closed=candles[:-1] if len(candles)>20 else candles
    if len(closed) < 20: return {"direction":"NEUTRAL","confidence":0,"score":0,"pattern":None,"reason":"Building candle history"}
    w=closed[-50:]
    closes=[float(x["close"]) for x in w]
    volumes=[float(x.get("volume") or 0) for x in w]
    # EMA helper.
    def ema(vals, period):
        k=2/(period+1); e=vals[0]
        for v in vals[1:]: e=v*k+e*(1-k)
        return e
    ema20=ema(closes[-20:],20)
    ema50=ema(closes,50) if len(closes)>=50 else ema(closes, min(20,len(closes)))
    score=0; reasons=[]
    last=closes[-1]
    if last>ema20: score+=20; reasons.append("above EMA20")
    else: score-=20; reasons.append("below EMA20")
    if ema20>ema50: score+=20; reasons.append("EMA20 trend up")
    else: score-=20; reasons.append("EMA20 trend down")
    # RSI(14).
    gains=[]; losses=[]
    for i in range(max(1,len(closes)-14),len(closes)):
        d=closes[i]-closes[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    avg_gain=sum(gains)/max(len(gains),1); avg_loss=sum(losses)/max(len(losses),1)
    rsi=100 if avg_loss==0 else 100-(100/(1+avg_gain/avg_loss))
    if 52<=rsi<=70: score+=12; reasons.append("RSI bullish")
    elif 30<=rsi<48: score-=12; reasons.append("RSI bearish")
    elif rsi>70: score-=5; reasons.append("RSI overbought")
    elif rsi<30: score+=5; reasons.append("RSI oversold")
    # Volume confirmation when usable.
    if not is_index and sum(volumes[-10:])>0 and sum(volumes[-20:])>0:
        avg_vol=sum(volumes[-20:])/20
        if volumes[-1]>avg_vol*1.2:
            score += 8 if closes[-1]>=closes[-2] else -8
            reasons.append("high volume confirms move")
    pattern=candle_pattern(closed)
    bullish={"Hammer","Inverted Hammer","Bullish Engulfing","Morning Star"}
    bearish={"Shooting Star","Bearish Engulfing","Evening Star"}
    if pattern in bullish: score+=20; reasons.append(pattern+" bullish")
    elif pattern in bearish: score-=20; reasons.append(pattern+" bearish")
    score=max(-100,min(100,score))
    direction="BULLISH ↑" if score>=20 else "BEARISH ↓" if score<=-20 else "NEUTRAL →"
    confidence=min(95,max(5,round(50+abs(score)*0.45)))
    return {"direction":direction,"confidence":confidence,"score":score,"pattern":pattern,"rsi":round(rsi,1),"ema20":round(ema20,2),"ema50":round(ema50,2),"reason":" • ".join(reasons[-4:])}

def align_time(ts, minutes):
    epoch=int(ts.timestamp()); return datetime.fromtimestamp(epoch-epoch%(minutes*60),tz=ts.tzinfo)

def apply_tick(candles,tick,minutes):
    bucket=align_time(tick["timestamp"],minutes); price=float(tick["price"]); volume=float(tick.get("volume") or 0)
    if not candles or datetime.fromisoformat(candles[-1]["time"]) != bucket:
        candles.append({"time":bucket.isoformat(),"open":price,"high":price,"low":price,"close":price,"volume":volume,"forming":True}); return
    x=candles[-1]; x["high"]=max(x["high"],price); x["low"]=min(x["low"],price); x["close"]=price
    if volume: x["volume"]=max(x["volume"],volume)

def extract_price(message):
    price=getattr(message,"last_traded_price",None); volume=getattr(message,"volume_traded_today",None)
    if price is None and hasattr(message,"model_dump"):
        d=message.model_dump(); price=d.get("last_traded_price") or d.get("ltp"); volume=d.get("volume_traded_today") or d.get("volume")
    if price is None and hasattr(message, "model_dump"):
        d=message.model_dump(); price=d.get("iv") or d.get("last_traded_price") or d.get("ltp"); volume=d.get("volume_traded_today") or d.get("volume")
    if price is None: raise ValueError("Kotak feed message did not contain index LTP")
    return float(price),float(volume or 0)

def history_client():
    key=os.getenv("KOTAK_CONSUMER_KEY")
    if not key: raise RuntimeError("KOTAK_CONSUMER_KEY is missing")
    if NeoAPI is None: raise RuntimeError("kotakneoapi is not installed")
    return NeoAPI(consumer_key=key,environment="prod")

def live_client():
    required=["KOTAK_CONSUMER_KEY","KOTAK_MOBILE","KOTAK_UCC","KOTAK_MPIN","KOTAK_TOTP_SECRET"]
    missing=[k for k in required if not os.getenv(k)]
    if missing: raise RuntimeError("Missing environment variables: "+", ".join(missing))
    if NeoAPI is None: raise RuntimeError("kotakneoapi is not installed")
    import pyotp
    client=NeoAPI(consumer_key=os.environ["KOTAK_CONSUMER_KEY"],environment="prod")
    client.totp_login(mobile_number=os.environ["KOTAK_MOBILE"],ucc=os.environ["KOTAK_UCC"],totp=pyotp.TOTP(os.environ["KOTAK_TOTP_SECRET"]).now())
    client.totp_validate(mpin=os.environ["KOTAK_MPIN"])
    return client

def normalize_search_results(response):
    data=response.get("data",response) if isinstance(response,dict) else response
    if isinstance(data,dict):
        for key in ("scrips","results","data"):
            if isinstance(data.get(key),list): data=data[key]; break
    if not isinstance(data,list): return []
    out=[]
    for row in data:
        if not isinstance(row,dict): continue
        symbol=row.get("display_symbol") or row.get("symbol") or row.get("trading_symbol") or row.get("pSymbol")
        token=row.get("instrument_token") or row.get("token") or row.get("pSymbol")
        if symbol and token: out.append({"symbol":str(symbol),"token":str(token),"segment":str(row.get("exchange_segment") or row.get("exchange") or "nse_cm")})
    return out[:30]

@app.get("/",response_class=HTMLResponse)
async def home(request:Request): return templates.TemplateResponse("index.html",{"request":request})

@app.get("/health")
async def health(): return {"ok":True,"service":"KotakScanner","version":"0.5.0"}

@app.get("/api/expiry-options")
async def expiry_options(index:str=Query("NIFTY 50"), expiry:str|None=None):
    try: return {"ok":True, **expiry_scanner(index, expiry=expiry)}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.get("/api/snapshot-1515")
async def snapshot_1515_api():
    now=datetime.now().astimezone()
    if now.hour>15 or (now.hour==15 and now.minute>=15):
        try: return {"ok":True,"captured":True,"snapshot":capture_1515()}
        except Exception as e: return {"ok":False,"error":str(e)}
    return {"ok":True,"captured":False,"message":"3:15 PM IST snapshot is not available yet today.","now":now.isoformat()}

@app.get("/api/search")
async def search(symbol:str=Query(...,min_length=1,max_length=40),segment:str=Query("nse_cm")):
    if segment not in {"nse_cm","bse_cm"}: return {"ok":False,"error":"Unsupported segment"}
    if segment=="nse_cm" and symbol.strip().upper() in {"NIFTY 50","NIFTY"}: return {"ok":True,"results":[{"symbol":"Nifty 50","token":"Nifty 50","segment":"nse_cm","is_index":True}]}
    if segment=="bse_cm" and symbol.strip().upper()=="SENSEX": return {"ok":True,"results":[{"symbol":"SENSEX","token":"SENSEX","segment":"bse_cm","is_index":True}]}
    try:
        client=history_client(); response=client.search_scrip(exchange_segment=segment,symbol=symbol.strip().upper())
        return {"ok":True,"results":normalize_search_results(response)}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.get("/api/history")
async def history(segment:str=Query("nse_cm"),token:str=Query(...,min_length=1),interval:str=Query("5min"),days:int=Query(1,ge=1,le=30)):
    if interval not in INTERVAL_MINUTES: return {"ok":False,"error":"Unsupported interval"}
    try:
        client=history_client(); end=datetime.now(); start=end-timedelta(days=days)
        response=client.historical_data(neosymbol=f"{segment}|{token}",interval=interval,from_date=start.strftime("%Y-%m-%d"),to_date=end.strftime("%Y-%m-%d"))
        rows=response.get("data",{}).get("candles",[])
        candles=[{"time":r[0],"open":r[1],"high":r[2],"low":r[3],"close":r[4],"volume":r[5] if len(r)>5 else 0,"forming":False} for r in rows]
        is_index=(segment,token) in INDEXES.values()
        return {"ok":True,"candles":candles,"pattern":candle_pattern(candles),"signal":market_signal(candles,is_index=is_index)}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.websocket("/ws")
async def stream(websocket:WebSocket):
    await websocket.accept(); segment=websocket.query_params.get("segment","nse_cm"); token=websocket.query_params.get("token",""); interval=websocket.query_params.get("interval","5min")
    if not token or interval not in INTERVAL_MINUTES:
        await websocket.send_json({"type":"error","message":"Token and valid interval are required"}); await websocket.close(); return
    candles=deque(maxlen=500); client=None
    try:
        client=live_client(); end=datetime.now(); start=end-timedelta(days=3)
        hist=client.historical_data(neosymbol=f"{segment}|{token}",interval=interval,from_date=start.strftime("%Y-%m-%d"),to_date=end.strftime("%Y-%m-%d"))
        for r in hist.get("data",{}).get("candles",[])[-499:]:
            candles.append({"time":r[0],"open":r[1],"high":r[2],"low":r[3],"close":r[4],"volume":r[5] if len(r)>5 else 0,"forming":False})
        is_index=(segment,token) in INDEXES.values()
        await websocket.send_json({"type":"snapshot","candles":list(candles),"pattern":candle_pattern(list(candles)),"signal":market_signal(list(candles),is_index=is_index),"instrument_type":"index" if is_index else "stock"})
        async with client.create_websocket() as ws:
            await ws.subscribe_scrips([WsToken(segment,token)])
            async for message in ws:
                if SFeedScrip is not None and SFeedIndex is not None and not isinstance(message,(SFeedScrip,SFeedIndex)): continue
                price,volume=extract_price(message); now=datetime.now().astimezone()
                apply_tick(candles,{"timestamp":now,"price":price,"volume":volume},INTERVAL_MINUTES[interval]); current=list(candles)
                await websocket.send_json({"type":"tick","price":price,"candles":current[-200:],"pattern":candle_pattern(current),"signal":market_signal(current,is_index=is_index),"status":"FORMING","updated_at":now.isoformat()})
    except WebSocketDisconnect: pass
    except Exception as e:
        try: await websocket.send_json({"type":"error","message":str(e)})
        except Exception: pass
    finally:
        if client is not None:
            try: client.logout()
            except Exception: pass

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host=os.getenv("HOST","0.0.0.0"),port=int(os.getenv("PORT",8000)))
