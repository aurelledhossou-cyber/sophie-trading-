import os
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Sophie Trading — Double Structure v6.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TWELVE_DATA_KEY  = os.getenv("TWELVE_DATA_KEY", "0a71d306b7f64336950805189681a0a2")
# matplotlib importé dynamiquement dans generate_chart()
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

cache = {
    "prices":      {},
    "signals":     {},   # {tf: [signals]}
    "last_update": None,
    "last_notification": None,
}

# ── ANNONCES ÉCONOMIQUES ──────────────────────────────────────────────────────
async def is_near_economic_event() -> dict:
    now = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.twelvedata.com/economic_calendar",
                params={
                    "apikey": TWELVE_DATA_KEY,
                    "start_date": (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                    "end_date":   (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                    "importance": "high",
                }
            )
            data = r.json()
            events = data.get("result", {}).get("list", [])
            if events:
                return {"blocked": True, "events": [e.get("event","") for e in events]}
    except Exception as e:
        print(f"Calendar: {e}")
    return {"blocked": False, "events": []}

# ── SESSION ───────────────────────────────────────────────────────────────────
def is_valid_session() -> dict:
    hp      = (datetime.now(timezone.utc).hour + 2) % 24
    weekday = datetime.now(timezone.utc).weekday()
    if weekday >= 5:
        return {"valid": False, "reason": "Week-end"}
    if hp < 8 or hp > 21:
        return {"valid": False, "reason": f"Hors 8h-21h ({hp}h)"}
    return {"valid": True, "hour": hp,
            "session": "LONDON" if hp < 17 else "NEW YORK"}

# ── UTILITAIRES ───────────────────────────────────────────────────────────────
def dp(pair): return 3 if "JPY" in pair else 5

def calc_mm100(candles):
    if len(candles) < 100: return None
    return sum(c["close"] for c in candles[-100:]) / 100

def find_pivots(candles, n=3):
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        if all(candles[i]["high"] > candles[i+k]["high"] for k in range(-n,n+1) if k!=0):
            highs.append({"price": candles[i]["high"], "idx": i})
        if all(candles[i]["low"]  < candles[i+k]["low"]  for k in range(-n,n+1) if k!=0):
            lows.append( {"price": candles[i]["low"],  "idx": i})
    return highs, lows

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1 — TENDANCE DOW SUR LE TF SÉLECTIONNÉ
# ══════════════════════════════════════════════════════════════════════════════
def detect_trend(candles, tf):
    if len(candles) < 20:
        return {"trend": "NEUTRE", "tf": tf}

    mm100       = calc_mm100(candles)
    highs, lows = find_pivots(candles, n=3)

    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "NEUTRE", "tf": tf, "mm100": mm100}

    sh, sl = highs[-2:], lows[-2:]
    hh = sh[1]["price"] > sh[0]["price"]
    hl = sl[1]["price"] > sl[0]["price"]
    lh = sh[1]["price"] < sh[0]["price"]
    ll = sl[1]["price"] < sl[0]["price"]

    if   hh and hl: trend = "HAUSSIÈRE"
    elif lh and ll: trend = "BAISSIÈRE"
    else:           return {"trend": "NEUTRE", "tf": tf, "mm100": mm100}

    # MM100
    mm100_ok, mm100_slope = True, None
    if mm100 and len(candles) >= 110:
        mm100_old   = sum(c["close"] for c in candles[-110:-10]) / 100
        mm100_slope = (mm100 - mm100_old) / mm100_old * 100
        mm100_flat  = abs(mm100_slope) < 0.05
        if mm100_flat:
            mm100_ok = True
        elif trend == "HAUSSIÈRE": mm100_ok = mm100_slope > 0
        else:                      mm100_ok = mm100_slope < 0

    amp = abs(sh[1]["price"]-sh[0]["price"])/sh[0]["price"]*100 + \
          abs(sl[1]["price"]-sl[0]["price"])/sl[0]["price"]*100
    force = min(100, int(amp * 300))

    last = candles[-1]["close"]
    return {
        "trend":       trend,
        "tf":          tf,
        "force":       force,
        "mm100":       round(mm100,5) if mm100 else None,
        "mm100_ok":    mm100_ok,
        "mm100_slope": round(mm100_slope,4) if mm100_slope else None,
        "mm100_pos":   "AU-DESSUS" if mm100 and last > mm100 else "EN-DESSOUS",
        "last_high":   sh[-1]["price"],
        "last_low":    sl[-1]["price"],
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 2 — FIGURE DE RETOURNEMENT EN FIN DE TENDANCE
# ══════════════════════════════════════════════════════════════════════════════
def detect_figure(candles, trend):
    if len(candles) < 25:
        return {"found": False}

    fig = candles[-15:]
    prev= candles[-40:-15]
    if len(prev) < 8:
        return {"found": False}

    fh = [c["high"] for c in fig]
    fl = [c["low"]  for c in fig]
    ph = [c["high"] for c in prev]
    pl = [c["low"]  for c in prev]

    # Vérifier qu'il y avait bien une tendance avant
    if trend == "BAISSIÈRE":
        trend_ok = max(ph[:5]) > max(ph[-5:])
    else:
        trend_ok = min(pl[:5]) < min(pl[-5:])
    if not trend_ok:
        return {"found": False}

    n         = len(fig)
    top_slope = (fh[-1] - fh[0]) / n
    bot_slope = (fl[-1] - fl[0]) / n
    range_pct = (max(fh) - min(fl)) / fig[-1]["close"] * 100

    is_tri  = top_slope < -0.000005 and bot_slope > 0.000005 and range_pct < 2.0
    is_cons = range_pct < 0.8

    if not is_tri and not is_cons:
        return {"found": False}

    return {
        "found":      True,
        "type":       "TRIANGLE" if is_tri else "CONSOLIDATION",
        "zone_high":  round(max(fh), 5),
        "zone_low":   round(min(fl), 5),
        "range_pct":  round(range_pct, 3),
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 3 — ZONE DE PRIX (≥2 retests ou top historique)
# ══════════════════════════════════════════════════════════════════════════════
def detect_zone(candles, trend, pair):
    if len(candles) < 30:
        return None

    highs, lows = find_pivots(candles[:-5], n=2)
    candidates  = lows if trend == "BAISSIÈRE" else highs
    if not candidates: return None

    best, best_score = None, 0
    for piv in candidates:
        level = piv["price"]
        tol   = level * 0.0015
        touches = rejections = 0
        for c in candles:
            if abs(c["high"]-level) < tol or abs(c["low"]-level) < tol:
                touches += 1
                wick = (c["high"]-max(c["open"],c["close"])) / (c["high"]-c["low"]+0.0000001)
                if wick > 0.35: rejections += 1
        is_historic = piv["idx"] <= 5
        if touches < 2 and not is_historic: continue
        rbase  = 1 if level>10 else (0.1 if level>1 else 0.01)
        rround = round(level/rbase)*rbase
        is_psych = abs(level-rround)/level < 0.003
        score = touches*20 + rejections*15 + (20 if is_historic else 0) + (20 if is_psych else 0)
        if score > best_score:
            best_score = score
            best = {"level": round(level,dp(pair)), "touches": touches,
                    "rejections": rejections, "is_historic": is_historic,
                    "is_psych": is_psych, "quality": min(100,score),
                    "type": "SUPPORT" if trend=="BAISSIÈRE" else "RÉSISTANCE"}
    return best

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 4 — ZONES PSYCHOLOGIQUES + DANGER + TP FANTÔME
# ══════════════════════════════════════════════════════════════════════════════
def detect_psych_zones(candles, direction, pair):
    last  = candles[-1]["close"]
    rbase = 1 if last>10 else (0.1 if last>1 else 0.01)
    precision = dp(pair)

    psych = []
    start = round((last - last*0.05) / rbase) * rbase
    for i in range(20):
        v = round(start + i*rbase, precision)
        if (direction=="LONG" and v > last) or (direction=="SHORT" and v < last):
            psych.append(v)
        if len(psych) >= 5: break

    highs, lows = find_pivots(candles[:-5], n=2)
    danger = []
    pivs = highs if direction=="LONG" else lows
    for p in pivs[-10:]:
        if direction=="LONG"  and p["price"] > last: danger.append(round(p["price"],precision))
        elif direction=="SHORT" and p["price"] < last: danger.append(round(p["price"],precision))

    return {
        "psych_levels": sorted(psych[:4]),
        "danger_zones": sorted(danger[:3]),
        "tp_fantome":   psych[0] if psych else None,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 5 — CONFIRMATION BOUGIE JAPONAISE À FORTE PROBABILITÉ
# + Cassure zone + SL/TP
# ══════════════════════════════════════════════════════════════════════════════
def detect_candle_confirmation(candles, zone_level, direction, pair):
    """
    Bougies japonaises à forte probabilité :
    1. ENGLOBANTE haussière/baissière  (corps englobant la bougie précédente)
    2. PIN BAR (mèche ≥ 2x le corps, rejet clair)
    3. MARUBOZU (bougie pleine, corps > 80%)
    4. CLÔTURE DE CASSURE (corps > 60%, casse la zone)
    5. MARTEAU / ÉTOILE FILANTE (sur zone)
    """
    if len(candles) < 3: return None

    last  = candles[-1]
    prev  = candles[-2]
    prev2 = candles[-3]

    body_last  = abs(last["close"]  - last["open"])
    body_prev  = abs(prev["close"]  - prev["open"])
    range_last = last["high"] - last["low"] + 0.0000001
    range_prev = prev["high"] - prev["low"] + 0.0000001
    body_ratio = body_last / range_last

    upper_wick = last["high"] - max(last["open"],last["close"])
    lower_wick = min(last["open"],last["close"]) - last["low"]

    signals = []

    # ── ENGLOBANTE ──────────────────────────────────────────────────────────
    if direction == "LONG":
        engulf = (last["close"] > last["open"] and
                  last["open"]  < prev["close"] and
                  last["close"] > prev["open"]  and
                  body_last > body_prev * 1.1)
    else:
        engulf = (last["close"] < last["open"] and
                  last["open"]  > prev["close"] and
                  last["close"] < prev["open"]  and
                  body_last > body_prev * 1.1)
    if engulf:
        signals.append({"type":"ENGLOBANTE","strength":92,"emoji":"🕯️"})

    # ── PIN BAR ──────────────────────────────────────────────────────────────
    tol = zone_level * 0.003
    if direction == "LONG":
        pin = (lower_wick > body_last * 2.0 and
               lower_wick > upper_wick * 2.5 and
               abs(last["low"] - zone_level) < tol * 3)
    else:
        pin = (upper_wick > body_last * 2.0 and
               upper_wick > lower_wick * 2.5 and
               abs(last["high"] - zone_level) < tol * 3)
    if pin:
        signals.append({"type":"PIN BAR","strength":90,"emoji":"📍"})

    # ── MARTEAU / ÉTOILE FILANTE ─────────────────────────────────────────────
    if direction == "LONG":
        hammer = (lower_wick > body_last * 1.5 and body_ratio > 0.25 and
                  last["close"] > last["open"])
        if hammer: signals.append({"type":"MARTEAU","strength":82,"emoji":"🔨"})
    else:
        shooting = (upper_wick > body_last * 1.5 and body_ratio > 0.25 and
                    last["close"] < last["open"])
        if shooting: signals.append({"type":"ÉTOILE FILE","strength":82,"emoji":"🌠"})

    # ── MARUBOZU ─────────────────────────────────────────────────────────────
    maru = (body_ratio > 0.82 and
            ((direction=="LONG" and last["close"]>last["open"]) or
             (direction=="SHORT" and last["close"]<last["open"])))
    if maru:
        signals.append({"type":"MARUBOZU","strength":88,"emoji":"💪"})

    # ── CLÔTURE DE CASSURE ───────────────────────────────────────────────────
    if direction == "LONG":
        cass = (last["close"] > zone_level and
                last["open"]  < zone_level * 1.003 and
                body_ratio    > 0.55 and
                last["close"] > last["open"])
    else:
        cass = (last["close"] < zone_level and
                last["open"]  > zone_level * 0.997 and
                body_ratio    > 0.55 and
                last["close"] < last["open"])
    if cass:
        signals.append({"type":"CASSURE","strength":85,"emoji":"⚡"})

    if not signals:
        return None

    best = max(signals, key=lambda x: x["strength"])
    return {
        "found":       True,
        "type":        best["type"],
        "strength":    best["strength"],
        "emoji":       best["emoji"],
        "all_signals": signals,
        "body_ratio":  round(body_ratio, 2),
    }

def calc_sl_tp(candles, trend_data, direction, zone, pair):
    last  = candles[-1]
    entry = last["close"]
    prec  = dp(pair)
    atr   = sum(c["high"]-c["low"] for c in candles[-14:]) / min(14,len(candles))

    if direction == "LONG":
        sl_base = trend_data.get("last_low", entry - atr*2)
        mm100   = trend_data.get("mm100")
        if mm100 and sl_base < mm100 < entry:
            sl = mm100 - atr*0.3
        else:
            sl = sl_base - atr*0.15
        sl = min(sl, entry - atr*1.2)
    else:
        sl_base = trend_data.get("last_high", entry + atr*2)
        mm100   = trend_data.get("mm100")
        if mm100 and entry < mm100 < sl_base:
            sl = mm100 + atr*0.3
        else:
            sl = sl_base + atr*0.15
        sl = max(sl, entry + atr*1.2)

    sl_dist = abs(entry - sl)
    if sl_dist < atr*0.3: return None

    tp_dist = sl_dist * 2.5
    tp = entry + tp_dist if direction=="LONG" else entry - tp_dist
    ratio = round(tp_dist/sl_dist, 2)

    return {"entry": round(entry,prec), "stop_loss": round(sl,prec),
            "take_profit": round(tp,prec), "ratio": ratio, "atr": round(atr,prec)}

# ══════════════════════════════════════════════════════════════════════════════
# MOTEUR PRINCIPAL — 6 ÉTAPES SUR LE TF SÉLECTIONNÉ
# ══════════════════════════════════════════════════════════════════════════════
def analyze_on_timeframe(candles, tf, pair, eco_blocked):
    """
    Applique les 6 étapes Double Structure sur UN timeframe donné.
    Le même algorithme tourne sur H4, H2, H1 ou M30 selon la sélection.
    """
    if eco_blocked or len(candles) < 30:
        return None

    session = is_valid_session()
    if not session["valid"]:
        return None

    log = [f"✅ Session {session['session']} ({session['hour']}h) · TF {tf}"]

    # ÉTAPE 1 — Tendance sur le TF
    trend = detect_trend(candles, tf)
    if trend["trend"] == "NEUTRE":
        return None
    direction = "LONG" if trend["trend"] == "HAUSSIÈRE" else "SHORT"
    log.append(f"✅ Tendance {tf}: {trend['trend']} (force {trend['force']}%)")

    if not trend.get("mm100_ok", True):
        log.append(f"❌ MM100 contre-tendance → AVORTE")
        return None
    log.append(f"✅ MM100: {trend.get('mm100_pos','—')} ({trend.get('mm100','—')})")

    # ÉTAPE 2 — Figure de retournement en fin de tendance
    figure = detect_figure(candles, trend["trend"])
    if not figure["found"]:
        return None
    log.append(f"✅ Figure: {figure['type']} (range {figure['range_pct']}%)")

    # ÉTAPE 3 — Zone de prix ≥2 retests
    zone = detect_zone(candles, trend["trend"], pair)
    if not zone:
        return None
    log.append(f"✅ Zone {zone['type']}: {zone['level']} ({zone['touches']} retests, qualité {zone['quality']}%)")

    # ÉTAPE 4 — Zones psychologiques + danger
    psych = detect_psych_zones(candles, direction, pair)

    # ÉTAPE 5 — Confirmation bougie japonaise
    candle_sig = detect_candle_confirmation(candles, zone["level"], direction, pair)
    if not candle_sig:
        return None
    log.append(f"✅ Bougie: {candle_sig['type']} {candle_sig['emoji']} (force {candle_sig['strength']}%)")

    # ÉTAPE 5 — SL/TP
    sl_tp = calc_sl_tp(candles, trend, direction, zone, pair)
    if not sl_tp or sl_tp["ratio"] < 2.5:
        return None
    log.append(f"✅ R/R: {sl_tp['ratio']}:1 ≥ 2.5")

    # SCORE
    score = 0
    score += 20                               # Tendance
    score += 10 if trend.get("mm100_ok") else 0
    score += 15                               # Figure
    score += min(20, zone["quality"] // 5)   # Zone qualité
    score += candle_sig["strength"] // 10    # Bougie
    score += 5  if sl_tp["ratio"] >= 3 else 0
    score += 5  if zone["is_psych"]   else 0
    score += 5  if zone["is_historic"]else 0
    score  = min(100, score)

    strength = "FORT" if score >= 80 else ("MOYEN" if score >= 60 else "FAIBLE")

    return {
        "pair":           pair,
        "timeframe":      tf,
        "direction":      direction,
        "strength":       strength,
        "score":          score,
        "log":            log,

        "trend":          trend["trend"],
        "trend_force":    trend["force"],
        "mm100":          trend.get("mm100"),
        "mm100_pos":      trend.get("mm100_pos"),
        "mm100_slope":    trend.get("mm100_slope"),

        "figure_type":    figure["type"],
        "zone_high":      figure["zone_high"],
        "zone_low":       figure["zone_low"],

        "price_zone":     zone["level"],
        "zone_type":      zone["type"],
        "zone_touches":   zone["touches"],
        "zone_quality":   zone["quality"],
        "zone_psych":     zone["is_psych"],

        "candle_type":    candle_sig["type"],
        "candle_emoji":   candle_sig["emoji"],
        "candle_strength":candle_sig["strength"],
        "all_candles":    [s["type"] for s in candle_sig["all_signals"]],

        "psych_levels":   psych["psych_levels"],
        "danger_zones":   psych["danger_zones"],
        "tp_fantome":     psych["tp_fantome"],

        "entry":          sl_tp["entry"],
        "stop_loss":      sl_tp["stop_loss"],
        "take_profit":    sl_tp["take_profit"],
        "ratio":          sl_tp["ratio"],
        "atr":            sl_tp["atr"],

        "session":        session["session"],
        "timestamp":      datetime.now(timezone.utc).isoformat(),
    }

# ── TWELVE DATA ───────────────────────────────────────────────────────────────
TF_INTERVAL = {"4H":"4h","2H":"2h","1H":"1h","30min":"30min"}
TF_SIZE     = {"4H":110,"2H":80,"1H":60,"30min":50}

async def fetch_candles(pair, interval, outputsize=110):
    symbol = pair.replace("/","")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.twelvedata.com/time_series", params={
                "symbol":symbol,"interval":interval,
                "outputsize":outputsize,"apikey":TWELVE_DATA_KEY})
            d = r.json()
            if "values" in d:
                return [{"open":float(v["open"]),"high":float(v["high"]),
                         "low":float(v["low"]),"close":float(v["close"]),
                         "time":v["datetime"]} for v in reversed(d["values"])]
    except Exception as e:
        print(f"Candles {pair} {interval}: {e}")
    return []

async def fetch_price(pair):
    symbol = pair.replace("/","")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.twelvedata.com/price",
                params={"symbol":symbol,"apikey":TWELVE_DATA_KEY})
            d = r.json()
            if "price" in d:
                return {"pair":pair,"price":float(d["price"]),"ok":True}
    except: pass
    return {"pair":pair,"price":None,"ok":False}

# ── SCAN À LA DEMANDE (API endpoint) ──────────────────────────────────────────
@app.post("/api/scan")
async def run_scan(body: dict):
    """
    Endpoint appelé par le frontend quand on clique SCANNER.
    Reçoit : {"timeframe": "4H", "pairs": ["EUR/USD","GBP/USD",...]}
    Retourne les signaux pour ce TF et ces paires.
    """
    tf    = body.get("timeframe", "4H")
    pairs = body.get("pairs", MAJOR_PAIRS)

    if tf not in TF_INTERVAL:
        return JSONResponse({"error": f"TF invalide: {tf}"}, status_code=400)

    interval   = TF_INTERVAL[tf]
    outputsize = TF_SIZE[tf]

    eco = await is_near_economic_event()
    signals = []

    for pair in pairs:
        try:
            candles = await fetch_candles(pair, interval, outputsize)
            await asyncio.sleep(0.3)
            if not candles:
                continue
            # Stocker bougies pour génération graphique
            if "candles_cache" not in cache:
                cache["candles_cache"] = {}
            cache["candles_cache"][f"{pair}_{tf}"] = candles
            result = analyze_on_timeframe(candles, tf, pair, eco["blocked"])
            if result:
                signals.append(result)
                print(f"  ✅ {tf} {pair} {result['direction']} {result['strength']} {result['score']}")
        except Exception as e:
            print(f"  ❌ {tf} {pair}: {e}")

    signals.sort(key=lambda x: -x["score"])
    cache["signals"][tf] = signals

    # ── GÉNÉRATION GRAPHIQUE + ENVOI TELEGRAM ────────────────────────────────
    hp = (datetime.now(timezone.utc).hour + 2) % 24
    if 8 <= hp <= 21 and signals:
        last_n = cache.get("last_notification")
        if not last_n or (datetime.now(timezone.utc)-datetime.fromisoformat(last_n)).seconds > 1800:
            strong = [s for s in signals if s["strength"]=="FORT"]
            # Envoyer graphique pour chaque signal FORT (max 3)
            for sig in strong[:3]:
                candles_for_chart = cache.get("candles_cache", {}).get(
                    f"{sig['pair']}_{sig['timeframe']}", []
                )
                if candles_for_chart:
                    chart_bytes = generate_chart(candles_for_chart, sig, sig["pair"])
                    if chart_bytes:
                        await send_telegram_chart(sig, chart_bytes)
                        await asyncio.sleep(1)
                    else:
                        # Pas de graphique disponible — envoyer texte seul
                        pass
            if strong:
                msg  = f"🎯 <b>Sophie Trading — Double Structure</b>\n"
                msg += f"📊 TF: <b>{tf}</b> · {datetime.now().strftime('%H:%M')}\n"
                msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
                msg += f"🟢 <b>{len(strong)} signal(s) FORT(s)</b>\n\n"
                for s in strong[:4]:
                    e = "🟢" if s["direction"]=="LONG" else "🔴"
                    msg += f"{e} <b>{s['pair']}</b> — {s['direction']}\n"
                    msg += f"   {s['candle_emoji']} {s['candle_type']} · {s['figure_type']}\n"
                    msg += f"   Zone {s['zone_type']} ({s['zone_touches']} retests)\n"
                    msg += f"   MM100: {s['mm100_pos']}\n"
                    msg += f"   🎯 Entrée: <code>{s['entry']}</code>\n"
                    msg += f"   🛡️ SL: <code>{s['stop_loss']}</code>\n"
                    msg += f"   💰 TP: <code>{s['take_profit']}</code>\n"
                    if s.get("tp_fantome"):
                        msg += f"   ⚠️ TP fantôme: {s['tp_fantome']}\n"
                    msg += f"   R/R: <b>{s['ratio']}:1</b> · Score: {s['score']}/100\n\n"
                msg += "⚠️ Vérifiez sur TradingView avant d'entrer"
                await send_telegram(msg)
                cache["last_notification"] = datetime.now(timezone.utc).isoformat()

    return JSONResponse({
        "signals":    signals,
        "timeframe":  tf,
        "pairs_scanned": len(pairs),
        "count":      len(signals),
        "eco_blocked":eco["blocked"],
        "eco_events": eco["events"],
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })

# ── AUTO-REFRESH DES PRIX ──────────────────────────────────────────────────────
async def refresh_prices():
    for r in await asyncio.gather(*[fetch_price(p) for p in ALL_PAIRS], return_exceptions=True):
        if isinstance(r, dict) and r.get("ok"):
            cache["prices"][r["pair"]] = r["price"]
    cache["last_update"] = datetime.now(timezone.utc).isoformat()

async def scheduler():
    while True:
        try: await refresh_prices()
        except Exception as e: print(f"Scheduler: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler())

# ── API ROUTES ────────────────────────────────────────────────────────────────
@app.get("/api/prices")
async def get_prices():
    return JSONResponse({"prices": cache["prices"], "last_update": cache["last_update"]})

@app.get("/api/signals")
async def get_signals():
    all_signals = []
    for tf_sigs in cache["signals"].values():
        all_signals.extend(tf_sigs)
    all_signals.sort(key=lambda x: -x["score"])
    hp = (datetime.now(timezone.utc).hour + 2) % 24
    return JSONResponse({
        "signals": all_signals, "last_update": cache["last_update"],
        "trading_window_active": 8 <= hp <= 21, "hour_paris": hp,
        "count": len(all_signals)
    })

@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "status": "online", "version": "6.0-double-structure",
        "last_update": cache["last_update"],
        "prices_count": len(cache["prices"]),
        "telegram_ok": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
    })

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html","r",encoding="utf-8") as f: return f.read()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT",8000)), reload=False)

