from fastapi import APIRouter, HTTPException
import os
import requests
from typing import Dict, Any, List

# Router específico para operaciones de cierre con Alpaca
router = APIRouter(prefix="/alpaca", tags=["alpaca"])


def get_alpaca_headers() -> dict:
    """
    Devuelve los headers necesarios para autenticar contra Alpaca.
    """
    api_key = os.getenv("APCA_API_KEY_ID")
    api_secret = os.getenv("APCA_API_SECRET_KEY")

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="Faltan las variables de entorno APCA_API_KEY_ID o APCA_API_SECRET_KEY",
        )

    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def get_trading_url() -> str:
    """
    Devuelve la URL base de trading (paper/live).
    Mantiene tu valor por defecto con /v2.
    """
    return os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    ).rstrip("/")


def get_open_positions() -> List[Dict[str, Any]]:
    """
    Lee todas las posiciones abiertas desde Alpaca.
    Sirve para encontrar qty y símbolo exacto (acciones u opciones).
    """
    trading_url = get_trading_url()
    url = f"{trading_url}/positions"

    try:
        r = requests.get(url, headers=get_alpaca_headers(), timeout=10)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca (GET posiciones): {e}",
        )

    if r.status_code == 404:
        return []

    if r.status_code >= 400:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"Error leyendo posiciones en Alpaca: {r.text}",
        )

    return r.json()


def place_close_order(symbol: str, qty: int) -> Dict[str, Any]:
    """
    Envía una ORDEN DE VENTA de mercado para cerrar una posición.
    Funciona para acciones y opciones.
    """
    trading_url = get_trading_url()
    url = f"{trading_url}/orders"

    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }

    try:
        r = requests.post(url, headers=get_alpaca_headers(), json=payload, timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando orden de cierre para {symbol}: {e}",
        )

    if r.status_code not in (200, 201):
        raise HTTPException(
            status_code=r.status_code,
            detail={
                "message": f"Error en orden SELL para {symbol}",
                "alpaca_status": r.status_code,
                "alpaca_body": body,
            },
        )

    return body


@router.post("/close-all")
def close_all_positions():
    """
    Cierra TODAS las posiciones abiertas en Alpaca.

    1) Intenta DELETE /positions (close all).
    2) Si falla, lee posiciones y envía órdenes SELL una por una (fallback).
    """
    trading_url = get_trading_url()
    url = f"{trading_url}/positions"

    try:
        r = requests.delete(url, headers=get_alpaca_headers(), timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca (DELETE posiciones): {e}",
        )

    # Éxito normal del endpoint close-all
    if r.status_code in (200, 207):
        return {"status": "ok", "mode": "delete_endpoint", "closed": body}

    # Fallback: cerrar una por una con órdenes de mercado
    positions = get_open_positions()
    closed = []

    for pos in positions:
        symbol = pos.get("symbol")
        if not symbol:
            continue

        try:
            qty = abs(int(float(pos.get("qty", 0))))
        except Exception:
            qty = 0

        if qty <= 0:
            continue

        order = place_close_order(symbol, qty)
        closed.append({"symbol": symbol, "qty": qty, "order": order})

    if not closed:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "No se pudieron cerrar posiciones en Alpaca",
                "alpaca_status": r.status_code,
                "alpaca_body": body,
            },
        )

    return {"status": "ok", "mode": "fallback_orders", "closed": closed}


@router.post("/close/{symbol}")
def close_symbol(symbol: str):
    """
    Cierra la posición abierta en un símbolo específico (si existe).

    1) Intenta DELETE /positions/{symbol}.
    2) Si Alpaca responde 404 pero sí hay posición, envía orden SELL de mercado.
    """
    trading_url = get_trading_url()
    url = f"{trading_url}/positions/{symbol.upper()}"

    try:
        r = requests.delete(url, headers=get_alpaca_headers(), timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca (DELETE posición {symbol}): {e}",
        )

    # Caso éxito directo
    if r.status_code in (200, 204):
        return {"status": "ok", "mode": "delete_endpoint", "closed": body}

    # 404: Alpaca dice que no hay posición → verificamos nosotros
    if r.status_code == 404:
        positions = get_open_positions()
        target_pos = None

        for pos in positions:
            if str(pos.get("symbol")) == symbol.upper():
                target_pos = pos
                break

        if not target_pos:
            # De verdad no hay posición
            raise HTTPException(
                status_code=404,
                detail=f"No hay posición abierta en {symbol.upper()}",
            )

        # Sí hay posición → la cerramos con SELL
        try:
            qty = abs(int(float(target_pos.get("qty", 0))))
        except Exception:
            qty = 0

        if qty <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"No se pudo determinar qty para cerrar {symbol.upper()}",
            )

        order = place_close_order(symbol.upper(), qty)

        return {
            "status": "ok",
            "mode": "fallback_order",
            "symbol": symbol.upper(),
            "qty": qty,
            "order": order,
        }

    # Otro error
    raise HTTPException(
        status_code=502,
        detail={
            "message": f"Error cerrando posición en Alpaca para {symbol.upper()}",
            "alpaca_status": r.status_code,
            "alpaca_body": body,
        },
    )
