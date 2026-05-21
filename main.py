import os
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Sophie Trading — Double Structure v4.0")
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

# Annonces économiques majeures (heure UTC) — bloquées 10min avant/après
# Format : (mois, jour, heure_utc, minute_utc, nom)
MAJOR_EVENTS = [
    # NFP — premier vendredi du mois à 13h30 UTC
    # BCE, Fed, CPI — dates fixes connues
    # On bloque par type d'événement récurrent
]

cache = {
    "prices":       {},
    "candles":      {},   # {pair: {"30min":[], "1h":[], "2h":[], "4h":[]}}
    "signals":      [],
    "last_update":  None,
    "last_notification": None,
}

# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 0 — VÉRIFICATION ANNONCES ÉCONOMIQUES ───────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
async def is_near_economic_event() -> dict:
    """
    Vérifie si on est dans les 10 minutes avant/après une annonce majeure.
    Utilise l'API Twelve Data pour récupérer le calendrier économique.
    Bloque : NFP, CPI, BCE, Fed, PIB, Emploi
    """
    now_utc = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.twelvedata.com/economic_calendar",
                params={
                    "apikey": TWELVE_DATA_KEY,
                    "start_date": (now_utc - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                    "end_date":   (now_utc + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                    "importance": "high",
                }
            )
            data = r.json()
            events = data.get("result", {}).get("list", [])
            if events:
                names = [e.get("event","") for e in events]
                return {"blocked": True, "events": names}
    except Exception as e:
        print(f"Calendar API error: {e}")
    return {"blocked": False, "events": []}


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 1 — TIMEFRAME CHECK (M30 → H4) ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
def is_valid_timeframe() -> dict:
    """
    Valide les conditions de marché selon l'horaire Paris.
    Sessions actives : London (9h-18h) et New York (14h-22h)
    Évite Asia (faible liquidité) et week-end.
    """
    now_paris = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)
    hour_paris = (now_paris.hour + 2) % 24
    weekday    = now_paris.weekday()  # 0=lundi, 6=dimanche

    if weekday >= 5:
        return {"valid": False, "reason": "WEEK-END — marchés fermés"}
    if hour_paris < 8 or hour_paris > 22:
        return {"valid": False, "reason": f"Hors session active ({hour_paris}h Paris)"}

    session = "LONDON" if 9 <= hour_paris <= 17 else ("NEW YORK" if 14 <= hour_paris <= 22 else "PRÉ-SESSION")
    return {"valid": True, "hour": hour_paris, "session": session}


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 2 — TENDANCE (DOW THEORY MULTI-TF) ─────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
def calculate_mm100(candles: list) -> float | None:
    """Calcule la Moyenne Mobile simple 100 périodes."""
    if len(candles) < 100:
        return None
    return sum(c["close"] for c in candles[-100:]) / 100

