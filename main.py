from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
import os
import requests
from routes.test_alpaca import router as test_alpaca_router

# 1) Cargar variables del .env
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

if APCA_API_KEY_ID is None or APCA_API_SECRET_KEY is None:
    raise RuntimeError("Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY en el .env")

print("DBG: KEY", APCA_API_KEY_ID[:4], "DATA_URL:", APCA_DATA_URL)

app = FastAPI()
app.include_router(test_alpaca_router)


def get_latest_trade(symbol: str):
    """Consulta el último trade de un símbolo en Alpaca."""
    url = f"{APCA_DATA_URL}/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
    }
    r = requests.get(url, headers=headers)
    # Debug para ver qué responde Alpaca
    print(f"DBG Alpaca {symbol}: {r.status_code} {r.text[:200]}")
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
                "exchange": trade.get("x"),
            }
        return data
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Alpaca HTTP error: {e}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {e}"
        )

