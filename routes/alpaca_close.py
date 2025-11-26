from fastapi import APIRouter, HTTPException
import os
import requests

router = APIRouter(prefix="/alpaca", tags=["alpaca"])


def get_alpaca_headers():
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
    }


@router.post("/close-all")
def close_all_positions():
    """
    Cierra TODAS las posiciones abiertas en Alpaca al mejor precio disponible.
    Úsalo SOLO cuando quieras salir completamente del mercado.
    """
    base_url = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    url = f"{base_url}/v2/positions"

    headers = get_alpaca_headers()

    try:
        resp = requests.delete(url, headers=headers)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error de conexión con Alpaca: {e}",
        )

    if resp.status_code not in (200, 207, 204):
        # 207 = multi-status cuando cierra varias
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error al cerrar posiciones en Alpaca: {resp.text}",
        )

    return {
        "status": "ok",
        "message": "Se enviaron las órdenes para cerrar todas las posiciones en Alpaca.",
        "alpaca_response": resp.json() if resp.text else None,
    }


@router.post("/close/{symbol}")
def close_symbol_position(symbol: str):
    """
    Cierra SOLO la posición de un símbolo específico (ej. QQQ, SPY, NVDA).
    """
    base_url = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    url = f"{base_url}/v2/positions/{symbol.upper()}"

    headers = get_alpaca_headers()

    try:
        resp = requests.delete(url, headers=headers)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error de conexión con Alpaca: {e}",
        )

    if resp.status_code not in (200, 204):
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error al cerrar posición de {symbol}: {resp.text}",
        )

    return {
        "status": "ok",
        "message": f"Se envió la orden para cerrar la posición de {symbol.upper()} en Alpaca.",
        "alpaca_response": resp.json() if resp.text else None,
    }
