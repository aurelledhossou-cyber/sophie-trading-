import os
import asyncio
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Sophie Trading API v2 — Professional")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWELVE_DATA_KEY  = os.getenv("TWELVE_DATA_KEY", "0a71d306b7f64336950805189681a0a2")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ALL_PAIRS = [
    "EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD","NZD/USD","USD/CAD",
    "EUR/GBP","EUR/JPY","EUR/CHF","EUR/AUD","EUR/NZD",
    "GBP/JPY","GBP/CHF","GBP/AUD","GBP/NZD",
    "AUD/JPY","AUD/NZD","AUD/CHF","AUD/CAD",
    "NZD/JPY","NZD/CHF","NZD/CAD","CAD/JPY","CAD/CHF","CHF/JPY"
]

cache = {
    "prices": {}, "candles_4h": {}, "candles_30min": {},
    "signals": [], "last_update": None, "last_notification": None,
}

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            })
    except Exception as e:
        print(f"Telegram error: {e}")

# ── TWELVE DATA ───────────────────────────────────────────────────────────────
async def fetch_candles(pair: str, interval: str, outputsize: int = 60) -> list:
    symbol = pair.replace("/", "")
    url = "https://api.twelvedata.com/time_series"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={
                "symbol": symbol, "interval": interval,
                "outputsize": outputsize, "apikey": TWELVE_DATA_KEY
            })
            data = r.json()
            if "values" in data:
                candles = []
                for v in reversed(data["values"]):
                    candles.append({
                        "open":   float(v["open"]),
                        "high":   float(v["high"]),
                        "low":    float(v["low"]),
                        "close":  float(v["close"]),
                        "volume": float(v.get("volume", 0)),
                        "time":   v["datetime"]
                    })
                return candles
    except Exception as e:
        print(f"Candles error {pair} {interval}: {e}")
    return []

async def fetch_price(pair: str) -> dict:
    symbol = pair.replace("/", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.twelvedata.com/price",
                params={"symbol": symbol, "apikey": TWELVE_DATA_KEY})
            data = r.json()
            if "price" in data:
                return {"pair": pair, "price": float(data["price"]), "ok": True}
    except Exception as e:
        print(f"Price error {pair}: {e}")
    return {"pair": pair, "price": None, "ok": False}

