from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from pydantic import BaseModel
import os
import json
import requests
from datetime import datetime

# Routers
from routes.test_alpaca import router as test_alpaca_router
from routes.recommend import router as recommend_router

# -----------------------------------------
# Cargar variables del entorno
# -----------------------------------------
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")
APCA_TRADING_URL = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets/v2")

if APCA_API_KEY_ID is None or APCA_API_SECRET_KEY is None:
    raise RuntimeError("Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY en .env")

TRADE_LOG_FILE = "trades.log"


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
    }


# -----------------------------------------
# Logging de trades
# -----------------------------------------
def log_trade(entry: dict):
    entry_with_time = {
        "timestamp": datetime.utcnow().isoformat(),
        **entry,
    }
    try:
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry_with_time) + "\n")
    except Exception as e:
        print("ERROR logging trade:", e)


def read_trade_logs(limit: int = 50):
    try:
        with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    lines = lines[-limit:]
    result = []
    for line in lines:
        try:
            result.append(json.loads(line.strip()))
        except:
            pass
    return result


# -----------------------------------------
# Modelos de request para /trade
# -----------------------------------------
class TradeRequest(BaseModel):
    symbol: str
    qty: int
    side: str           # "buy" o "sell"
    type: str = "market"
    time_in_force: str = "day"
    limit_price: float | None = None


# -----------------------------------------
# Crear app FASTAPI
# -----------------------------------------
app = FastAPI()

# Incluir routers externos
app.include_router(test_alpaca_router)
app.include_router(recommend_router)


# -----------------------------------------
# Endpoint para snapshot rápido
# -----------------------------------------
def get_latest_trade(symbol: str):
    url = f"{APCA_DATA_URL}/stocks/{symbol}/trades/latest"
    headers = alpaca_headers()
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


@app.get("/snapshot")
def market_snapshot():
    """Devuelve precio actual de QQQ, SPY, NVDA."""
    symbols = ["QQQ", "SPY", "NVDA"]
    data = {}

    try:
        for s in symbols:
            raw = get_latest_trade(s)
            trade = raw.get("trade", {})
            data[s] = {
                "price": trade.get("p"),
                "time": trade.get("t"),
            }

        return {"status": "ok", "data": data}

    except Exception as e:
        return {"status": "error", "message": str(e)}


# -----------------------------------------
# Endpoint para ejecutar órdenes PAPER
# -----------------------------------------
@app.post("/trade")
def place_trade(req: TradeRequest):
    order_url = f"{APCA_TRADING_URL}/orders"

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
        r = requests.post(order_url, headers=alpaca_headers(), json=payload)

        log_trade({
            "endpoint": "/trade",
            "request": payload,
            "status_code": r.status_code,
            "response_preview": r.text[:300],
        })

        r.raise_for_status()

        return {
            "status": "ok",
            "alpaca_response": r.json(),
        }

    except requests.HTTPError as e:
        log_trade({
            "endpoint": "/trade",
            "request": payload,
            "error": str(e),
        })
        raise HTTPException(status_code=400, detail=f"Alpaca error: {e}")

    except Exception as e:
        log_trade({
            "endpoint": "/trade",
            "request": payload,
            "error": str(e),
        })
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------
# Endpoint para leer logs
# -----------------------------------------
@app.get("/trades-log")
def get_trades_log(limit: int = 50):
    logs = read_trade_logs(limit)
    return {"count": len(logs), "items": logs}
