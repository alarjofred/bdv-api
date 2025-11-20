from fastapi import APIRouter
import requests
import os

router = APIRouter()

# URL base del propio servidor
API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

@router.get("/recommend")
def recommend_trade():
    """Genera recomendaciones usando datos del endpoint /snapshot."""

    try:
        # 1. Obtener snapshot interno
        snapshot_url = f"{API_BASE}/snapshot"
        snapshot = requests.get(snapshot_url).json()

        recommendations = []

        for symbol, info in snapshot.items():
            price = info["price"]

            # Lógica ultra simple basada en movimiento intradía
            if "price" not in info:
                continue

            # Ejemplo de sesgo básico
            if price > info["price"] * 0.999:
                recommendation = "BUY CALL"
                target = round(price * 1.01, 2)
                stop = round(price * 0.99, 2)
            elif price < info["price"] * 1.001:
                recommendation = "BUY PUT"
                target = round(price * 0.99, 2)
                stop = round(price * 1.01, 2)
            else:
                recommendation = "HOLD"
                target = price
                stop = price

            recommendations.append({
                "symbol": symbol,
                "price": price,
                "recommendation": recommendation,
                "target": target,
                "stop": stop
            })

        return {
            "status": "ok",
            "recommendations": recommendations,
            "note": "Basado en snapshot. Lógica simple; el GPT BDV aplicará análisis avanzado."
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
