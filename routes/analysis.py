# routes/analysis.py
import requests
import os
import numpy as np
from fastapi import APIRouter

router = APIRouter(prefix="/analysis", tags=["analysis"])

# ===============================
#  CONFIGURACIÓN Y UTILIDADES
# ===============================
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

def alpaca_headers():
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
    a = np.convolve(values, weights, mode='full')[:len(values)]
    a[:period] = a[period]
    return a[-1]

def calc_rsi(closes, period=14):
    """Cálculo manual de RSI."""
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
    if price > ema9 > ema20: score += 1
    if rsi > 55: score += 1
    if vol_ratio > 1.1: score += 1

    if score >= 2:
        bias = "bullish"
        confidence = min(0.5 + score * 0.2, 1.0)
    elif score == 1:
        bias = "neutral"
        confidence = 0.4
    else:
        bias = "bearish"
        confidence = 0.7

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "ema9": round(ema9, 2),
        "ema20": round(ema20, 2),
        "rsi": round(rsi, 2),
        "volume_ratio": round(vol_ratio, 2),
        "bias": bias,
        "confidence": round(confidence, 2),
    }
