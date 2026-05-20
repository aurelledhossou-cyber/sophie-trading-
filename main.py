import os
import asyncio
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Sophie Trading API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWELVE_DATA_KEY  = os.getenv("TWELVE_DATA_KEY", "0a71d306b7f64336950805189681a0a2")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAJOR_PAIRS = ["EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD","NZD/USD","USD/CAD"]
MINOR_PAIRS = [
    "EUR/GBP","EUR/JPY","EUR/CHF","EUR/AUD","EUR/NZD",
    "GBP/JPY","GBP/CHF","GBP/AUD","GBP/NZD",
    "AUD/JPY","AUD/NZD","AUD/CHF","AUD/CAD",
    "NZD/JPY","NZD/CHF","NZD/CAD",
    "CAD/JPY","CAD/CHF","CHF/JPY"
]
ALL_PAIRS = MAJOR_PAIRS + MINOR_PAIRS

# ── CACHE ─────────────────────────────────────────────────────────────────────
cache = {
    "prices": {},
    "candles": {},
    "signals": [],
    "last_update": None,
    "last_notification": None,
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
async def fetch_price(pair: str) -> dict:
    symbol = pair.replace("/", "")
    url = "https://api.twelvedata.com/price"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params={"symbol": symbol, "apikey": TWELVE_DATA_KEY})
            data = r.json()
            if "price" in data:
                return {"pair": pair, "price": float(data["price"]), "ok": True}
    except Exception as e:
        print(f"Price error {pair}: {e}")
    return {"pair": pair, "price": None, "ok": False}

async def fetch_candles(pair: str, interval="1h", outputsize=50) -> list:
    symbol = pair.replace("/", "")
    url = "https://api.twelvedata.com/time_series"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "apikey": TWELVE_DATA_KEY
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
        print(f"Candles error {pair}: {e}")
    return []

# ── ANALYSE STRATÉGIQUE ───────────────────────────────────────────────────────
def analyze_strategy(candles: list, pair: str) -> dict | None:
    if not candles or len(candles) < 30:
        return None
    recent = candles[-50:]
    last   = recent[-1]
    prev   = recent[:-1]

    recent_highs = [c["high"] for c in prev[-10:]]
    recent_lows  = [c["low"]  for c in prev[-10:]]
    is_uptrend   = recent_highs[-1] > recent_highs[0] and recent_lows[-1] > recent_lows[0]
    is_downtrend = recent_highs[-1] < recent_highs[0] and recent_lows[-1] < recent_lows[0]
    if not is_uptrend and not is_downtrend:
        return None
    trend = "HAUSSIÈRE" if is_uptrend else "BAISSIÈRE"

    last_n = recent[-8:]
    range_h = max(c["high"] for c in last_n)
    range_l = min(c["low"]  for c in last_n)
    range_size = (range_h - range_l) / last["close"] if last["close"] else 0
    is_consolidating = range_size < 0.008
    top_slope = (last_n[-1]["high"] - last_n[0]["high"]) / len(last_n)
    bot_slope = (last_n[-1]["low"]  - last_n[0]["low"])  / len(last_n)
    is_triangle = abs(top_slope) < abs(bot_slope * 0.6) or abs(bot_slope) < abs(top_slope * 0.6)
    if not is_consolidating and not is_triangle:
        return None

    swing_highs, swing_lows = [], []
    for i in range(2, len(prev) - 2):
        if prev[i]["high"] > prev[i-1]["high"] and prev[i]["high"] > prev[i+1]["high"] and \
           prev[i]["high"] > prev[i-2]["high"] and prev[i]["high"] > prev[i+2]["high"]:
            swing_highs.append(prev[i]["high"])
        if prev[i]["low"] < prev[i-1]["low"] and prev[i]["low"] < prev[i+1]["low"] and \
           prev[i]["low"] < prev[i-2]["low"] and prev[i]["low"] < prev[i+2]["low"]:
            swing_lows.append(prev[i]["low"])

    price_zone = None
    if is_downtrend and swing_lows:
        price_zone = {"level": min(swing_lows[-3:]), "type": "SUPPORT"}
    if is_uptrend and swing_highs:
        price_zone = {"level": max(swing_highs[-3:]), "type": "RÉSISTANCE"}
    if not price_zone:
        return None

    round_base    = 1 if last["close"] > 10 else (0.1 if last["close"] > 1 else 0.01)
    nearest_round = round(last["close"] / round_base) * round_base
    near_psych    = abs(last["close"] - nearest_round) / last["close"] < 0.003 if last["close"] else False

    atr        = sum(c["high"] - c["low"] for c in recent[-14:]) / 14
    sl_dist    = atr * 1.2
    tp_dist    = sl_dist * 2.5
    direction  = -1 if is_downtrend else 1
    entry      = last["close"]
    stop_loss  = entry - direction * sl_dist
    take_profit= entry + direction * tp_dist
    ratio      = round(tp_dist / sl_dist, 2) if sl_dist else 0

    breakout = (is_downtrend and last["close"] < price_zone["level"] and last["close"] < last["open"]) or \
               (is_uptrend   and last["close"] > price_zone["level"] and last["close"] > last["open"])
    reintegrates = (is_downtrend and last["close"] > price_zone["level"]) or \
                   (is_uptrend   and last["close"] < price_zone["level"])
    if reintegrates:
        return None

    from datetime import datetime, timezone
    hour = (datetime.now(timezone.utc).hour + 2) % 24
    in_trading_hours = 8 <= hour <= 21

    score = 0
    if is_consolidating or is_triangle: score += 30
    if breakout:          score += 35
    if near_psych:        score += 15
    if ratio >= 3.0:      score += 10
    if in_trading_hours:  score += 10
    strength = "FORT" if score >= 80 else ("MOYEN" if score >= 55 else "FAIBLE")

    return {
        "pair":         pair,
        "trend":        trend,
        "figure":       "Triangle" if is_triangle else "Consolidation",
        "direction":    "SHORT" if is_downtrend else "LONG",
        "entry":        round(entry, 5),
        "stop_loss":    round(stop_loss, 5),
        "take_profit":  round(take_profit, 5),
        "ratio":        ratio,
        "strength":     strength,
        "score":        score,
        "breakout":     breakout,
        "in_trading_hours": in_trading_hours,
        "near_psych":   near_psych,
        "atr":          round(atr, 5),
        "price_zone":   price_zone,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }

