from fastapi import APIRouter
import os
import requests

router = APIRouter()

# URL base de tu propia API en Render
API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

@router.get("/recommend")
def recommend_trade():
    """
    Genera recomendaciones simples usando los datos del endpoint /snapshot.
    """
    try:
        # 1) Llamar al endpoint /snapshot de esta misma API
        snapshot_url = f"{API_BASE}/snapshot"
        resp = requests.get(snapshot_url)
        resp.raise_for_status()
        snapshot = resp.json()

        # snapshot debería tener forma: {"status": "ok", "data": {...}}
        market = snapshot.get("data", {})
        if not market:
            return {
                "status": "error",
                "message": "Respuesta de /snapshot no tiene campo 'data'"
            }

        recommendations = []

        for symbol, info in market.items():
            price = info.get("price")
            if price is None:
                continue

            # Lógica muy básica: por ahora todo neutral
            bias = "neutral"
            suggestion = "wait"
            target = price
            stop = price

            recommendations.append({
                "symbol": symbol,
                "price": price,
                "bias": bias,
                "suggestion": suggestion,
                "target": target,
                "stop": stop
            })

        return {
            "status": "ok",
            "recommendations": recommendations,
            "note": "Basado en /snapshot. Lógica simple; el GPT BDV aplica análisis y gestión de riesgo."
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