# ══════════════════════════════════════════════════════════════════════════════
# ── MOTEUR D'ANALYSE PROFESSIONNEL ───────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def detect_dow_trend(candles: list, lookback: int = 100) -> dict:
    """
    Dow Theory professionnelle sur 100 dernières bougies 4H.
    Pivots confirmés par 5 bougies de chaque côté pour fiabilité maximale.
    Analyse HH+HL (haussier) ou LH+LL (baissier) sur toute la structure.
    """
    # On travaille sur les 100 dernières bougies minimum
    c = candles[-lookback:] if len(candles) >= lookback else candles
    if len(c) < 15:
        return {"trend": "NEUTRE", "strength": 0, "pivots": []}

    # Identifier les pivots avec confirmation 5 bougies de chaque côté
    # (plus strict = pivots de structure réels, pas du bruit)
    pivots = []
    for i in range(5, len(c) - 5):
        # Swing High : high strictement supérieur aux 5 bougies de chaque côté
        if (c[i]["high"] > c[i-1]["high"] and c[i]["high"] > c[i-2]["high"] and
            c[i]["high"] > c[i-3]["high"] and c[i]["high"] > c[i-4]["high"] and
            c[i]["high"] > c[i-5]["high"] and c[i]["high"] > c[i+1]["high"] and
            c[i]["high"] > c[i+2]["high"] and c[i]["high"] > c[i+3]["high"] and
            c[i]["high"] > c[i+4]["high"] and c[i]["high"] > c[i+5]["high"]):
            pivots.append({"type": "SH", "price": c[i]["high"], "idx": i, "time": c[i]["time"]})
        # Swing Low : low strictement inférieur aux 5 bougies de chaque côté
        if (c[i]["low"] < c[i-1]["low"] and c[i]["low"] < c[i-2]["low"] and
            c[i]["low"] < c[i-3]["low"] and c[i]["low"] < c[i-4]["low"] and
            c[i]["low"] < c[i-5]["low"] and c[i]["low"] < c[i+1]["low"] and
            c[i]["low"] < c[i+2]["low"] and c[i]["low"] < c[i+3]["low"] and
            c[i]["low"] < c[i+4]["low"] and c[i]["low"] < c[i+5]["low"]):
            pivots.append({"type": "SL", "price": c[i]["low"], "idx": i, "time": c[i]["time"]})

    if len(pivots) < 4:
        return {"trend": "NEUTRE", "strength": 0, "pivots": pivots}

    # Analyser les 4 derniers pivots pour Dow
    recent = sorted(pivots, key=lambda x: x["idx"])[-6:]
    highs  = [p for p in recent if p["type"] == "SH"]
    lows   = [p for p in recent if p["type"] == "SL"]

    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "NEUTRE", "strength": 0, "pivots": pivots}

    # HH + HL = haussier
    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"]  > lows[-2]["price"]
    # LH + LL = baissier
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"]  < lows[-2]["price"]

    # Force de la tendance (0-100)
    if hh and hl:
        # Mesure l'amplitude des HH et HL
        hh_amp = (highs[-1]["price"] - highs[-2]["price"]) / highs[-2]["price"] * 100
        hl_amp = (lows[-1]["price"]  - lows[-2]["price"])  / lows[-2]["price"]  * 100
        strength = min(100, int((hh_amp + hl_amp) * 500))
        return {"trend": "HAUSSIÈRE", "strength": max(40, strength), "pivots": pivots,
                "last_high": highs[-1]["price"], "last_low": lows[-1]["price"],
                "prev_high": highs[-2]["price"], "prev_low": lows[-2]["price"]}
    elif lh and ll:
        lh_amp = (highs[-2]["price"] - highs[-1]["price"]) / highs[-2]["price"] * 100
        ll_amp = (lows[-2]["price"]  - lows[-1]["price"])  / lows[-2]["price"]  * 100
        strength = min(100, int((lh_amp + ll_amp) * 500))
        return {"trend": "BAISSIÈRE", "strength": max(40, strength), "pivots": pivots,
                "last_high": highs[-1]["price"], "last_low": lows[-1]["price"],
                "prev_high": highs[-2]["price"], "prev_low": lows[-2]["price"]}

    return {"trend": "NEUTRE", "strength": 0, "pivots": pivots}


def detect_key_zones(candles: list, trend: str) -> list:
    """
    Détecte les zones S/R de qualité professionnelle :
    - Zone testée plusieurs fois = plus forte
    - Zone avec rejets clairs (mèches)
    - Zone proche d'un niveau psychologique = bonus
    """
    if len(candles) < 20:
        return []

    zones = []
    c = candles[-50:] if len(candles) >= 50 else candles

    # Trouver les zones de congestion (prix revient plusieurs fois)
    for i in range(5, len(c) - 5):
        level = c[i]["high"] if c[i]["high"] > c[i]["close"] else c[i]["low"]
        tolerance = level * 0.0015  # 0.15% de tolérance

        # Compter combien de fois le prix a touché ce niveau
        touches = 0
        rejections = 0
        for j in range(len(c)):
            if j == i:
                continue
            # Touch sur high
            if abs(c[j]["high"] - level) < tolerance:
                touches += 1
                # Rejet = mèche longue (> 60% de la bougie totale)
                body = abs(c[j]["close"] - c[j]["open"])
                wick = c[j]["high"] - max(c[j]["open"], c[j]["close"])
                if body > 0 and wick / (c[j]["high"] - c[j]["low"] + 0.0000001) > 0.4:
                    rejections += 1
            # Touch sur low
            if abs(c[j]["low"] - level) < tolerance:
                touches += 1
                wick = min(c[j]["open"], c[j]["close"]) - c[j]["low"]
                body = abs(c[j]["close"] - c[j]["open"])
                if body > 0 and wick / (c[j]["high"] - c[j]["low"] + 0.0000001) > 0.4:
                    rejections += 1

        if touches >= 2:
            # Vérifier si niveau psychologique
            round_base = 1 if level > 10 else (0.1 if level > 1 else 0.01)
            nearest_round = round(level / round_base) * round_base
            is_psych = abs(level - nearest_round) / (level + 0.0000001) < 0.003

            quality = min(100, touches * 20 + rejections * 15 + (20 if is_psych else 0))
            zone_type = "RÉSISTANCE" if trend == "HAUSSIÈRE" else "SUPPORT"

            zones.append({
                "level":      round(level, 5),
                "touches":    touches,
                "rejections": rejections,
                "is_psych":   is_psych,
                "quality":    quality,
                "type":       zone_type,
            })

    # Dédupliquer les zones proches
    zones_clean = []
    for z in sorted(zones, key=lambda x: -x["quality"]):
        tolerance = z["level"] * 0.002
        if not any(abs(z["level"] - zc["level"]) < tolerance for zc in zones_clean):
            zones_clean.append(z)

    return sorted(zones_clean, key=lambda x: -x["quality"])[:5]


