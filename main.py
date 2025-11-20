from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from pydantic import BaseModel
import os
import json
import requests
from datetime import datetime

from routes.test_alpaca import router as test_alpaca_router

# 1) Cargar variables del .env
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")
APCA_TRADING_URL = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets/v2")

if APCA_API_KEY_ID is None or APCA_API_SECRET_KEY is None:
    raise RuntimeError("Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY en el .env")

TRADE_LOG_FILE = "trades.log"

app = FastAPI(title="BDV Opciones API", version="1.0.0")

# Incluir ruta de /test-alpaca
app.include_router(test_alpaca_router)


# ---------- Helpers Alpaca ----------

def alpaca_headers():
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
    }


def get_latest_trade(symbol: str) -> dict:
    """Consulta el último trade de un símbolo en Alpaca (data API)."""
    url = f"{APCA_DATA_URL}/stocks/{symbol}/trades/latest"
    r = requests.get(url, headers=alpaca_headers())
    r.raise_for_status()
    return r.json()


def get_daily_change(symbol: str) -> dict:
    """
    Devuelve cambio diario usando barras 1D.
    Calcula % de cambio del último close vs close anterior.
    """
    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {"timeframe": "1Day", "limit": 2}
    r = requests.get(url, headers=alpaca_headers(), params=params)
    r.raise_for_status()
    data = r.json()

    bars = data.get("bars", [])
    if len(bars) < 2:
        return {"symbol": symbol, "error": "Not enough bars"}

    prev_close = bars[-2]["c"]
    last_close = bars[-1]["c"]
    change_pct = (last_close - prev_close) / prev_close * 100

    return {
        "symbol": symbol,
        "prev_close": prev_close,
        "last_close": last_close,
        "change_pct": change_pct,
    }


# ---------- Logs de trading ----------

def log_trade(entry: dict):
    """Guarda un log sencillo en trades.log (JSON por línea)."""
    entry_with_time = {
        "timestamp": datetime.utcnow().isoformat(),
        **entry,
    }
    try:
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry_with_time) + "\n")
    except Exception as e:
        # No queremos que un fallo de log tumbe la API
        print("ERROR logging trade:", e)


def read_trade_logs(limit: int = 50):
    """Lee los últimos 'limit' registros del log."""
    try:
        with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    lines = lines[-limit:]
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


# ---------- Modelos Pydantic ----------

class TradeRequest(BaseModel):
    symbol: str
    qty: int
    side: str  # "buy" o "sell"
    type: str = "market"  # "market" o "limit"
    time_in_force: str = "day"  # "day", "gtc", etc.
    limit_price: float | None = None
    paper: bool = True  # Campo informativo: por defecto usamos paper trading


# ---------- Endpoints ----------

@app.get("/snapshot")
def market_snapshot():
    """
    Devuelve último precio y hora de QQQ, SPY y NVDA.
    """
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
        return {
            "status": "ok",
            "data": data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/recommend")
def recommend_signals():
    """
    Genera una recomendación básica por símbolo usando cambio diario.
    Esto es una lógica simple inicial para que el GPT la use como insumo.
    """
    symbols = ["QQQ", "SPY", "NVDA"]
    recommendations = []

    for sym in symbols:
        try:
            info = get_daily_change(sym)
            if "error" in info:
                recommendations.append(
                    {
                        "symbol": sym,
                        "status": "no_data",
                        "reason": info["error"],
                    }
                )
                continue

            change = info["change_pct"]
            if change > 0.8:
                bias = "bullish"
                suggestion = "prefer_call"
            elif change < -0.8:
                bias = "bearish"
                suggestion = "prefer_put"
            else:
                bias = "neutral"
                suggestion = "wait"

            recommendations.append(
                {
                    "symbol": sym,
                    "change_pct": round(change, 2),
                    "bias": bias,
                    "suggestion": suggestion,
                }
            )
        except Exception as e:
            recommendations.append(
                {
                    "symbol": sym,
                    "status": "error",
                    "reason": str(e),
                }
            )

    return {
        "status": "ok",
        "recommendations": recommendations,
        "note": (
            "Lógica básica. El GPT BDV debe combinar esto con contexto intradía, "
            "volatilidad y gestión de riesgo para dar la señal final."
        ),
    }


@app.post("/trade")
def place_trade(req: TradeRequest):
    """
    Crea una orden en Alpaca (POR DEFECTO PAPER TRADING).

    IMPORTANTE:
    - Usa APCA_TRADING_URL, por defecto https://paper-api.alpaca.markets/v2
    - Si vas a usar live trading, cambia APCA_TRADING_URL en el .env
    """
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
        # Guardar log del intento
        log_trade(
            {
                "endpoint": "/trade",
                "request": payload,
                "status_code": r.status_code,
                "response_preview": r.text[:300],
            }
        )
        r.raise_for_status()
        return {
            "status": "ok",
            "alpaca_response": r.json(),
        }
    except requests.HTTPError as e:
        # Log también en caso de error
        log_trade(
            {
                "endpoint": "/trade",
                "request": payload,
                "error": str(e),
            }
        )
        raise HTTPException(status_code=400, detail=f"Alpaca error: {e}")
    except Exception as e:
        log_trade(
            {
                "endpoint": "/trade",
                "request": payload,
                "error": str(e),
            }
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/trades-log")
def get_trades_log(limit: int = 50):
    """
    Devuelve los últimos 'limit' registros de trading guardados en trades.log.
    """
    logs = read_trade_logs(limit=limit)
    return {
        "count": len(logs),
        "items": logs,
    }


@app.get("/health")
def health_check():
    return {"status": "ok"}
