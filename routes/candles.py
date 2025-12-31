from fastapi import APIRouter, HTTPException, Query
import os
import requests
from typing import Any, Dict, List, Optional

router = APIRouter(prefix="/candles", tags=["candles"])

APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2").rstrip("/")
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

def _alpaca_headers() -> Dict[str, str]:
    if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Faltan APCA_API_KEY_ID / APCA_API_SECRET_KEY en el entorno."
        )
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "accept": "application/json",
    }

@router.get("")
def get_candles(
    symbol: str = Query(..., min_length=1),
    timeframe: str = Query("5Min"),
    limit: int = Query(200, ge=1, le=10000),
    adjustment: str = Query("raw"),
    feed: Optional[str] = Query(None),  # "iex" o "sip" (si tu plan lo permite)
) -> Dict[str, Any]:
    """
    Devuelve velas (bars) desde Alpaca Market Data v2.
    Endpoint Alpaca: /v2/stocks/{symbol}/bars
    """
    symbol = symbol.upper().strip()

    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "adjustment": adjustment,
    }
    if feed:
        params["feed"] = feed

    try:
        r = requests.get(url, headers=_alpaca_headers(), params=params, timeout=20)

        # Si Alpaca devuelve error, lo mostramos claro (y NO 500 genérico)
        if r.status_code != 200:
            try:
                err = r.json()
            except Exception:
                err = {"raw": r.text}
            raise HTTPException(
                status_code=r.status_code,
                detail={"alpaca_error": err, "url": url, "params": params},
            )

        payload = r.json()
        bars = payload.get("bars") or payload.get("data") or []

        return {
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "count": len(bars),
            "bars": bars,
            "next_page_token": payload.get("next_page_token"),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"message": str(e), "url": url, "params": params},
        )
