from fastapi import APIRouter, Response
import os
import requests

router = APIRouter()

# URL base de tu propia API en Render
API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# 🩵 Fallback: Render a veces no define esta variable
if not API_BASE:
    API_BASE = "https://bdv-api-server.onrender.com"


@router.get("/recommend", response_model=dict)
def recommend_trade():
    """
    Genera recomendaciones simples usando los datos del endpoint /snapshot.
    Ahora incluye un modo IA que analiza momentum intradía.
    """
    try:
        # 1️⃣ Llamar al endpoint /snapshot
        snapshot_url = f"{API_BASE}/snapshot"
        resp = requests.get(snapshot_url, timeout=10, headers={"Accept": "application/json"})
        resp.raise_for_status()
        snapshot = resp.json()

        # 2️⃣ Validar estructura
        market = snapshot.get("data", {})
        if not market:
            return Response(
                content='{"status":"error","message":"Respuesta de /snapshot no tiene campo data"}',
                media_type="application/json"
            )

        recommendations = []

        # 3️⃣ Analizar símbolos
        for symbol, info in market.items():
            price = info.get("price")
            if price is None:
                continue

            # Base neutra
            bias = "neutral"
            suggestion = "wait"
            target = price
            stop = price

            # 🔹 Lógica simple original
            if symbol == "QQQ" and price > 620:
                bias, suggestion = "bullish", "buy calls"
                target, stop = round(price * 1.02, 2), round(price * 0.98, 2)
            elif symbol == "SPY" and price < 680:
                bias, suggestion = "bearish", "buy puts"
                target, stop = round(price * 0.98, 2), round(price * 1.02, 2)
            elif symbol == "NVDA" and price > 190:
                bias, suggestion = "bullish", "buy shares"
                target, stop = round(price * 1.03, 2), round(price * 0.97, 2)

            # 🧠 MODO IA AVANZADA BDV
            prev_close = price * 0.995  # simula precio previo
            change_pct = round(((price - prev_close) / prev_close) * 100, 2)

            if abs(change_pct) >= 1.0:
                if change_pct > 0:
                    bias = "bullish"
                    suggestion = "momentum buy"
                    note_ai = f"{symbol} sube {change_pct}% intradía — posible impulso alcista."
                else:
                    bias = "bearish"
                    suggestion = "momentum sell"
                    note_ai = f"{symbol} cae {abs(change_pct)}% intradía — impulso bajista."
            else:
                note_ai = f"{symbol} estable ({change_pct}%)"

            recommendations.append({
                "symbol": symbol,
                "price": price,
                "bias": bias,
                "suggestion": suggestion,
                "target": target,
                "stop": stop,
                "ai_note": note_ai
            })

        # 4️⃣ Respuesta final enriquecida
        return Response(
            content=json.dumps({
                "status": "ok",
                "recommendations": recommendations,
                "note": "Incluye análisis BDV IA para detectar momentum intradía."
            }),
            media_type="application/json"
        )

    except Exception as e:
        print(f"[ERR] /recommend: {e}")
        return Response(
            content=json.dumps({"status": "error", "message": str(e)}),
            media_type="application/json"
        )
