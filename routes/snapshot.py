from fastapi import APIRouter, HTTPException, Query
import os
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

router = APIRouter(prefix="/snapshot", tags=["snapshot"])

# ------------------------------------------------------
# Normalización: DATA_URL NO debe terminar en /v2
# ------------------------------------------------------
_raw_data = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
DATA_URL = _raw_data[:-3] if _raw_data.endswith("/v2") else _raw_data  # quita /v2 si existe

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")


def _headers() -> Dict[str, str]:
    if not API_KEY or not API_SECRET:
        raise HTTPException(status_code=500, detail="Missing Alpaca keys (APCA_API_KEY_ID / APCA_API_SECRET_KEY)")
    return {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
        "Accept": "application/json",
    }


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return float(values[-1])
    k = 2 / (period + 1)
    ema_val = float(values[0])
    for v in values[1:]:
        ema_val = float(v) * k + ema_val * (1 - k)
    return float(ema_val)


def _rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = float(values[i]) - float(values[i - 1])
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _infer_bias_and_strength(price: float, ema_fast: float, ema_slow: float, rsi: float) -> Dict[str, Any]:
    # separación relativa EMA
    sep = abs(ema_fast - ema_slow) / max(price, 1e-9)

    # bias
    if ema_fast > ema_slow and rsi >= 52:
        bias = "bullish"
    elif ema_fast < ema_slow and rsi <= 48:
        bias = "bearish"
    else:
        bias = "neutral"

    # strength 1..3 (suave)
    strength = 1
    if sep >= 0.0015:
        strength = 2
    if sep >= 0.0030:
        strength = 3

    if bias == "neutral":
        strength = 1

    return {"bias_inferred": bias, "trend_strength": strength, "sep": sep}


@router.get("/indicators")
def indicators(
    # soporta symbol=QQQ (uno) o symbols=QQQ,SPY,NVDA (varios)
    symbol: Optional[str] = Query(default=None),
    symbols: str = Query(default="QQQ,SPY,NVDA"),
    timeframe: str = Query(default="5Min"),
    limit: int = Query(default=60, ge=10, le=1000),
):
    # Decide lista final de símbolos
    if symbol and str(symbol).strip():
        syms = [str(symbol).strip().upper()]
    else:
        syms = [s.strip().upper() for s in str(symbols).split(",") if s.strip()]
        if not syms:
            syms = ["QQQ"]

    end = datetime.utcnow()
    start = end - timedelta(hours=8)

    # Alpaca Market Data: /v2/stocks/bars
    url = f"{DATA_URL}/v2/stocks/bars"
    params = {
        "symbols": ",".join(syms),
        "timeframe": timeframe,
        "start": start.isoformat() + "Z",
        "end": end.isoformat() + "Z",
        "limit": str(limit),
        "adjustment": "raw",
    }

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=12)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"message": "Network error calling Alpaca", "error": str(e), "url": url})

    if r.status_code != 200:
        # devuelve detalles útiles para debug
        raise HTTPException(
            status_code=r.status_code,
            detail={
                "message": "Alpaca rejected market data request",
                "alpaca_status": r.status_code,
                "alpaca_body": r.text,
                "url": url,
                "params": params,
            },
        )

    payload = r.json() if r.text else {}
    bars = payload.get("bars", {}) if isinstance(payload, dict) else {}

    out: Dict[str, Any] = {}

    for sym in syms:
        rows = bars.get(sym, []) if isinstance(bars, dict) else []
        closes = []
        for x in rows:
            try:
                closes.append(float(x.get("c")))
            except Exception:
                continue

        if len(closes) < 20:
            out[sym] = {"status": "insufficient_data", "count": len(closes)}
            continue

        price = closes[-1]
        ema_fast = _ema(closes[-30:], 9)
        ema_slow = _ema(closes[-60:], 21)
        rsi_val = _rsi(closes, 14)
        inf = _infer_bias_and_strength(price, ema_fast, ema_slow, rsi_val)

        out[sym] = {
            "status": "ok",
            "price": price,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi_val,
            **inf,
        }

    return {"status": "ok", "data": out, "meta": {"data_url": DATA_URL, "timeframe": timeframe, "limit": limit, "symbols": syms}}
