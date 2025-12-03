from fastapi import APIRouter, HTTPException
import os
import requests

# 🔔 Import para enviar mensajes a Telegram
from routes.telegram_notify import send_telegram_message

router = APIRouter(tags=["trade"])


def get_alpaca_headers() -> dict:
    """
    Headers para autenticar contra Alpaca.
    Usa las mismas variables que el resto del sistema.
    """
    api_key = os.getenv("APCA_API_KEY_ID")
    api_secret = os.getenv("APCA_API_SECRET_KEY")

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY en el servidor",
        )

    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


@router.post("/trade")
def place_trade(payload: dict):
    """
    Enviar una orden a Alpaca (acciones u opciones).

    Espera un body como:
    {
      "symbol": "QQQ" o "QQQ251202C00621000",
      "side": "buy" o "sell",
      "qty": 1
    }

    Siempre envía:
      - type = "market"
      - time_in_force = "day"

    Alpaca detecta si es acción u opción según el símbolo.
    """

    symbol = payload.get("symbol")
    side = payload.get("side")
    qty = payload.get("qty")

    if not symbol or not side or not qty:
        raise HTTPException(
            status_code=400,
            detail="Faltan campos en la orden. Requiere: symbol, side, qty",
        )

    side = str(side).lower().strip()
    if side not in ("buy", "sell"):
        raise HTTPException(
            status_code=400,
            detail="El campo 'side' debe ser 'buy' o 'sell'",
        )

    try:
        qty_int = int(qty)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="El campo 'qty' debe ser numérico entero",
        )

    # Leemos la base, pero nos aseguramos de que SIEMPRE tenga /v2
    base_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    ).rstrip("/")

    if not base_url.endswith("/v2"):
        base_url = base_url.rstrip("/") + "/v2"

    # 👉 ESTA es la URL final CORRECTA: .../v2/orders
    url = f"{base_url}/orders"

    body = {
        "symbol": str(symbol),
        "qty": str(qty_int),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }

    try:
        r = requests.post(url, headers=get_alpaca_headers(), json=body, timeout=10)
        data = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Error llamando a Alpaca para enviar orden: {e}",
                "alpaca_url": url,
            },
        )

    if r.status_code >= 400:
        # Propagamos el error de Alpaca (pero ya con la URL correcta)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error al enviar orden a Alpaca",
                "alpaca_status": r.status_code,
                "alpaca_url": url,
                "alpaca_body": data,
            },
        )

    # 🔔 Si llegamos aquí, la orden se envió OK a Alpaca → mandamos alerta a Telegram
    try:
        status_text = data.get("status", "desconocido")
    except AttributeError:
        status_text = "desconocido"

    message = (
        "⚡ <b>BDV OPTIONS LIVE — Nueva operación</b>\n"
        f"Símbolo: <b>{symbol}</b>\n"
        f"Side: <b>{side.upper()}</b>\n"
        f"Cantidad: <b>{qty_int}</b>\n"
        f"Estado Alpaca: <code>{status_text}</code>"
    )

    telegram_result = send_telegram_message(message)

    return {
        "status": "ok",
        "alpaca_url": url,
        "alpaca_order": data,
        "telegram_notify": telegram_result,
    }
