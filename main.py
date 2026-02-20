from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timedelta
import json
from typing import Dict, Any, List, Optional

# ---------------------------------
# Cargar variables del entorno
# ---------------------------------
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

# Data URL: lo normal es https://data.alpaca.markets/v2
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2").rstrip("/")

# Trading URL: normaliza para que siempre termine en /v2
_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
if _raw_trading.endswith("/v2"):
    APCA_TRADING_URL = _raw_trading
else:
    APCA_TRADING_URL = f"{_raw_trading}/v2"

# Build id (útil para auditar deploy)
BUILD_ID = os.getenv("BUILD_ID", "unknown")

# ✅ DISCO PERSISTENTE (Render Disk)
PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
os.makedirs(PERSIST_DIR, exist_ok=True)
TRADES_LOG_FILE = os.path.join(PERSIST_DIR, "trades-log.jsonl")


def has_alpaca_keys() -> bool:
    return bool(APCA_API_KEY_ID and APCA_API_SECRET_KEY)


def alpaca_headers() -> dict:
    """Headers básicos para cualquier llamada a Alpaca."""
    if not has_alpaca_keys():
        raise HTTPException(
            status_code=500,
            detail="Faltan APCA_API_KEY_ID / APCA_API_SECRET_KEY en el entorno (Render Environment).",
        )
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Accept": "application/json",
    }


# ---------------------------------
# IMPORT DE ROUTERS
# ---------------------------------
from routes.test_alpaca import router as test_alpaca_router
from routes.recommend import router as recommend_router
from routes.signals import router as signals_router
from routes.config import router as config_router
from routes.monitor import router as monitor_router
from routes.signals_ai import router as signals_ai_router
from routes.alpaca_close import router as alpaca_close_router
from routes.agent import router as agent_router  # ✅ routes/agent.py

# ✅ IMPORTS COMO ROUTERS (estos son los correctos para /trade, telegram, pending_trades)
from routes import trade
from routes import telegram_notify
from routes import pending_trades

# ✅ snapshot router (si existe)
# Importa el router completo (no solo "router as snapshot_router") para poder auditar y evitar crash.
try:
    from routes.snapshot import router as snapshot_router
except Exception as e:
    snapshot_router = None
    print(f"[WARN] No se pudo importar routes.snapshot: {e}")

# Estos pueden fallar si el archivo no existe / tiene error.
try:
    from routes import analysis
except Exception as e:
    analysis = None
    print(f"[WARN] No se pudo importar routes.analysis: {e}")

try:
    from routes import candles
except Exception as e:
    candles = None
    print(f"[WARN] No se pudo importar routes.candles: {e}")


# ---------------------------------
# Inicializar FastAPI ✅
# ---------------------------------
app = FastAPI(
    title="BDV API",
    version="0.1.0",
    servers=[
        {
            "url": "https://bdv-api-server.onrender.com",
            "description": "Render production",
        }
    ],
)

# ✅ Root healthcheck en "/"
@app.get("/", include_in_schema=False)
def root():
    return {
        "status": "ok",
        "service": "bdv-api",
        "message": "alive",
        "build_id": BUILD_ID,
        "alpaca_keys_loaded": has_alpaca_keys(),
        "persist_dir": PERSIST_DIR,
        "apca_data_url": APCA_DATA_URL,
        "apca_trading_url": APCA_TRADING_URL,
        "snapshot_router_loaded": bool(snapshot_router is not None),
    }


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "build_id": BUILD_ID, "alpaca_keys_loaded": has_alpaca_keys()}


# ---------------------------------
# Incluir routers
# ---------------------------------
app.include_router(test_alpaca_router)
app.include_router(recommend_router)
app.include_router(signals_router)
app.include_router(config_router)
app.include_router(monitor_router)
app.include_router(signals_ai_router)
app.include_router(alpaca_close_router)
app.include_router(agent_router)  # ✅ AGENTE

# ✅ IMPORTANTE: /trade debe venir SOLO de routes/trade.py
app.include_router(trade.router)

app.include_router(telegram_notify.router)
app.include_router(pending_trades.router)

# ✅ Agrega snapshot router si existe
# Nota: Si tu routes/snapshot.py incluye @router.get("/") dentro de prefix="/snapshot",
# podría chocar con @app.get("/snapshot") de abajo.
# Aun así, normalmente NO choca si tu router solo expone /snapshot/indicators u otros subpaths.
if snapshot_router is not None:
    app.include_router(snapshot_router)

if analysis is not None:
    app.include_router(analysis.router)

if candles is not None:
    app.include_router(candles.router)


