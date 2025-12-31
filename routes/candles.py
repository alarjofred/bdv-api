from fastapi import APIRouter, HTTPException
import os, requests, json, time
from datetime import datetime, timezone

router = APIRouter(prefix="/candles", tags=["candles"])

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
os.makedirs(PERSIST_DIR, exist_ok=True)

def alpaca_headers():
    if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
        return None
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
    }

def cache_path(symbol: str, timeframe: str):
    safe_tf = timeframe.replace("/", "_")
    return os.path.join(PERSIST_DIR, f"{symbol}_{safe_tf}.json")

@router.get("")
def get_candles(symbol: str, tf: str = "5Min", limit: int = 78, use_cache: bool = True, cache_ttl_sec: int = 30):
    """
    tf: 5Min | 15Min | 1Hour
    limit recomendado:
      5Min=78 (1 dia aprox)
      15Min=130 (5 dias aprox)
      1Hour=120 (1 mes aprox)
    """
    headers = alpaca_headers()
    if headers is None:
        raise HTTPException(status_code=500, detail="Faltan APCA_API_KEY_ID / APCA_API_SECRET_KEY")

    # Cache simple en /data para evitar pegarle a Alpaca cada request
    cpath = cache_path(symbol, tf)
    now = time.time()

    if use_cache and os.path.exists(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if now - cached.get("_ts", 0) <= cache_ttl_sec:
                return {"status":"ok","source":"cache","symbol":symbol,"tf":tf,"limit":limit,"bars":cached["bars"]}
        except:
            pass

    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {"timeframe": tf, "limit": limit, "adjustment": "raw"}
    r = requests.get(url, headers=headers, params=params, timeout=15)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Alpaca bars error: {r.status_code} {r.text}")

    payload = r.json()
    bars = payload.get("bars", [])  # Alpaca v2 suele devolver "bars"
    # Normalizamos por si cambia
    out = []
    for b in bars:
        out.append({
            "t": b.get("t"),  # timestamp ISO
            "o": b.get("o"),
            "h": b.get("h"),
            "l": b.get("l"),
            "c": b.get("c"),
            "v": b.get("v"),
        })

    # guardamos cache
    try:
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump({"_ts": now, "bars": out}, f, ensure_ascii=False)
    except:
        pass

    return {"status":"ok","source":"alpaca","symbol":symbol,"tf":tf,"limit":limit,"bars":out}
