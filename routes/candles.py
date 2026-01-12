from fastapi import APIRouter, HTTPException, Query
from dotenv import load_dotenv
import os, json, time, requests, hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, List

load_dotenv()

router = APIRouter(tags=["candles"])

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
os.makedirs(PERSIST_DIR, exist_ok=True)

# Límites DUROS por timeframe (para evitar respuestas gigantes)
MAX_LIMIT_BY_TIMEFRAME = {
    "1Min": 200,
    "5Min": 80,
    "15Min": 120,
    "1Hour": 200,
    "1Day": 365,
}

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
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _safe_timeframe(tf: str) -> str:
    # Normaliza timeframe a lo que tu API espera
    tf = (tf or "5Min").strip()
    allowed = set(MAX_LIMIT_BY_TIMEFRAME.keys())
    if tf not in allowed:
        # Deja pasar otros timeframes si los usas, pero con un max conservador
        # O puedes forzar 400. Aquí forzamos 400 para endurecer.
        raise HTTPException(status_code=400, detail=f"timeframe inválido: {tf}. Usa: {sorted(allowed)}")
    return tf

def _effective_limit(timeframe: str, limit: int) -> int:
    max_allowed = MAX_LIMIT_BY_TIMEFRAME.get(timeframe, 80)
    return min(limit, max_allowed)

def _cache_key(
    symbol: str,
    timeframe: str,
    limit_effective: int,
    start: Optional[str],
    end: Optional[str],
    feed: str,
    adjustment: str,
    compact: bool,
    fields: Optional[List[str]],
) -> str:
    # Cache key estable: si cambia cualquier parámetro, cambia el cache
    raw = json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "limit": limit_effective,
        "start": start,
        "end": end,
        "feed": feed,
        "adjustment": adjustment,
        "compact": compact,
        "fields": fields or [],
    }, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def _cache_path(cache_key: str) -> str:
    return os.path.join(PERSIST_DIR, f"candles_cache_{cache_key}.json")

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
    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "feed": feed,
        "adjustment": adjustment,
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    r = requests.get(url, headers=headers(), params=params, timeout=15)
    print(f"[candles] GET {r.url} -> {r.status_code}")
    if r.status_code >= 400:
        print(f"[candles] ERR body: {r.text[:500]}")

    r.raise_for_status()
    return r.json()

def _compact_bar(b: dict) -> dict:
    # Alpaca bars típicos: t,o,h,l,c,v,n,vw
    # Reducimos a lo esencial para análisis técnico
    return {
        "t": b.get("t"),
        "o": b.get("o"),
        "h": b.get("h"),
        "l": b.get("l"),
        "c": b.get("c"),
        "v": b.get("v"),
        # Si quieres aún más pequeño, elimina o/h/l/v y deja solo t/c/v
    }

def _select_fields(b: dict, fields: List[str]) -> dict:
    out = {}
    for f in fields:
        if f in b:
            out[f] = b.get(f)
    return out

@router.get("/candles")
def get_candles(
    symbol: str = Query(..., description="Ej: SPY, QQQ, NVDA"),
    timeframe: str = Query("5Min", description="1Min, 5Min, 15Min, 1Hour, 1Day"),
    limit: int = Query(50, ge=1, le=10000),  # ✅ Default BAJO (antes 200)
    start: Optional[str] = Query(None, description="ISO8601, ej: 2025-12-20T00:00:00Z"),
    end: Optional[str] = Query(None, description="ISO8601, ej: 2025-12-31T23:59:59Z"),
    feed: str = Query("iex", description="iex (free), sip (si tienes suscripción)"),
    adjustment: str = Query("raw", description="raw/all"),
    use_cache: bool = Query(True),
    cache_ttl_sec: int = Query(30, ge=0, le=600),

    # ✅ NUEVO: respuesta compacta por defecto para evitar “respuesta demasiado grande”
    compact: bool = Query(True, description="Si True, devuelve velas compactas (recomendado para GPT/connector)."),

    # ✅ NUEVO: si quieres aún menor, puedes pedir fields específicos de Alpaca: t,o,h,l,c,v,n,vw
    fields: Optional[str] = Query(None, description="Campos separados por coma. Ej: t,c,v (solo funciona si compact=False)."),
):
    if not has_keys():
        raise HTTPException(status_code=500, detail="Alpaca keys not configured.")

    tf = _safe_timeframe(timeframe)
    limit_effective = _effective_limit(tf, limit)

    # Si intentan forzar enorme, igual lo capamos
    # (Si prefieres bloquear, cambia por raise HTTPException(400,...))
    if limit_effective != limit:
        print(f"[candles] limit capped: requested={limit} effective={limit_effective} tf={tf}")

    # Default inteligente de rango si NO mandan start/end
    if not start and not end:
        now = datetime.now(timezone.utc)
        # 10 días calendario para cubrir ~5 días de trading
        start_dt = now - timedelta(days=10)
        start = _iso(start_dt)
        end = _iso(now)

    field_list: Optional[List[str]] = None
    if fields:
        field_list = [x.strip() for x in fields.split(",") if x.strip()]

    cache_key = _cache_key(symbol, tf, limit_effective, start, end, feed, adjustment, compact, field_list)
    cache_file = _cache_path(cache_key)

    if use_cache and cache_ttl_sec > 0:
        cached = _read_cache(cache_file, cache_ttl_sec)
        if cached:
            return cached

    try:
        raw = fetch_bars_from_alpaca(
            symbol=symbol,
            timeframe=tf,
            limit=limit_effective,
            start=start,
            end=end,
            feed=feed,
            adjustment=adjustment,
        )

        bars = raw.get("bars") or []

        # ✅ Reducimos tamaño
        if compact:
            bars_out = [_compact_bar(b) for b in bars]
        else:
            if field_list:
                bars_out = [_select_fields(b, field_list) for b in bars]
            else:
                bars_out = bars  # full (solo para debugging)

        payload = {
            "status": "ok",
            "symbol": symbol,
            "timeframe": tf,
            "limit_requested": limit,
            "limit_effective": limit_effective,
            "count": len(bars_out),
            "bars": bars_out,
            "next_page_token": raw.get("next_page_token"),
            "range": {"start": start, "end": end},
            "feed": feed,
            "adjustment": adjustment,
            "compact": compact,
        }

        if use_cache and cache_ttl_sec > 0:
            _write_cache(cache_file, payload)

        return payload

    except requests.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Alpaca bars error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