def detect_entry_candle(candles_1h: list, zone_level: float, direction: str) -> dict:
    """
    Détecte les bougies de confirmation d'entrée professionnelles :
    1. Clôture de cassure
    2. Pin bar / rejet
    3. Englobante haussière/baissière
    4. Bougie de confirmation après cassure
    """
    if len(candles_1h) < 3:
        return {"found": False}

    last   = candles_1h[-1]
    prev   = candles_1h[-2]
    prev2  = candles_1h[-3]
    tolerance = zone_level * 0.002

    body_last  = abs(last["close"]  - last["open"])
    body_prev  = abs(prev["close"]  - prev["open"])
    range_last = last["high"] - last["low"] + 0.0000001
    range_prev = prev["high"] - prev["low"] + 0.0000001

    signals_found = []

    # ── 1. CLÔTURE DE CASSURE ─────────────────────────────────────────────────
    if direction == "LONG":
        breakout = last["close"] > zone_level and prev["close"] <= zone_level
    else:
        breakout = last["close"] < zone_level and prev["close"] >= zone_level
    if breakout:
        signals_found.append({"type": "CASSURE", "strength": 85, "emoji": "⚡"})

    # ── 2. PIN BAR SUR ZONE ───────────────────────────────────────────────────
    if direction == "LONG":
        lower_wick = min(last["open"], last["close"]) - last["low"]
        upper_wick = last["high"] - max(last["open"], last["close"])
        is_pin_bar = (lower_wick > body_last * 2 and lower_wick > upper_wick * 2
                      and abs(last["low"] - zone_level) < tolerance * 3)
    else:
        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        is_pin_bar = (upper_wick > body_last * 2 and upper_wick > lower_wick * 2
                      and abs(last["high"] - zone_level) < tolerance * 3)
    if is_pin_bar:
        signals_found.append({"type": "PIN BAR", "strength": 90, "emoji": "📍"})

    # ── 3. ENGLOBANTE ─────────────────────────────────────────────────────────
    if direction == "LONG":
        engulfing = (last["close"] > last["open"] and  # haussière
                     last["open"]  < prev["close"] and  # ouvre sous clôture précédente
                     last["close"] > prev["open"]  and  # ferme au-dessus ouverture précédente
                     body_last > body_prev)
    else:
        engulfing = (last["close"] < last["open"] and  # baissière
                     last["open"]  > prev["close"] and
                     last["close"] < prev["open"]  and
                     body_last > body_prev)
    if engulfing:
        signals_found.append({"type": "ENGLOBANTE", "strength": 88, "emoji": "🕯️"})

    # ── 4. BOUGIE DE CONFIRMATION ─────────────────────────────────────────────
    # Cassure sur la bougie précédente + confirmation par clôture dans la même direction
    if direction == "LONG":
        prev_broke = prev["close"] > zone_level
        confirmed  = last["close"] > last["open"] and last["close"] > prev["close"]
    else:
        prev_broke = prev["close"] < zone_level
        confirmed  = last["close"] < last["open"] and last["close"] < prev["close"]
    if prev_broke and confirmed:
        signals_found.append({"type": "CONFIRMATION", "strength": 80, "emoji": "✅"})

    if not signals_found:
        return {"found": False}

    best = max(signals_found, key=lambda x: x["strength"])
    return {
        "found":        True,
        "type":         best["type"],
        "strength":     best["strength"],
        "emoji":        best["emoji"],
        "all_signals":  signals_found,
        "entry_price":  last["close"],
    }


