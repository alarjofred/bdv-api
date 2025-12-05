from fastapi import APIRouter
import os
import requests

router = APIRouter()

# URL base de tu propia API en Render
API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# 🩵 agregado: fallback automático si la variable no existe o está vacía
if not API_BASE:
    API_BASE = "https://bdv-api-server.onrender.com"

@router.get("/recommend")
def recommend_trade():
    """
    Genera recomendaciones simples usando los datos del endpoint /snapshot.
    """
    try:
        # 1) Llamar al endpoint /snapshot de esta misma API
        snapshot_url = f"{API_BASE}/snapshot"

        # 🩵 agregado: timeout para evitar bloqueos si Render se demora
        resp = requests.get(snapshot_url, timeout=10)

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

            # 🩵 agregado: lógica condicional simple para dar análisis mínimo
            if symbol == "QQQ" and price > 620:
                bias, suggestion = "bullish", "buy calls"
                target, stop = round(price * 1.02, 2), round(price * 0.98, 2)
            elif symbol == "SPY" and price < 680:
                bias, suggestion = "bearish", "buy puts"
                target, stop = round(price * 0.98, 2), round(price * 1.02, 2)
            elif symbol == "NVDA" and price > 190:
                bias, suggestion = "bullish", "buy shares"
                target, stop = round(price * 1.03, 2), round(price * 0.97, 2)

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
        # 🩵 agregado: registro de errores en consola de Render
        print(f"[ERR] /recommend: {e}")
        return {"status": "error", "message": str(e)}