# ── BACKGROUND WORKER ────────────────────────────────────────────────────────
async def update_market_data():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Updating market data...")
    tasks = [fetch_price(p) for p in ALL_PAIRS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict) and r.get("ok"):
            cache["prices"][r["pair"]] = r["price"]

    # Candles pour les majeures seulement (quota API)
    signals = []
    for pair in MAJOR_PAIRS:
        candles = await fetch_candles(pair, interval="1h", outputsize=50)
        if candles:
            cache["candles"][pair] = candles
            sig = analyze_strategy(candles, pair)
            if sig:
                signals.append(sig)
        await asyncio.sleep(0.5)

    signals.sort(key=lambda x: x["score"], reverse=True)
    cache["signals"]     = signals
    cache["last_update"] = datetime.now(timezone.utc).isoformat()

    # Notification Telegram si signaux forts dans les horaires
    hour = (datetime.now(timezone.utc).hour + 2) % 24
    if 8 <= hour <= 21:
        strong = [s for s in signals if s["strength"] == "FORT"]
        total  = len(signals)
        if total > 0:
            last_notif = cache.get("last_notification")
            should_notify = True
            if last_notif:
                delta = (datetime.now(timezone.utc) - datetime.fromisoformat(last_notif)).seconds
                should_notify = delta > 3600  # max 1 notif/heure
            if should_notify:
                msg = f"📡 <b>Sophie Trading — {datetime.now().strftime('%H:%M')}</b>\n\n"
                msg += f"🎯 <b>{total} opportunité(s)</b> détectée(s)\n"
                msg += f"🟢 Signaux FORTS : {len(strong)}\n\n"
                for s in strong[:3]:
                    emoji = "🟢" if s["direction"] == "LONG" else "🔴"
                    msg += f"{emoji} <b>{s['pair']}</b> — {s['direction']}\n"
                    msg += f"   Entrée : {s['entry']} | R/R : {s['ratio']}:1\n"
                msg += f"\n⏰ Fenêtre active : 8h–21h (Paris)"
                await send_telegram(msg)
                cache["last_notification"] = datetime.now(timezone.utc).isoformat()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done — {len(signals)} signals, {len(cache['prices'])} prices")

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
@app.get("/api/prices")
async def get_prices():
    return JSONResponse({
        "prices":      cache["prices"],
        "last_update": cache["last_update"],
        "count":       len(cache["prices"])
    })

@app.get("/api/signals")
async def get_signals():
    hour = (datetime.now(timezone.utc).hour + 2) % 24
    return JSONResponse({
        "signals":     cache["signals"],
        "last_update": cache["last_update"],
        "trading_window_active": 8 <= hour <= 21,
        "hour_paris":  hour,
        "count":       len(cache["signals"])
    })

@app.get("/api/candles/{pair}")
async def get_candles(pair: str):
    pair = pair.replace("-", "/").upper()
    candles = cache["candles"].get(pair, [])
    return JSONResponse({"pair": pair, "candles": candles})

@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "status":      "online",
        "last_update": cache["last_update"],
        "prices_count":  len(cache["prices"]),
        "signals_count": len(cache["signals"]),
        "telegram_configured": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
    })

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