def calculate_sl_tp(candles_1h: list, dow_4h: dict, direction: str, pair: str) -> dict:
    """
    SL : dernier HH ou LL de la structure Dow (règle 1 de votre stratégie)
    TP : R/R minimum 2.5 avec vérification du prochain S/R
    """
    last = candles_1h[-1]
    entry = last["close"]
    is_jpy = "JPY" in pair

    # SL basé sur le dernier pivot Dow
    if direction == "LONG":
        # SL sous le dernier LL (Low de structure)
        sl_level = dow_4h.get("last_low", entry)
        # Ajouter un petit buffer de 10% de l'ATR
        atr = sum(c["high"] - c["low"] for c in candles_1h[-14:]) / 14
        sl = sl_level - atr * 0.1
    else:
        # SL au-dessus du dernier HH
        sl_level = dow_4h.get("last_high", entry)
        atr = sum(c["high"] - c["low"] for c in candles_1h[-14:]) / 14
        sl = sl_level + atr * 0.1

    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return None

    # TP minimum R/R 2.5
    tp_distance = sl_distance * 2.5
    tp = entry + tp_distance if direction == "LONG" else entry - tp_distance
    ratio = round(tp_distance / sl_distance, 2)

    return {
        "entry":     round(entry, 3 if is_jpy else 5),
        "stop_loss": round(sl,    3 if is_jpy else 5),
        "take_profit": round(tp,  3 if is_jpy else 5),
        "ratio":     ratio,
        "sl_distance": round(sl_distance, 5),
        "atr":       round(atr, 5),
    }


def check_trading_conditions(entry_candle: dict, sl_tp: dict) -> dict:
    """Vérifie toutes les conditions de trading professionnel"""
    conditions = []
    score = 0

    # Horaire 8h-21h Paris
    hour = (datetime.now(timezone.utc).hour + 2) % 24
    in_hours = 8 <= hour <= 21
    conditions.append({"check": "Horaire 8h-21h", "ok": in_hours, "weight": 15})
    if in_hours: score += 15

    # R/R minimum 2.5
    rr_ok = sl_tp["ratio"] >= 2.5
    conditions.append({"check": f"R/R ≥ 2.5 ({sl_tp['ratio']}:1)", "ok": rr_ok, "weight": 25})
    if rr_ok: score += 25

    # Signal de bougie trouvé
    conditions.append({"check": f"Signal bougie ({entry_candle['type']})", "ok": True, "weight": 35})
    score += 35

    # Force du signal
    sig_strong = entry_candle["strength"] >= 85
    conditions.append({"check": f"Force signal ({entry_candle['strength']}%)", "ok": sig_strong, "weight": 15})
    if sig_strong: score += 15

    # Multiple signaux = bonus
    multi = len(entry_candle.get("all_signals", [])) > 1
    conditions.append({"check": "Signaux multiples", "ok": multi, "weight": 10})
    if multi: score += 10

    return {
        "score":      min(100, score),
        "conditions": conditions,
        "tradeable":  score >= 50 and rr_ok and in_hours,
    }


