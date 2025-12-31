# routes/analysis.py
import os
import json
from datetime import datetime, timedelta, timezone

import requests
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi_utils.tasks import repeat_every
from fastapi import FastAPI

try:
    # opcional para local; en Render usarás env vars
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

router = APIRouter(prefix="/analysis", tags=["analysis"])

# ===============================
#  LOG + HISTÓRICO PERSISTENTE (Render Disk)
# ===============================
# Render Disk típico: /data (ya lo montaste)
PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
os.makedirs(PERSIST_DIR, exist_ok=True)

LOG_FILE = os.path.join(PERSIST_DIR, "analysis-log.jsonl")

analysis_history = []  # memoria en runtime (se rellena desde disco al startup)

def _safe_json_loads(line: str):
    try:
        return json.loads(line)
    except Exception:
        return None

def load_history_from_disk(max_lines: int = 5000):
    """
    Carga el histórico desde /data/analysis-log.jsonl a memoria (analysis_history)
    para que NO se pierda al reiniciar Render.
    """
    if not os.path.exists(LOG_FILE):
        print(f"[HISTORY] No existe log aún: {LOG_FILE}")
        return

    loaded = 0
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = _safe_json_loads(line)
                if obj is not None:
                    analysis_history.append(obj)
                    loaded += 1

        # Recortar por seguridad
        if len(analysis_history) > max_lines:
            analysis_history[:] = analysis_history[-max_lines:]

        print(f"[HISTORY] Cargados {loaded} registros desde {LOG_FILE}. Mem={len(analysis_history)}")
    except Exception as e:
        print(f"[HISTORY] Error cargando historial desde disco: {e}")

def append_analysis_log(entry: dict):
    """Guarda el resultado en memoria + archivo persistente en /data."""
    try:
        analysis_history.append(entry)

        # Evitar crecimiento infinito en memoria
        if len(analysis_history) > 5000:
            analysis_history[:] = analysis_history[-2000:]

        # Guardar en archivo persistente (best-effort)
        line = json.dumps(entry, ensure_ascii=False)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir el log de análisis: {e}")

# ===============================
#  CONFIGURACIÓN ALPACA
# ===============================
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

# ✅ IMPORTANTE PARA CUENTAS FREE:
# IEX suele ser el feed permitido. SIP puede devolverte vacío/denegado.
APCA_DATA_FEED = os.getenv("APCA_DATA_FEED", "iex")  # "iex" o "sip"

def alpaca_headers():
    if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
        # No rompas el server completo: devuelve error cuando se use analysis
        return None
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Accept": "application/json",
    }

# ===============================
#  INDICADORES
# ===============================
def ema(values, period=20):
    if len(values) < period:
        return float(np.mean(values))
    weights = np.exp(np.linspace(-1.0, 0.0, period))
    weights /= weights.sum()
    a = np.convolve(values, weights, mode="full")[: len(values)]
    a[:period] = a[period]
    return float(a[-1])

def calc_rsi(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 2:
        return 50.0
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else 0
    rsi = 100 - (100 / (1 + rs)) if down != 0 else 100.0

    for delta in deltas[period:]:
        upval = delta if delta > 0 else 0
        downval = -delta if delta < 0 else 0
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down if down != 0 else 0
        rsi = 100 - (100 / (1 + rs)) if down != 0 else 100.0

    return float(rsi)

# ===============================
#  ALPACA BARS (ROBUSTO)
# ===============================
def fetch_bars(symbol: str, timeframe: str = "5Min", limit: int = 200):
    headers = alpaca_headers()
    if headers is None:
        raise HTTPException(status_code=500, detail="Faltan APCA_API_KEY_ID / APCA_API_SECRET_KEY en el entorno.")

    # ✅ para evitar respuestas vacías cuando el mercado está “raro”:
    # pedimos desde hace ~3 días
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=3)).isoformat()

    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "adjustment": "raw",
        "feed": APCA_DATA_FEED,
        "start": start,
    }

    r = requests.get(url, headers=headers, params=params, timeout=15)
    # Debug útil si algo falla:
    print(f"[DBG] bars {symbol} => {r.status_code} url={r.url}")

    if r.status_code >= 400:
        # no escondas el error (sirve para diagnosticar feed/permiso)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Error consultando bars en Alpaca",
                "status": r.status_code,
                "body": r.text[:500],
                "url": r.url,
            },
        )

    j = r.json()

    # Alpaca normalmente: {"bars":[...]}
    bars = j.get("bars")

    # A veces puede venir distinto; deja fallback:
    if bars is None:
        bars = j.get("bar") or j.get("data") or []

    # Si por cualquier razón llega como dict, conviértelo a lista:
    if isinstance(bars, dict):
        bars = bars.get(symbol) or bars.get(symbol.upper()) or []

    if not isinstance(bars, list):
        bars = []

    return bars

