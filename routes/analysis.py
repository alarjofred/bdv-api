# routes/analysis.py
import requests
import os
import numpy as np
from fastapi import APIRouter
import json
from datetime import datetime  # ✅ Manejo de fecha y hora

router = APIRouter(prefix="/analysis", tags=["analysis"])

# ===============================
#  LOG DE ANÁLISIS
# ===============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "analysis-log.jsonl")  # ✅ Log local en carpeta routes

# 🧠 Memoria temporal (Render no guarda archivos entre reinicios)
analysis_history = []

def append_analysis_log(entry: dict):
    """Guarda el resultado en memoria y opcionalmente en archivo local."""
    try:
        # 🧠 Guardar en memoria temporal
        analysis_history.append(entry)

        # 💾 Guardar también en archivo local (opcional, tolera errores)
        line = json.dumps(entry, ensure_ascii=False)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir el log de análisis: {e}")

# ===============================
#  CONFIGURACIÓN Y UTILIDADES
# ===============================
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

def alpaca_headers():
    """Cabeceras de autenticación para API de Alpaca."""
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Accept": "application/json",
    }

# ===============================
#  FUNCIONES TÉCNICAS
# ===============================
def ema(values, period=20):
    """Calcula una media exponencial (EMA)."""
    if len(values) < period:
        return np.mean(values)
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    a = np.convolve(values, weights, mode="full")[:len(values)]
    a[:period] = a[period]
    return a[-1]

def calc_rsi(closes, period=14):
    """Cálculo manual del RSI."""
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else 0
    rsi = 100 - (100 / (1 + rs))
    for delta in deltas[period:]:
        upval = delta if delta > 0 else 0
        downval = -delta if delta < 0 else 0
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down if down != 0 else 0
        rsi = 100 - (100 / (1 + rs))
    return rsi

# ===============================
#  ENDPOINT PRINCIPAL
# ===============================
@router.get("/bias/{symbol}")
def get_market_bias(symbol: str):
    """
    Evalúa tendencia, momentum y fuerza usando EMA9, EMA20, RSI y volumen.
    Devuelve un bias (bullish/bearish/neutral) con nivel de confianza.
    """
    url = f"{APCA_DATA_URL}/stocks/{symbol}/bars?timeframe=5Min&limit=100"
    r = requests.get(url, headers=alpaca_headers(), timeout=10)
    r.raise_for_status()
    bars = r.json().get("bars", [])
    
    if not bars:
        return {"symbol": symbol, "bias": "neutral", "note": "No se recibieron datos"}

    closes = np.array([b["c"] for b in bars])
    volumes = np.array([b["v"] for b in bars])

    if len(closes) < 30:
        return {"symbol": symbol, "bias": "neutral", "note": "Datos insuficientes"}

    # --- Cálculos técnicos ---
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    rsi = calc_rsi(closes)
    vol_ratio = volumes[-1] / np.mean(volumes[-20:])
    price = closes[-1]

    # --- Panel técnico de decisión ---
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

    # ✅ Guardar log histórico
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": symbol,
        "price": round(price, 2),
        "ema9": round(ema9, 2),
        "ema20": round(ema20, 2),
        "rsi": round(rsi, 2),
        "volume_ratio": round(vol_ratio, 2),
        "bias": bias,
        "confidence": round(confidence, 2),
    }
    append_analysis_log(log_entry)

    # --- Retorno normal ---
    return log_entry

# ===============================
#  ENDPOINT: HISTORIAL TEMPORAL
# ===============================
@router.get("/history")
def get_analysis_history(limit: int = 10):
    """
    Devuelve los últimos análisis guardados en memoria temporal.
    Ideal para Render (sin archivos persistentes).
    """
    return list(reversed(analysis_history[-limit:]))

# ===============================
#  ENDPOINT: SINCRONIZACIÓN PANEL BDV
# ===============================
@router.get("/sync")
def sync_analysis_data():
    """
    Endpoint de sincronización con el Panel IA BDV.
    Devuelve la lista de análisis más recientes agrupados por símbolo.
    """
    if not analysis_history:
        return {"status": "empty", "message": "No hay datos para sincronizar."}

    # Agrupar por símbolo (último análisis de cada uno)
    latest_by_symbol = {}
    for entry in reversed(analysis_history):
        sym = entry.get("symbol")
        if sym and sym not in latest_by_symbol:
            latest_by_symbol[sym] = entry

    # Ordenar por nombre del símbolo
    synced_data = [
        {"symbol": sym, **latest_by_symbol[sym]} for sym in sorted(latest_by_symbol.keys())
    ]

    return {
        "status": "ok",
        "count": len(synced_data),
        "synced": synced_data,
        "note": "Datos listos para integración con Panel IA BDV"
    }

# ===============================
#  SINCRONIZACIÓN AUTOMÁTICA (AUTO-SYNC)
# ===============================
from fastapi_utils.tasks import repeat_every
from fastapi import FastAPI

def register_auto_sync(app: FastAPI):
    """
    Registra una tarea automática que actualiza QQQ, SPY y NVDA
    cada 1 minuto para mantener /sync actualizado.
    """
    @app.on_event("startup")
   @repeat_every(seconds=60)  # ⏱️ cada 1 minuto
    def auto_sync_task() -> None:
        symbols = ["QQQ", "SPY", "NVDA"]
        print("[AUTO-SYNC] Iniciando actualización automática...")
        for sym in symbols:
            try:
                get_market_bias(sym)
                print(f"[AUTO-SYNC] ✅ {sym} actualizado correctamente")
            except Exception as e:
                print(f"[AUTO-SYNC] ⚠️ Error al actualizar {sym}: {e}")
        print("[AUTO-SYNC] Finalizado.")
        print(f"[AUTO-SYNC] Última actualización completada a {datetime.utcnow().isoformat()}")