def detect_trend_dow(candles: list, tf_label: str) -> dict:
    """
    Dow Theory par timeframe :
    - Identifie les pivots de structure (SH/SL) avec confirmation 3 bougies
    - Combine avec MM100 pour valider la direction
    - Retourne : tendance, force, mm100, position prix vs MM100
    """
    if len(candles) < 30:
        return {"trend": "NEUTRE", "tf": tf_label}

    c = candles[-80:] if len(candles) >= 80 else candles

    # Calcul MM100 sur toutes les bougies disponibles
    mm100 = calculate_mm100(candles)

    # Pivots avec confirmation 3 bougies de chaque côté
    swing_highs, swing_lows = [], []
    for i in range(3, len(c) - 3):
        if (c[i]["high"] > c[i-1]["high"] and c[i]["high"] > c[i-2]["high"] and
            c[i]["high"] > c[i-3]["high"] and c[i]["high"] > c[i+1]["high"] and
            c[i]["high"] > c[i+2]["high"] and c[i]["high"] > c[i+3]["high"]):
            swing_highs.append({"price": c[i]["high"], "idx": i})
        if (c[i]["low"] < c[i-1]["low"] and c[i]["low"] < c[i-2]["low"] and
            c[i]["low"] < c[i-3]["low"] and c[i]["low"] < c[i+1]["low"] and
            c[i]["low"] < c[i+2]["low"] and c[i]["low"] < c[i+3]["low"]):
            swing_lows.append({"price": c[i]["low"], "idx": i})

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"trend": "NEUTRE", "tf": tf_label, "mm100": mm100}

    sh = swing_highs[-2:]
    sl = swing_lows[-2:]

    hh = sh[1]["price"] > sh[0]["price"]  # Higher High
    hl = sl[1]["price"] > sl[0]["price"]  # Higher Low
    lh = sh[1]["price"] < sh[0]["price"]  # Lower High
    ll = sl[1]["price"] < sl[0]["price"]  # Lower Low

    last_close = c[-1]["close"]

    # MM100 : plate si pente < 0.1%
    mm100_slope = None
    mm100_flat  = False
    if mm100 and len(candles) >= 110:
        mm100_old = sum(c_["close"] for c_ in candles[-110:-10]) / 100
        mm100_slope = (mm100 - mm100_old) / mm100_old * 100
        mm100_flat  = abs(mm100_slope) < 0.05

    # Position du prix vs MM100
    price_above_mm100 = (last_close > mm100) if mm100 else None

    if hh and hl:
        trend = "HAUSSIÈRE"
        # MM100 confirme si prix au-dessus ET MM100 haussière
        mm100_ok = (price_above_mm100 and (not mm100_flat or mm100_slope and mm100_slope > 0)) if mm100 else True
    elif lh and ll:
        trend = "BAISSIÈRE"
        mm100_ok = (not price_above_mm100 and (not mm100_flat or mm100_slope and mm100_slope < 0)) if mm100 else True
    else:
        trend = "NEUTRE"
        mm100_ok = False

    # Force de la tendance (0-100)
    if trend != "NEUTRE":
        amp_h = abs(sh[1]["price"] - sh[0]["price"]) / sh[0]["price"] * 100
        amp_l = abs(sl[1]["price"] - sl[0]["price"]) / sl[0]["price"] * 100
        force = min(100, int((amp_h + amp_l) * 300))
    else:
        force = 0

    return {
        "trend":            trend,
        "tf":               tf_label,
        "force":            force,
        "mm100":            round(mm100, 5) if mm100 else None,
        "mm100_flat":       mm100_flat,
        "mm100_slope":      round(mm100_slope, 4) if mm100_slope else None,
        "mm100_ok":         mm100_ok,
        "price_above_mm100": price_above_mm100,
        "last_high":        sh[-1]["price"],
        "last_low":         sl[-1]["price"],
        "prev_high":        sh[-2]["price"],
        "prev_low":         sl[-2]["price"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 3 — FIGURE DE RETOURNEMENT / CONSOLIDATION ─────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
def detect_reversal_figure(candles: list, trend: str) -> dict:
    """
    Détecte :
    - Triangle (convergence des highs/lows)
    - Consolidation (range serré)
    - Wedge (biseau)
    en fin de tendance, signalant un retournement imminent.
    """
    if len(candles) < 15:
        return {"found": False}

    recent = candles[-15:]
    highs  = [c["high"] for c in recent]
    lows   = [c["low"]  for c in recent]

    # Pentes des tops et bottoms
    n = len(recent)
    top_slope = (highs[-1] - highs[0]) / n
    bot_slope = (lows[-1]  - lows[0])  / n

    range_pct = (max(highs) - min(lows)) / recent[-1]["close"] * 100

    # TRIANGLE : tops descendent ET bottoms montent (ou convergence)
    is_triangle = (
        top_slope < -0.00001 and bot_slope > 0.00001 and
        abs(top_slope) > 0 and abs(bot_slope) > 0
    )

    # CONSOLIDATION : range serré < 0.8%
    is_consolidation = range_pct < 0.8

    # WEDGE : les deux pentes dans la même direction mais convergeant
    is_wedge = (
        abs(top_slope - bot_slope) < abs(top_slope) * 0.3 and
        not is_triangle and not is_consolidation
    )

    if not (is_triangle or is_consolidation or is_wedge):
        return {"found": False}

    figure_type = "TRIANGLE" if is_triangle else ("CONSOLIDATION" if is_consolidation else "WEDGE")

    # Vérifier que la figure est en fin de tendance (pas au milieu)
    trend_candles = candles[-40:-15] if len(candles) >= 40 else candles[:len(candles)-15]
    if not trend_candles:
        return {"found": False}

    return {
        "found":       True,
        "type":        figure_type,
        "range_pct":   round(range_pct, 3),
        "top_slope":   round(top_slope, 6),
        "bot_slope":   round(bot_slope, 6),
        "zone_high":   round(max(highs), 5),
        "zone_low":    round(min(lows),  5),
        "zone_mid":    round((max(highs) + min(lows)) / 2, 5),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 4 — MM100 : PLATE OU DANS LE SENS DU RETOURNEMENT ──────────────────
# ══════════════════════════════════════════════════════════════════════════════
def check_mm100_alignment(trend_data: dict, figure: dict, direction: str) -> dict:
    """
    MM100 doit être :
    - PLATE (mm100_flat = True) → prix est dans une zone de retournement
    - OU dans le sens du retournement (direction LONG = MM100 haussière)
    Si MM100 va à l'opposé → AVORTE
    """
    if not trend_data.get("mm100"):
        return {"valid": True, "reason": "MM100 non calculable (pas assez de données)"}

    mm100_flat  = trend_data.get("mm100_flat", False)
    mm100_slope = trend_data.get("mm100_slope", 0) or 0

    if mm100_flat:
        return {
            "valid":  True,
            "status": "PLATE",
            "reason": f"MM100 plate (pente {mm100_slope:.4f}%) → zone de retournement confirmée"
        }

    if direction == "LONG" and mm100_slope > 0:
        return {
            "valid":  True,
            "status": "HAUSSIÈRE",
            "reason": f"MM100 dans le sens LONG (pente +{mm100_slope:.4f}%)"
        }
    if direction == "SHORT" and mm100_slope < 0:
        return {
            "valid":  True,
            "status": "BAISSIÈRE",
            "reason": f"MM100 dans le sens SHORT (pente {mm100_slope:.4f}%)"
        }

    return {
        "valid":  False,
        "status": "CONTRE-TENDANCE",
        "reason": f"MM100 opposée à la direction ({mm100_slope:.4f}%) → AVORTE"
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 5 — CASSURE DE ZONE D'ACTION ───────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
def detect_zone_breakout(candles_30min: list, figure: dict, direction: str) -> dict:
    """
    Détecte la cassure de la zone d'action (figure de retournement).
    Deux scénarios selon le schéma :
    A) Cassure légère → attendre une autre bougie
    B) Cassure forte avec impulsion → attendre pull back sur zone
    C) Pas de pull back → AVORTE
    """
    if not figure["found"] or len(candles_30min) < 5:
        return {"valid": False, "reason": "Pas de figure détectée"}

    zone_high = figure["zone_high"]
    zone_low  = figure["zone_low"]
    zone_size = zone_high - zone_low

    last  = candles_30min[-1]
    prev  = candles_30min[-2]
    prev2 = candles_30min[-3]

    # ── BOUGIE JAPONAISE DE QUALITÉ ───────────────────────────────────────────
    body       = abs(last["close"] - last["open"])
    total_range = last["high"] - last["low"] + 0.0000001
    body_ratio  = body / total_range

    # Belle clôture : corps > 60% de la bougie + direction correcte
    if direction == "LONG":
        good_candle = (last["close"] > last["open"] and body_ratio > 0.6)
    else:
        good_candle = (last["close"] < last["open"] and body_ratio > 0.6)

    if not good_candle:
        return {"valid": False, "reason": "Pas de belle bougie japonaise (corps < 60%)"}

    # ── CASSURE DE LA ZONE ────────────────────────────────────────────────────
    if direction == "LONG":
        broke_zone = last["close"] > zone_high
        breakout_strength = (last["close"] - zone_high) / zone_size if zone_size > 0 else 0
    else:
        broke_zone = last["close"] < zone_low
        breakout_strength = (zone_low - last["close"]) / zone_size if zone_size > 0 else 0

    if not broke_zone:
        return {"valid": False, "reason": "Zone non cassée"}

    # ── SCÉNARIO A : Cassure légère (<50% de la zone) → attendre autre bougie ─
    if breakout_strength < 0.5:
        return {
            "valid":      False,
            "scenario":   "A",
            "reason":     f"Cassure légère ({breakout_strength:.1%}) → attendre 2ème bougie de confirmation",
            "wait":       True,
            "zone_high":  zone_high,
            "zone_low":   zone_low,
        }

    # ── SCÉNARIO B : Cassure forte (≥50%) → chercher pull back ───────────────
    # Pull back = retour sur la zone cassée (ex-résistance = nouveau support)
    if direction == "LONG":
        # Pull back = prix redescend tester zone_high (maintenant support)
        pullback_done = (
            prev["low"]  <= zone_high * 1.002 and  # a touché la zone
            prev["close"] > zone_high and           # a clôturé au-dessus
            last["close"] > zone_high               # confirmation
        )
        pullback_current = last["low"] <= zone_high * 1.002
    else:
        # Pull back = prix remonte tester zone_low (maintenant résistance)
        pullback_done = (
            prev["high"] >= zone_low  * 0.998 and
            prev["close"] < zone_low  and
            last["close"] < zone_low
        )
        pullback_current = last["high"] >= zone_low * 0.998

    if pullback_done or pullback_current:
        return {
            "valid":              True,
            "scenario":           "B",
            "reason":             "Cassure forte + pull back confirmé → PRISE DE POSITION",
            "breakout_strength":  round(breakout_strength, 3),
            "pullback_done":      pullback_done,
            "zone_high":          zone_high,
            "zone_low":           zone_low,
            "body_ratio":         round(body_ratio, 2),
        }

    # Cassure forte mais pas encore de pull back → AVORTE selon schéma
    return {
        "valid":    False,
        "scenario": "B-WAIT",
        "reason":   f"Cassure forte ({breakout_strength:.1%}) mais pas de pull back → AVORTE (attendre retour zone)",
        "zone_high": zone_high,
        "zone_low":  zone_low,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉTAPE 6 — CALCUL SL/TP AVEC MM100 ────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
def calculate_sl_tp_mm100(
    candles_30min: list,
    trend_4h: dict,
    direction: str,
    figure: dict,
    pair: str
) -> dict | None:
    """
    SL : sous/au-dessus du dernier pivot Dow (HH ou LL) sur 4H
         avec buffer basé sur l'ATR 30min
    TP : prochain niveau de structure + vérification R/R ≥ 2.5
    MM100 sert de référence pour SL si plus proche que le pivot
    """
    if not candles_30min:
        return None

    last   = candles_30min[-1]
    entry  = last["close"]
    is_jpy = "JPY" in pair
    dp     = 3 if is_jpy else 5

    # ATR 30min (14 périodes)
    atr = sum(c["high"] - c["low"] for c in candles_30min[-14:]) / min(14, len(candles_30min))

    # ── STOP LOSS ─────────────────────────────────────────────────────────────
    if direction == "LONG":
        # SL sous le dernier LL (pivot bas de structure 4H)
        sl_pivot = trend_4h.get("last_low", entry - atr * 2)
        # Si MM100 est entre le prix et le pivot → SL sous MM100
        mm100 = trend_4h.get("mm100")
        if mm100 and sl_pivot < mm100 < entry:
            sl_candidate = mm100 - atr * 0.3
        else:
            sl_candidate = sl_pivot - atr * 0.2
        sl = min(sl_candidate, entry - atr * 1.5)  # SL minimum 1.5 ATR

    else:  # SHORT
        sl_pivot = trend_4h.get("last_high", entry + atr * 2)
        mm100 = trend_4h.get("mm100")
        if mm100 and entry < mm100 < sl_pivot:
            sl_candidate = mm100 + atr * 0.3
        else:
            sl_candidate = sl_pivot + atr * 0.2
        sl = max(sl_candidate, entry + atr * 1.5)

    sl_distance = abs(entry - sl)
    if sl_distance < atr * 0.5:
        return None  # SL trop serré = signal invalide

    # ── TAKE PROFIT — R/R minimum 2.5 ────────────────────────────────────────
    tp_distance = sl_distance * 2.5
    tp = (entry + tp_distance) if direction == "LONG" else (entry - tp_distance)
    ratio = round(tp_distance / sl_distance, 2)

    return {
        "entry":      round(entry, dp),
        "stop_loss":  round(sl,    dp),
        "take_profit":round(tp,    dp),
        "ratio":      ratio,
        "atr_30min":  round(atr,   dp),
        "sl_distance":round(sl_distance, dp),
        "mm100_used": trend_4h.get("mm100") is not None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── MOTEUR PRINCIPAL : DOUBLE STRUCTURE ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
async def analyze_double_structure(
    candles: dict,  # {"30min":[], "1h":[], "2h":[], "4h":[]}
    pair: str,
    economic_blocked: bool
) -> dict | None:
    """
    Schéma logique DOUBLE STRUCTURE complet :
    0. Annonce économique proche ? → AVORTE
    1. TimeFrame M30 → H4 valide ? → AVORTE si non
    2. Tendance haussière/baissière ? → AVORTE si neutre
    3. Figure de retournement/consolidation ? → AVORTE si non
    4. MM100 plate ou dans le sens du retournement ? → AVORTE si non
    5. R/R > 2.5 ? → AVORTE si non
    6. Belle clôture bougie + cassure zone d'action ?
       → Légère = attendre
       → Forte + pull back = PRISE DE POSITION
       → Pas de pull back = AVORTE
    """
    c30  = candles.get("30min", [])
    c1h  = candles.get("1h",    [])
    c2h  = candles.get("2h",    [])
    c4h  = candles.get("4h",    [])

    log = []  # Traçabilité du raisonnement

    # ── ÉTAPE 0 : Annonces économiques ───────────────────────────────────────
    if economic_blocked:
        return None  # Silencieux — pas de log pour les annonces

    # ── ÉTAPE 1 : Timeframe valide ────────────────────────────────────────────
    tf_check = is_valid_timeframe()
    if not tf_check["valid"]:
        return None
    log.append(f"✅ Session: {tf_check['session']} ({tf_check['hour']}h Paris)")

    # ── ÉTAPE 2 : Tendance multi-timeframe ────────────────────────────────────
    # Analyser chaque TF disponible : 4H = directeur, 2H+1H = filtres, 30min = entrée
    trend_4h  = detect_trend_dow(c4h,  "4H")  if c4h  else {"trend":"NEUTRE","tf":"4H"}
    trend_2h  = detect_trend_dow(c2h,  "2H")  if c2h  else {"trend":"NEUTRE","tf":"2H"}
    trend_1h  = detect_trend_dow(c1h,  "1H")  if c1h  else {"trend":"NEUTRE","tf":"1H"}
    trend_30m = detect_trend_dow(c30,  "30m") if c30  else {"trend":"NEUTRE","tf":"30m"}

    # La tendance directrice est le 4H
    if trend_4h["trend"] == "NEUTRE":
        log.append("❌ 4H: Tendance neutre → AVORTE")
        return None
    direction = "LONG" if trend_4h["trend"] == "HAUSSIÈRE" else "SHORT"
    log.append(f"✅ 4H: Tendance {trend_4h['trend']} (force: {trend_4h['force']}%)")

    # Filtre : au moins 2 TF sur 3 (2H, 1H, 30min) dans la même direction
    sub_trends = [trend_2h, trend_1h, trend_30m]
    aligned = sum(1 for t in sub_trends if t["trend"] == trend_4h["trend"])
    if aligned < 1:
        log.append(f"❌ Sous-TF: 0/{len(sub_trends)} alignés → AVORTE")
        return None
    log.append(f"✅ Sous-TF alignés: {aligned}/{len(sub_trends)} ({','.join(t['tf'] for t in sub_trends if t['trend']==trend_4h['trend'])})")

    # ── ÉTAPE 3 : Figure de retournement ──────────────────────────────────────
    # On cherche la figure sur le TF d'entrée (30min) et confirmation 1H
    figure_30m = detect_reversal_figure(c30, trend_4h["trend"]) if c30 else {"found": False}
    figure_1h  = detect_reversal_figure(c1h, trend_4h["trend"]) if c1h else {"found": False}
    figure     = figure_30m if figure_30m["found"] else figure_1h

    if not figure["found"]:
        log.append("❌ Aucune figure de retournement/consolidation → AVORTE")
        return None
    log.append(f"✅ Figure: {figure['type']} (range: {figure['range_pct']}%)")

    # ── ÉTAPE 4 : MM100 plate ou dans le sens ─────────────────────────────────
    mm100_check = check_mm100_alignment(trend_4h, figure, direction)
    if not mm100_check["valid"]:
        log.append(f"❌ MM100: {mm100_check['reason']} → AVORTE")
        return None
    log.append(f"✅ MM100 {mm100_check['status']}: {mm100_check['reason']}")

    # ── ÉTAPE 5 : Pré-calcul R/R ─────────────────────────────────────────────
    sl_tp_test = calculate_sl_tp_mm100(c30, trend_4h, direction, figure, pair)
    if not sl_tp_test or sl_tp_test["ratio"] < 2.5:
        log.append(f"❌ R/R insuffisant ({sl_tp_test['ratio'] if sl_tp_test else '—'}:1 < 2.5) → AVORTE")
        return None
    log.append(f"✅ R/R: {sl_tp_test['ratio']}:1 ≥ 2.5")

    # ── ÉTAPE 6 : Cassure de zone + Pull back ─────────────────────────────────
    breakout = detect_zone_breakout(c30, figure, direction)
    if not breakout["valid"]:
        if breakout.get("wait"):
            log.append(f"⏳ {breakout['reason']}")
        else:
            log.append(f"❌ {breakout['reason']} → AVORTE")
        return None
    log.append(f"✅ {breakout['reason']}")

    # ── SCORE FINAL ───────────────────────────────────────────────────────────
    score = 0
    score += 20  # Tendance 4H validée
    score += min(20, aligned * 7)  # Alignement sous-TF
    score += 15  # Figure détectée
    score += 15  # MM100 alignée
    score += 15  # R/R validé
    score += 15  # Cassure + pull back

    # Bonus
    if trend_4h["force"] > 60:  score += 5
    if sl_tp_test["ratio"] >= 3: score += 5
    if breakout.get("pullback_done"): score += 5
    score = min(100, score)

    strength = "FORT" if score >= 80 else ("MOYEN" if score >= 60 else "FAIBLE")

    return {
        "pair":            pair,
        "direction":       direction,
        "strength":        strength,
        "score":           score,

        # Tendances
        "trend_4h":        trend_4h["trend"],
        "trend_2h":        trend_2h["trend"],
        "trend_1h":        trend_1h["trend"],
        "trend_30m":       trend_30m["trend"],
        "trend_force_4h":  trend_4h["force"],
        "aligned_tfs":     aligned,

        # MM100
        "mm100_4h":        trend_4h.get("mm100"),
        "mm100_status":    mm100_check["status"],

        # Figure
        "figure_type":     figure["type"],
        "figure_tf":       "30min" if figure_30m["found"] else "1H",
        "zone_high":       figure["zone_high"],
        "zone_low":        figure["zone_low"],

        # Cassure
        "breakout_scenario": breakout["scenario"],
        "pullback_done":   breakout.get("pullback_done", False),

        # SL/TP
        "entry":           sl_tp_test["entry"],
        "stop_loss":       sl_tp_test["stop_loss"],
        "take_profit":     sl_tp_test["take_profit"],
        "ratio":           sl_tp_test["ratio"],
        "atr_30min":       sl_tp_test["atr_30min"],

        # Meta
        "timeframe_trend": "4H",
        "timeframe_entry": "30min",
        "session":         tf_check["session"],
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "log":             log,
    }


# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
            )
    except Exception as e:
        print(f"Telegram error: {e}")


# ── TWELVE DATA ───────────────────────────────────────────────────────────────
async def fetch_candles(pair: str, interval: str, outputsize: int = 110) -> list:
    symbol = pair.replace("/", "")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.twelvedata.com/time_series", params={
                "symbol": symbol, "interval": interval,
                "outputsize": outputsize, "apikey": TWELVE_DATA_KEY
            })
            data = r.json()
            if "values" in data:
                return [{"open": float(v["open"]), "high": float(v["high"]),
                         "low": float(v["low"]), "close": float(v["close"]),
                         "volume": float(v.get("volume", 0)), "time": v["datetime"]}
                        for v in reversed(data["values"])]
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
    except: pass
    return {"pair": pair, "price": None, "ok": False}


# ── BACKGROUND WORKER ─────────────────────────────────────────────────────────
async def update_market_data():
    now = datetime.now().strftime('%H:%M:%S')
    print(f"[{now}] Double Structure scan — {len(ALL_PAIRS)} pairs...")

    # Vérifier annonces économiques
    eco = await is_near_economic_event()
    if eco["blocked"]:
        print(f"  ⛔ Annonces économiques bloquées: {eco['events']}")

    # Fetch prix
    price_tasks = [fetch_price(p) for p in ALL_PAIRS]
    for r in await asyncio.gather(*price_tasks, return_exceptions=True):
        if isinstance(r, dict) and r.get("ok"):
            cache["prices"][r["pair"]] = r["price"]

    # Fetch bougies + analyser
    signals = []
    for pair in ALL_PAIRS:
        try:
            candles = {}
            # 4H : 110 bougies (100 pour MM100 + 10 buffer)
            candles["4h"]    = await fetch_candles(pair, "4h",    110)
            await asyncio.sleep(0.25)
            # 2H : 60 bougies
            candles["2h"]    = await fetch_candles(pair, "2h",    60)
            await asyncio.sleep(0.25)
            # 1H : 60 bougies
            candles["1h"]    = await fetch_candles(pair, "1h",    60)
            await asyncio.sleep(0.25)
            # 30min : 50 bougies
            candles["30min"] = await fetch_candles(pair, "30min", 50)
            await asyncio.sleep(0.25)

            cache["candles"][pair] = candles

            result = await analyze_double_structure(candles, pair, eco["blocked"])
            if result:
                signals.append(result)
                print(f"  ✅ {pair} {result['direction']} {result['strength']} score:{result['score']}")
        except Exception as e:
            print(f"  ❌ {pair}: {e}")

    signals.sort(key=lambda x: -x["score"])
    cache["signals"]    = signals
    cache["last_update"]= datetime.now(timezone.utc).isoformat()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Terminé — {len(signals)} signaux")

    # ── NOTIFICATION TELEGRAM ─────────────────────────────────────────────────
    hour = (datetime.now(timezone.utc).hour + 2) % 24
    if 8 <= hour <= 22 and signals:
        last_notif = cache.get("last_notification")
        should_notify = not last_notif or \
            (datetime.now(timezone.utc) - datetime.fromisoformat(last_notif)).seconds > 3600

        if should_notify:
            strong = [s for s in signals if s["strength"] == "FORT"]
            medium = [s for s in signals if s["strength"] == "MOYEN"]

            msg  = f"🎯 <b>Sophie Trading — Double Structure</b>\n"
            msg += f"🕐 {datetime.now().strftime('%H:%M')} · Session {signals[0].get('session','')}\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📊 <b>{len(signals)} opportunité(s)</b>\n"
            msg += f"🟢 FORT: {len(strong)} | 🟡 MOYEN: {len(medium)}\n\n"

            for s in signals[:4]:
                emoji = "🟢" if s["direction"] == "LONG" else "🔴"
                msg += f"{emoji} <b>{s['pair']}</b> — {s['direction']} ({s['strength']})\n"
                msg += f"   📐 {s['figure_type']} sur {s['figure_tf']}\n"
                msg += f"   📊 Tendances: 4H:{s['trend_4h'][0]} 2H:{s['trend_2h'][0]} 1H:{s['trend_1h'][0]} 30m:{s['trend_30m'][0]}\n"
                msg += f"   📉 MM100: {s['mm100_status']} ({s['mm100_4h']})\n"
                msg += f"   ⚡ Scenario {s['breakout_scenario']} · Pull back: {'✅' if s['pullback_done'] else '⏳'}\n"
                msg += f"   Entrée: <code>{s['entry']}</code>\n"
                msg += f"   SL: <code>{s['stop_loss']}</code> | TP: <code>{s['take_profit']}</code>\n"
                msg += f"   R/R: <b>{s['ratio']}:1</b> | Score: {s['score']}/100\n\n"

            if eco["blocked"]:
                msg += f"⚠️ Annonces bloquées: {', '.join(eco['events'][:2])}\n"
            msg += f"⚠️ Vérifiez toujours sur TradingView avant d'entrer"

            await send_telegram(msg)
            cache["last_notification"] = datetime.now(timezone.utc).isoformat()


async def scheduler():
    while True:
        try:
            await update_market_data()
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler())

# ── API ROUTES ────────────────────────────────────────────────────────────────
@app.get("/api/signals")
async def get_signals():
    hour = (datetime.now(timezone.utc).hour + 2) % 24
    return JSONResponse({
        "signals":     cache["signals"],
        "last_update": cache["last_update"],
        "trading_window_active": 8 <= hour <= 22,
        "hour_paris":  hour,
        "count":       len(cache["signals"])
    })

@app.get("/api/prices")
async def get_prices():
    return JSONResponse({"prices": cache["prices"], "last_update": cache["last_update"]})

@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "status":   "online",
        "version":  "4.0-double-structure",
        "strategy": "Double Structure: 4H(Dow+MM100) → 2H → 1H → 30min",
        "last_update":    cache["last_update"],
        "signals_count":  len(cache["signals"]),
        "pairs_scanned":  len(ALL_PAIRS),
        "telegram_ok":    bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
    })

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
