from fastapi import APIRouter, HTTPException
import os
import requests

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
    }


@router.post("/close-all")
def close_all_positions():
    """
    Cierra TODAS las posiciones abiertas en Alpaca al mejor precio disponible.
    Úsalo solo cuando quieras salir completamente del mercado.
    """
    trading_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    )
    url = f"{trading_url}/positions"

    try:
        r = requests.delete(url, headers=get_alpaca_headers(), timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca: {e}",
        )

    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error cerrando posiciones en Alpaca",
                "alpaca_status": r.status_code,
                "alpaca_body": body,
            },
        )

    return {"status": "ok", "closed": body}


@router.post("/close/{symbol}")
def close_symbol(symbol: str):
    """
    Cierra la posición abierta en un símbolo específico (si existe).
    Ejemplo: POST /alpaca/close/QQQ
    """
    trading_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    )
    url = f"{trading_url}/positions/{symbol.upper()}"

    try:
        r = requests.delete(url, headers=get_alpaca_headers(), timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca: {e}",
        )

    if r.status_code == 404:
        # No hay posición para ese símbolo
        raise HTTPException(
            status_code=404,
            detail=f"No hay posición abierta en {symbol.upper()}",
        )

    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error cerrando posición en Alpaca",
                "alpaca_status": r.status_code,
                "alpaca_body": body,
            },
        )

    return {"status": "ok", "closed": body}
