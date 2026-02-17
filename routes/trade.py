from fastapi import APIRouter, HTTPException
import os
import requests
from typing import Any, Dict

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


def _alpaca_base_url() -> str:
    """
    Normaliza APCA_TRADING_URL para que termine en /v2 (paper o live).
    """
    base_url = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    if not base_url.endswith("/v2"):
        base_url = base_url.rstrip("/") + "/v2"
    return base_url


@router.post("/trade")
def place_trade(payload: Dict[str, Any]):
    """
    Enviar una orden a Alpaca (acciones).
    Nota: Opciones NO se envían por /v2/orders; requieren el stack de options/trading correspondiente.
    """

    symbol = payload.get("symbol")
    side = payload.get("side")
    qty = payload.get("qty")

    if symbol is None or side is None or qty is None:
        raise HTTPException(
            status_code=400,
            detail="Faltan campos en la orden. Requiere: symbol, side, qty",
        )

    # ✅ Normalización fuerte del símbolo (evita spy vs SPY)
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="El campo 'symbol' no puede estar vacío")

    side = str(side).lower().strip()
    if side not in ("buy", "sell"):
        raise HTTPException(
            status_code=400,
            detail="El campo 'side' debe ser 'buy' o 'sell'",
        )

    try:
        qty_int = int(qty)
    except Exception:
        raise HTTPException(status_code=400, detail="El campo 'qty' debe ser numérico entero")

    if qty_int <= 0:
        raise HTTPException(status_code=400, detail="El campo 'qty' debe ser > 0")

    base_url = _alpaca_base_url()
    url = f"{base_url}/orders"

    # ✅ Defaults explícitos (pero permitimos override si más adelante lo necesitas)
    order_type = str(payload.get("type", "market")).lower().strip()
    tif = str(payload.get("time_in_force", "day")).lower().strip()

    body: Dict[str, Any] = {
        "symbol": symbol,
        "qty": str(qty_int),
        "side": side,
        "type": order_type,
        "time_in_force": tif,
    }

    # ✅ limit_price SOLO si es LIMIT
    if order_type == "limit":
        limit_price = payload.get("limit_price")
        if limit_price is None:
            raise HTTPException(status_code=400, detail="Para órdenes LIMIT se requiere 'limit_price'")
        try:
            lp = float(limit_price)
        except Exception:
            raise HTTPException(status_code=400, detail="'limit_price' debe ser numérico")
        if lp <= 0:
            raise HTTPException(status_code=400, detail="'limit_price' debe ser > 0")
        body["limit_price"] = str(lp)

    # 🔔 Notificación 1: ORDEN SOLICITADA
    try:
        send_alert("execution", {
            "symbol": symbol,
            "side": side,
            "qty": qty_int,
            "price": order_type,
            "target": "-",
            "stop": "-",
            "mode": "Solicitud desde GPT BDV"
        })
    except Exception as e:
        print(f"[WARN] No se pudo enviar alerta de solicitud: {e}")

    # Llamada a Alpaca
    try:
        r = requests.post(url, headers=get_alpaca_headers(), json=body, timeout=15)
        data = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Error de red llamando a Alpaca: {e}",
                "alpaca_url": url,
            },
        )

    # ✅ Si Alpaca rechaza, devuelve 400/422 al cliente (no 502)
    if r.status_code >= 400:
        # Mapea: 401/403 auth, 422 validation, 429 rate limit, etc.
        raise HTTPException(
            status_code=r.status_code,
            detail={
                "message": "Alpaca rechazó la orden",
                "alpaca_status": r.status_code,
                "alpaca_url": url,
                "alpaca_body": data,
                "sent_body": body,
            },
        )

    # Estado (new/accepted/filled/etc)
    status_text = "pendiente"
    if isinstance(data, dict):
        status_text = str(data.get("status", "pendiente"))

    # 🔔 Notificación 2: ORDEN ENVIADA (no afirmar “ejecutada” si no está filled)
    try:
        send_alert("execution", {
            "symbol": symbol,
            "side": side,
            "qty": qty_int,
            "price": data.get("filled_avg_price", order_type) if isinstance(data, dict) else order_type,
            "target": "-",
            "stop": "-",
            "mode": "Paper" if "paper" in base_url else "Live",
            "status": status_text
        })
    except Exception as e:
        print(f"[WARN] No se pudo enviar alerta de ejecución: {e}")

    message = (
        "⚡ <b>BDV — Orden enviada</b>\n"
        f"Símbolo: <b>{symbol}</b>\n"
        f"Side: <b>{side.upper()}</b>\n"
        f"Cantidad: <b>{qty_int}</b>\n"
        f"Tipo: <b>{order_type.upper()}</b>\n"
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
# 🔒 ENDPOINT DE CIERRE “SIMULADO” (solo notificación)
# =====================================================
@router.post("/trade/close")
def close_trade(symbol: str, reason: str = "Target alcanzado +10%", pl: str = "+10%"):
    """
    Simula cierre de operación (solo notifica).
    Si quieres cierre REAL, usa el endpoint /alpaca/close/{symbol}.
    """
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol inválido")

    try:
        send_alert("close", {
            "symbol": symbol,
            "reason": reason,
            "pl": pl,
            "percent": pl
        })
        return {"status": "ok", "message": f"Operación {symbol} cerrada (notificada)."}
    except Exception as e:
        print(f"[ERR] No se pudo enviar alerta de cierre: {e}")
        raise HTTPException(status_code=500, detail=f"Error notificando cierre: {e}")
