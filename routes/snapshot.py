from fastapi import APIRouter, HTTPException, Query
import os
import requests
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/snapshot", tags=["snapshot"])

# -------------------------------------------------------------------
# NORMALIZACIÓN DE URLS / FEED
# -------------------------------------------------------------------
# IMPORTANTÍSIMO:
# - DATA_URL NO debe incluir /v2
#   (si viene con /v2 por error, lo corregimos)
_raw_data_url = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
DATA_URL = _raw_data_url[:-3] if _raw_data_url.endswith("/v2") else _raw_data_url

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")

# Para cuentas sin SIP, forzamos IEX por defecto (puedes override por query)
DEFAULT_FEED = os.getenv("APCA_DATA_FEED", "iex").strip().lower()  # iex | sip

# Si piden sip y falla por permisos, reintenta con iex (true por defecto)
ALLOW_FEED_FALLBACK = str(os.getenv("APCA_FEED_FALLBACK", "true")).strip().lower() in (
    "1", "true", "yes", "y", "on"
)

# Timeouts ajustables
HTTP_TIMEOUT_SEC = int(str(os.getenv("APCA_HTTP_TIMEOUT", "15")).strip() or "15")


def _headers() -> Dict[str, str]:
    if not API_KEY or not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Missing Alpaca keys (APCA_API_KEY_ID / APCA_API_SECRET_KEY)",
        )
    return {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
        "Accept": "application/json",
    }


# -------------------------------------------------------------------
# INDICADORES (EMA / RSI)
# -------------------------------------------------------------------
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
    # separación relativa EMA
    sep = abs(ema_fast - ema_slow) / max(price, 1e-9)

    # bias
    if ema_fast > ema_slow and rsi >= 52:
        bias = "bullish"
    elif ema_fast < ema_slow and rsi <= 48:
        bias = "bearish"
    else:
        bias = "neutral"

    # strength 1..3
    strength = 1
    if sep >= 0.0015:
        strength = 2
    if sep >= 0.0030:
        strength = 3
    if bias == "neutral":
        strength = 1

    return {"bias_inferred": bias, "trend_strength": strength, "sep": sep}


# -------------------------------------------------------------------
# PARSING ROBUSTO DE BARS (Alpaca puede variar estructura)
# -------------------------------------------------------------------
def _extract_bars_by_symbol(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    """
    Esperados comunes:
    - {"bars": {"QQQ": [..], "SPY":[..]}}
    - {"bars": [ { "S":"QQQ", "c":..., ... }, ... ]} (menos común)
    """
    if not isinstance(payload, dict):
        return {}

    bars = payload.get("bars")
    if isinstance(bars, dict):
        return {k: (v if isinstance(v, list) else []) for k, v in bars.items()}

    if isinstance(bars, list):
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in bars:
            if not isinstance(row, dict):
                continue
            sym = row.get("S") or row.get("symbol")
            if not sym:
                continue
            sym = str(sym).strip().upper()
            out.setdefault(sym, []).append(row)
        return out

    return {}


def _request_bars(url: str, params: Dict[str, Any]) -> Tuple[int, str, Any]:
    r = requests.get(url, headers=_headers(), params=params, timeout=HTTP_TIMEOUT_SEC)
    text = r.text or ""
    try:
        js = r.json() if text else {}
    except Exception:
        js = {}
    return r.status_code, text, js


@router.get("/indicators")
def indicators(
    # Acepta ambos para que Swagger no te “engañe”
    symbol: Optional[str] = Query(default=None, description="Símbolo único (alternativa a symbols)"),
    symbols: str = Query(default="QQQ,SPY,NVDA", description="Lista CSV de símbolos"),
    timeframe: str = Query(default="5Min", description="Ej: 1Min, 5Min, 15Min, 1Hour, 1Day"),
    limit: int = Query(default=200, ge=10, le=10000),
    lookback_hours: int = Query(default=48, ge=6, le=240),
    feed: Optional[str] = Query(default=None, description="iex o sip (si tu plan permite sip)"),
    # Ajustables por query si luego quieres afinar estrategia
    ema_fast_period: int = Query(default=9, ge=2, le=200),
    ema_slow_period: int = Query(default=21, ge=2, le=400),
    rsi_period: int = Query(default=14, ge=2, le=200),
    min_bars: int = Query(default=20, ge=10, le=500),
):
    # Normaliza símbolos
    if symbol:
        syms = [symbol.strip().upper()]
    else:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not syms:
            syms = ["QQQ"]

    # Rango ampliado para evitar insufficient_data
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=int(lookback_hours))

    # Endpoint correcto (SIN duplicar /v2)
    url = f"{DATA_URL}/v2/stocks/bars"

    # Feed seguro
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
        "feed": use_feed,
    }

    status_code, raw_text, payload = _request_bars(url, params)

    # Si pidieron SIP y el plan no lo permite, reintenta con IEX (si está habilitado)
    feed_fallback_used = False
    if (
        status_code == 403
        and use_feed == "sip"
        and ALLOW_FEED_FALLBACK
        and isinstance(raw_text, str)
        and ("subscription" in raw_text.lower() or "sip" in raw_text.lower())
    ):
        params_retry = dict(params)
        params_retry["feed"] = "iex"
        status_code, raw_text, payload = _request_bars(url, params_retry)
        if status_code == 200:
            feed_fallback_used = True
            use_feed = "iex"
            params = params_retry

    if status_code != 200:
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": "Alpaca rejected market data request",
                "alpaca_status": status_code,
                "alpaca_body": raw_text,
                "url": url,
                "params": params,
            },
        )

    bars_by_sym = _extract_bars_by_symbol(payload)

    out: Dict[str, Any] = {}
    for sym in syms:
        rows = bars_by_sym.get(sym, []) or []
        closes = []
        for x in rows:
            if not isinstance(x, dict):
                continue
            c = x.get("c")
            if c is None:
                continue
            try:
                closes.append(float(c))
            except Exception:
                continue

        if len(closes) < min_bars:
            out[sym] = {"status": "insufficient_data", "count": len(closes), "min_bars": min_bars}
            continue

        price = closes[-1]

        # Ventanas razonables (evitar slicing vacío)
        # Tomamos más barras que el periodo por estabilidad.
        ema_fast = _ema(closes[-max(ema_fast_period * 8, 60):], ema_fast_period)
        ema_slow = _ema(closes[-max(ema_slow_period * 8, 120):], ema_slow_period)
        rsi_val = _rsi(closes, rsi_period)

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
            "feed_fallback_used": feed_fallback_used,
            "timeframe": timeframe,
            "limit": limit,
            "lookback_hours": lookback_hours,
            "min_bars": min_bars,
            "ema_fast_period": ema_fast_period,
            "ema_slow_period": ema_slow_period,
            "rsi_period": rsi_period,
        },
    }
