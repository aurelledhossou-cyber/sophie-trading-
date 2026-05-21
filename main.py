import os
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Sophie Trading — Double Structure v5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    "prices": {}, "candles": {},
    "signals": [], "last_update": None, "last_notification": None,
}

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 0 — ANNONCES ÉCONOMIQUES (bloquer 10min avant/après)
# ══════════════════════════════════════════════════════════════════════════════
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
        print(f"Calendar API: {e}")
    return {"blocked": False, "events": []}

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1 — TIMEFRAME VALIDE + SESSION (8h-21h Paris)
# ══════════════════════════════════════════════════════════════════════════════
def is_valid_session() -> dict:
    now_utc  = datetime.now(timezone.utc)
    hp       = (now_utc.hour + 2) % 24          # heure Paris
    weekday  = now_utc.weekday()                 # 0=lun, 6=dim
    if weekday >= 5:
        return {"valid": False, "reason": "Week-end"}
    if hp < 8 or hp > 21:
        return {"valid": False, "reason": f"Hors 8h-21h ({hp}h Paris)"}
    session = "LONDON" if hp < 17 else "NEW YORK"
    return {"valid": True, "hour": hp, "session": session}

# ══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════
def calc_mm100(candles: list) -> float | None:
    if len(candles) < 100:
        return None
    return sum(c["close"] for c in candles[-100:]) / 100

def find_pivots(candles: list, n: int = 3) -> tuple[list, list]:
    """Swing highs et lows confirmés par n bougies de chaque côté."""
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        if all(candles[i]["high"] > candles[i+k]["high"] for k in range(-n, n+1) if k != 0):
            highs.append({"price": candles[i]["high"], "idx": i, "time": candles[i]["time"]})
        if all(candles[i]["low"]  < candles[i+k]["low"]  for k in range(-n, n+1) if k != 0):
            lows.append( {"price": candles[i]["low"],  "idx": i, "time": candles[i]["time"]})
    return highs, lows

def is_jpy_pair(pair: str) -> bool:
    return "JPY" in pair

