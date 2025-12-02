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
    ).rstrip("/")

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

    Para evitar problemas con opciones, primero leemos TODAS las posiciones
    desde Alpaca, buscamos el símbolo, y después cerramos usando asset_id.
    Esto es mucho más robusto.

    Ejemplo: POST /alpaca/close/QQQ251202C00621000
    """
    trading_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    ).rstrip("/")

    headers = get_alpaca_headers()
    symbol_up = symbol.upper()

    # 1) Leer todas las posiciones abiertas
    try:
        pos_resp = requests.get(f"{trading_url}/positions", headers=headers, timeout=10)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error leyendo posiciones en Alpaca: {e}",
        )

    if pos_resp.status_code != 200:
        raise HTTPException(
            status_code=pos_resp.status_code,
            detail={
                "message": "Error al leer posiciones en Alpaca",
                "alpaca_status": pos_resp.status_code,
                "alpaca_body": pos_resp.text,
            },
        )

    try:
        positions = pos_resp.json()
    except Exception:
        positions = []

    # 2) Buscar la posición que tenga ese símbolo
    target = None
    for p in positions:
        if str(p.get("symbol", "")).upper() == symbol_up:
            target = p
            break

    if not target:
        # Desde el punto de vista de la API: no hay posición abierta con ese símbolo
        raise HTTPException(
            status_code=404,
            detail=f"No hay posición abierta en {symbol_up}",
        )

    asset_id = target.get("asset_id")
    if not asset_id:
        # Si por alguna razón no hay asset_id, usamos el símbolo como fallback
        asset_id = symbol_up

    # 3) Cerrar usando asset_id (forma más robusta)
    close_url = f"{trading_url}/positions/{asset_id}"

    try:
        r = requests.delete(close_url, headers=headers, timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca para cerrar posición: {e}",
        )

    if r.status_code == 404:
        # Alpaca dice que no existe la posición (puede haber sido cerrada justo antes)
        raise HTTPException(
            status_code=404,
            detail=f"No hay posición abierta en {symbol_up} (asset_id={asset_id})",
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

    return {
        "status": "ok",
        "symbol": symbol_up,
        "asset_id": asset_id,
        "closed": body,
    }
Fix close_symbol with asset_id
