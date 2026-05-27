import os
import io
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Sophie Trading — Double Structure v7.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TWELVE_DATA_KEY  = os.getenv("TWELVE_DATA_KEY", "0a71d306b7f64336950805189681a0a2")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAJOR_PAIRS = ["EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD","NZD/USD","USD/CAD"]
MINOR_PAIRS = [
    "EUR/GBP","EUR/JPY","EUR/CHF","EUR/AUD","EUR/NZD",
    "GBP/JPY","GBP/CHF","GBP/AUD","GBP/NZD",
    "AUD/JPY","AUD/NZD","AUD/CHF","AUD/CAD",
    "NZD/JPY","NZD/CHF","NZD/CAD","CAD/JPY","CAD/CHF","CHF/JPY"
]
ALL_PAIRS = MAJOR_PAIRS + MINOR_PAIRS

# TF_SIZE : 300 bougies = bon compromis qualité/quota
# 4H=50j · 2H=25j · 1H=12j · 30min=6j
TF_INTERVAL = {"4H":"4h","2H":"2h","1H":"1h","30min":"30min"}
TF_SIZE     = {"4H":300,"2H":300,"1H":300,"30min":300}

cache = {
    "prices":        {},
    "signals":       {},
    "candles_cache": {},
    "last_update":   None,
    "sent_alerts":   set(),
    "last_notification": None,
    "api_calls_today": 0,
    "api_calls_reset": None,
}

def track_api_call(n=1):
    now = datetime.now(timezone.utc)
    if cache["api_calls_reset"]:
        last = datetime.fromisoformat(cache["api_calls_reset"])
        if (now - last).total_seconds() > 86400:
            cache["api_calls_today"] = 0
            cache["api_calls_reset"] = now.isoformat()
    else:
        cache["api_calls_reset"] = now.isoformat()
    cache["api_calls_today"] += n
    if cache["api_calls_today"] >= 700:
        print(f"QUOTA ALERTE: {cache['api_calls_today']}/800")
    return cache["api_calls_today"]

