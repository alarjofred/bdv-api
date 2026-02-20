from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import os
import requests
from datetime import datetime
import json

# ---------------------------------
# Cargar variables del entorno
# ---------------------------------
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

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
# Normalización URLs Alpaca ✅
# ---------------------------------

# DATA BASE: SIEMPRE sin /v2 (para evitar v2/v2)
_raw_data = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
APCA_DATA_BASE = _raw_data[:-3] if _raw_data.endswith("/v2") else _raw_data

# TRADING BASE: SIEMPRE sin /v2 (porque los routers internos suelen agregar /v2)
_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
APCA_TRADING_BASE = _raw_trading[:-3] if _raw_trading.endswith("/v2") else _raw_trading

# ---------------------------------
# ✅ DISCO PERSISTENTE (Render Disk)
# Acepta BDV_PERSIST_DIR o PERSIST_DIR
# ---------------------------------
PERSIST_DIR = os.getenv("BDV_PERSIST_DIR") or os.getenv("PERSIST_DIR") or "/data"
os.makedirs(PERSIST_DIR, exist_ok=True)
TRADES_LOG_FILE = os.path.join(PERSIST_DIR, "trades-log.jsonl")

def append_trade_log(entry: dict) -> None:
    """Guarda una línea JSON por trade en archivo persistente (/data)."""
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with open(TRADES_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir en el log de trades: {e}")

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
from routes.agent import router as agent_router

# ✅ routers correctos para /trade, telegram, pending_trades
from routes import trade
from routes import telegram_notify
from routes import pending_trades

# ✅ snapshot router (/snapshot/indicators)
try:
    from routes.snapshot import router as snapshot_router
except Exception as e:
    snapshot_router = None
    print(f"[WARN] No se pudo importar routes.snapshot: {e}")

# opcionales
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
# Inicializar FastAPI
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
        "alpaca_keys_loaded": has_alpaca_keys(),
        "persist_dir": PERSIST_DIR,
        "apca_data_base": APCA_DATA_BASE,
        "apca_trading_base": APCA_TRADING_BASE,
        "snapshot_router_loaded": bool(snapshot_router is not None),
    }

@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "alpaca_keys_loaded": has_alpaca_keys()}

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
app.include_router(agent_router)

# ✅ IMPORTANTE: /trade debe venir SOLO de routes/trade.py
app.include_router(trade.router)

app.include_router(telegram_notify.router)
app.include_router(pending_trades.router)

# ✅ /snapshot/indicators (si snapshot_router carga)
if snapshot_router is not None:
    app.include_router(snapshot_router)

if analysis is not None:
    app.include_router(analysis.router)

if candles is not None:
    app.include_router(candles.router)

# ---------------------------------
# Función auxiliar: última cotización (bid/ask)
# ---------------------------------
def get_latest_quote(symbol: str) -> dict:
    """
    Endpoint Alpaca: /v2/stocks/{symbol}/quotes/latest -> {"quote": {...}}
    """
    url = f"{APCA_DATA_BASE}/v2/stocks/{symbol}/quotes/latest"
    r = requests.get(url, headers=alpaca_headers(), timeout=10)
    print(f"[DBG] Alpaca latest quote {symbol}: {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    return r.json()

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

        return {"status": "ok", "data": data}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERR] /snapshot: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting snapshot: {e}")

# ---------------------------------
# Endpoint /trades-log (leer log persistente)
# ---------------------------------
@app.get("/trades-log")
def get_trades_log(limit: int = 10):
    """
    Devuelve las últimas operaciones registradas en el log persistente.
    OJO: este archivo se llena si algún componente escribe en TRADES_LOG_FILE.
    """
    try:
        if not os.path.exists(TRADES_LOG_FILE):
            return {"status": "ok", "log": [], "file": TRADES_LOG_FILE}

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

        entries = entries[-limit:]
        return {"status": "ok", "log": entries, "file": TRADES_LOG_FILE}

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
