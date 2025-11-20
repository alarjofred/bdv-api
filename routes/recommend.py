from fastapi import APIRouter
from dotenv import load_dotenv
import os
import requests

router = APIRouter()

# Cargar variables de entorno (las mismas que usa main.py)
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
    }


def get_daily_change(symbol: str):
    """
    Usa barras diarias para calcular el cambio % entre el último close
    y el close anterior.
    """
    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {"timeframe": "1Day", "limit": 2}
    r = requests.get(url, headers=alpaca_headers(), params=params)
    r.raise_for_status()
    data = r.json()
    bars = data.get("bars", [])
    if len(bars) < 2:
        return None

    prev_close = bars[-2]["c"]
    last_close = bars[-1]["c"]
    change_pct = (last_close - prev_close) / prev_close * 100

    return {
        "symbol": symbol,
        "prev_close": prev_close,
        "last_close": last_close,
        "change_pct": change_pct,
    }


@router.get("/recommend")
def recommend():
    """
    Genera una recomendación básica para QQQ, SPY y NVDA:
    - bullish  -> prefer_call
    - bearish  -> prefer_put
    - neutral  -> wait
    """
    symbols = ["QQQ", "SPY", "NVDA"]
    recommendations = []

    for sym in symbols:
        try:
            info = get_daily_change(sym)
            if not info:
                recommendations.append(
                {
                    "symbol": sym,
                    "status": "no_data",
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
        "note": "Lógica básica; el GPT BDV debe combinar esto con contexto intradía y gestión de riesgo.",
    }
