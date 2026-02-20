from fastapi import APIRouter, HTTPException
import os
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/snapshot", tags=["snapshot"])

# DATA_URL base SIN /v2
DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
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
        return values[-1]
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return float(ema)


def _rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _infer_bias_and_strength(price: float, ema_fast: float, ema_slow: float, rsi: float) -> Dict[str, Any]:
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

    return {"bias_inferred": bias, "trend_strength": strength, "sep": float(sep)}


def _fetch_bars_multi(
    syms: List[str],
    timeframe: str,
    limit: int,
    hours_back: int,
    feed: Optional[str] = None,
) -> Dict[str, Any]:
    # endpoint multi-symbol
    url = f"{DATA_URL}/v2/stocks/bars"

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)

    params: Dict[str, Any] = {
        "symbols": ",".join(syms),
        "timeframe": timeframe,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": str(limit),
        "adjustment": "raw",
    }
    if feed:
        params["feed"] = feed  # clave para planes sin SIP

    r = requests.get(url, headers=_headers(), params=params, timeout=15)
    if r.status_code != 200:
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
    return r.json()


def _compute_indicators_from_payload(syms: List[str], payload: Dict[str, Any], min_needed: int = 20) -> Dict[str, Any]:
    bars = payload.get("bars", {}) if isinstance(payload, dict) else {}
    out: Dict[str, Any] = {}

    for sym in syms:
        rows = bars.get(sym, []) if isinstance(bars, dict) else []
        closes = [float(x["c"]) for x in rows if isinstance(x, dict) and "c" in x]

        if len(closes) < min_needed:
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

    return out


@router.get("/indicators")
def indicators(
    # Swagger a veces envía symbol=QQQ, así que soportamos ambos
    symbol: Optional[str] = None,
    symbols: str = "QQQ,SPY,NVDA",
    timeframe: str = "5Min",
    limit: int = 60,
):
    # Normaliza símbolos
    if symbol and str(symbol).strip():
        syms = [str(symbol).strip().upper()]
    else:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not syms:
            syms = ["QQQ"]

    # 1) Intento intradía con IEX (evita error SIP)
    intraday_payload = _fetch_bars_multi(
        syms=syms,
        timeframe=timeframe,
        limit=limit,
        hours_back=8,
        feed="iex",
    )
    out = _compute_indicators_from_payload(syms, intraday_payload, min_needed=20)

    # 2) Fallback: si un símbolo no tiene data suficiente, usamos 1Day (casi siempre permitido)
    need_fallback = [s for s in syms if out.get(s, {}).get("status") != "ok"]
    fallback_meta: Dict[str, Any] = {}

    if need_fallback:
        daily_payload = _fetch_bars_multi(
            syms=need_fallback,
            timeframe="1Day",
            limit=max(60, limit),
            hours_back=24 * 120,  # 120 días
            feed=None,  # daily suele funcionar sin feed
        )
        daily_out = _compute_indicators_from_payload(need_fallback, daily_payload, min_needed=20)

        for s in need_fallback:
            if daily_out.get(s, {}).get("status") == "ok":
                out[s] = daily_out[s]
                fallback_meta[s] = {"used_fallback": True, "fallback_timeframe": "1Day"}

    return {
        "status": "ok",
        "data": out,
        "meta": {
            "data_url": DATA_URL,
            "intraday_endpoint": "/v2/stocks/bars",
            "intraday_feed": "iex",
            "intraday_timeframe": timeframe,
            "intraday_limit": limit,
            "fallback": fallback_meta,
        },
    }