# ===============================
#  CORE: CALCULAR BIAS (SIN DEPENDER DEL ROUTE)
# ===============================
def compute_market_bias(symbol: str) -> dict:
    bars = fetch_bars(symbol, timeframe="5Min", limit=200)

    if not bars:
        # Aquí está tu caso actual
        return {"symbol": symbol.upper(), "bias": "neutral", "note": "No se recibieron datos (bars vacío). Revisa feed/mercado."}

    closes = np.array([b.get("c") for b in bars if b.get("c") is not None], dtype=float)
    volumes = np.array([b.get("v") for b in bars if b.get("v") is not None], dtype=float)

    if len(closes) < 30 or len(volumes) < 30:
        return {"symbol": symbol.upper(), "bias": "neutral", "note": "Datos insuficientes (menos de 30 barras)."}

    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    rsi = calc_rsi(closes, 14)

    vol_base = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    vol_ratio = float(volumes[-1] / vol_base) if vol_base > 0 else 1.0
    price = float(closes[-1])

    # Scoring simple (tu lógica)
    score = 0
    if price > ema9 > ema20:
        score += 1
    if rsi > 55:
        score += 1
    if vol_ratio > 1.1:
        score += 1

    if score >= 2:
        bias = "bullish"
        confidence = min(0.5 + score * 0.2, 1.0)
    elif score == 1:
        bias = "neutral"
        confidence = 0.4
    else:
        bias = "bearish"
        confidence = 0.7

    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "ema9": round(ema9, 2),
        "ema20": round(ema20, 2),
        "rsi": round(rsi, 2),
        "volume_ratio": round(vol_ratio, 2),
        "bias": bias,
        "confidence": round(float(confidence), 2),
    }

    append_analysis_log(log_entry)
    return log_entry

# ===============================
#  ENDPOINTS
# ===============================
@router.get("/bias/{symbol}")
def get_market_bias(symbol: str):
    """Devuelve el último análisis y lo guarda en history."""
    return compute_market_bias(symbol)

@router.post("/run")
@router.get("/run")
def run_analysis(symbols: str = "QQQ,SPY,NVDA"):
    """
    Fuerza análisis para símbolos separados por coma.
    Ej: /analysis/run?symbols=QQQ,SPY,NVDA
    """
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        raise HTTPException(status_code=400, detail="Debes pasar al menos 1 símbolo en symbols=...")

    results = []
    for s in syms:
        try:
            results.append(compute_market_bias(s))
        except Exception as e:
            results.append({"symbol": s, "bias": "neutral", "note": f"Error: {e}"})

    return {"status": "ok", "count": len(results), "results": results}

@router.get("/history")
def get_analysis_history(limit: int = 10):
    # devuelve los últimos N (en orden reciente->antiguo)
    return list(reversed(analysis_history[-limit:]))

@router.get("/sync")
def sync_analysis_data():
    if not analysis_history:
        return {"status": "empty", "message": "No hay datos para sincronizar."}

    latest_by_symbol = {}
    for entry in reversed(analysis_history):
        sym = entry.get("symbol")
        if sym and sym not in latest_by_symbol:
            latest_by_symbol[sym] = entry

    synced_data = [{"symbol": sym, **latest_by_symbol[sym]} for sym in sorted(latest_by_symbol.keys())]

    return {
        "status": "ok",
        "count": len(synced_data),
        "synced": synced_data,
        "note": "Datos listos para integración con Panel IA BDV",
    }

@router.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "BDV API Server",
        "analysis_count": len(analysis_history),
        "last_update": analysis_history[-1]["timestamp"] if analysis_history else None,
        "feed": APCA_DATA_FEED,
        "log_file": LOG_FILE,
        "persist_dir": PERSIST_DIR,
    }

# ===============================
#  AUTO-SYNC (cada 60s)
# ===============================
def register_auto_sync(app: FastAPI):
    """
    IMPORTANTE:
    - Esta función debe ser llamada desde main.py (o donde creas FastAPI)
      Ej:
        from routes.analysis import register_auto_sync
        register_auto_sync(app)
    """

    @app.on_event("startup")
    def _load_history_once():
        # Cargar histórico persistente a memoria al iniciar
        load_history_from_disk()

    @app.on_event("startup")
    @repeat_every(seconds=60)
    def auto_sync_task() -> None:
        symbols = ["QQQ", "SPY", "NVDA"]
        print("[AUTO-SYNC] tick…")

        for sym in symbols:
            try:
                out = compute_market_bias(sym)
                print(f"[AUTO-SYNC] {sym} => {out.get('bias')} {out.get('note','')}".strip())
            except Exception as e:
                print(f"[AUTO-SYNC] ⚠️ {sym} error: {e}")