# ── ANNONCES ÉCONOMIQUES ──────────────────────────────────────────────────────
async def is_near_economic_event():
    now = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.get("https://api.twelvedata.com/economic_calendar", params={
                "apikey": TWELVE_DATA_KEY,
                "start_date": (now-timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                "end_date":   (now+timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                "importance": "high",
            })
            d = r.json()
            events = d.get("result",{}).get("list",[])
            if events:
                return {"blocked":True,"events":[e.get("event","") for e in events]}
    except: pass
    return {"blocked":False,"events":[]}

# ── SESSION ───────────────────────────────────────────────────────────────────
def is_valid_session():
    hp      = (datetime.now(timezone.utc).hour+2)%24
    weekday = datetime.now(timezone.utc).weekday()
    if weekday >= 5: return {"valid":False,"hour":hp,"session":"FERME"}
    if hp < 8 or hp > 21: return {"valid":False,"hour":hp,"session":f"HORS SESSION ({hp}h)"}
    return {"valid":True,"hour":hp,"session":"LONDON" if hp<17 else "NEW YORK"}

# ── UTILITAIRES ───────────────────────────────────────────────────────────────
def dp(pair): return 3 if "JPY" in pair else 5
def calc_mm100(candles):
    if len(candles)<100: return None
    return sum(c["close"] for c in candles[-100:])/100
def find_pivots(candles, n=3):
    highs,lows=[],[]
    for i in range(n,len(candles)-n):
        if all(candles[i]["high"]>candles[i+k]["high"] for k in range(-n,n+1) if k!=0):
            highs.append({"price":candles[i]["high"],"idx":i})
        if all(candles[i]["low"] <candles[i+k]["low"]  for k in range(-n,n+1) if k!=0):
            lows.append( {"price":candles[i]["low"], "idx":i})
    return highs,lows

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1 — TENDANCE DOW
# ══════════════════════════════════════════════════════════════════════════════
def detect_trend(candles, tf):
    if len(candles)<20: return {"trend":"NEUTRE","tf":tf}
    mm100 = calc_mm100(candles)
    c500  = candles[-500:] if len(candles)>500 else candles
    highs,lows = find_pivots(c500,n=3)
    if len(highs)<2 or len(lows)<2:
        return {"trend":"NEUTRE","tf":tf,"mm100":mm100}
    sh,sl = highs[-2:],lows[-2:]
    hh = sh[1]["price"]>sh[0]["price"]
    hl = sl[1]["price"]>sl[0]["price"]
    lh = sh[1]["price"]<sh[0]["price"]
    ll = sl[1]["price"]<sl[0]["price"]
    if   hh and hl: trend="HAUSSIERE"
    elif lh and ll: trend="BAISSIERE"
    else: return {"trend":"NEUTRE","tf":tf,"mm100":mm100}
    mm100_ok,mm100_slope=True,None
    if mm100 and len(candles)>=110:
        mm100_old=sum(c["close"] for c in candles[-110:-10])/100
        mm100_slope=(mm100-mm100_old)/mm100_old*100
        mm100_flat=abs(mm100_slope)<0.05
        mm100_ok=mm100_flat or (trend=="HAUSSIERE" and mm100_slope>0) or (trend=="BAISSIERE" and mm100_slope<0)
    amp=abs(sh[1]["price"]-sh[0]["price"])/sh[0]["price"]*100+abs(sl[1]["price"]-sl[0]["price"])/sl[0]["price"]*100
    return {
        "trend":trend,"tf":tf,"force":min(100,int(amp*300)),
        "mm100":round(mm100,5) if mm100 else None,
        "mm100_ok":mm100_ok,"mm100_slope":round(mm100_slope,4) if mm100_slope else None,
        "mm100_pos":"AU-DESSUS" if mm100 and candles[-1]["close"]>mm100 else "EN-DESSOUS",
        "last_high":sh[-1]["price"],"last_low":sl[-1]["price"],
        "prev_high":sh[-2]["price"],"prev_low":sl[-2]["price"],
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 2 — FIGURE (Triangle ≥50 bougies / Consolidation ≥30 bougies)
# ══════════════════════════════════════════════════════════════════════════════
def detect_figure(candles, trend):
    if len(candles)<60: return {"found":False,"reason":"Pas assez de données"}
    for window in [200,150,100,80,60,50]:
        if len(candles)<window+10: continue
        fig=candles[-window:]; prev=candles[-(window+20):-window]
        if len(prev)<10: continue
        fh=[c["high"] for c in fig]; fl=[c["low"] for c in fig]
        ph=[c["high"] for c in prev];pl=[c["low"]  for c in prev]
        trend_ok=(max(ph[:5])>max(ph[-5:])) if trend=="BAISSIERE" else (min(pl[:5])<min(pl[-5:]))
        if not trend_ok: continue
        n=len(fig); top_slope=(fh[-1]-fh[0])/n; bot_slope=(fl[-1]-fl[0])/n
        range_pct=(max(fh)-min(fl))/fig[-1]["close"]*100
        is_tri=top_slope<-0.000003 and bot_slope>0.000003 and range_pct<3.0 and window>=50
        if is_tri:
            return {"found":True,"type":"TRIANGLE","nb_candles":window,
                    "zone_high":round(max(fh),5),"zone_low":round(min(fl),5),
                    "range_pct":round(range_pct,3),
                    "reliability":"HAUTE" if window>=80 else "MOYENNE"}
    for window in [100,80,60,50,40,30]:
        if len(candles)<window+10: continue
        fig=candles[-window:]; prev=candles[-(window+20):-window]
        if len(prev)<8: continue
        fh=[c["high"] for c in fig]; fl=[c["low"] for c in fig]
        ph=[c["high"] for c in prev];pl=[c["low"]  for c in prev]
        trend_ok=(max(ph[:5])>max(ph[-5:])) if trend=="BAISSIERE" else (min(pl[:5])<min(pl[-5:]))
        if not trend_ok: continue
        range_pct=(max(fh)-min(fl))/fig[-1]["close"]*100
        if range_pct<1.5 and window>=30:
            return {"found":True,"type":"CONSOLIDATION","nb_candles":window,
                    "zone_high":round(max(fh),5),"zone_low":round(min(fl),5),
                    "range_pct":round(range_pct,3),
                    "reliability":"HAUTE" if window>=40 else "MOYENNE"}
    return {"found":False,"reason":"Aucune figure valide (Triangle≥50 / Consolidation≥30 bougies)"}

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 3 — ZONE DE PRIX (≥2 retests)
# ══════════════════════════════════════════════════════════════════════════════
def detect_zone(candles, trend, pair):
    if len(candles)<30: return None
    c800=candles[-800:] if len(candles)>800 else candles
    highs,lows=find_pivots(c800[:-5],n=2)
    candidates=lows if trend=="BAISSIERE" else highs
    if not candidates: return None
    best,best_score=None,0
    for piv in candidates:
        level=piv["price"]; tol=level*0.0015
        touches=rejections=0
        for c in c800:
            if abs(c["high"]-level)<tol or abs(c["low"]-level)<tol:
                touches+=1
                wick=(c["high"]-max(c["open"],c["close"]))/(c["high"]-c["low"]+0.0000001)
                if wick>0.35: rejections+=1
        is_historic=piv["idx"]<=5
        if touches<2 and not is_historic: continue
        rbase=1 if level>10 else (0.1 if level>1 else 0.01)
        rround=round(level/rbase)*rbase
        is_psych=abs(level-rround)/level<0.003
        score=touches*20+rejections*15+(20 if is_historic else 0)+(20 if is_psych else 0)
        if score>best_score:
            best_score=score
            best={"level":round(level,dp(pair)),"touches":touches,
                  "rejections":rejections,"is_historic":is_historic,
                  "is_psych":is_psych,"quality":min(100,score),
                  "type":"SUPPORT" if trend=="BAISSIERE" else "RESISTANCE"}
    return best

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 4 — ZONES PSYCHOLOGIQUES
# ══════════════════════════════════════════════════════════════════════════════
def detect_psych_zones(candles, direction, pair):
    last=candles[-1]["close"]; rbase=1 if last>10 else (0.1 if last>1 else 0.01)
    prec=dp(pair); psych=[]
    start=round((last-last*0.05)/rbase)*rbase
    for i in range(20):
        v=round(start+i*rbase,prec)
        if (direction=="LONG" and v>last) or (direction=="SHORT" and v<last): psych.append(v)
        if len(psych)>=5: break
    c300=candles[-300:] if len(candles)>300 else candles
    highs,lows=find_pivots(c300[:-5],n=2)
    danger=[]
    for p in (highs if direction=="LONG" else lows)[-15:]:
        if direction=="LONG" and p["price"]>last: danger.append(round(p["price"],prec))
        elif direction=="SHORT" and p["price"]<last: danger.append(round(p["price"],prec))
    return {"psych_levels":sorted(psych[:4]),"danger_zones":sorted(danger[:3]),
            "tp_fantome":psych[0] if psych else None}

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 5 — BOUGIE DE CONFIRMATION
# ══════════════════════════════════════════════════════════════════════════════
def detect_candle_confirmation(candles, zone_level, direction, pair):
    if len(candles)<3: return None
    last,prev=candles[-1],candles[-2]
    body_last=abs(last["close"]-last["open"])
    body_prev=abs(prev["close"]-prev["open"])
    range_last=last["high"]-last["low"]+0.0000001
    body_ratio=body_last/range_last
    upper_wick=last["high"]-max(last["open"],last["close"])
    lower_wick=min(last["open"],last["close"])-last["low"]
    tol=zone_level*0.003; signals=[]
    # Englobante
    if direction=="LONG":
        eng=last["close"]>last["open"] and last["open"]<prev["close"] and last["close"]>prev["open"] and body_last>body_prev*1.1
    else:
        eng=last["close"]<last["open"] and last["open"]>prev["close"] and last["close"]<prev["open"] and body_last>body_prev*1.1
    if eng: signals.append({"type":"ENGLOBANTE","strength":92,"emoji":"ENG"})
    # Pin Bar
    if direction=="LONG":
        pin=lower_wick>body_last*2.0 and lower_wick>upper_wick*2.5 and abs(last["low"]-zone_level)<tol*3
    else:
        pin=upper_wick>body_last*2.0 and upper_wick>lower_wick*2.5 and abs(last["high"]-zone_level)<tol*3
    if pin: signals.append({"type":"PIN BAR","strength":90,"emoji":"PIN"})
    # Marteau / Etoile
    if direction=="LONG" and lower_wick>body_last*1.5 and body_ratio>0.25 and last["close"]>last["open"]:
        signals.append({"type":"MARTEAU","strength":82,"emoji":"MAR"})
    if direction=="SHORT" and upper_wick>body_last*1.5 and body_ratio>0.25 and last["close"]<last["open"]:
        signals.append({"type":"ETOILE FILANTE","strength":82,"emoji":"ETL"})
    # Marubozu
    if body_ratio>0.82 and ((direction=="LONG" and last["close"]>last["open"]) or (direction=="SHORT" and last["close"]<last["open"])):
        signals.append({"type":"MARUBOZU","strength":88,"emoji":"MRZ"})
    # Cassure
    if direction=="LONG":
        cass=last["close"]>zone_level and last["open"]<zone_level*1.003 and body_ratio>0.55 and last["close"]>last["open"]
    else:
        cass=last["close"]<zone_level and last["open"]>zone_level*0.997 and body_ratio>0.55 and last["close"]<last["open"]
    if cass: signals.append({"type":"CASSURE","strength":85,"emoji":"CAS"})
    if not signals: return None
    best=max(signals,key=lambda x:x["strength"])
    return {"found":True,"type":best["type"],"strength":best["strength"],
            "emoji":best["emoji"],"all_signals":signals,"body_ratio":round(body_ratio,2)}

def calc_sl_tp(candles, trend_data, direction, pair):
    last=candles[-1]; entry=last["close"]; prec=dp(pair)
    atr=sum(c["high"]-c["low"] for c in candles[-20:])/min(20,len(candles))
    if direction=="LONG":
        sl_base=trend_data.get("last_low",entry-atr*2)
        mm100=trend_data.get("mm100")
        sl=(mm100-atr*0.3) if (mm100 and sl_base<mm100<entry) else sl_base-atr*0.15
        sl=min(sl,entry-atr*1.2)
    else:
        sl_base=trend_data.get("last_high",entry+atr*2)
        mm100=trend_data.get("mm100")
        sl=(mm100+atr*0.3) if (mm100 and entry<mm100<sl_base) else sl_base+atr*0.15
        sl=max(sl,entry+atr*1.2)
    sl_dist=abs(entry-sl)
    if sl_dist<atr*0.3: return None
    tp=entry+sl_dist*2.5 if direction=="LONG" else entry-sl_dist*2.5
    return {"entry":round(entry,prec),"stop_loss":round(sl,prec),
            "take_profit":round(tp,prec),"ratio":2.5,"atr":round(atr,prec)}

# ══════════════════════════════════════════════════════════════════════════════
# ANALYSE PRINCIPALE — 3 NIVEAUX D'ALERTE
# ══════════════════════════════════════════════════════════════════════════════
def analyze_full(candles, tf, pair):
    if len(candles)<30: return None
    session=is_valid_session()
    if not session["valid"]: return None
    trend=detect_trend(candles,tf)
    if trend["trend"]=="NEUTRE": return None
    if not trend.get("mm100_ok",True): return None
    direction="LONG" if trend["trend"]=="HAUSSIERE" else "SHORT"
    figure=detect_figure(candles,trend["trend"])
    if not figure["found"]: return None
    zone=detect_zone(candles,trend["trend"],pair)
    if not zone: return None
    psych=detect_psych_zones(candles,direction,pair)
    current=candles[-1]["close"]
    zone_lev=zone["level"]; prec=dp(pair)
    distance_pct=abs(current-zone_lev)/zone_lev*100
    if distance_pct>1.5: return None
    alert_level="EN_APPROCHE"
    if distance_pct<=0.3: alert_level="SUR_ZONE"
    candle_sig=detect_candle_confirmation(candles,zone_lev,direction,pair)
    sl_tp=calc_sl_tp(candles,trend,direction,pair)
    if candle_sig and sl_tp and sl_tp["ratio"]>=2.5: alert_level="CONFIRME"
    manque=[]
    if not candle_sig: manque.append("Bougie de confirmation (Englobante / Pin Bar / Marubozu)")
    if not sl_tp or sl_tp["ratio"]<2.5: manque.append(f"R/R >= 2.5")
    if distance_pct>0.3: manque.append(f"Contact avec la zone {zone_lev} (distance: {distance_pct:.2f}%)")
    score=0
    score+=20; score+=10 if trend.get("mm100_ok") else 0; score+=15
    score+=min(20,zone["quality"]//5)
    score+=15 if alert_level=="CONFIRME" else (8 if alert_level=="SUR_ZONE" else 3)
    score+=5 if sl_tp and sl_tp["ratio"]>=3 else 0
    score+=5 if zone["is_psych"] else 0
    score=min(100,score)
    strength="FORT" if score>=80 else ("MOYEN" if score>=60 else "FAIBLE")
    return {
        "pair":pair,"timeframe":tf,"direction":direction,
        "alert_level":alert_level,"strength":strength,"score":score,
        "distance_pct":round(distance_pct,3),"on_zone":distance_pct<=0.3,
        "manque":manque,"trend":trend["trend"],"trend_force":trend["force"],
        "mm100":trend.get("mm100"),"mm100_pos":trend.get("mm100_pos"),
        "mm100_slope":trend.get("mm100_slope"),
        "figure_type":figure["type"],"figure_candles":figure.get("nb_candles",0),
        "figure_reliability":figure.get("reliability","—"),
        "zone_high":figure["zone_high"],"zone_low":figure["zone_low"],
        "price_zone":zone["level"],"zone_type":zone["type"],
        "zone_touches":zone["touches"],"zone_quality":zone["quality"],
        "zone_psych":zone["is_psych"],"current_price":round(current,prec),
        "candle_type":candle_sig["type"] if candle_sig else None,
        "candle_emoji":candle_sig["emoji"] if candle_sig else None,
        "candle_strength":candle_sig["strength"] if candle_sig else None,
        "psych_levels":psych["psych_levels"],"danger_zones":psych["danger_zones"],
        "tp_fantome":psych["tp_fantome"],
        "entry":sl_tp["entry"] if sl_tp else None,
        "stop_loss":sl_tp["stop_loss"] if sl_tp else None,
        "take_profit":sl_tp["take_profit"] if sl_tp else None,
        "ratio":sl_tp["ratio"] if sl_tp else None,
        "atr":sl_tp["atr"] if sl_tp else None,
        "session":session["session"],
        "timestamp":datetime.now(timezone.utc).isoformat(),
    }

# ══════════════════════════════════════════════════════════════════════════════
# GRAPHIQUE MATPLOTLIB
# ══════════════════════════════════════════════════════════════════════════════
def generate_chart(candles, signal, pair):
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except: return None
    c=candles[-60:] if len(candles)>60 else candles; n=len(c)
    if n<10: return None
    fig,(ax,ax_vol)=plt.subplots(2,1,figsize=(14,8),
        gridspec_kw={"height_ratios":[4,1]},facecolor="#060810")
    for a in [ax,ax_vol]:
        a.set_facecolor("#0c0f18"); a.tick_params(colors="#475569",labelsize=8)
        for sp in a.spines.values(): sp.set_color("#1e2840")
        a.yaxis.tick_right()
    for i,candle in enumerate(c):
        o,h,l,cl=candle["open"],candle["high"],candle["low"],candle["close"]
        color="#00c878" if cl>=o else "#ef4444"
        ax.plot([i,i],[l,h],color=color,linewidth=0.9,zorder=2)
        bh=abs(cl-o) or (h-l)*0.015
        ax.add_patch(plt.Rectangle((i-0.38,min(o,cl)),0.76,bh,color=color,zorder=3,alpha=0.9))
    mm_vals=[]
    for i in range(n):
        sub=c[max(0,i-99):i+1]
        if len(sub)>=5: mm_vals.append((i,sum(x["close"] for x in sub)/len(sub)))
    if mm_vals: ax.plot([v[0] for v in mm_vals],[v[1] for v in mm_vals],
        color="#3b82f6",linewidth=2,label="MM100",zorder=4,alpha=0.85)
    zl=signal.get("price_zone")
    if zl:
        tol=zl*0.0015; ax.axhspan(zl-tol,zl+tol,color="#f59e0b",alpha=0.13,zorder=1)
        ax.axhline(zl,color="#f59e0b",linewidth=1.8,linestyle="--",alpha=0.85,zorder=4)
        ax.text(n+0.3,zl,f" ZONE {signal.get('zone_type','')} {zl}\n ({signal.get('zone_touches',0)} retests)",
            color="#f59e0b",fontsize=8,va="center",fontfamily="monospace")
    fh,fl=signal.get("zone_high"),signal.get("zone_low")
    if fh and fl:
        fs=max(0,n-16)
        rf=plt.Rectangle((fs-0.5,fl),n-fs+0.5,fh-fl,linewidth=2,edgecolor="#8b5cf6",
            facecolor=(0.545,0.361,0.965,0.04),linestyle="--",zorder=5)
        ax.add_patch(rf)
        ax.text(fs+0.5,fh+fh*0.0003,f" {signal.get('figure_type','')}",
            color="#8b5cf6",fontsize=9,fontfamily="monospace",fontweight="bold")
    entry=signal.get("entry"); sl=signal.get("stop_loss"); tp=signal.get("take_profit")
    direction=signal.get("direction","LONG")
    if entry:
        ax.axhline(entry,color="#ffffff",linewidth=1.3,alpha=0.9,zorder=6)
        ax.text(0.5,entry,f" ENTREE {entry}",color="#ffffff",fontsize=8,fontfamily="monospace",va="bottom")
    if sl:
        ax.axhline(sl,color="#ef4444",linewidth=1.3,linestyle="-.",alpha=0.9,zorder=6)
        ax.text(0.5,sl,f" SL {sl}",color="#ef4444",fontsize=8,fontfamily="monospace",va="top")
    if tp:
        ax.axhline(tp,color="#00e87a",linewidth=1.3,linestyle="-.",alpha=0.9,zorder=6)
        ax.text(0.5,tp,f" TP {tp}",color="#00e87a",fontsize=8,fontfamily="monospace",va="bottom")
    if entry and sl and tp:
        ax.axhspan(min(entry,sl),max(entry,sl),color="#ef4444",alpha=0.06,zorder=1)
        ax.axhspan(min(entry,tp),max(entry,tp),color="#00e87a",alpha=0.06,zorder=1)
    tpf=signal.get("tp_fantome")
    if tpf: ax.axhline(tpf,color="#f97316",linewidth=0.9,linestyle=":",alpha=0.8,zorder=6)
    for psy in signal.get("psych_levels",[])[:4]:
        ax.axhline(psy,color="#6a8aaa",linewidth=0.5,linestyle=":",alpha=0.4,zorder=2)
    al=signal.get("alert_level","")
    al_colors={"EN_APPROCHE":"#f59e0b","SUR_ZONE":"#f97316","CONFIRME":"#00e87a"}
    al_labels={"EN_APPROCHE":"EN APPROCHE","SUR_ZONE":"SUR LA ZONE — ATTENTE CONFIRMATION","CONFIRME":"SIGNAL CONFIRME"}
    alc=al_colors.get(al,"#c8d8e8"); all_=al_labels.get(al,"")
    last_c=c[-1]
    ax.annotate(f"{all_}\n{signal.get('candle_type','') or 'Attente bougie...'}",
        xy=(n-1,last_c["low"] if direction=="LONG" else last_c["high"]),
        xytext=(max(0,n-12),last_c["close"]*(0.9985 if direction=="LONG" else 1.0015)),
        arrowprops=dict(arrowstyle="->",color=alc,lw=1.5),
        color=alc,fontsize=8.5,ha="center",fontfamily="monospace",
        bbox=dict(boxstyle="round,pad=0.4",facecolor="#0d1117",edgecolor=alc+"88",alpha=0.9))
    manque=signal.get("manque",[])
    if manque:
        mtxt="POUR CONFIRMER:\n"+"".join(f"  - {m}\n" for m in manque)
        ax.text(0.02,0.04,mtxt,transform=ax.transAxes,color="#f5c842",fontsize=8,
            fontfamily="monospace",verticalalignment="bottom",
            bbox=dict(boxstyle="round,pad=0.5",facecolor="#0d1117",edgecolor="#f5c84255",alpha=0.9))
    for i,candle in enumerate(c):
        vol=candle.get("volume",0)
        if vol>0:
            col="#00c878" if candle["close"]>=candle["open"] else "#ef4444"
            ax_vol.bar(i,vol,color=col,alpha=0.5,width=0.7)
    al_emoji={"EN_APPROCHE":"[!]","SUR_ZONE":"[!!]","CONFIRME":"[OK]"}.get(al,"")
    fig.suptitle(f"{pair}  |  {signal.get('timeframe','?')}  |  {direction}  |  {al_emoji} {all_}  |  Score {signal.get('score',0)}/100",
        color="#e2e8f0",fontsize=12,fontweight="bold",fontfamily="monospace",y=0.99)
    ax.set_title(
        f"Tendance: {signal.get('trend','')} ({signal.get('trend_force',0)}%)  |  "
        f"MM100: {signal.get('mm100_pos','')}  |  "
        f"Figure: {signal.get('figure_type','')} {signal.get('figure_candles',0)} bougies  |  "
        f"Zone: {signal.get('price_zone','')} ({signal.get('zone_touches',0)} retests)  |  "
        f"Session: {signal.get('session','')}",
        color="#64748b",fontsize=8,fontfamily="monospace",pad=6)
    items=[
        mpatches.Patch(color="#3b82f6",label="MM100"),
        mpatches.Patch(color="#f59e0b",label=f"Zone {signal.get('zone_type','')} ({signal.get('zone_touches',0)} retests)"),
        mpatches.Patch(color="#8b5cf6",label=signal.get("figure_type","")),
        mpatches.Patch(color="#ffffff",label=f"Entree {entry or '--'}"),
        mpatches.Patch(color="#ef4444",label=f"SL {sl or '--'}"),
        mpatches.Patch(color="#00e87a",label=f"TP {tp or '--'}"),
    ]
    ax.legend(handles=items,loc="upper left",facecolor="#0c0f18",
        edgecolor="#1e2840",labelcolor="#cbd5e1",fontsize=7.5,ncol=2)
    ax.text(0.5,0.5,"SOPHIE TRADING",transform=ax.transAxes,
        color="#ffffff",alpha=0.025,fontsize=38,ha="center",va="center",
        fontweight="bold",rotation=15)
    step=max(1,n//10); tpos=list(range(0,n,step))
    tlbl=[c[i].get("time","")[5:13] if i<n else "" for i in tpos]
    ax.set_xticks(tpos); ax.set_xticklabels(tlbl,rotation=25,ha="right",color="#475569",fontsize=7.5)
    ax_vol.set_xticks([]); ax.set_xlim(-1,n+8)
    ax.grid(axis="y",color="#1e2840",linewidth=0.5,alpha=0.5)
    ax_vol.grid(axis="y",color="#1e2840",linewidth=0.4,alpha=0.3)
    ax_vol.set_xlim(-1,n+8)
    plt.tight_layout(rect=[0,0,1,0.97])
    buf=io.BytesIO()
    plt.savefig(buf,format="png",dpi=130,facecolor="#060810",bbox_inches="tight")
    plt.close(fig); buf.seek(0)
    return buf.read()

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"})
    except Exception as e: print(f"TG: {e}")

async def send_telegram_chart(signal, chart_bytes):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not chart_bytes: return
    pair=signal["pair"]; tf=signal["timeframe"]; direction=signal["direction"]
    al=signal["alert_level"]; score=signal["score"]
    al_emoji={"EN_APPROCHE":"[!]","SUR_ZONE":"[!!]","CONFIRME":"[OK]"}.get(al,"")
    al_txt={"EN_APPROCHE":"EN APPROCHE","SUR_ZONE":"SUR LA ZONE","CONFIRME":"SIGNAL CONFIRME"}.get(al,"")
    dir_e="LONG" if direction=="LONG" else "SHORT"
    caption=(
        f"{al_emoji} <b>Sophie Trading — {al_txt}</b>\n"
        f"{dir_e} <b>{pair}</b>  |  {tf}  |  Score {score}/100\n"
        f"{'━'*24}\n"
        f"  Tendance  : {signal.get('trend','?')} ({signal.get('trend_force',0)}%)\n"
        f"  MM100     : {signal.get('mm100_pos','?')}\n"
        f"  Figure    : {signal.get('figure_type','?')} {signal.get('figure_candles',0)} bougies [{signal.get('figure_reliability','?')}]\n"
        f"  Zone      : {signal.get('zone_type','?')} {signal.get('price_zone','?')} ({signal.get('zone_touches',0)} retests)\n"
        f"  Distance  : {signal.get('distance_pct','?')}%\n"
        f"  Session   : {signal.get('session','?')}\n"
    )
    if signal.get("candle_type"):
        caption+=f"  Bougie    : {signal.get('candle_type','')} (force {signal.get('candle_strength',0)}%)\n"
    if signal.get("entry"):
        caption+=(f"\n  Entree    : <code>{signal['entry']}</code>\n"
                  f"  SL        : <code>{signal['stop_loss']}</code>\n"
                  f"  TP        : <code>{signal['take_profit']}</code>\n"
                  f"  R/R       : <b>{signal['ratio']}:1</b>\n")
    if signal.get("tp_fantome"):
        caption+=f"  TP fantome: <code>{signal['tp_fantome']}</code>\n"
    manque=signal.get("manque",[])
    if manque:
        caption+=f"\n<b>Pour confirmation :</b>\n"
        for m in manque: caption+=f"  - {m}\n"
    caption+="\n<i>Verifiez sur TradingView avant d'entrer</i>"
    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            await cl.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id":TELEGRAM_CHAT_ID,"caption":caption,"parse_mode":"HTML"},
                files={"photo":("chart.png",chart_bytes,"image/png")})
    except Exception as e:
        print(f"TG chart: {e}")
        await send_telegram(caption)

# ── TWELVE DATA ───────────────────────────────────────────────────────────────
async def fetch_candles(pair, interval, outputsize=300):
    symbol=pair.replace("/",""); track_api_call(1)
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r=await cl.get("https://api.twelvedata.com/time_series",params={
                "symbol":symbol,"interval":interval,
                "outputsize":outputsize,"apikey":TWELVE_DATA_KEY})
            d=r.json()
            if "values" in d:
                return [{"open":float(v["open"]),"high":float(v["high"]),
                         "low":float(v["low"]),"close":float(v["close"]),
                         "volume":float(v.get("volume",0)),"time":v["datetime"]}
                        for v in reversed(d["values"])]
    except Exception as e: print(f"Candles {pair} {interval}: {e}")
    return []

async def fetch_price(pair):
    symbol=pair.replace("/",""); track_api_call(1)
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            r=await cl.get("https://api.twelvedata.com/price",
                params={"symbol":symbol,"apikey":TWELVE_DATA_KEY})
            d=r.json()
            if "price" in d: return {"pair":pair,"price":float(d["price"]),"ok":True}
    except: pass
    return {"pair":pair,"price":None,"ok":False}

# ── SCAN ENDPOINT ─────────────────────────────────────────────────────────────
@app.post("/api/scan")
async def run_scan(body: dict):
    tf=body.get("timeframe","4H"); pairs=body.get("pairs",MAJOR_PAIRS)
    if tf not in TF_INTERVAL:
        return JSONResponse({"error":f"TF invalide: {tf}"},status_code=400)
    interval=TF_INTERVAL[tf]; outputsize=TF_SIZE[tf]
    eco=await is_near_economic_event(); signals=[]
    for pair in pairs:
        try:
            candles=await fetch_candles(pair,interval,outputsize)
            await asyncio.sleep(0.3)
            if not candles: continue
            cache["candles_cache"][f"{pair}_{tf}"]=candles
            result=analyze_full(candles,tf,pair)
            if not result: continue
            signals.append(result)
            al=result["alert_level"]; zl=result["price_zone"]
            key=f"{pair}_{tf}_{zl}_{al}"
            if key not in cache["sent_alerts"]:
                cache["sent_alerts"].add(key)
                chart=generate_chart(candles,result,pair)
                await send_telegram_chart(result,chart)
                await asyncio.sleep(1)
        except Exception as e: print(f"  {pair} {tf}: {e}")
    signals.sort(key=lambda x:({"CONFIRME":0,"SUR_ZONE":1,"EN_APPROCHE":2}.get(x["alert_level"],3),-x["score"]))
    cache["signals"][tf]=signals
    cache["last_update"]=datetime.now(timezone.utc).isoformat()
    return JSONResponse({
        "signals":signals,"timeframe":tf,"pairs_scanned":len(pairs),
        "count":len(signals),"eco_blocked":eco["blocked"],"eco_events":eco["events"],
        "quota_used":cache["api_calls_today"],
        "timestamp":datetime.now(timezone.utc).isoformat(),
    })

# ── DEMO ENDPOINT ─────────────────────────────────────────────────────────────
@app.post("/api/demo")
async def run_demo(body: dict):
    """Mode demo quand quota API epuise — données réalistes simulées."""
    tf=body.get("timeframe","4H"); pairs=body.get("pairs",MAJOR_PAIRS)
    now=datetime.now(timezone.utc).isoformat()
    market={
        "EUR/USD":{"price":1.08542,"trend":"HAUSSIERE","score":72,"zone":1.0820,"touches":3,"figure":"CONSOLIDATION","nb_candles":45,"alert":"SUR_ZONE","direction":"LONG","entry":1.0854,"sl":1.0798,"tp":1.0994,"ratio":2.5},
        "GBP/USD":{"price":1.27341,"trend":"HAUSSIERE","score":85,"zone":1.2700,"touches":4,"figure":"TRIANGLE","nb_candles":68,"alert":"CONFIRME","direction":"LONG","entry":1.2734,"sl":1.2655,"tp":1.2932,"ratio":2.5},
        "USD/JPY":{"price":149.823,"trend":"BAISSIERE","score":61,"zone":150.500,"touches":2,"figure":"CONSOLIDATION","nb_candles":32,"alert":"EN_APPROCHE","direction":"SHORT","entry":149.823,"sl":151.200,"tp":146.373,"ratio":2.5},
        "USD/CHF":{"price":0.89124,"trend":"NEUTRE","score":28,"zone":None,"touches":1,"figure":None,"nb_candles":0,"alert":None,"direction":None,"entry":None,"sl":None,"tp":None,"ratio":None},
        "AUD/USD":{"price":0.64892,"trend":"BAISSIERE","score":78,"zone":0.6510,"touches":3,"figure":"TRIANGLE","nb_candles":55,"alert":"SUR_ZONE","direction":"SHORT","entry":0.6489,"sl":0.6542,"tp":0.6356,"ratio":2.5},
        "NZD/USD":{"price":0.59234,"trend":"NEUTRE","score":35,"zone":None,"touches":1,"figure":None,"nb_candles":0,"alert":None,"direction":None,"entry":None,"sl":None,"tp":None,"ratio":None},
        "USD/CAD":{"price":1.36521,"trend":"HAUSSIERE","score":55,"zone":1.3600,"touches":2,"figure":"CONSOLIDATION","nb_candles":38,"alert":"EN_APPROCHE","direction":"LONG","entry":1.3652,"sl":1.3580,"tp":1.3832,"ratio":2.5},
    }
    signals=[]; prices={}
    for pair in pairs:
        d=market.get(pair)
        if not d: continue
        prices[pair]=d["price"]
        if not d["alert"]: continue
        manque=[]
        if d["alert"]=="EN_APPROCHE": manque=["Contact direct avec la zone","Bougie de confirmation"]
        elif d["alert"]=="SUR_ZONE": manque=["Bougie de confirmation (Englobante / Pin Bar / Marubozu)"]
        strength="FORT" if d["score"]>=80 else ("MOYEN" if d["score"]>=60 else "FAIBLE")
        signals.append({
            "pair":pair,"timeframe":tf,"direction":d["direction"],
            "alert_level":d["alert"],"strength":strength,"score":d["score"],
            "current_price":d["price"],"trend":d["trend"],"trend_force":d["score"]-10,
            "mm100":round(d["price"]*0.998,5),"mm100_pos":"AU-DESSUS" if d["direction"]=="LONG" else "EN-DESSOUS",
            "figure_type":d["figure"],"figure_candles":d["nb_candles"],"figure_reliability":"HAUTE" if d["score"]>=80 else "MOYENNE",
            "price_zone":d["zone"],"zone_type":"SUPPORT" if d["direction"]=="LONG" else "RESISTANCE",
            "zone_touches":d["touches"],"zone_quality":min(100,d["touches"]*25),
            "zone_psych":False,"zone_high":round(d["zone"]*1.002,5) if d["zone"] else None,
            "zone_low":round(d["zone"]*0.998,5) if d["zone"] else None,
            "distance_pct":round(abs(d["price"]-d["zone"])/d["zone"]*100,3) if d["zone"] else 0,
            "entry":d["entry"],"stop_loss":d["sl"],"take_profit":d["tp"],"ratio":d["ratio"],
            "atr":round(d["price"]*0.003,5),
            "candle_type":"ENGLOBANTE" if d["alert"]=="CONFIRME" else None,
            "candle_emoji":"ENG" if d["alert"]=="CONFIRME" else None,
            "candle_strength":92 if d["alert"]=="CONFIRME" else None,
            "psych_levels":[round(d["price"]*x,5) for x in [1.01,1.02,1.03]] if d["price"] else [],
            "danger_zones":[round(d["price"]*1.015,5)] if d["price"] else [],
            "tp_fantome":round(d["tp"]*0.995,5) if d["tp"] else None,
            "manque":manque,"session":"LONDON","timestamp":now,"demo":True,
        })
    signals.sort(key=lambda x:({"CONFIRME":0,"SUR_ZONE":1,"EN_APPROCHE":2}.get(x["alert_level"],3),-x["score"]))
    cache["signals"][tf]=signals; cache["prices"].update(prices)
    return JSONResponse({"signals":signals,"timeframe":tf,"pairs_scanned":len(pairs),
        "count":len(signals),"eco_blocked":False,"eco_events":[],"timestamp":now,
        "demo_mode":True,"message":"MODE DEMO — Quota API epuise. Données simulées réalistes."})

# ── REFRESH PRIX (5 min — majeures uniquement) ────────────────────────────────
async def refresh_prices():
    for r in await asyncio.gather(*[fetch_price(p) for p in MAJOR_PAIRS],return_exceptions=True):
        if isinstance(r,dict) and r.get("ok"):
            cache["prices"][r["pair"]]=r["price"]
    cache["last_update"]=datetime.now(timezone.utc).isoformat()

async def scheduler():
    while True:
        try: await refresh_prices()
        except Exception as e: print(f"Scheduler: {e}")
        await asyncio.sleep(1800)  # 30 minutes — économie quota maximale

# ── STARTUP ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler())
    asyncio.create_task(send_startup_message())

async def send_startup_message():
    await asyncio.sleep(3)
    now=datetime.now(timezone.utc); hp=(now.hour+2)%24
    session="LONDON" if 9<=hp<=17 else ("NEW YORK" if hp<=21 else "HORS SESSION")
    window="ACTIVE (8h-21h)" if 8<=hp<=21 else f"INACTIVE ({hp}h Paris)"
    msg=(
        f"Sophie Trading — Double Structure v7.1\n"
        f"{'='*30}\n"
        f"Serveur en ligne · {now.strftime('%d/%m/%Y %H:%M')} UTC\n"
        f"Session actuelle : {session}\n"
        f"Fenetre de trading : {window}\n\n"
        f"Algorithme configure :\n"
        f"  - 26 paires (7 maj + 19 min)\n"
        f"  - 4 timeframes : 4H · 2H · 1H · 30min\n"
        f"  - 300 bougies par TF (4H=50j · 1H=12j)\n"
        f"  - 6 etapes Double Structure\n"
        f"  - Triangle>=50 bougies | Consolidation>=30\n"
        f"  - 3 niveaux : Approche · Zone · Confirme\n"
        f"  - Refresh auto prix : toutes les 30min\n"
        f"  - Quota API : 800 req/jour (plan gratuit)\n"
        f"  - Consommation auto : ~336 req/jour\n"
        f"  - Scans manuels restants : ~33/jour\n\n"
        f"Rappel regles cles :\n"
        f"  OK Zone >= 2 retests\n"
        f"  OK R/R minimum 2.5:1\n"
        f"  OK Bougie de confirmation obligatoire\n"
        f"  OK Entrees entre 8h et 21h (Paris)\n"
        f"  X  Jamais entrer sur bougie en cours\n"
        f"  X  Reintegration zone = signal invalide\n\n"
        f"Cliquez SCANNER sur le dashboard.\n"
        f"Bonne session de trading !"
    )
    await send_telegram(msg)

# ── API ROUTES ────────────────────────────────────────────────────────────────
@app.get("/api/prices")
async def get_prices():
    return JSONResponse({"prices":cache["prices"],"last_update":cache["last_update"]})

@app.get("/api/signals")
async def get_signals():
    all_sigs=[]
    for tf_sigs in cache["signals"].values(): all_sigs.extend(tf_sigs)
    all_sigs.sort(key=lambda x:({"CONFIRME":0,"SUR_ZONE":1,"EN_APPROCHE":2}.get(x.get("alert_level"),3),-x["score"]))
    hp=(datetime.now(timezone.utc).hour+2)%24
    return JSONResponse({"signals":all_sigs,"last_update":cache["last_update"],
                         "trading_window_active":8<=hp<=21,"count":len(all_sigs)})

@app.get("/api/status")
async def get_status():
    quota_used=cache.get("api_calls_today",0)
    return JSONResponse({
        "status":"online","version":"7.1-double-structure",
        "last_update":cache["last_update"],
        "telegram_ok":bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "sent_alerts":len(cache["sent_alerts"]),
        "quota_used":quota_used,"quota_limit":800,
        "quota_pct":round(quota_used/800*100,1),
        "quota_status":"OK" if quota_used<600 else ("ATTENTION" if quota_used<750 else "CRITIQUE"),
    })

@app.get("/",response_class=HTMLResponse)
async def root():
    with open("index.html","r",encoding="utf-8") as f: return f.read()

if __name__=="__main__":
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT",8000)),reload=False)
