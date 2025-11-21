from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from pydantic import BaseModel
import os
import json
import requests
from datetime import datetime

# ==========================
# Routers externos (otros archivos)
# ==========================
from routes.test_alpaca import router as test_alpaca_router
from routes.recommend import router as recommend_router
from routes.signals import router as signals_router
from routes.config import router as config_router

# ==========================
# Cargar variables de entorno
# ==========================
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")
APCA_TRADING_URL = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets/v2")

# Archivo donde guardamos el log de operaciones
TRADE_LOG_FILE = "trades-log.jsonl"  # un JSON por línea

if APCA_API_KEY_ID is None or APCA_API_SECRET_KEY is None:
    raise RuntimeError("Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY en el .env")


def alpaca_headers():
    """Headers estándar para llamar a la API de Alpaca."""
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Content-Type": "application/json",
    }


# ==========================
# Crear app FastAPI
# ==========================
app = FastAPI(title="BDV API", version="0.1.0")

# Incluir routers de otros archivos
app.include_router(test_alpaca_router)
app.include_router(recommend_router)
app.include_router(signals_router)
app.include_router(config_router)


# ==========================
# /snapshot  (último precio QQQ, SPY, NVDA)
# ==========================
def get_latest_trade(symbol: str):
    """Consulta el último trade de un símbolo en Alpaca DATA v2."""
    url = f"{APCA_DATA_URL}/stocks/{symbol}/trades/latest"
    r = requests.get(url, headers=alpaca_headers())
    print(f"[DBG] Alpaca latest trade {symbol}: {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    return r.json()


@app.get("/snapshot")
def market_snapshot():
    """Devuelve último precio y hora de QQQ, SPY y NVDA."""
    try:
        symbols = ["QQQ", "SPY", "NVDA"]
        data = {}

        for sym in symbols:
            raw = get_latest_trade(sym)
            trade = raw.get("trade", {})
            data[sym] = {
                "price": trade.get("p"),
                "time": trade.get("t"),
            }

        return {"status": "ok", "data": data}
    except Exception as e:
        print("[ERR] /snapshot:", e)
        raise HTTPException(status_code=500, detail=str(e))


# ==========================
# /trade  (ejecutar orden en Alpaca)
# ==========================
class TradeRequest(BaseModel):
    symbol: str
    side: str              # "buy" o "sell"
    qty: int
    type: str = "market"   # market / limit
    time_in_force: str = "day"  # day / gtc / etc.


def log_trade(payload, response_json, error: str | None = None):
    """Guarda en archivo cada operación enviada a Alpaca."""
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "payload": payload,
        "response": response_json,
        "error": error,
    }
    try:
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print("[ERR] al escribir log:", e)


@app.post("/trade")
def place_trade(req: TradeRequest):
    """Envía una orden a Alpaca y la registra en el log."""
    url = f"{APCA_TRADING_URL}/v2/orders"

    payload = {
        "symbol": req.symbol,
        "side": req.side,
        "qty": req.qty,
        "type": req.type,
        "time_in_force": req.time_in_force,
    }

    try:
        r = requests.post(url, headers=alpaca_headers(), data=json.dumps(payload))
        print(f"[DBG] /trade {req.symbol}: {r.status_code} {r.text[:200]}")

        if not r.ok:
            # Error HTTP de Alpaca
            log_trade(payload, r.text, error="HTTP_ERROR")
            raise HTTPException(status_code=r.status_code, detail=r.text)

        data = r.json()
        log_trade(payload, data)
        return {"status": "ok", "order": data}

    except HTTPException:
        # Re-lanzar para que FastAPI devuelva el status correcto
        raise
    except Exception as e:
        # Cualquier otra excepción
        log_trade(payload, None, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ==========================
# /trades-log  (ver historial de órdenes)
# ==========================
@app.get("/trades-log")
def get_trades_log(limit: int = 50):
    """Devuelve las últimas operaciones registradas en el log."""
    try:
        if not os.path.exists(TRADE_LOG_FILE):
            return {"status": "ok", "log": []}

        lines = []
        with open(TRADE_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))

        # Solo las últimas N
        return {"status": "ok", "log": lines[-limit:]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
