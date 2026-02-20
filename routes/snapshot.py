from fastapi import APIRouter, HTTPException, Query
import os, requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/snapshot", tags=["snapshot"])

# IMPORTANTÍSIMO:
# - DATA_URL NO debe incluir /v2
# - TRADING_URL no se usa aquí
DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")

# Para cuentas sin SIP, forzamos IEX por defecto (puedes override por query)
DEFAULT_FEED = os.getenv("APCA_DATA_FEED", "iex").strip().lower()  # iex | sip (si tu plan lo permite)


def _headers():
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
    return ema


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

    return {"bias_inferred": bias, "trend_strength": strength, "sep": sep}


@router.get("/indicators")
def indicators(
    # Acepta ambos para que Swagger no te “engaňe”:
    symbol: Optional[str] = Query(default=None, description="Símbolo único (alternativa a symbols)"),
    symbols: str = Query(default="QQQ,SPY,NVDA", description="Lista CSV de símbolos"),
    timeframe: str = Query(default="5Min", description="Ej: 1Min, 5Min, 15Min, 1Hour, 1Day"),
    limit: int = Query(default=200, ge=10, le=10000),
    lookback_hours: int = Query(default=48, ge=6, le=240),
    feed: Optional[str] = Query(default=None, description="iex o sip (si tu plan permite sip)"),
):
    # Normaliza símbolos
    if symbol:
        syms = [symbol.strip().upper()]
    else:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not syms:
            syms = ["QQQ"]

    # Rango: ampliado por defecto para evitar insufficient_data
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=int(lookback_hours))

    # Endpoint correcto (SIN duplicar /v2)
    url = f"{DATA_URL}/v2/stocks/bars"

    # Feed seguro para planes sin SIP
    use_feed = (feed or DEFAULT_FEED or "iex").strip().lower()
    if use_feed not in ("iex", "sip"):
        use_feed = "iex"

    params = {
        "symbols": ",".join(syms),
        "timeframe": timeframe,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": str(limit),
        "adjustment": "raw",
        "feed": use_feed,  # <-- clave para evitar 403 SIP
    }

    r = requests.get(url, headers=_headers(), params=params, timeout=15)
    if r.status_code != 200:
        # Devuelve detalles útiles (tu screenshot lo muestra así)
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
        rows = bars.get(sym, []) or []
        closes = [float(x.get("c")) for x in rows if x.get("c") is not None]

        if len(closes) < 20:
            out[sym] = {"status": "insufficient_data", "count": len(closes)}
            continue

        price = closes[-1]
        ema_fast = _ema(closes[-60:], 9)
        ema_slow = _ema(closes[-120:], 21)
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

    return {
        "status": "ok",
        "data": out,
        "meta": {
            "data_url": DATA_URL,
            "endpoint": "/v2/stocks/bars",
            "feed": use_feed,
            "timeframe": timeframe,
            "limit": limit,
            "lookback_hours": lookback_hours,
        },
    }
