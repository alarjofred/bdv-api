from fastapi import APIRouter
import requests
import os

router = APIRouter()

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

@router.get("/recommend")
def recommend_trade(symbol: str = "QQQ"):
    """Genera una recomendación de trading basada en el último precio."""

    try:
        # 1. Llamar a snapshot interno
        url = f"{API_BASE}/snapshot"
        snapshot = requests.get(url).json()

        if symbol not in snapshot:
            return {
                "status": "error",
                "message": f"No hay datos para {symbol}"
            }

        price = snapshot[symbol]["price"]

        # 2. Lógica simple de recomendación
        if price > snapshot[symbol]["price"] * 0.999:
            recomendacion = "BUY CALL"
            target = round(price * 1.01, 2)
            stop = round(price * 0.99, 2)
        elif price < snapshot[symbol]["price"] * 1.001:
            recomendacion = "BUY PUT"
            target = round(price * 0.99, 2)
            stop = round(price * 1.01, 2)
        else:
            recomendacion = "HOLD"
            target = price
            stop = price

        return {
            "status": "success",
            "symbol": symbol,
            "price": price,
            "recommendation": recomendacion,
            "target": target,
            "stop": stop
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
