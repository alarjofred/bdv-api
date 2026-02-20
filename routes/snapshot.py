from fastapi import APIRouter, HTTPException
import os, requests
from typing import Dict, Any, List
from datetime import datetime, timedelta

router = APIRouter(prefix="/snapshot", tags=["snapshot"])

DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")

def _headers():
    if not API_KEY or not API_SECRET:
        raise HTTPException(status_code=500, detail="Missing Alpaca keys")
    return {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
        "Accept": "application/json",
    }

def _ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return values[-1]
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema

def _rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i-1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def _infer_bias_and_strength(price: float, ema_fast: float, ema_slow: float, rsi: float) -> Dict[str, Any]:
    # Fuerza base por separación EMA
    sep = abs(ema_fast - ema_slow) / max(price, 1e-9)

    # Bias
    if ema_fast > ema_slow and rsi >= 52:
        bias = "bullish"
    elif ema_fast < ema_slow and rsi <= 48:
        bias = "bearish"
    else:
        bias = "neutral"

    # Strength 1..3 (suave, estable)
    strength = 1
    if sep >= 0.0015:  # ~0.15%
        strength = 2
    if sep >= 0.0030:  # ~0.30%
        strength = 3

    # Si neutral, bajamos fuerza
    if bias == "neutral":
        strength = 1

    return {"bias_inferred": bias, "trend_strength": strength, "sep": sep}

@router.get("/indicators")
def indicators(symbols: str = "QQQ,SPY,NVDA", timeframe: str = "5Min", limit: int = 60):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        syms = ["QQQ"]

    end = datetime.utcnow()
    start = end - timedelta(hours=8)

    url = f"{DATA_URL}/v2/stocks/bars"
    params = {
        "symbols": ",".join(syms),
        "timeframe": timeframe,
        "start": start.isoformat() + "Z",
        "end": end.isoformat() + "Z",
        "limit": str(limit),
        "adjustment": "raw",
    }

    r = requests.get(url, headers=_headers(), params=params, timeout=12)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail={"alpaca": r.text, "url": url})

    payload = r.json()
    bars = payload.get("bars", {})

    out: Dict[str, Any] = {}
    for sym in syms:
        rows = bars.get(sym, [])
        closes = [float(x["c"]) for x in rows if "c" in x]
        if len(closes) < 20:
            out[sym] = {"status": "insufficient_data", "count": len(closes)}
            continue

        price = closes[-1]
        ema_fast = _ema(closes[-30:], 9)
        ema_slow = _ema(closes[-60:], 21)
        rsi = _rsi(closes, 14)

        inf = _infer_bias_and_strength(price, ema_fast, ema_slow, rsi)

        out[sym] = {
            "status": "ok",
            "price": price,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi,
            **inf,
        }

    return {"status": "ok", "data": out}
