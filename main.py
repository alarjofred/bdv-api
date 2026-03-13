from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import os
import requests
from datetime import datetime
import json

# ---------------------------------
# Cargar variables del entorno
# ---------------------------------
load_dotenv()

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

BUILD_ID = os.getenv("BUILD_ID", "unknown")


def _normalize_data_url_v2(raw: str) -> str:
    raw = (raw or "https://data.alpaca.markets").strip().rstrip("/")
    if raw.endswith("/v2"):
        return raw
    return raw + "/v2"


APCA_DATA_URL = _normalize_data_url_v2(os.getenv("APCA_DATA_URL", "https://data.alpaca.markets"))

# Normaliza TRADING_URL para que siempre use /v2
_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
APCA_TRADING_URL = _raw_trading if _raw_trading.endswith("/v2") else f"{_raw_trading}/v2"

# ✅ DISCO PERSISTENTE (Render Disk) — ALINEADO con routes/config.py
PERSIST_DIR = (os.getenv("BDV_PERSIST_DIR", "/var/data") or "/var/data").strip()
os.makedirs(PERSIST_DIR, exist_ok=True)
TRADES_LOG_FILE = os.path.join(PERSIST_DIR, "trades-log.jsonl")


def has_alpaca_keys() -> bool:
    return bool(APCA_API_KEY_ID and APCA_API_SECRET_KEY)


def alpaca_headers() -> dict:
    if not has_alpaca_keys():
        raise HTTPException(
            status_code=500,
            detail="Faltan APCA_API_KEY_ID / APCA_API_SECRET_KEY en el entorno (Render Environment).",
        )
    return {
        "APCA-API-KEY-ID": APCA_API_KEY_ID,
        "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
        "Accept": "application/json",
    }


# ---------------------------------
# IMPORT DE ROUTERS
# ---------------------------------
from routes.test_alpaca import router as test_alpaca_router
from routes.recommend import router as recommend_router
from routes.signals import router as signals_router
from routes.config import router as config_router
from routes.monitor import router as monitor_router
from routes.signals_ai import router as signals_ai_router
from routes.alpaca_close import router as alpaca_close_router
from routes.agent import router as agent_router

from routes import trade
from routes import telegram_notify
from routes import pending_trades

# ✅ snapshot router (para /snapshot/indicators)
try:
    from routes.snapshot import router as snapshot_router
except Exception as e:
    snapshot_router = None
    print(f"[WARN] No se pudo importar routes.snapshot: {e}")

# Opcionales
try:
    from routes import analysis
except Exception as e:
    analysis = None
    print(f"[WARN] No se pudo importar routes.analysis: {e}")

try:
    from routes import candles
except Exception as e:
    candles = None
    print(f"[WARN] No se pudo importar routes.candles: {e}")


# ---------------------------------
# Inicializar FastAPI
# ---------------------------------
app = FastAPI(
    title="BDV API",
    version="0.1.0",
    servers=[
        {"url": "https://bdv-api-server.onrender.com", "description": "Render production"}
    ],
)

# ✅ Asegura persistencia de defaults auto/medium en primer arranque
@app.on_event("startup")
def _startup():
    try:
        from routes.config import ensure_config_persisted
        ensure_config_persisted()
    except Exception as e:
        print(f"[WARN] ensure_config_persisted failed: {e}")


@app.get("/", include_in_schema=False)
def root():
    return {
        "status": "ok",
        "service": "bdv-api",
        "message": "alive",
        "build_id": BUILD_ID,
        "alpaca_keys_loaded": has_alpaca_keys(),
        "persist_dir": PERSIST_DIR,
        "apca_data_url": APCA_DATA_URL,
        "apca_trading_url": APCA_TRADING_URL,
        "snapshot_router_loaded": bool(snapshot_router is not None),
    }


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "alpaca_keys_loaded": has_alpaca_keys(), "build_id": BUILD_ID}


# ---------------------------------
# Incluir routers
# ---------------------------------
app.include_router(test_alpaca_router)
app.include_router(recommend_router)
app.include_router(signals_router)
app.include_router(config_router)
app.include_router(monitor_router)
app.include_router(signals_ai_router)
app.include_router(alpaca_close_router)
app.include_router(agent_router)

# /trade SOLO desde routes/trade.py
app.include_router(trade.router)
app.include_router(telegram_notify.router)
app.include_router(pending_trades.router)

