from fastapi import APIRouter, HTTPException
import os
import requests

# 🔔 Import para enviar mensajes a Telegram
from routes.telegram_notify import send_telegram_message, send_alert

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

    base_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    ).rstrip("/")

    if not base_url.endswith("/v2"):
        base_url = base_url.rstrip("/") + "/v2"

    url = f"{base_url}/orders"

    body = {
        "symbol": str(symbol),
        "qty": str(qty_int),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }

    # 🔔 Notificación 1: ORDEN SOLICITADA DESDE GPT BDV
    try:
        send_alert("execution", {
            "symbol": symbol,
            "side": side,
            "qty": qty_int,
            "price": "market",
            "target": "-",
            "stop": "-",
            "mode": "Solicitud desde GPT BDV"
        })
    except Exception as e:
        print(f"[WARN] No se pudo enviar alerta de solicitud: {e}")

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
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error al enviar orden a Alpaca",
                "alpaca_status": r.status_code,
                "alpaca_url": url,
                "alpaca_body": data,
            },
        )

    # 🔔 Notificación 2: ORDEN EJECUTADA EN ALPACA
    try:
        status_text = data.get("status", "pendiente")
    except AttributeError:
        status_text = "pendiente"

    try:
        send_alert("execution", {
            "symbol": symbol,
            "side": side,
            "qty": qty_int,
            "price": data.get("filled_avg_price", "market"),
            "target": "-",
            "stop": "-",
            "mode": "Paper" if "paper" in base_url else "Live"
        })
    except Exception as e:
        print(f"[WARN] No se pudo enviar alerta de ejecución: {e}")

    # Mensaje de texto clásico (lo tuyo original)
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


# =====================================================
# 🔒 NUEVO ENDPOINT DE CIERRE DE OPERACIÓN
# =====================================================
@router.post("/trade/close")
def close_trade(symbol: str, reason: str = "Target alcanzado +10%", pl: str = "+10%"):
    """
    Simula cierre de operación o salida real.
    Envía notificación a Telegram cuando se cumple la señal de salida.
    """
    try:
        send_alert("close", {
            "symbol": symbol,
            "reason": reason,
            "pl": pl,
            "percent": pl
        })
        return {"status": "ok", "message": f"Operación {symbol} cerrada y notificada."}
    except Exception as e:
        print(f"[ERR] No se pudo enviar alerta de cierre: {e}")
        raise HTTPException(status_code=500, detail=f"Error notificando cierre: {e}")