# ---------------------------------
# Funciones auxiliares de mercado (data)
# ---------------------------------
def get_latest_quote(symbol: str) -> dict:
    """
    Consulta la última cotización (bid/ask) en Alpaca.
    Endpoint: /stocks/{symbol}/quotes/latest -> devuelve {"quote": {...}}
    """
    url = f"{APCA_DATA_URL}/stocks/{symbol}/quotes/latest"
    r = requests.get(url, headers=alpaca_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_bars(symbol: str, timeframe: str = "5Min", limit: int = 60) -> List[Dict[str, Any]]:
    """
    Trae velas (bars) desde Alpaca Data v2:
    /stocks/bars?symbols=...&timeframe=...&limit=...
    """
    url = f"{APCA_DATA_URL}/stocks/bars"
    params = {"symbols": symbol, "timeframe": timeframe, "limit": int(limit)}
    r = requests.get(url, headers=alpaca_headers(), params=params, timeout=12)
    r.raise_for_status()
    data = r.json() if r.text else {}
    # Alpaca devuelve {"bars": {"QQQ": [ ... ]}} en v2
    bars_map = data.get("bars", {}) if isinstance(data, dict) else {}
    bars = bars_map.get(symbol, [])
    return bars if isinstance(bars, list) else []


def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return float(values[-1])
    k = 2 / (period + 1)
    e = float(values[0])
    for v in values[1:]:
        e = float(v) * k + e * (1 - k)
    return float(e)


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    # calcula cambios de los últimos "period"
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


def infer_bias_and_strength(closes: List[float]) -> Dict[str, Any]:
    """
    Heurística simple pero útil para PRODUCCIÓN (paper):
    - bias bullish si EMA20 > EMA50 y RSI14 > 55
    - bias bearish si EMA20 < EMA50 y RSI14 < 45
    - neutral si no cumple
    - strength:
        1 = neutral/no señal
        2 = tendencia (EMA20 vs EMA50) pero RSI no confirma
        3 = tendencia + RSI confirma
    """
    if not closes or len(closes) < 20:
        return {"bias": "neutral", "trend_strength": 1, "ema20": None, "ema50": None, "rsi14": None}

    e20 = ema(closes[-60:], 20)
    e50 = ema(closes[-60:], 50)
    r14 = rsi(closes[-60:], 14)

    bias = "neutral"
    strength = 1

    if e20 > e50:
        bias = "bullish"
        strength = 2
        if r14 > 55:
            strength = 3
    elif e20 < e50:
        bias = "bearish"
        strength = 2
        if r14 < 45:
            strength = 3
    else:
        bias = "neutral"
        strength = 1

    return {
        "bias": bias,
        "trend_strength": int(strength),
        "ema20": float(e20),
        "ema50": float(e50),
        "rsi14": float(r14),
    }


# ---------------------------------
# Endpoint /snapshot (SE MANTIENE porque monitor.py lo usa)
# ---------------------------------
@app.get("/snapshot")
def market_snapshot():
    """
    Devuelve último precio (ask) y hora de QQQ, SPY y NVDA (usando quotes).
    """
    try:
        if not has_alpaca_keys():
            raise HTTPException(status_code=500, detail="Faltan keys de Alpaca para /snapshot.")

        symbols = ["QQQ", "SPY", "NVDA"]
        data = {}

        for sym in symbols:
            raw = get_latest_quote(sym)
            quote = raw.get("quote") or {}

            data[sym] = {
                "price": quote.get("ap"),
                "time": quote.get("t"),
                "bid": quote.get("bp"),
                "ask": quote.get("ap"),
            }

        return {"status": "ok", "data": data, "build_id": BUILD_ID}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERR] /snapshot: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting snapshot: {e}")


# ---------------------------------
# ✅ Fallback Endpoint /snapshot/indicators (para IA live)
# Si ya lo tienes en routes/snapshot.py, igual puedes usar este;
# si prefieres uno solo, luego lo quitamos.
# ---------------------------------
@app.get("/snapshot/indicators")
def snapshot_indicators(symbol: str = "QQQ", timeframe: str = "5Min", limit: int = 60):
    """
    Devuelve indicadores básicos para que monitor/IA decida bias y trend_strength.
    - bias: bullish/bearish/neutral
    - trend_strength: 1..3
    """
    symbol = str(symbol).strip().upper()
    if symbol not in ("QQQ", "SPY", "NVDA"):
        symbol = "QQQ"

    try:
        if not has_alpaca_keys():
            raise HTTPException(status_code=500, detail="Faltan keys de Alpaca para /snapshot/indicators.")

        bars = get_bars(symbol, timeframe=timeframe, limit=limit)
        closes = [float(b.get("c")) for b in bars if b.get("c") is not None]

        info = infer_bias_and_strength(closes)

        return {
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": int(limit),
            "indicators": info,
            "bars_count": len(closes),
            "build_id": BUILD_ID,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERR] /snapshot/indicators: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting indicators: {e}")


# ---------------------------------
# Log de trades en archivo persistente
# ---------------------------------
def append_trade_log(entry: dict) -> None:
    """Guarda una línea JSON por trade en archivo persistente (/data)."""
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with open(TRADES_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir en el log de trades: {e}")


# ---------------------------------
# Endpoint /trades-log (leer log persistente)
# ---------------------------------
@app.get("/trades-log")
def get_trades_log(limit: int = 10):
    """
    Devuelve las últimas operaciones registradas en el log persistente.
    """
    try:
        if not os.path.exists(TRADES_LOG_FILE):
            return {"status": "ok", "log": [], "file": TRADES_LOG_FILE, "build_id": BUILD_ID}

        entries = []
        with open(TRADES_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue

        entries = entries[-int(limit):]
        return {"status": "ok", "log": entries, "file": TRADES_LOG_FILE, "build_id": BUILD_ID}

    except Exception as e:
        print(f"[ERR] /trades-log: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading trades log: {e}")


# ---------------------------------
# Auto-sync (si tu analysis router lo usa)
# ---------------------------------
try:
    if analysis is not None:
        from routes.analysis import register_auto_sync
        register_auto_sync(app)
except Exception as e:
    print(f"[WARN] register_auto_sync no pudo registrarse: {e}")


# ---------------------------------
# UI (panel)
# ---------------------------------
if os.path.isdir("ui"):
    app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")
