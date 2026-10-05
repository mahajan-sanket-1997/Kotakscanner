import os
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

load_dotenv()
try:
    from neo_api_client import NeoAPI
    from neo_api_client.websocket.feed import WsToken, SFeedScrip
except ImportError:
    NeoAPI = None
    WsToken = None
    SFeedScrip = None

app = FastAPI(title="KotakScanner", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
INTERVAL_MINUTES = {"1min":1,"3min":3,"5min":5,"10min":10,"15min":15,"30min":30,"60min":60}

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
    if price is None: raise ValueError("Kotak feed message did not contain LTP")
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

@app.get("/",response_class=HTMLResponse)
async def home(request:Request): return templates.TemplateResponse("index.html",{"request":request})

@app.get("/health")
async def health(): return {"ok":True,"service":"KotakScanner"}

@app.get("/api/history")
async def history(segment:str=Query("nse_cm"),token:str=Query(...,min_length=1),interval:str=Query("5min"),days:int=Query(1,ge=1,le=30)):
    if interval not in INTERVAL_MINUTES: return {"ok":False,"error":"Unsupported interval"}
    try:
        client=history_client(); end=datetime.now(); start=end-timedelta(days=days)
        response=client.historical_data(neosymbol=f"{segment}|{token}",interval=interval,from_date=start.strftime("%Y-%m-%d"),to_date=end.strftime("%Y-%m-%d"))
        rows=response.get("data",{}).get("candles",[])
        candles=[{"time":r[0],"open":r[1],"high":r[2],"low":r[3],"close":r[4],"volume":r[5] if len(r)>5 else 0,"forming":False} for r in rows]
        return {"ok":True,"candles":candles,"pattern":candle_pattern(candles)}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.websocket("/ws")
async def stream(websocket:WebSocket):
    await websocket.accept(); segment=websocket.query_params.get("segment","nse_cm"); token=websocket.query_params.get("token",""); interval=websocket.query_params.get("interval","5min")
    if not token or interval not in INTERVAL_MINUTES:
        await websocket.send_json({"type":"error","message":"Token and valid interval are required"}); await websocket.close(); return
    candles=deque(maxlen=500); client=None
    try:
        client=live_client(); end=datetime.now(); start=end-timedelta(days=1)
        hist=client.historical_data(neosymbol=f"{segment}|{token}",interval=interval,from_date=start.strftime("%Y-%m-%d"),to_date=end.strftime("%Y-%m-%d"))
        for r in hist.get("data",{}).get("candles",[])[-499:]: candles.append({"time":r[0],"open":r[1],"high":r[2],"low":r[3],"close":r[4],"volume":r[5] if len(r)>5 else 0,"forming":False})
        await websocket.send_json({"type":"snapshot","candles":list(candles),"pattern":candle_pattern(list(candles))})
        async with client.create_websocket() as ws:
            await ws.subscribe_scrips([WsToken(segment,token)])
            async for message in ws:
                if SFeedScrip is not None and not isinstance(message,SFeedScrip): continue
                price,volume=extract_price(message); now=datetime.now().astimezone()
                apply_tick(candles,{"timestamp":now,"price":price,"volume":volume},INTERVAL_MINUTES[interval]); current=list(candles)
                await websocket.send_json({"type":"tick","price":price,"candles":current[-200:],"pattern":candle_pattern(current),"status":"FORMING","updated_at":now.isoformat()})
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
    uvicorn.run(app,host=os.getenv("HOST","0.0.0.0"),port=int(os.getenv("PORT","8000")))
