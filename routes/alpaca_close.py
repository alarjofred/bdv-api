from fastapi import APIRouter, HTTPException
import os
import requests

# Router específico para operaciones con Alpaca
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


def get_trading_base_url() -> str:
    """
    Construye la URL base correcta para el trading de Alpaca.

    Usa por defecto el entorno PAPER:
    https://paper-api.alpaca.markets/v2
    """
    trading_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets",
    ).rstrip("/")
    return f"{trading_url}/v2"


# ---------------------------------------------------------------------
#  GET /alpaca/positions  → ver todas las posiciones (debug)
# ---------------------------------------------------------------------
@router.get("/positions")
def get_positions():
    """
    Devuelve todas las posiciones abiertas en Alpaca (acciones y opciones).
    Sirve para debug: ver exactamente qué símbolos ve la API.
    """
    base_url = get_trading_base_url()
    headers = get_alpaca_headers()

    try:
        resp = requests.get(f"{base_url}/positions", headers=headers, timeout=10)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca para leer posiciones: {e}",
        )

    if resp.status_code != 200:
        body = resp.json() if resp.text else {}
        raise HTTPException(
            status_code=resp.status_code,
            detail={
                "message": "Error leyendo posiciones en Alpaca",
                "alpaca_status": resp.status_code,
                "alpaca_body": body,
            },
        )

    return resp.json()


# ---------------------------------------------------------------------
#  POST /alpaca/close-all  → cerrar TODO
# ---------------------------------------------------------------------
@router.post("/close-all")
def close_all_positions():
    """
    Cierra TODAS las posiciones abiertas en Alpaca al mejor precio disponible.
    Úsalo solo cuando quieras salir completamente del mercado.
    """
    base_url = get_trading_base_url()
    headers = get_alpaca_headers()

    try:
        resp = requests.delete(f"{base_url}/positions", headers=headers, timeout=10)
        body = resp.json() if resp.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca: {e}",
        )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error cerrando posiciones en Alpaca",
                "alpaca_status": resp.status_code,
                "alpaca_body": body,
            },
        )

    return {"status": "ok", "closed": body}


# ---------------------------------------------------------------------
#  POST /alpaca/close/{symbol}  → cerrar un símbolo (acción u opción)
# ---------------------------------------------------------------------
@router.post("/close/{symbol}")
def close_symbol(symbol: str):
    """
    Cierra la posición abierta en un símbolo específico (si existe).

    Ejemplos:
    - POST /alpaca/close/QQQ
    - POST /alpaca/close/QQQ251202C00621000   (opción de QQQ)
    """
    base_url = get_trading_base_url()
    headers = get_alpaca_headers()
    symbol_up = symbol.upper()

    # 1) Leer TODAS las posiciones de Alpaca
    try:
        positions_resp = requests.get(f"{base_url}/positions", headers=headers, timeout=10)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca para leer posiciones: {e}",
        )

    if positions_resp.status_code != 200:
        body = positions_resp.json() if positions_resp.text else {}
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error leyendo posiciones en Alpaca",
                "alpaca_status": positions_resp.status_code,
                "alpaca_body": body,
            },
        )

    positions = positions_resp.json()

    # 2) Buscar si existe una posición EXACTAMENTE con ese símbolo
    symbols_open = [str(p.get("symbol", "")).upper() for p in positions]

    if symbol_up not in symbols_open:
        raise HTTPException(
            status_code=404,
            detail=f"No hay posición abierta en {symbol_up}. Posiciones abiertas: {symbols_open}",
        )

    # 3) Cerrar la posición usando el símbolo (así lo requiere Alpaca)
    try:
        close_resp = requests.delete(
            f"{base_url}/positions/{symbol_up}",
            headers=headers,
            timeout=10,
        )
        body = close_resp.json() if close_resp.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca para cerrar posición: {e}",
        )

    if close_resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error cerrando posición en Alpaca",
                "alpaca_status": close_resp.status_code,
                "alpaca_body": body,
            },
        )

    return {
        "status": "ok",
        "symbol": symbol_up,
        "closed": body,
    }
