from fastapi import APIRouter, HTTPException, Query
import os
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

router = APIRouter(prefix="/snapshot", tags=["snapshot"])

# -----------------------------
# ENV (NORMALIZACIÓN)
# -----------------------------
_raw_data = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
# Si viene con /v2, lo quitamos para evitar /v2/v2/...
DATA_URL = _raw_data[:-3] if _raw_data.endswith("/v2") else _raw_data

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

# -----------------------------
# INDICADORES
# -----------------------------
def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return float(values[-1])
    k = 2 / (period + 1)
    ema = float(values[0])
    for v in values[1:]:
        ema = float(v) * k + ema * (1 - k)
    return float(ema)

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
    # separación relativa EMA (suave)
    sep = abs(ema_fast - ema_slow) / max(price, 1e-9)

    if ema_fast > ema_slow and rsi >= 52:
        bias = "bullish"
    elif ema_fast < ema_slow and rsi <= 48:
        bias = "bearish"
    else:
        bias = "neutral"

    strength = 1
    if sep >= 0.0015:
        strength = 2
    if sep >= 0.0030:
        strength = 3

    if bias == "neutral":
        strength = 1

    return {"bias_inferred": bias, "trend_strength": int(strength), "sep": float(sep)}

def _parse_symbols(symbol: Optional[str], symbols: Optional[str]) -> List[str]:
    # permite symbol=QQQ o symbols=QQQ,SPY,NVDA
    raw = symbols or symbol or "QQQ"
    out: List[str] = []
    for s in str(raw).split(","):
        s = s.strip().upper()
        if s:
            out.append(s)
    return out or ["QQQ"]

@router.get("/indicators")
def indicators(
    symbol: Optional[str] = Query(None, description="Alias: un solo símbolo (ej: QQQ)"),
    symbols: Optional[str] = Query("QQQ,SPY,NVDA", description="Lista CSV (ej: QQQ,SPY,NVDA)"),
    timeframe: str = Query("5Min", description="Ej: 1Min, 5Min, 15Min, 1Hour, 1Day"),
    time_frame: Optional[str] = Query(None, description="Alias de timeframe"),
    limit: int = Query(60, ge=10, le=1000),
    feed: str = Query("iex", description="iex (free) o sip (requiere plan). Default iex."),
):
    syms = _parse_symbols(symbol=symbol, symbols=symbols)
    tf = (time_frame or timeframe or "5Min").strip()
    feed = (feed or "iex").strip().lower()

    end = datetime.utcnow()
    start = end - timedelta(hours=8)

    url = f"{DATA_URL}/v2/stocks/bars"
    params = {
        "symbols": ",".join(syms),
        "timeframe": tf,
        "start": start.isoformat() + "Z",
        "end": end.isoformat() + "Z",
        "limit": str(limit),
        "adjustment": "raw",
        # ✅ clave para evitar 403 en cuentas sin SIP
        "feed": feed,
    }

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=12)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"message": f"Error calling Alpaca bars: {e}", "url": url, "params": params})

    if r.status_code != 200:
        # devuelve detalle completo para debug
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

    payload = r.json()
    bars = payload.get("bars", {}) if isinstance(payload, dict) else {}

    out: Dict[str, Any] = {}
    for sym in syms:
        rows = bars.get(sym, []) if isinstance(bars, dict) else []
        closes = [float(x.get("c")) for x in rows if isinstance(x, dict) and x.get("c") is not None]

        if len(closes) < 20:
            out[sym] = {"status": "insufficient_data", "count": len(closes)}
            continue

        price = float(closes[-1])
        ema_fast = _ema(closes[-30:], 9)
        ema_slow = _ema(closes[-60:], 21)
        rsi_val = _rsi(closes, 14)
        inf = _infer_bias_and_strength(price, ema_fast, ema_slow, rsi_val)

        out[sym] = {
            "status": "ok",
            "price": price,
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "rsi": float(rsi_val),
            **inf,
        }

    return {
        "status": "ok",
        "data": out,
        "meta": {
            "data_url": DATA_URL,
            "endpoint": "/v2/stocks/bars",
            "timeframe": tf,
            "limit": limit,
            "feed": feed,
            "symbols": syms,
        },
    }
