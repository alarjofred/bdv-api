# routes/analysis.py
import os
import json
import requests
import numpy as np
from datetime import datetime
from fastapi import APIRouter
from fastapi import FastAPI
from fastapi_utils.tasks import repeat_every

router = APIRouter(prefix="/analysis", tags=["analysis"])

# ===============================
#  LOG DE ANÁLISIS
# ===============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "analysis-log.jsonl")

# 🧠 Memoria temporal (Render no garantiza persistencia de disco entre reinicios)
analysis_history = []


def append_analysis_log(entry: dict):
    """Guarda el resultado en memoria y opcionalmente en archivo local."""
    try:
        analysis_history.append(entry)

        line = json.dumps(entry, ensure_ascii=False)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir el log de análisis: {e}")


# ===============================
#  CONFIGURACIÓN ALPACA
# ===============================
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

# ✅ CLAVE: FEED correcto para BARS (en cuentas sin SIP, usa IEX)
APCA_DATA_FEED = os.getenv("APCA_DATA_FEED", "iex")  # "iex" recomendado


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Accept": "application/json",
    }


# ===============================
#  FUNCIONES TÉCNICAS
# ===============================
def ema(values, period=20):
    """Calcula EMA simple."""
    values = np.array(values, dtype=float)
    if len(values) == 0:
        return 0.0
    if len(values) < period:
        return float(np.mean(values))

    weights = np.exp(np.linspace(-1.0, 0.0, period))
    weights /= weights.sum()
    a = np.convolve(values, weights, mode="full")[: len(values)]
    a[:period] = a[period]
    return float(a[-1])


def calc_rsi(closes, period=14):
    """RSI manual."""
    closes = np.array(closes, dtype=float)
    if len(closes) < period + 1:
        return 50.0

    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = (-seed[seed < 0]).sum() / period

    if down == 0:
        return 100.0

    rs = up / down
    rsi = 100 - (100 / (1 + rs))

    for delta in deltas[period:]:
        upval = delta if delta > 0 else 0.0
        downval = -delta if delta < 0 else 0.0
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down if down != 0 else 0
        rsi = 100 - (100 / (1 + rs)) if down != 0 else 100.0

    return float(rsi)


def _fetch_bars(symbol: str, timeframe: str = "5Min", limit: int = 100) -> list:
    """
    Trae barras desde Alpaca.
    ✅ Incluye feed=iex por defecto para evitar 'bars: []' en cuentas sin SIP.
    """
    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "adjustment": "raw",
        "feed": APCA_DATA_FEED,
    }

    r = requests.get(url, headers=alpaca_headers(), params=params, timeout=10)

    # Debug útil en logs de Render
    print(f"[DBG] bars {symbol} {timeframe} status={r.status_code} body={r.text[:200]}")

    r.raise_for_status()
    data = r.json() or {}
    return data.get("bars", []) or []


# ===============================
#  ENDPOINT PRINCIPAL: BIAS
# ===============================
@router.get("/bias/{symbol}")
def get_market_bias(symbol: str):
    """
    Evalúa tendencia, momentum y fuerza usando EMA9, EMA20, RSI y volumen.
    Devuelve bias (bullish/bearish/neutral) con confianza.
    """
    symbol = symbol.upper().strip()

    # 1) Intento intradía 5Min
    bars = _fetch_bars(symbol, timeframe="5Min", limit=120)

    # 2) Si está fuera de horario o no hay data, usa 1Day como fallback
    if not bars:
        bars = _fetch_bars(symbol, timeframe="1Day", limit=60)

    if not bars:
        return {"symbol": symbol, "bias": "neutral", "note": "No se recibieron datos (bars vacíos)"}

    closes = np.array([b.get("c") for b in bars if b.get("c") is not None], dtype=float)
    volumes = np.array([b.get("v", 0) for b in bars], dtype=float)

    if len(closes) < 30:
        return {"symbol": symbol, "bias": "neutral", "note": "Datos insuficientes"}

    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    rsi = calc_rsi(closes)
    price = float(closes[-1])

    # volumen relativo (si no hay volumen útil, cae a 1.0)
    vol_base = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    vol_ratio = float(volumes[-1] / vol_base) if vol_base and vol_base != 0 else 1.0

    # --- Score simple ---
    score = 0
    if price > ema9 > ema20:
        score += 1
    if rsi > 55:
        score += 1
    if vol_ratio > 1.1:
        score += 1

    if score >= 2:
        bias = "bullish"
        confidence = min(0.5 + score * 0.2, 1.0)
    elif score == 1:
        bias = "neutral"
        confidence = 0.45
    else:
        bias = "bearish"
        confidence = 0.7

    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": symbol,
        "price": round(price, 2),
        "ema9": round(ema9, 2),
        "ema20": round(ema20, 2),
        "rsi": round(rsi, 2),
        "volume_ratio": round(vol_ratio, 2),
        "bias": bias,
        "confidence": round(float(confidence), 2),
    }
    append_analysis_log(log_entry)
    return log_entry


# ===============================
#  ENDPOINT: HISTORIAL TEMPORAL
# ===============================
@router.get("/history")
def get_analysis_history(limit: int = 10):
    return list(reversed(analysis_history[-limit:]))


# ===============================
#  ENDPOINT: SINCRONIZACIÓN PANEL BDV
# ===============================
@router.get("/sync")
def sync_analysis_data():
    if not analysis_history:
        return {"status": "empty", "message": "No hay datos para sincronizar."}

    latest_by_symbol = {}
    for entry in reversed(analysis_history):
        sym = entry.get("symbol")
        if sym and sym not in latest_by_symbol:
            latest_by_symbol[sym] = entry

    synced_data = [{"symbol": sym, **latest_by_symbol[sym]} for sym in sorted(latest_by_symbol.keys())]

    return {
        "status": "ok",
        "count": len(synced_data),
        "synced": synced_data,
        "note": "Datos listos para integración con Panel IA BDV",
    }


# ===============================
#  AUTO-SYNC (1 minuto)
# ===============================
def register_auto_sync(app: FastAPI):
    @app.on_event("startup")
    @repeat_every(seconds=60)
    def auto_sync_task() -> None:
        symbols = ["QQQ", "SPY", "NVDA"]
        print("[AUTO-SYNC] Iniciando actualización automática...")

        for sym in symbols:
            try:
                get_market_bias(sym)
                print(f"[AUTO-SYNC] ✅ {sym} actualizado")
            except Exception as e:
                print(f"[AUTO-SYNC] ⚠️ Error {sym}: {e}")

        print(f"[AUTO-SYNC] Fin. {datetime.utcnow().isoformat()}")


# ===============================
#  HEALTH
# ===============================
@router.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "BDV API Server",
        "analysis_count": len(analysis_history),
        "last_update": analysis_history[-1]["timestamp"] if analysis_history else None,
        "note": "Servicio activo ✅",
    }
