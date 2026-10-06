import json
from datetime import datetime, time
import numpy as np
import pandas as pd
import yfinance as yf

df=yf.download("^NSEI",start="2026-09-01",end="2026-10-07",interval="5m",auto_adjust=False,progress=False,prepost=False)
if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
df.columns=[str(x).lower() for x in df.columns]
df.index=pd.to_datetime(df.index)
df.index=df.index.tz_localize("UTC").tz_convert("Asia/Kolkata") if df.index.tz is None else df.index.tz_convert("Asia/Kolkata")
df=df.between_time("09:15","15:30").copy()
for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
df=df.dropna(subset=["open","high","low","close"])

c,h,l=df.close,df.high,df.low
df["ema9"]=c.ewm(span=9,adjust=False).mean()
df["ema21"]=c.ewm(span=21,adjust=False).mean()
df["ema50"]=c.ewm(span=50,adjust=False).mean()
e12=c.ewm(span=12,adjust=False).mean(); e26=c.ewm(span=26,adjust=False).mean()
df["macd"]=e12-e26; df["macd_signal"]=df.macd.ewm(span=9,adjust=False).mean()
d=c.diff(); g=d.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); ls=(-d.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
df["rsi"]=(100-100/(1+g/ls.replace(0,np.nan))).fillna(100)
tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
df["atr"]=tr.ewm(alpha=1/14,adjust=False).mean()
up=h.diff(); dn=-l.diff(); pdm=up.where((up>dn)&(up>0),0.0); mdm=dn.where((dn>up)&(dn>0),0.0)
pdi=100*pdm.ewm(alpha=1/14,adjust=False).mean()/df.atr.replace(0,np.nan)
mdi=100*mdm.ewm(alpha=1/14,adjust=False).mean()/df.atr.replace(0,np.nan)
df["adx"]=(100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)).ewm(alpha=1/14,adjust=False).mean()
typ=(h+l+c)/3; sess=df.index.date
df["vwap"]=(typ*df.volume.fillna(0)).groupby(sess).cumsum()/df.volume.fillna(0).groupby(sess).cumsum().replace(0,np.nan)
df["atr_pct"]=df.atr/c*100
df["atr_pct_q"]=df.atr_pct.shift(1).rolling(100).quantile(.75)
df["high_20"]=h.shift(1).rolling(20).max()
df["low_20"]=l.shift(1).rolling(20).min()

# V2: regime-aware weighted ensemble.
def signal(r):
    if r.name.time()<time(9,30) or r.name.time()>time(15,5) or time(12,0)<=r.name.time()<time(13,30): return 0
    s=0
    s += 15 if r.ema9>r.ema21 else -15
    s += 15 if r.ema21>r.ema50 else -15
    s += 15 if r.close>r.vwap else -15
    s += 10 if r.macd>r.macd_signal else -10
    if 52<=r.rsi<=68: s+=10
    elif 32<=r.rsi<48: s-=10
    elif r.rsi>72: s-=5
    elif r.rsi<28: s+=5
    if r.adx>=20: s += 10 if r.ema21>r.ema50 else -10
    if pd.notna(r.high_20) and r.close>r.high_20: s+=10
    if pd.notna(r.low_20) and r.close<r.low_20: s-=10
    if pd.notna(r.atr_pct_q) and r.atr_pct>r.atr_pct_q and abs(s)<55: return 0
    return 1 if s>=50 else -1 if s<=-50 else 0

df["signal"]=df.apply(signal,axis=1)
td=df[df.index.date>=pd.Timestamp("2026-09-07").date()].copy()

trades=[]; pos=0; ep=et=score_at_entry=None; stop=target=None
for i in range(len(td)-1):
    r=td.iloc[i]; nxt=td.iloc[i+1]; ts=r.name
    if pos==0 and r.signal!=0 and ts.time()<time(15,0):
        pos=int(r.signal); ep=float(nxt.open); et=nxt.name; a=float(r.atr)
        stop=ep-pos*1.2*a; target=ep+pos*1.8*a; score_at_entry=float(r.adx); continue
    if pos:
        exit_price=None; exit_ts=None
        if pos==1 and r.low<=stop: exit_price=stop; exit_ts=ts
        elif pos==-1 and r.high>=stop: exit_price=stop; exit_ts=ts
        elif pos==1 and r.high>=target: exit_price=target; exit_ts=ts
        elif pos==-1 and r.low<=target: exit_price=target; exit_ts=ts
        elif (pos==1 and r.signal==-1) or (pos==-1 and r.signal==1):
            exit_price=float(nxt.open); exit_ts=nxt.name
        elif ts.time()>=time(15,5):
            exit_price=float(r.close); exit_ts=ts
        if exit_price is not None:
            ret=(exit_price-ep)/ep*pos*100
            trades.append({"entry":et.isoformat(),"exit":exit_ts.isoformat(),"side":"LONG" if pos==1 else "SHORT","return_pct":ret,"adx":score_at_entry})
            pos=0

t=pd.DataFrame(trades)
if len(t):
    w=t[t.return_pct>0]; lo=t[t.return_pct<=0]; eq=(1+t.return_pct/100).cumprod(); dd=eq/eq.cummax()-1
    s={"trades":len(t),"wins":len(w),"losses":len(lo),"win_rate_pct":round(len(w)/len(t)*100,2),"strategy_return_pct":round((eq.iloc[-1]-1)*100,2),"profit_factor":round(w.return_pct.sum()/-lo.return_pct.sum(),3) if len(lo) else None,"max_drawdown_pct":round(float(dd.min()*100),2),"avg_trade_pct":round(t.return_pct.mean(),4),"long_trades":int((t.side=="LONG").sum()),"short_trades":int((t.side=="SHORT").sum())}
else: s={"trades":0}
s["nifty_buy_hold_pct"]=round((td.iloc[-1].close/td.iloc[0].open-1)*100,2); s["generated_at"]=datetime.now().isoformat()
t.to_csv("nifty_5m_v2_trades.csv",index=False); json.dump(s,open("nifty_5m_v2_backtest.json","w"),indent=2); print(json.dumps(s,indent=2))