# /snapshot/indicators si existe
if snapshot_router is not None:
    app.include_router(snapshot_router)

if analysis is not None:
    app.include_router(analysis.router)

if candles is not None:
    app.include_router(candles.router)


# ---------------------------------
# Función auxiliar: última cotización (bid/ask)
# ---------------------------------
def get_latest_quote(symbol: str) -> dict:
    url = f"{APCA_DATA_URL}/stocks/{symbol}/quotes/latest"
    r = requests.get(url, headers=alpaca_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------
# Endpoint /snapshot (monitor.py lo usa)
# ---------------------------------
@app.get("/snapshot")
def market_snapshot():
    if not has_alpaca_keys():
        raise HTTPException(status_code=500, detail="Faltan keys de Alpaca para /snapshot.")

    symbols = ["QQQ", "SPY", "NVDA"]
    data = {}

    try:
        for sym in symbols:
            raw = get_latest_quote(sym)
            quote = raw.get("quote") or {}
            data[sym] = {
                "price": quote.get("ap"),
                "time": quote.get("t"),
                "bid": quote.get("bp"),
                "ask": quote.get("ap"),
            }
        return {"status": "ok", "data": data, "build_id": BUILD_ID}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting snapshot: {e}")


# ---------------------------------
# Log de trades (persistente)
# ---------------------------------
def append_trade_log(entry: dict) -> None:
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with open(TRADES_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] No se pudo escribir en el log de trades: {e}")


@app.get("/trades-log")
def get_trades_log(limit: int = 10):
    try:
        if not os.path.exists(TRADES_LOG_FILE):
            return {"status": "ok", "log": [], "file": TRADES_LOG_FILE, "build_id": BUILD_ID}

        entries = []
        with open(TRADES_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue

        entries = entries[-limit:]
        return {"status": "ok", "log": entries, "file": TRADES_LOG_FILE, "build_id": BUILD_ID}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading trades log: {e}")


# ---------------------------------
# Auto-sync (si tu analysis router lo usa)
# ---------------------------------
try:
    if analysis is not None:
        from routes.analysis import register_auto_sync
        register_auto_sync(app)
except Exception as e:
    print(f"[WARN] register_auto_sync no pudo registrarse: {e}")


# ---------------------------------
# UI (panel)
# ---------------------------------
if os.path.isdir("ui"):
    app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

@app.get("/snapshot/indicators")
def snapshot_indicators(
    symbols: str = "QQQ,SPY,NVDA",
    timeframe: str = "5Min",
    limit: int = 200,
    lookback_hours: int = 48,
):
    try:
        import numpy as np
        from datetime import timezone, timedelta

        def _safe_float(v, default=0.0):
            try:
                return float(v)
            except Exception:
                return default

        def _ema(values, period):
            if len(values) == 0:
                return 0.0
            if len(values) < period:
                return float(np.mean(values))
            alpha = 2 / (period + 1)
            ema_val = float(values[0])
            for x in values[1:]:
                ema_val = alpha * float(x) + (1 - alpha) * ema_val
            return float(ema_val)

        def _rsi(closes, period=14):
            if len(closes) < period + 1:
                return 50.0
            deltas = np.diff(closes)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)

            avg_gain = np.mean(gains[:period])
            avg_loss = np.mean(losses[:period])

            if avg_loss == 0:
                return 100.0

            for i in range(period, len(deltas)):
                avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
                avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

            if avg_loss == 0:
                return 100.0

            rs = avg_gain / avg_loss
            return float(100 - (100 / (1 + rs)))

        def _fetch_bars(symbol: str, tf: str, lim: int, start_iso: str):
            url = f"{APCA_DATA_URL}/stocks/{symbol}/bars"
            params = {
                "timeframe": tf,
                "limit": lim,
                "adjustment": "raw",
                "feed": os.getenv("APCA_DATA_FEED", "iex"),
                "start": start_iso,
            }
            r = requests.get(url, headers=alpaca_headers(), params=params, timeout=15)
            r.raise_for_status()
            j = r.json()
            bars = j.get("bars", [])
            if isinstance(bars, dict):
                bars = bars.get(symbol, []) or []
            if not isinstance(bars, list):
                bars = []
            return bars

        def _build_symbol_context(symbol: str):
            now = datetime.now(timezone.utc)
            start_intraday = (now - timedelta(hours=max(lookback_hours, 24))).isoformat()
            start_daily = (now - timedelta(days=10)).isoformat()

            bars_5m = _fetch_bars(symbol, timeframe, limit, start_intraday)
            bars_1d = _fetch_bars(symbol, "1Day", 5, start_daily)

            if not bars_5m:
                return {
                    "symbol": symbol,
                    "data_quality_ok": False,
                    "bias_inferred": "neutral",
                    "trend_strength": 1,
                    "reason": "no_intraday_bars",
                }

            closes = np.array([_safe_float(b.get("c")) for b in bars_5m if b.get("c") is not None], dtype=float)
            highs = np.array([_safe_float(b.get("h")) for b in bars_5m if b.get("h") is not None], dtype=float)
            lows = np.array([_safe_float(b.get("l")) for b in bars_5m if b.get("l") is not None], dtype=float)
            volumes = np.array([_safe_float(b.get("v")) for b in bars_5m if b.get("v") is not None], dtype=float)

            if len(closes) < 30 or len(volumes) < 30:
                return {
                    "symbol": symbol,
                    "data_quality_ok": False,
                    "bias_inferred": "neutral",
                    "trend_strength": 1,
                    "reason": "insufficient_intraday_bars",
                    "bars_count": len(closes),
                }

            price = float(closes[-1])
            ema9 = _ema(closes, 9)
            ema20 = _ema(closes, 20)
            rsi14 = _rsi(closes, 14)

            vol_base = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
            vol_ratio = float(volumes[-1] / vol_base) if vol_base > 0 else 1.0

            prev_day_high = None
            prev_day_low = None
            prev_day_close = None

            if len(bars_1d) >= 2:
                prev = bars_1d[-2]
                prev_day_high = _safe_float(prev.get("h"), 0.0)
                prev_day_low = _safe_float(prev.get("l"), 0.0)
                prev_day_close = _safe_float(prev.get("c"), 0.0)

            bullish_points = 0
            bearish_points = 0

            if price > ema9 > ema20:
                bullish_points += 1
            if price < ema9 < ema20:
                bearish_points += 1

            if rsi14 >= 58:
                bullish_points += 1
            if rsi14 <= 42:
                bearish_points += 1

            if vol_ratio >= 1.15:
                if closes[-1] >= closes[-2]:
                    bullish_points += 1
                else:
                    bearish_points += 1

            if prev_day_close and prev_day_close > 0:
                if price > prev_day_close:
                    bullish_points += 1
                elif price < prev_day_close:
                    bearish_points += 1

            if bullish_points >= bearish_points + 1 and bullish_points >= 2:
                bias = "bullish"
                trend_strength = min(3, bullish_points)
            elif bearish_points >= bullish_points + 1 and bearish_points >= 2:
                bias = "bearish"
                trend_strength = min(3, bearish_points)
            else:
                bias = "neutral"
                trend_strength = 1

            spread_proxy = float(highs[-1] - lows[-1]) if len(highs) and len(lows) else 0.0
            data_quality_ok = bool(price > 0 and ema9 > 0 and ema20 > 0 and spread_proxy >= 0)

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "bars_count": len(closes),
                "data_quality_ok": data_quality_ok,
                "bias_inferred": bias,
                "trend_strength": int(trend_strength),
                "price": round(price, 4),
                "ema9": round(float(ema9), 4),
                "ema20": round(float(ema20), 4),
                "rsi14": round(float(rsi14), 2),
                "vol_ratio": round(float(vol_ratio), 2),
                "prev_day_high": round(float(prev_day_high), 4) if prev_day_high else None,
                "prev_day_low": round(float(prev_day_low), 4) if prev_day_low else None,
                "prev_day_close": round(float(prev_day_close), 4) if prev_day_close else None,
                "price_vs_prev_close": round(float(price - prev_day_close), 4) if prev_day_close else None,
            }

        syms = []
        for s in symbols.split(","):
            s = s.strip().upper()
            if s and s not in syms:
                syms.append(s)

        data = {}
        errors = {}

        for sym in syms:
            try:
                data[sym] = _build_symbol_context(sym)
            except Exception as e:
                errors[sym] = str(e)

        return {
            "status": "ok",
            "data": data,
            "errors": errors,
            "meta": {
                "timeframe": timeframe,
                "limit": limit,
                "lookback_hours": lookback_hours,
                "feed": os.getenv("APCA_DATA_FEED", "iex"),
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error building /snapshot/indicators: {e}")