def dp(pair: str) -> int:
    return 3 if is_jpy_pair(pair) else 5

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1 — TENDANCE (DOW THEORY) PAR TIMEFRAME
# Détection visuelle : LH+LL (baissier) ou HH+HL (haussier)
# Ligne de tendance calculée sur les pivots
# ══════════════════════════════════════════════════════════════════════════════
def detect_trend(candles: list, tf: str) -> dict:
    """
    Dow Theory visuelle :
    - Pivots confirmés 3 bougies chaque côté
    - LH+LL = baissier / HH+HL = haussier
    - Ligne de tendance = droite reliant les 2 derniers pivots
    - MM100 : plate ou dans le sens = valide
    """
    if len(candles) < 20:
        return {"trend": "NEUTRE", "tf": tf}

    mm100 = calc_mm100(candles)
    highs, lows = find_pivots(candles, n=3)

    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "NEUTRE", "tf": tf, "mm100": mm100}

    sh = highs[-2:]  # 2 derniers swing highs
    sl = lows[-2:]   # 2 derniers swing lows

    hh = sh[1]["price"] > sh[0]["price"]   # Higher High
    hl = sl[1]["price"] > sl[0]["price"]   # Higher Low
    lh = sh[1]["price"] < sh[0]["price"]   # Lower High
    ll = sl[1]["price"] < sl[0]["price"]   # Lower Low

    if hh and hl:
        trend = "HAUSSIÈRE"
    elif lh and ll:
        trend = "BAISSIÈRE"
    else:
        return {"trend": "NEUTRE", "tf": tf, "mm100": mm100}

    # Ligne de tendance (reliant les 2 derniers highs pour baissier, lows pour haussier)
    if trend == "BAISSIÈRE":
        tl_p1, tl_p2 = sh[0]["price"], sh[1]["price"]
        tl_slope = (tl_p2 - tl_p1) / max(sh[1]["idx"] - sh[0]["idx"], 1)
    else:
        tl_p1, tl_p2 = sl[0]["price"], sl[1]["price"]
        tl_slope = (tl_p2 - tl_p1) / max(sl[1]["idx"] - sl[0]["idx"], 1)

    # MM100 alignement
    last_close = candles[-1]["close"]
    mm100_ok   = False
    mm100_flat = False
    mm100_slope_pct = None
    if mm100 and len(candles) >= 110:
        mm100_old = sum(c["close"] for c in candles[-110:-10]) / 100
        mm100_slope_pct = (mm100 - mm100_old) / mm100_old * 100
        mm100_flat = abs(mm100_slope_pct) < 0.05
        if mm100_flat:
            mm100_ok = True
        elif trend == "HAUSSIÈRE" and mm100_slope_pct > 0:
            mm100_ok = True
        elif trend == "BAISSIÈRE" and mm100_slope_pct < 0:
            mm100_ok = True

    # Force de la tendance
    amp_h = abs(sh[1]["price"] - sh[0]["price"]) / sh[0]["price"] * 100
    amp_l = abs(sl[1]["price"] - sl[0]["price"]) / sl[0]["price"] * 100
    force = min(100, int((amp_h + amp_l) * 300))

    return {
        "trend":       trend,
        "tf":          tf,
        "force":       force,
        "mm100":       round(mm100, 5) if mm100 else None,
        "mm100_ok":    mm100_ok,
        "mm100_flat":  mm100_flat,
        "mm100_slope": round(mm100_slope_pct, 4) if mm100_slope_pct else None,
        "mm100_pos":   "AU-DESSUS" if mm100 and last_close > mm100 else "EN-DESSOUS",
        "last_high":   sh[-1]["price"],
        "last_low":    sl[-1]["price"],
        "prev_high":   sh[-2]["price"],
        "prev_low":    sl[-2]["price"],
        "tl_slope":    round(tl_slope, 6),
        "tl_last":     round(tl_p2, 5),
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 2 — FIGURE DE RETOURNEMENT EN FIN DE TENDANCE
# Consolidation (rectangle) ou triangle EN FIN de mouvement
# ══════════════════════════════════════════════════════════════════════════════
def detect_figure_end_of_trend(candles: list, trend: str) -> dict:
    """
    Détecte visuellement :
    - CONSOLIDATION : rectangle horizontal en fin de tendance (range < 0.8%)
    - TRIANGLE : convergence tops/bottoms en fin de tendance
    La figure doit être sur les 15-20 DERNIÈRES bougies (fin de tendance)
    """
    if len(candles) < 25:
        return {"found": False}

    # Les 15 dernières bougies = zone de figure potentielle
    fig_candles = candles[-15:]
    # Les bougies précédentes = vérification que c'est bien une tendance
    trend_candles = candles[-40:-15]

    if len(trend_candles) < 10:
        return {"found": False}

    fig_highs = [c["high"] for c in fig_candles]
    fig_lows  = [c["low"]  for c in fig_candles]
    t_highs   = [c["high"] for c in trend_candles]
    t_lows    = [c["low"]  for c in trend_candles]

    range_fig = max(fig_highs) - min(fig_lows)
    range_pct = range_fig / fig_candles[-1]["close"] * 100

    # Vérifier que la tendance précédente est réelle
    if trend == "BAISSIÈRE":
        trend_ok = max(t_highs[:5]) > max(t_highs[-5:])   # Highs qui baissent
    else:
        trend_ok = min(t_lows[:5]) < min(t_lows[-5:])     # Lows qui montent

    if not trend_ok:
        return {"found": False}

    # Pentes des tops et bottoms de la figure
    n = len(fig_candles)
    top_slope = (fig_highs[-1] - fig_highs[0]) / n
    bot_slope = (fig_lows[-1]  - fig_lows[0])  / n

    # TRIANGLE : tops descendent ET bottoms montent (convergence)
    is_triangle = (
        top_slope < -0.000005 and bot_slope > 0.000005 and
        range_pct < 2.0
    )
    # CONSOLIDATION : range serré horizontal
    is_consolidation = range_pct < 0.8 and abs(top_slope) < abs(bot_slope * 3)

    if not is_triangle and not is_consolidation:
        return {"found": False}

    figure_type = "TRIANGLE" if is_triangle else "CONSOLIDATION"

    return {
        "found":      True,
        "type":       figure_type,
        "zone_high":  round(max(fig_highs), 5),
        "zone_low":   round(min(fig_lows),  5),
        "zone_mid":   round((max(fig_highs) + min(fig_lows)) / 2, 5),
        "range_pct":  round(range_pct, 3),
        "nb_candles": n,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 3 — ZONE DE PRIX (support/résistance avec ≥2 retests ou top historique)
# ══════════════════════════════════════════════════════════════════════════════
def detect_price_zone(candles: list, trend: str) -> dict | None:
    """
    Zone de prix valide :
    - Support horizontal (tendance baissière) ou résistance (haussière)
    - Testée au moins 2 fois (2 retests) OU top/bottom historique
    - Tolérance : 0.15% autour du niveau
    """
    if len(candles) < 30:
        return None

    highs, lows = find_pivots(candles[:-5], n=2)  # Exclure les 5 dernières (figure en cours)

    # Candidats selon la tendance
    candidates = lows if trend == "BAISSIÈRE" else highs
    if not candidates:
        return None

    best_zone = None
    best_score = 0

    for pivot in candidates:
        level = pivot["price"]
        tol   = level * 0.0015   # 0.15% de tolérance

        touches     = 0
        rejections  = 0
        is_historic = False

        for c in candles:
            # Touch sur le niveau
            if abs(c["high"] - level) < tol or abs(c["low"] - level) < tol:
                touches += 1
                body  = abs(c["close"] - c["open"])
                total = c["high"] - c["low"] + 0.0000001
                wick_ratio = (c["high"] - max(c["open"], c["close"])) / total
                if wick_ratio > 0.35:
                    rejections += 1

        # Top/bottom historique = premier pivot dans les données
        if pivot["idx"] <= 5:
            is_historic = True

        if touches < 2 and not is_historic:
            continue

        # Score de qualité de la zone
        score = touches * 20 + rejections * 15
        if is_historic:     score += 20
        # Niveau psychologique ?
        rbase  = 1 if level > 10 else (0.1 if level > 1 else 0.01)
        rround = round(level / rbase) * rbase
        is_psych = abs(level - rround) / level < 0.003
        if is_psych: score += 20

        if score > best_score:
            best_score = score
            best_zone  = {
                "level":       round(level, 5),
                "touches":     touches,
                "rejections":  rejections,
                "is_historic": is_historic,
                "is_psych":    is_psych,
                "quality":     min(100, score),
                "type":        "SUPPORT" if trend == "BAISSIÈRE" else "RÉSISTANCE",
            }

    return best_zone

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 4 — ZONES PSYCHOLOGIQUES + ZONES DANGEREUSES + TP FANTÔME
# ══════════════════════════════════════════════════════════════════════════════
def detect_psych_zones(candles: list, direction: str, entry_price: float, pair: str) -> dict:
    """
    Identifie :
    1. Niveaux psychologiques (nombres ronds) entre entrée et TP
    2. Zones dangereuses (anciens supports/résistances entre entrée et TP)
    3. TP fantôme (niveau psy juste avant l'objectif)
    """
    last_price = candles[-1]["close"]
    rbase = 1 if last_price > 10 else (0.1 if last_price > 1 else 0.01)

    # Niveaux psychologiques dans la zone pertinente
    psych_levels = []
    search_range = last_price * 0.05  # 5% autour du prix
    current = round((last_price - search_range) / rbase) * rbase
    while current <= last_price + search_range:
        if (direction == "LONG"  and current > last_price) or \
           (direction == "SHORT" and current < last_price):
            psych_levels.append(round(current, dp(pair)))
        current = round(current + rbase, dp(pair))

    # Zones dangereuses = anciens pivots entre entrée et TP estimé
    highs, lows = find_pivots(candles[:-5], n=2)
    danger_zones = []
    all_pivots = (highs if direction == "LONG" else lows)
    for piv in all_pivots[-10:]:
        if direction == "LONG"  and piv["price"] > last_price:
            danger_zones.append(round(piv["price"], dp(pair)))
        elif direction == "SHORT" and piv["price"] < last_price:
            danger_zones.append(round(piv["price"], dp(pair)))

    # TP fantôme = premier niveau psy ou zone dangereuse avant l'objectif
    tp_fantome = psych_levels[0]  if psych_levels  else None
    danger_near= danger_zones[0]  if danger_zones   else None

    return {
        "psych_levels":  sorted(psych_levels[:5]),
        "danger_zones":  sorted(danger_zones[:3]),
        "tp_fantome":    tp_fantome,
        "danger_near":   danger_near,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 5 — PRISE DE POSITION
# Clôture bougie cassant la zone + SL sous dernier HH/LL + R/R ≥ 2.5
# ══════════════════════════════════════════════════════════════════════════════
def detect_breakout_entry(candles_30m: list, zone: dict, trend_4h: dict,
                          figure: dict, direction: str, pair: str) -> dict | None:
    """
    Conditions d'entrée :
    1. Belle bougie japonaise (corps ≥ 60%) cassant la zone de prix
    2. SL sous le dernier plus haut/bas de structure (Dow)
    3. R/R ≥ 2.5
    4. TP avant la première zone dangereuse/psychologique
    """
    if len(candles_30m) < 5:
        return None

    zone_level = zone["level"]
    last       = candles_30m[-1]
    entry      = last["close"]
    precision  = dp(pair)
    is_jpy     = is_jpy_pair(pair)

    # ── BELLE BOUGIE JAPONAISE ────────────────────────────────────────────────
    body       = abs(last["close"] - last["open"])
    total_range= last["high"] - last["low"] + 0.0000001
    body_ratio = body / total_range

    if body_ratio < 0.55:   # Corps insuffisant
        return None

    good_dir = (direction == "LONG"  and last["close"] > last["open"]) or \
               (direction == "SHORT" and last["close"] < last["open"])
    if not good_dir:
        return None

    # ── CASSURE DE LA ZONE ────────────────────────────────────────────────────
    if direction == "LONG":
        broke = last["close"] > zone_level and last["open"] < zone_level * 1.002
    else:
        broke = last["close"] < zone_level and last["open"] > zone_level * 0.998
    if not broke:
        return None

    # ── STOP LOSS sous le dernier HH ou LL (Dow Theory) ──────────────────────
    atr = sum(c["high"] - c["low"] for c in candles_30m[-14:]) / min(14, len(candles_30m))
    if direction == "LONG":
        sl_pivot = trend_4h.get("last_low", entry - atr * 2)
        # Si MM100 entre le prix et le pivot → SL sous MM100
        mm100 = trend_4h.get("mm100")
        if mm100 and sl_pivot < mm100 < entry:
            sl = mm100 - atr * 0.3
        else:
            sl = sl_pivot - atr * 0.15
        sl = min(sl, entry - atr * 1.2)
    else:
        sl_pivot = trend_4h.get("last_high", entry + atr * 2)
        mm100 = trend_4h.get("mm100")
        if mm100 and entry < mm100 < sl_pivot:
            sl = mm100 + atr * 0.3
        else:
            sl = sl_pivot + atr * 0.15
        sl = max(sl, entry + atr * 1.2)

    sl_dist = abs(entry - sl)
    if sl_dist < atr * 0.3:
        return None

    # ── TAKE PROFIT — R/R minimum 2.5 ────────────────────────────────────────
    tp_dist = sl_dist * 2.5
    tp = (entry + tp_dist) if direction == "LONG" else (entry - tp_dist)
    ratio = round(tp_dist / sl_dist, 2)

    # Vérifier zones dangereuses entre entrée et TP
    psych = detect_psych_zones(candles_30m, direction, entry, pair)

    # Ajuster TP si zone dangereuse avant l'objectif
    tp_adjusted = tp
    if psych["danger_near"]:
        dn = psych["danger_near"]
        if (direction == "LONG"  and entry < dn < tp) or \
           (direction == "SHORT" and tp < dn < entry):
            # TP juste avant la zone dangereuse
            tp_adjusted = dn * 0.9995 if direction == "LONG" else dn * 1.0005
            ratio = round(abs(tp_adjusted - entry) / sl_dist, 2)

    if ratio < 2.5:
        return None

    return {
        "entry":        round(entry,        precision),
        "stop_loss":    round(sl,           precision),
        "take_profit":  round(tp_adjusted,  precision),
        "ratio":        ratio,
        "body_ratio":   round(body_ratio,   2),
        "atr":          round(atr,          precision),
        "psych_zones":  psych,
        "tp_adjusted":  tp_adjusted != tp,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 6 — MANAGEMENT (trail SL selon Dow + zones psychologiques)
# Informatif — affiché dans l'alerte pour guider le trader
# ══════════════════════════════════════════════════════════════════════════════
def get_position_management(candles_30m: list, direction: str,
                            entry: float, sl: float, tp: float,
                            psych: dict, pair: str) -> dict:
    """
    Conseils de management selon l'Étape 6 :
    - Trail SL sur derniers HH/LL (Dow)
    - SL sous zones psychologiques
    - Management de probabilités
    """
    precision = dp(pair)
    _, lows = find_pivots(candles_30m[-20:], n=2)
    highs, _ = find_pivots(candles_30m[-20:], n=2)

    trail_levels = []
    if direction == "LONG" and lows:
        for l in lows[-3:]:
            if entry < l["price"] < tp:
                trail_levels.append(round(l["price"] - (l["price"] * 0.001), precision))
    elif direction == "SHORT" and highs:
        for h in highs[-3:]:
            if tp < h["price"] < entry:
                trail_levels.append(round(h["price"] + (h["price"] * 0.001), precision))

    # Niveaux psy comme SL trail
    for psy in psych.get("psych_levels", []):
        if direction == "LONG"  and entry < psy < tp:
            trail_levels.append(round(psy * 0.9995, precision))
        elif direction == "SHORT" and tp < psy < entry:
            trail_levels.append(round(psy * 1.0005, precision))

    trail_levels = sorted(set(trail_levels))

    return {
        "trail_sl_levels": trail_levels,
        "advice": [
            f"SL initial : {sl}",
            f"Déplacer SL à BE après +{round(abs(tp-entry)*0.33, precision)}",
            *[f"Trail SL → {lvl}" for lvl in trail_levels[:3]],
            f"TP final : {tp}",
        ]
    }

# ══════════════════════════════════════════════════════════════════════════════
# MOTEUR PRINCIPAL — DOUBLE STRUCTURE COMPLÈTE (6 ÉTAPES)
# ══════════════════════════════════════════════════════════════════════════════
async def analyze_double_structure(candles: dict, pair: str, eco_blocked: bool) -> dict | None:
    c30 = candles.get("30min", [])
    c1h = candles.get("1h",    [])
    c2h = candles.get("2h",    [])
    c4h = candles.get("4h",    [])
    log = []

    # ÉTAPE 0 — Annonce économique
    if eco_blocked:
        return None

    # ÉTAPE 1 — Session 8h-21h
    session = is_valid_session()
    if not session["valid"]:
        return None
    log.append(f"✅ Session {session['session']} ({session['hour']}h Paris)")

    # ÉTAPE 1 — Tendance multi-TF (4H directeur)
    t4h = detect_trend(c4h,  "4H") if c4h  else {"trend": "NEUTRE", "tf": "4H"}
    t2h = detect_trend(c2h,  "2H") if c2h  else {"trend": "NEUTRE", "tf": "2H"}
    t1h = detect_trend(c1h,  "1H") if c1h  else {"trend": "NEUTRE", "tf": "1H"}
    t30 = detect_trend(c30, "30m") if c30  else {"trend": "NEUTRE", "tf": "30m"}

    if t4h["trend"] == "NEUTRE":
        log.append("❌ 4H neutre → AVORTE")
        return None

    direction = "LONG" if t4h["trend"] == "HAUSSIÈRE" else "SHORT"
    log.append(f"✅ Tendance 4H : {t4h['trend']} (force {t4h['force']}%)")

    # Au moins 1 TF sous-jacent aligné
    aligned = sum(1 for t in [t2h, t1h, t30] if t["trend"] == t4h["trend"])
    if aligned == 0:
        log.append("❌ Aucun TF sous-jacent aligné → AVORTE")
        return None
    log.append(f"✅ TF alignés : {aligned}/3 ({','.join(t['tf'] for t in [t2h,t1h,t30] if t['trend']==t4h['trend'])})")

    # MM100 alignée sur 4H
    if not t4h.get("mm100_ok", True):
        log.append(f"❌ MM100 4H contre-tendance → AVORTE")
        return None
    log.append(f"✅ MM100 4H : {t4h.get('mm100_pos','—')} ({t4h.get('mm100','—')})")

    # ÉTAPE 2 — Figure de retournement en fin de tendance
    fig = detect_figure_end_of_trend(c30 or c1h, t4h["trend"])
    if not fig["found"]:
        log.append("❌ Aucune figure de retournement en fin de tendance → AVORTE")
        return None
    log.append(f"✅ Figure : {fig['type']} (range {fig['range_pct']}%)")

    # ÉTAPE 3 — Zone de prix (≥2 retests ou top historique)
    zone = detect_price_zone(c4h or c1h, t4h["trend"])
    if not zone:
        log.append("❌ Pas de zone de prix valide (< 2 retests) → AVORTE")
        return None
    log.append(f"✅ Zone {zone['type']} : {zone['level']} ({zone['touches']} touches, qualité {zone['quality']}%)")

    # ÉTAPE 4 — Zones psychologiques (informatif, calculé après entrée)

    # ÉTAPE 5 — Cassure + SL/TP
    entry_data = detect_breakout_entry(c30, zone, t4h, fig, direction, pair)
    if not entry_data:
        log.append("❌ Pas de cassure valide ou R/R insuffisant → AVORTE")
        return None
    log.append(f"✅ Cassure confirmée · R/R {entry_data['ratio']}:1 · Corps {entry_data['body_ratio']*100:.0f}%")

    # ÉTAPE 6 — Management
    mgmt = get_position_management(
        c30, direction,
        entry_data["entry"], entry_data["stop_loss"], entry_data["take_profit"],
        entry_data["psych_zones"], pair
    )

    # SCORE FINAL
    score = 0
    score += 20                              # Tendance 4H
    score += min(15, aligned * 5)           # Alignement TF
    score += 10 if t4h.get("mm100_ok") else 0  # MM100
    score += 15                              # Figure détectée
    score += min(20, zone["quality"] // 5)  # Qualité zone
    score += 15                              # Cassure validée
    score += 5  if entry_data["ratio"] >= 3  else 0
    score += 5  if zone["is_psych"]          else 0
    score += 5  if zone["is_historic"]       else 0
    score = min(100, score)

    strength = "FORT" if score >= 80 else ("MOYEN" if score >= 60 else "FAIBLE")

    return {
        "pair":         pair,
        "direction":    direction,
        "strength":     strength,
        "score":        score,
        "log":          log,

        # Étape 1 — Tendances
        "trend_4h":     t4h["trend"],
        "trend_2h":     t2h["trend"],
        "trend_1h":     t1h["trend"],
        "trend_30m":    t30["trend"],
        "trend_force":  t4h["force"],
        "aligned_tfs":  aligned,

        # MM100
        "mm100_4h":     t4h.get("mm100"),
        "mm100_pos":    t4h.get("mm100_pos"),
        "mm100_slope":  t4h.get("mm100_slope"),

        # Étape 2 — Figure
        "figure_type":  fig["type"],
        "figure_range": fig["range_pct"],
        "zone_high":    fig["zone_high"],
        "zone_low":     fig["zone_low"],

        # Étape 3 — Zone de prix
        "price_zone":   zone["level"],
        "zone_type":    zone["type"],
        "zone_touches": zone["touches"],
        "zone_quality": zone["quality"],
        "zone_psych":   zone["is_psych"],
        "zone_historic":zone["is_historic"],

        # Étape 4 — Zones psy
        "psych_levels": entry_data["psych_zones"]["psych_levels"],
        "danger_zones": entry_data["psych_zones"]["danger_zones"],
        "tp_fantome":   entry_data["psych_zones"]["tp_fantome"],

        # Étape 5 — Entrée
        "entry":        entry_data["entry"],
        "stop_loss":    entry_data["stop_loss"],
        "take_profit":  entry_data["take_profit"],
        "ratio":        entry_data["ratio"],
        "body_ratio":   entry_data["body_ratio"],
        "atr":          entry_data["atr"],
        "tp_adjusted":  entry_data["tp_adjusted"],

        # Étape 6 — Management
        "trail_levels": mgmt["trail_sl_levels"],
        "mgmt_advice":  mgmt["advice"],

        # Meta
        "timeframe_trend": "4H",
        "timeframe_entry": "30min",
        "session":         session["session"],
        "timestamp":       datetime.now(timezone.utc).isoformat(),
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
        print(f"Telegram: {e}")

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
                return [{"open":float(v["open"]),"high":float(v["high"]),
                         "low":float(v["low"]),"close":float(v["close"]),
                         "volume":float(v.get("volume",0)),"time":v["datetime"]}
                        for v in reversed(data["values"])]
    except Exception as e:
        print(f"Candles {pair} {interval}: {e}")
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Double Structure v5 — {len(ALL_PAIRS)} paires")

    eco = await is_near_economic_event()
    if eco["blocked"]:
        print(f"  ⛔ Annonces: {eco['events']}")

    for r in await asyncio.gather(*[fetch_price(p) for p in ALL_PAIRS], return_exceptions=True):
        if isinstance(r, dict) and r.get("ok"):
            cache["prices"][r["pair"]] = r["price"]

    signals = []
    for pair in ALL_PAIRS:
        try:
            c = {}
            c["4h"]    = await fetch_candles(pair, "4h",    110); await asyncio.sleep(0.2)
            c["2h"]    = await fetch_candles(pair, "2h",     60); await asyncio.sleep(0.2)
            c["1h"]    = await fetch_candles(pair, "1h",     60); await asyncio.sleep(0.2)
            c["30min"] = await fetch_candles(pair, "30min",  50); await asyncio.sleep(0.2)
            cache["candles"][pair] = c
            result = await analyze_double_structure(c, pair, eco["blocked"])
            if result:
                signals.append(result)
                print(f"  ✅ {pair} {result['direction']} {result['strength']} score:{result['score']}")
        except Exception as e:
            print(f"  ❌ {pair}: {e}")

    signals.sort(key=lambda x: -x["score"])
    cache["signals"]     = signals
    cache["last_update"] = datetime.now(timezone.utc).isoformat()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {len(signals)} signaux Double Structure")

    # Telegram
    hp = (datetime.now(timezone.utc).hour + 2) % 24
    if 8 <= hp <= 21 and signals:
        last = cache.get("last_notification")
        if not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last)).seconds > 3600:
            strong = [s for s in signals if s["strength"]=="FORT"]
            msg  = f"🎯 <b>Sophie Trading — Double Structure v5</b>\n"
            msg += f"🕐 {datetime.now().strftime('%H:%M')} · {signals[0].get('session','')}\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📊 <b>{len(signals)} signal(s)</b> · FORT: {len(strong)}\n\n"
            for s in signals[:4]:
                e = "🟢" if s["direction"]=="LONG" else "🔴"
                msg += f"{e} <b>{s['pair']}</b> — {s['direction']} ({s['strength']} {s['score']}/100)\n"
                msg += f"   📐 {s['figure_type']} · Zone {s['zone_type']} ({s['zone_touches']} retests)\n"
                msg += f"   TF: 4H:{s['trend_4h'][0]} 2H:{s['trend_2h'][0]} 1H:{s['trend_1h'][0]} 30m:{s['trend_30m'][0]}\n"
                msg += f"   MM100: {s['mm100_pos']} ({s['mm100_4h']})\n"
                msg += f"   🎯 Entrée: <code>{s['entry']}</code>\n"
                msg += f"   🛡️ SL: <code>{s['stop_loss']}</code> | TP: <code>{s['take_profit']}</code>\n"
                if s.get("tp_fantome"):
                    msg += f"   ⚠️ TP fantôme : {s['tp_fantome']}\n"
                msg += f"   R/R: <b>{s['ratio']}:1</b>\n\n"
            msg += "⚠️ Vérifiez sur TradingView avant d'entrer"
            await send_telegram(msg)
            cache["last_notification"] = datetime.now(timezone.utc).isoformat()

async def scheduler():
    while True:
        try: await update_market_data()
        except Exception as e: print(f"Scheduler: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler())

@app.get("/api/signals")
async def get_signals():
    hp = (datetime.now(timezone.utc).hour + 2) % 24
    return JSONResponse({"signals": cache["signals"], "last_update": cache["last_update"],
                         "trading_window_active": 8 <= hp <= 21, "hour_paris": hp,
                         "count": len(cache["signals"])})

@app.get("/api/prices")
async def get_prices():
    return JSONResponse({"prices": cache["prices"], "last_update": cache["last_update"]})

@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "status": "online", "version": "5.0-double-structure",
        "strategy": "Double Structure 6 étapes : Tendance→Figure→Zone(2retests)→Psy→Cassure→Management",
        "last_update": cache["last_update"], "signals_count": len(cache["signals"]),
        "pairs_scanned": len(ALL_PAIRS), "telegram_ok": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
    })

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r", encoding="utf-8") as f: return f.read()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
