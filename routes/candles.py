from fastapi import APIRouter, HTTPException, Query
from dotenv import load_dotenv
import os, json, time, requests
from datetime import datetime, timedelta, timezone
from typing import Optional

load_dotenv()

router = APIRouter(tags=["candles"])

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
os.makedirs(PERSIST_DIR, exist_ok=True)

def has_keys():
    return bool(APCA_API_KEY_ID and APCA_API_SECRET_KEY)

def headers():
    if not has_keys():
        raise HTTPException(status_code=500, detail="Missing Alpaca keys in environment.")
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Accept": "application/json",
    }

def _iso(dt: datetime) -> str:
    # Alpaca espera ISO8601 con Z
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _cache_path(symbol: str, timeframe: str) -> str:
    safe = f"{symbol}_{timeframe}".replace("/", "_")
    return os.path.join(PERSIST_DIR, f"candles_cache_{safe}.json")

def _read_cache(path: str, ttl: int):
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("_ts", 0)
        if time.time() - ts <= ttl:
            return data
        return None
    except Exception:
        return None

def _write_cache(path: str, payload: dict):
    try:
        payload["_ts"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass

def fetch_bars_from_alpaca(
    symbol: str,
    timeframe: str,
    limit: int,
    start: Optional[str],
    end: Optional[str],
    feed: str,
    adjustment: str,
) -> dict:
    # Endpoint correcto Alpaca Data v2:
    # GET /v2/stocks/{symbol}/bars
    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "feed": feed,              # para cuentas free suele funcionar "iex"
        "adjustment": adjustment,  # "raw" o "all"
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    r = requests.get(url, headers=headers(), params=params, timeout=15)

    # Debug corto en logs (Render)
    print(f"[candles] GET {r.url} -> {r.status_code}")
    if r.status_code >= 400:
        print(f"[candles] ERR body: {r.text[:500]}")

    r.raise_for_status()
    return r.json()

@router.get("/candles")
def get_candles(
    symbol: str = Query(..., description="Ej: SPY, QQQ, NVDA"),
    timeframe: str = Query("5Min", description="1Min, 5Min, 15Min, 1Hour, 1Day"),
    limit: int = Query(200, ge=1, le=10000),
    # Si NO mandas start/end, Alpaca normalmente devuelve las últimas `limit` velas.
    # Pero para evitar 0 fuera de sesión, ponemos un default inteligente (últimos ~10 días calendario):
    start: Optional[str] = Query(None, description="ISO8601, ej: 2025-12-20T00:00:00Z"),
    end: Optional[str] = Query(None, description="ISO8601, ej: 2025-12-31T23:59:59Z"),
    feed: str = Query("iex", description="iex (free), sip (si tienes suscripción)"),
    adjustment: str = Query("raw", description="raw/all"),
    use_cache: bool = Query(True),
    cache_ttl_sec: int = Query(30, ge=0, le=600),
):
    if not has_keys():
        raise HTTPException(status_code=500, detail="Alpaca keys not configured.")

    cache_file = _cache_path(symbol, timeframe)
    if use_cache and cache_ttl_sec > 0:
        cached = _read_cache(cache_file, cache_ttl_sec)
        if cached:
            return cached

    # Default inteligente de rango si NO mandan start/end:
    # Así siempre trae velas aunque sea madrugada.
    if not start and not end:
        now = datetime.now(timezone.utc)
        # 10 días calendario para cubrir ~5 días de trading
        start_dt = now - timedelta(days=10)
        start = _iso(start_dt)
        end = _iso(now)

    try:
        raw = fetch_bars_from_alpaca(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            start=start,
            end=end,
            feed=feed,
            adjustment=adjustment,
        )

        bars = raw.get("bars") or []
        payload = {
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "count": len(bars),
            "bars": bars,
            "next_page_token": raw.get("next_page_token"),
            "range": {"start": start, "end": end},
            "feed": feed,
            "adjustment": adjustment,
        }

        if use_cache and cache_ttl_sec > 0:
            _write_cache(cache_file, payload)

        return payload

    except requests.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Alpaca bars error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
