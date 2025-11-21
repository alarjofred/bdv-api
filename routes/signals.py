from fastapi import APIRouter
import requests
import os

router = APIRouter()

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

@router.get("/signals/generate")
def generate_signals():
    """
    Genera una señal de trading para que Alex la revise manualmente
    en Schwab / Ucharts, sin ejecutar en Alpaca.
    """

    try:
        snapshot_url = f"{API_BASE}/snapshot"
        snapshot = requests.get(snapshot_url).json()

        data = snapshot.get("data", {})
        signals = []

        for symbol, info in data.items():
            price = info.get("price")

            if price:
                signals.append({
                    "symbol": symbol,
                    "price": price,
                    "direction": "call" if price > price * 0.999 else "put",
                    "suggestion": f"Revisar {symbol} {'CALL' if price > price*0.999 else 'PUT'} en Schwab",
                    "note": "Esta señal es para operar manualmente, no ejecuta en Alpaca."
                })

        return {
            "status": "ok",
            "signals": signals
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