# ── ANALYSE COMPLÈTE MULTI-TIMEFRAME ─────────────────────────────────────────
def analyze_pair_professional(candles_4h: list, candles_1h: list, pair: str) -> dict | None:
    """
    Analyse complète en 5 étapes :
    1. Tendance 4H (Dow Theory professionnelle)
    2. Zones S/R de qualité sur 4H
    3. Prix proche d'une zone de qualité
def analyze_pair_professional(candles_4h: list, candles_30min: list, pair: str) -> dict | None:
    """
    Analyse complète multi-timeframe :
    1. Tendance 4H — Dow Theory sur 100 bougies (pivots confirmés 5 bougies)
    2. Zones S/R de qualité sur 4H (touches + rejets + niveaux psy)
    3. Prix 30min proche d'une zone 4H
    4. Signal de bougie d'entrée sur 30min
    5. Validation des conditions (horaire, R/R ≥ 2.5, etc.)
    """
    if len(candles_4h) < 20 or len(candles_30min) < 10:
        return None

    # ── ÉTAPE 1 : Tendance 4H sur 100 bougies ────────────────────────────────
    dow_4h = detect_dow_trend(candles_4h, lookback=100)
    if dow_4h["trend"] == "NEUTRE":
        return None
    direction = "LONG" if dow_4h["trend"] == "HAUSSIÈRE" else "SHORT"

    # ── ÉTAPE 2 : Zones S/R sur 4H ───────────────────────────────────────────
    zones_4h = detect_key_zones(candles_4h, dow_4h["trend"])
    if not zones_4h:
        return None

    # ── ÉTAPE 3 : Prix 30min proche d'une zone 4H ────────────────────────────
    current_price = candles_30min[-1]["close"]
    active_zone = None
    for zone in zones_4h:
        tolerance = zone["level"] * 0.003  # 0.3% de tolérance
        if abs(current_price - zone["level"]) < tolerance:
            active_zone = zone
            break

    if not active_zone:
        return None

    # ── ÉTAPE 4 : Signal de bougie d'entrée sur 30min ────────────────────────
    entry_candle = detect_entry_candle(candles_30min, active_zone["level"], direction)
    if not entry_candle["found"]:
        return None

    # ── ÉTAPE 5 : SL/TP + Validation ─────────────────────────────────────────
    sl_tp = calculate_sl_tp(candles_30min, dow_4h, direction, pair)
    if not sl_tp or sl_tp["ratio"] < 2.5:
        return None

    conditions = check_trading_conditions(entry_candle, sl_tp)
    if not conditions["tradeable"]:
        return None

    strength = "FORT" if conditions["score"] >= 80 else ("MOYEN" if conditions["score"] >= 60 else "FAIBLE")

    return {
        "pair":           pair,
        "direction":      direction,
        "trend_4h":       dow_4h["trend"],
        "trend_strength": dow_4h["strength"],
        "pivots_count":   len(dow_4h.get("pivots", [])),
        "zone":           active_zone,
        "entry_signal":   entry_candle["type"],
        "entry_emoji":    entry_candle["emoji"],
        "all_signals":    entry_candle.get("all_signals", []),
        "entry":          sl_tp["entry"],
        "stop_loss":      sl_tp["stop_loss"],
        "take_profit":    sl_tp["take_profit"],
        "ratio":          sl_tp["ratio"],
        "atr":            sl_tp["atr"],
        "score":          conditions["score"],
        "conditions":     conditions["conditions"],
        "strength":       strength,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "timeframe_trend": "4H",
        "timeframe_entry": "30min",
    }


# ── BACKGROUND WORKER ─────────────────────────────────────────────────────────
async def update_market_data():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(ALL_PAIRS)} pairs...")

    # Fetch prix en parallèle
    price_tasks = [fetch_price(p) for p in ALL_PAIRS]
    price_results = await asyncio.gather(*price_tasks, return_exceptions=True)
    for r in price_results:
        if isinstance(r, dict) and r.get("ok"):
            cache["prices"][r["pair"]] = r["price"]

    # Fetch bougies et analyser
    signals =
