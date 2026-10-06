import json, math
from datetime import datetime, time
import numpy as np
import pandas as pd
import yfinance as yf

TICKER="^NSEI"
START="2026-09-01"
END="2026-10-07"
TRADE_START="2026-09-07"

df=yf.download(TICKER,start=START,end=END,interval="5m",auto_adjust=False,progress=False,prepost=False)
if df.empty:
    raise SystemExit("No 5-minute data returned by Yahoo Finance")
if isinstance(df.columns,pd.MultiIndex):
    df.columns=df.columns.get_level_values(0)
df=df.rename(columns=str.lower)
df.index=pd.to_datetime(df.index)
if df.index.tz is None:
    df.index=df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
else:
    df.index=df.index.tz_convert("Asia/Kolkata")
df=df.between_time("09:15","15:30").copy()
for c in ["open","high","low","close","volume"]:
    df[c]=pd.to_numeric(df[c],errors="coerce")
df=df.dropna(subset=["open","high","low","close"]).copy()

# Indicators, calculated only from current/past bars.
close=df.close
high=df.high
low=df.low
df["ema20"]=close.ewm(span=20,adjust=False).mean()
df["ema50"]=close.ewm(span=50,adjust=False).mean()
ema12=close.ewm(span=12,adjust=False).mean()
ema26=close.ewm(span=26,adjust=False).mean()
df["macd"]=ema12-ema26
df["macd_signal"]=df["macd"].ewm(span=9,adjust=False).mean()

delta=close.diff()
gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
rs=gain/loss.replace(0,np.nan)
df["rsi"]=(100-(100/(1+rs))).fillna(100)

tr=pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
df["atr"]=tr.ewm(alpha=1/14,adjust=False).mean()

up=high.diff()
down=-low.diff()
plus_dm=up.where((up>down)&(up>0),0.0)
minus_dm=down.where((down>up)&(down>0),0.0)
atr14=df["atr"].replace(0,np.nan)
plus_di=100*plus_dm.ewm(alpha=1/14,adjust=False).mean()/atr14
minus_di=100*minus_dm.ewm(alpha=1/14,adjust=False).mean()/atr14
dx=100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0,np.nan)
df["adx"]=dx.ewm(alpha=1/14,adjust=False).mean()

typical=(high+low+close)/3
session=df.index.date
pv=(typical*df["volume"].fillna(0)).groupby(session).cumsum()
vv=df["volume"].fillna(0).groupby(session).cumsum()
df["vwap"]=pv/vv.replace(0,np.nan)

df["hh20"]=high.shift(1).rolling(20).max()
df["ll20"]=low.shift(1).rolling(20).min()

def score(r):
    s=0
    if r.close>r.ema20: s+=10
    else: s-=10
    if r.ema20>r.ema50: s+=15
    else: s-=15
    if r.macd>r.macd_signal: s+=15
    else: s-=15
    if 52<=r.rsi<=70: s+=10
    elif 30<=r.rsi<48: s-=10
    elif r.rsi>70: s-=5
    elif r.rsi<30: s+=5
    if r.adx>=20:
        s += 10 if r.ema20>r.ema50 else -10
    if pd.notna(r.vwap):
        s += 15 if r.close>r.vwap else -15
    if pd.notna(r.hh20) and r.close>r.hh20: s+=15
    if pd.notna(r.ll20) and r.close<r.ll20: s-=15
    return s

df["score"]=df.apply(score,axis=1)
df["signal"]=np.select([df.score>=50,df.score<=-50],[1,-1],default=0)

trade_df=df[df.index.date>=pd.Timestamp(TRADE_START).date()].copy()
trades=[]
position=0
entry_price=None
entry_time=None
entry_score=None

for i in range(len(trade_df)-1):
    r=trade_df.iloc[i]
    nxt=trade_df.iloc[i+1]
    ts=r.name
    nxt_ts=nxt.name
    eod=ts.time()>=time(15,10)
    desired=1 if r.signal==1 else -1 if r.signal==-1 else 0
    # Enter/reverse at next bar open only after a closed signal bar.
    if position==0 and desired!=0 and not eod:
        position=desired; entry_price=float(nxt.open); entry_time=nxt_ts; entry_score=float(r.score); continue
    if position!=0:
        exit_now=eod or (position==1 and r.score<20) or (position==-1 and r.score>-20)
        if exit_now:
            exit_price=float(nxt.open) if not eod else float(r.close)
            ret=(exit_price-entry_price)/entry_price*position
            trades.append({"entry":entry_time.isoformat(),"exit":(nxt_ts if not eod else ts).isoformat(),"side":"LONG" if position==1 else "SHORT","entry_price":entry_price,"exit_price":exit_price,"return_pct":ret*100,"entry_score":entry_score})
            position=0; entry_price=None
            # allow fresh entry on a later bar, not same bar.

if position!=0:
    r=trade_df.iloc[-1]
    exit_price=float(r.close)
    ret=(exit_price-entry_price)/entry_price*position
    trades.append({"entry":entry_time.isoformat(),"exit":r.name.isoformat(),"side":"LONG" if position==1 else "SHORT","entry_price":entry_price,"exit_price":exit_price,"return_pct":ret*100,"entry_score":entry_score})

t=pd.DataFrame(trades)
if t.empty:
    summary={"status":"ok","trades":0}
else:
    wins=t[t.return_pct>0]
    losses=t[t.return_pct<=0]
    gross_profit=float(wins.return_pct.sum())
    gross_loss=float(-losses.return_pct.sum())
    eq=(1+t.return_pct/100).cumprod()
    dd=eq/eq.cummax()-1
    summary={
        "status":"ok",
        "data_start":str(df.index.min()),
        "data_end":str(df.index.max()),
        "trade_start":TRADE_START,
        "trade_end":"2026-10-06",
        "bars":int(len(trade_df)),
        "trades":int(len(t)),
        "wins":int(len(wins)),
        "losses":int(len(losses)),
        "win_rate_pct":round(len(wins)/len(t)*100,2),
        "strategy_return_pct":round((eq.iloc[-1]-1)*100,2),
        "avg_trade_pct":round(t.return_pct.mean(),4),
        "median_trade_pct":round(t.return_pct.median(),4),
        "profit_factor":round(gross_profit/gross_loss,3) if gross_loss else None,
        "max_drawdown_pct":round(float(dd.min()*100),2),
        "avg_winner_pct":round(float(wins.return_pct.mean()),4) if len(wins) else None,
        "avg_loser_pct":round(float(losses.return_pct.mean()),4) if len(losses) else None,
        "long_trades":int((t.side=="LONG").sum()),
        "short_trades":int((t.side=="SHORT").sum()),
    }
    t.to_csv("nifty_5m_trades.csv",index=False)

# Benchmark from first trade-period open to last close.
benchmark=(trade_df.iloc[-1].close/trade_df.iloc[0].open-1)*100
summary["nifty_buy_hold_pct"]=round(float(benchmark),2)
summary["generated_at"]=datetime.now().isoformat()
with open("nifty_5m_backtest.json","w") as f: json.dump(summary,f,indent=2)
print(json.dumps(summary,indent=2))
