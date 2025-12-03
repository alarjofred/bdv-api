from fastapi import APIRouter, HTTPException
import os
import requests

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

    trading_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    ).rstrip("/")

    # ESTA es la URL exacta que queremos ver en caso de error
    url = f"{trading_url}/orders"

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
        # AHORA veremos la URL exacta que Alpaca dijo 404
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error al enviar orden a Alpaca",
                "alpaca_status": r.status_code,
                "alpaca_url": url,
                "alpaca_body": data,
            },
        )

    # En caso de éxito, también devolvemos la URL para confirmar
    return {
        "status": "ok",
        "alpaca_url": url,
        "alpaca_order": data,
    }
