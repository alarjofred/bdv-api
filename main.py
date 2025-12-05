from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional, Literal
import os
import json
import requests
from datetime import datetime

# ---------------------------------
# Cargar variables del entorno
# ---------------------------------
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")
APCA_TRADING_URL = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets/v2")

TRADES_LOG_FILE = "trades-log.jsonl"

if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    raise RuntimeError("Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY en el entorno (.env o secretos de Render)")

def alpaca_headers() -> dict:
    """Headers básicos para cualquier llamada a Alpaca."""
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
from routes import trade
from routes import telegram_notify
from routes import pending_trades

# ---------------------------------
# Inicializar FastAPI
# ---------------------------------
app = FastAPI(title="BDV API", version="0.1.0")

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
app.include_router(trade.router)
app.include_router(telegram_notify.router)
app.include_router(pending_trades.router)

# ---------------------------------
# Función auxiliar: última cotización (bid/ask)
# ---------------------------------
def get_latest_quote(symbol: str) -> dict:
    """Consulta la última cotización (bid/ask) en Alpaca para obtener precio más actual."""
    url = f"{APCA_DATA_URL}/stocks/{symbol}/quotes/latest"
    r = requests.get(url, headers=alpaca_headers(), timeout=10)
    print(f"[DBG] Alpaca latest quote {symbol}: {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    return r.json()

# ---------------------------------
# Endpoint /snapshot
# ---------------------------------
@app.get("/snapshot")
def market_snapshot():
    """
    Devuelve último precio y hora de QQQ, SPY y NVDA (usando quotes en vivo).
    """
    try:
        symbols = ["QQQ", "SPY", "NVDA"]
        data = {}

        for sym in symbols:
            raw = get_latest_quote(sym)
            quote = raw.get("quote", {}) or raw.get("quotes", [{}])[0]
            data[sym] = {"price": quote.get("ap"), "time": quote.get("t")}

        return {"status": "ok", "data": data}
    except Exception as e:
        print(f"[ERR] /snapshot: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting snapshot: {e}")


# ---------------------------------
# Endpoint /recommend
# ---------------------------------
@app.get("/recommend")
def recommend():
    """
    Devuelve recomendaciones básicas de mercado.
    """
    data = {
        "status": "ok",
        "recommendations": [
            {
                "symbol": "QQQ",
                "price": 622.91,
                "bias": "neutral",
                "suggestion": "wait",
                "target": 622.91,
                "stop": 622.91,
            },
            {
                "symbol": "SPY",
                "price": 684.28,
                "bias": "neutral",
                "suggestion": "wait",
                "target": 684.28,
                "stop": 684.28,
            },
            {
                "symbol": "NVDA",
                "price": 183.53,
                "bias": "neutral",
                "suggestion": "wait",
                "target": 183.53,
                "stop": 183.53,
            },
        ],
        "note": "Basado en snapshot. Lógica simple; el GPT BDV aplica análisis y gestión de riesgo.",
    }

    return JSONResponse(content=data)

# ---------------------------------
# Modelo para /trade
# ---------------------------------
class TradeRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    qty: int
    type: Literal["market", "limit"] = "market"
    time_in_force: Literal["day", "gtc"] = "day"
    limit_price: Optional[float] = None

# ---------------------------------
# Log de trades en archivo local
# ---------------------------------
def append_trade_log(entry: dict) -> None:
    """Guarda una línea JSON por trade en un archivo local."""
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with open(TRADES_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir en el log de trades: {e}")

# ---------------------------------
# Endpoint /trade  (ejecutar orden en Alpaca)
# ---------------------------------
@app.post("/trade")
def place_trade(req: TradeRequest):
    """
    Envía una orden a Alpaca y la registra en el log.
    """
    orders_url = f"{APCA_TRADING_URL}/orders"
    payload = {
        "symbol": req.symbol,
        "qty": req.qty,
        "side": req.side,
        "type": req.type,
        "time_in_force": req.time_in_force,
    }

    if req.type == "limit" and req.limit_price is not None:
        payload["limit_price"] = req.limit_price

    try:
        print(f"[DBG] Enviando orden Alpaca: {payload}")
        r = requests.post(orders_url, headers=alpaca_headers(), json=payload, timeout=10)
        raw_text = r.text
        print(f"[DBG] Respuesta Alpaca: {r.status_code} {raw_text[:300]}")

        try:
            body = r.json()
        except Exception:
            body = {"raw": raw_text}

        status = "ok" if r.status_code < 400 else "error"

        log_entry = {
            "timestamp_utc": datetime.utcnow().isoformat(),
            "symbol": req.symbol,
            "side": req.side,
            "qty": req.qty,
            "type": req.type,
            "time_in_force": req.time_in_force,
            "status": status,
            "http_status": r.status_code,
            "alpaca_response": body,
        }
        append_trade_log(log_entry)

        if status == "error":
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Error placing order in Alpaca",
                    "alpaca_status": r.status_code,
                    "alpaca_body": body,
                },
            )

        return {"status": "ok", "order": body}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERR] /trade: {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error placing trade: {e}")

# ---------------------------------
# Endpoint /trades-log  (leer log)
# ---------------------------------
@app.get("/trades-log")
def get_trades_log(limit: int = 10):
    """
    Devuelve las últimas operaciones registradas en el log local.
    """
    try:
        if not os.path.exists(TRADES_LOG_FILE):
            return {"status": "ok", "log": []}

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
        return {"status": "ok", "log": entries}
    except Exception as e:
        print(f"[ERR] /trades-log: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading trades log: {e}")