# ══════════════════════════════════════════════════════════════════════════════
# GRAPHIQUE MATPLOTLIB — GÉNÉRÉ ET ENVOYÉ SUR TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
import io
import math

def generate_chart(candles: list, signal: dict, pair: str) -> bytes | None:
    """
    Génère un graphique professionnel avec :
    - Bougies japonaises (OHLCV)
    - MM100 en bleu
    - Zone de prix en jaune (avec retests)
    - Figure de retournement encadrée en violet
    - Niveaux SL (rouge) / Entrée (blanc) / TP (vert)
    - Bougie de confirmation marquée
    - Zones psychologiques en tirets
    Retourne les bytes PNG de l'image.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyArrowPatch
        import numpy as np
    except ImportError:
        print("matplotlib non disponible")
        return None

    # Dernières 60 bougies max pour lisibilité
    c = candles[-60:] if len(candles) > 60 else candles
    n = len(c)
    if n < 10:
        return None

    # ── FIGURE & STYLE ────────────────────────────────────────────────────────
    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(14, 8),
        gridspec_kw={"height_ratios": [4, 1]},
        facecolor="#0a0c10"
    )
    for a in [ax, ax_vol]:
        a.set_facecolor("#0d1117")
        a.tick_params(colors="#3a5068", labelsize=8)
        a.spines["bottom"].set_color("#1a2a3a")
        a.spines["top"].set_color("#1a2a3a")
        a.spines["left"].set_color("#1a2a3a")
        a.spines["right"].set_color("#1a2a3a")
        a.yaxis.tick_right()

    xs = list(range(n))

    # ── BOUGIES ───────────────────────────────────────────────────────────────
    for i, candle in enumerate(c):
        o, h, l, cl = candle["open"], candle["high"], candle["low"], candle["close"]
        color  = "#00c878" if cl >= o else "#ff4d6d"
        # Mèche
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        # Corps
        body_bot = min(o, cl)
        body_h   = abs(cl - o) or (h - l) * 0.01
        rect = plt.Rectangle((i - 0.35, body_bot), 0.7, body_h,
                              color=color, zorder=3, alpha=0.9)
        ax.add_patch(rect)

    # ── MM100 ─────────────────────────────────────────────────────────────────
    mm_values = []
    for i in range(n):
        # MM100 calculée sur les bougies disponibles jusqu'à ce point
        start = max(0, i - 99)
        subset = c[start:i+1]
        if len(subset) >= 5:
            mm_values.append((i, sum(x["close"] for x in subset) / len(subset)))
    if mm_values:
        mm_xs = [v[0] for v in mm_values]
        mm_ys = [v[1] for v in mm_values]
        ax.plot(mm_xs, mm_ys, color="#00c4ff", linewidth=1.5,
                label="MM100", zorder=4, alpha=0.8)

    # ── ZONE DE PRIX (support/résistance) ────────────────────────────────────
    zone_level = signal.get("price_zone")
    if zone_level:
        tol = zone_level * 0.0015
        ax.axhspan(zone_level - tol, zone_level + tol,
                   color="#f5c842", alpha=0.15, zorder=1)
        ax.axhline(zone_level, color="#f5c842", linewidth=1.2,
                   linestyle="--", alpha=0.7, zorder=4)
        ax.text(n - 1, zone_level, f" ZONE {signal.get('zone_type','')}\n {zone_level}",
                color="#f5c842", fontsize=7, va="center",
                fontfamily="monospace")

    # ── FIGURE DE RETOURNEMENT (encadrée) ────────────────────────────────────
    fig_high = signal.get("zone_high")
    fig_low  = signal.get("zone_low")
    if fig_high and fig_low:
        fig_start = max(0, n - 16)
        rect_fig = plt.Rectangle(
            (fig_start - 0.5, fig_low),
            n - fig_start + 0.5,
            fig_high - fig_low,
            linewidth=1.5, edgecolor="#a78bff",
            facecolor="rgba(167,139,255,0.05)",
            linestyle="--", zorder=5
        )
        try:
            rect_fig.set_facecolor((0.655, 0.545, 1.0, 0.04))
            ax.add_patch(rect_fig)
        except: pass
        ax.text(fig_start, fig_high,
                f" {signal.get('figure_type','FIGURE')}",
                color="#a78bff", fontsize=8,
                fontfamily="monospace", va="bottom")

    # ── NIVEAUX ENTRÉE / SL / TP ──────────────────────────────────────────────
    entry = signal.get("entry")
    sl    = signal.get("stop_loss")
    tp    = signal.get("take_profit")

    if entry:
        ax.axhline(entry, color="#ffffff", linewidth=1.2,
                   linestyle="-", alpha=0.9, zorder=6)
        ax.text(0.5, entry, f" ENTRÉE {entry}",
                color="#ffffff", fontsize=7.5,
                fontfamily="monospace", va="bottom")
    if sl:
        ax.axhline(sl, color="#ff4d6d", linewidth=1.2,
                   linestyle="-.", alpha=0.9, zorder=6)
        ax.text(0.5, sl, f" SL {sl}",
                color="#ff4d6d", fontsize=7.5,
                fontfamily="monospace", va="top")
    if tp:
        ax.axhline(tp, color="#00ff9d", linewidth=1.2,
                   linestyle="-.", alpha=0.9, zorder=6)
        ax.text(0.5, tp, f" TP {tp}",
                color="#00ff9d", fontsize=7.5,
                fontfamily="monospace", va="bottom")

    # Zone SL/TP colorée
    if entry and sl and tp:
        direction = signal.get("direction","LONG")
        if direction == "LONG":
            ax.axhspan(sl, entry, color="#ff4d6d", alpha=0.06, zorder=1)
            ax.axhspan(entry, tp, color="#00ff9d", alpha=0.06, zorder=1)
        else:
            ax.axhspan(entry, sl, color="#ff4d6d", alpha=0.06, zorder=1)
            ax.axhspan(tp, entry, color="#00ff9d", alpha=0.06, zorder=1)

    # ── BOUGIE DE CONFIRMATION (marquée avec flèche) ──────────────────────────
    last_candle = c[-1]
    candle_emoji = signal.get("candle_emoji","⚡")
    candle_type  = signal.get("candle_type","")
    direction    = signal.get("direction","LONG")
    if direction == "LONG":
        arrow_y = last_candle["low"] * 0.9998
        ax.annotate(
            f"{candle_emoji} {candle_type}",
            xy=(n-1, last_candle["low"]),
            xytext=(n-1, arrow_y),
            arrowprops=dict(arrowstyle="->", color="#00ff9d", lw=1.5),
            color="#00ff9d", fontsize=8, ha="center",
            fontfamily="monospace"
        )
    else:
        arrow_y = last_candle["high"] * 1.0002
        ax.annotate(
            f"{candle_emoji} {candle_type}",
            xy=(n-1, last_candle["high"]),
            xytext=(n-1, arrow_y),
            arrowprops=dict(arrowstyle="->", color="#ff4d6d", lw=1.5),
            color="#ff4d6d", fontsize=8, ha="center",
            fontfamily="monospace"
        )

    # ── ZONES PSYCHOLOGIQUES (tirets fins) ───────────────────────────────────
    for psy in signal.get("psych_levels", [])[:3]:
        ax.axhline(psy, color="#6a8aaa", linewidth=0.6,
                   linestyle=":", alpha=0.5, zorder=2)
        ax.text(n * 0.02, psy, f" psy {psy}",
                color="#6a8aaa", fontsize=6.5,
                fontfamily="monospace", va="bottom", alpha=0.7)

    # ── TP FANTÔME ────────────────────────────────────────────────────────────
    tp_fantome = signal.get("tp_fantome")
    if tp_fantome:
        ax.axhline(tp_fantome, color="#ff7c5c", linewidth=0.8,
                   linestyle=":", alpha=0.6, zorder=2)
        ax.text(n * 0.5, tp_fantome, f" ⚠️ TP fantôme {tp_fantome}",
                color="#ff7c5c", fontsize=7,
                fontfamily="monospace", va="bottom", alpha=0.8)

    # ── VOLUMES ───────────────────────────────────────────────────────────────
    for i, candle in enumerate(c):
        vol = candle.get("volume", 0)
        if vol > 0:
            col = "#00c878" if candle["close"] >= candle["open"] else "#ff4d6d"
            ax_vol.bar(i, vol, color=col, alpha=0.5, width=0.7)
    ax_vol.set_ylabel("VOL", color="#3a5068", fontsize=7)

    # ── TITRE ─────────────────────────────────────────────────────────────────
    tf       = signal.get("timeframe","—")
    strength = signal.get("strength","—")
    score    = signal.get("score", 0)
    s_color  = "#00ff9d" if strength=="FORT" else "#f5c842" if strength=="MOYEN" else "#ff7c5c"
    d_color  = "#00ff9d" if direction=="LONG" else "#ff5c7c"

    fig.suptitle(
        f"{pair}  ·  {tf}  ·  {direction}  ·  {strength}  ({score}/100)  ·  R/R {signal.get('ratio','—')}:1",
        color="#e8f4ff", fontsize=12, fontweight="bold",
        fontfamily="monospace", y=0.98
    )

    # Sous-titre avec détails
    subtitle = (
        f"Tendance: {signal.get('trend','—')}  ·  "
        f"Figure: {signal.get('figure_type','—')}  ·  "
        f"Zone {signal.get('zone_type','—')}: {zone_level} ({signal.get('zone_touches',0)} retests)  ·  "
        f"MM100: {signal.get('mm100_pos','—')}  ·  "
        f"Session: {signal.get('session','—')}"
    )
    ax.set_title(subtitle, color="#6a8aaa", fontsize=7.5,
                 fontfamily="monospace", pad=6)

    # Légende
    legend_items = [
        mpatches.Patch(color="#00c4ff",  label="MM100"),
        mpatches.Patch(color="#f5c842",  label=f"Zone {signal.get('zone_type','')}"),
        mpatches.Patch(color="#a78bff",  label=signal.get("figure_type","")),
        mpatches.Patch(color="#ffffff",  label="Entrée"),
        mpatches.Patch(color="#ff4d6d",  label="Stop Loss"),
        mpatches.Patch(color="#00ff9d",  label="Take Profit"),
    ]
    ax.legend(handles=legend_items, loc="upper left",
              facecolor="#0d1117", edgecolor="#1a2a3a",
              labelcolor="#c8d8e8", fontsize=7.5)

    # Watermark
    ax.text(0.5, 0.5, "SOPHIE TRADING",
            transform=ax.transAxes,
            color="#ffffff", alpha=0.04,
            fontsize=32, ha="center", va="center",
            fontweight="bold", rotation=20)

    # Axe X — labels de temps simplifiés
    step = max(1, n // 10)
    tick_positions = list(range(0, n, step))
    tick_labels = []
    for idx in tick_positions:
        t = c[idx].get("time","")
        # Afficher seulement date + heure
        tick_labels.append(t[5:16] if len(t) >= 16 else t)
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=30, ha="right",
                       color="#3a5068", fontsize=7)
    ax_vol.set_xticks([])

    ax.set_xlim(-1, n + 1)
    ax.grid(axis="y", color="#1a2a3a", linewidth=0.5, alpha=0.5)
    ax_vol.grid(axis="y", color="#1a2a3a", linewidth=0.5, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    # ── EXPORT PNG ────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130,
                facecolor="#0a0c10", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def send_telegram_chart(signal: dict, chart_bytes: bytes):
    """Envoie le graphique PNG + les détails du signal sur Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not chart_bytes:
        return
    pair      = signal["pair"]
    tf        = signal["timeframe"]
    direction = signal["direction"]
    strength  = signal["strength"]
    score     = signal["score"]
    e_emoji   = "🟢" if direction == "LONG" else "🔴"
    caption   = (
        f"🎯 <b>Sophie Trading — Double Structure</b>\n"
        f"{e_emoji} <b>{pair}</b> · {tf} · {direction} · {strength} ({score}/100)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 {signal.get('figure_type','')} · Zone {signal.get('zone_type','')} "
        f"({signal.get('zone_touches',0)} retests)\n"
        f"📊 Tendance: {signal.get('trend','')} · MM100: {signal.get('mm100_pos','')}\n"
        f"{signal.get('candle_emoji','')} Bougie: {signal.get('candle_type','')}\n\n"
        f"🎯 Entrée:    <code>{signal.get('entry','')}</code>\n"
        f"🛡️ SL:       <code>{signal.get('stop_loss','')}</code>\n"
        f"💰 TP:       <code>{signal.get('take_profit','')}</code>\n"
        f"📈 R/R:      <b>{signal.get('ratio','')}:1</b>\n"
        f"⏱️ Session:  {signal.get('session','')}\n"
    )
    if signal.get("tp_fantome"):
        caption += f"⚠️ TP fantôme: {signal['tp_fantome']}\n"
    caption += "\n⚠️ <i>Vérifiez sur TradingView avant d'entrer</i>"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID,
                      "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("chart.png", chart_bytes, "image/png")}
            )
    except Exception as e:
        print(f"Telegram chart error: {e}")
        # Fallback — envoyer juste le texte
        await send_telegram(caption)
