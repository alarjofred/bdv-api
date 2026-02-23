from fastapi import APIRouter, HTTPException, Header
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

# ✅ Importa también save_pending_trades si lo tienes en pending_trades.py
from .pending_trades import PENDING_TRADES
try:
    from .pending_trades import save_pending_trades  # opcional
except Exception:
    save_pending_trades = None

router = APIRouter(prefix="/monitor", tags=["monitor"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# ✅ Normaliza APCA_TRADING_URL para que NO termine en /v2 (porque abajo ya agregamos /v2/...)
_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
TRADING_URL = _raw_trading[:-3] if _raw_trading.endswith("/v2") else _raw_trading

# Seguridad: el cron/agente debe enviar este header
BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()

# ✅ Build id para verificar que Swagger/Cron pegan al mismo deploy
BUILD_ID = os.getenv("BUILD_ID", "unknown")

# ✅ Cooldown por símbolo+side (en memoria por proceso Render)
# key = "QQQ|buy" => ts utc
_LAST_ENTRY_BY_KEY: Dict[str, datetime] = {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false")
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


AUTO_ENTRY_COOLDOWN_SEC = _env_int("AUTO_ENTRY_COOLDOWN_SEC", 1800)  # 30 min
AI_TREND_MIN = _env_int("AI_TREND_MIN", 2)  # fallback si market_ctx falla
AI_MIN_CONFIDENCE = _env_float("AI_MIN_CONFIDENCE", 0.75)

# ✅ “Dual support” (Market Context gate)
MARKET_CTX_ENABLED = str(os.getenv("MARKET_CTX_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "y", "on")
MARKET_TREND_MIN = _env_int("MARKET_TREND_MIN", 2)  # si trend_strength < esto => no auto-entry
MARKET_CTX_TIMEFRAME = os.getenv("MARKET_CTX_TIMEFRAME", "5Min").strip()
MARKET_CTX_LIMIT = _env_int("MARKET_CTX_LIMIT", 200)
MARKET_CTX_LOOKBACK_HOURS = _env_int("MARKET_CTX_LOOKBACK_HOURS", 48)
MARKET_CTX_FEED = os.getenv("APCA_DATA_FEED", "").strip().lower()  # iex/sip (si existe). si vacio, snapshot.py decide default

# ✅ Preparado para Paper/Live “on demand”
DEFAULT_ALPACA_MODE = os.getenv("ALPACA_MODE", "paper").strip().lower()  # paper | live

# =====================================================
# ✅ NUEVO: CIERRE ASISTIDO POR IA (apagado por defecto)
# =====================================================
AI_CLOSE_ENABLED = _bool_env("AI_CLOSE_ENABLED", False)
AI_CLOSE_MIN_CONF = _env_float("AI_CLOSE_MIN_CONF", 0.75)
AI_CLOSE_TREND_MIN = _env_int("AI_CLOSE_TREND_MIN", 2)


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    """
    Protege endpoints del agente/monitor contra llamadas externas no autorizadas.
    Si BDV_AGENT_SECRET está definido, exige header X-BDV-SECRET.
    """
    if BDV_AGENT_SECRET:
        if (not x_bdv_secret) or (x_bdv_secret.strip() != BDV_AGENT_SECRET):
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _api_headers() -> Dict[str, str]:
    """
    Headers para llamadas internas entre endpoints del mismo servicio.
    Incluye X-BDV-SECRET si está configurado, para pasar autenticación interna.
    """
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if BDV_AGENT_SECRET:
        h["X-BDV-SECRET"] = BDV_AGENT_SECRET
    return h


def _with_build_id(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload["build_id"] = BUILD_ID
    return payload


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"status": "error", "http": resp.status_code, "body": resp.text}


def get_alpaca_headers() -> Dict[str, str]:
    api_key = os.getenv("APCA_API_KEY_ID")
    api_secret = os.getenv("APCA_API_SECRET_KEY")
    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY")
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def get_config_status() -> Dict[str, Any]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/config/status", headers=_api_headers(), timeout=5)
        data = _safe_json(resp)
        return data.get("data", data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_alpaca_mode_from_config(config: Dict[str, Any]) -> str:
    """
    Preparado para dual paper/live.
    Si luego agregas esto al panel (/config), monitor lo respeta sin redeploy.

    Orden:
      1) config["alpaca_mode"] si existe (paper/live)
      2) env ALPACA_MODE
    """
    mode = str(config.get("alpaca_mode", "") or "").strip().lower()
    if mode in ("paper", "live"):
        return mode
    mode = DEFAULT_ALPACA_MODE
    return mode if mode in ("paper", "live") else "paper"


def get_account_and_positions() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    headers = get_alpaca_headers()

    acc_resp = requests.get(f"{TRADING_URL}/v2/account", headers=headers, timeout=5)
    if acc_resp.status_code != 200:
        raise HTTPException(status_code=acc_resp.status_code, detail=f"Error cuenta: {acc_resp.text}")
    account = acc_resp.json()

    pos_resp = requests.get(f"{TRADING_URL}/v2/positions", headers=headers, timeout=5)
    if pos_resp.status_code not in (200, 404):
        raise HTTPException(status_code=pos_resp.status_code, detail=f"Error posiciones: {pos_resp.text}")

    positions: List[Dict[str, Any]] = [] if pos_resp.status_code == 404 else pos_resp.json()
    return account, positions


def get_risk_params(risk_mode: str) -> Dict[str, float]:
    risk_mode = (risk_mode or "low").lower()
    if risk_mode == "high":
        return {"tp_per_trade": 0.30, "sl_per_trade": 0.15, "daily_target": 0.05, "daily_max_loss": 0.02}
    if risk_mode == "medium":
        return {"tp_per_trade": 0.20, "sl_per_trade": 0.10, "daily_target": 0.03, "daily_max_loss": 0.015}
    return {"tp_per_trade": 0.15, "sl_per_trade": 0.08, "daily_target": 0.02, "daily_max_loss": 0.01}


def is_after_close_time() -> bool:
    """
    True si hora NY >= 15:45. Maneja DST correctamente.
    """
    now_ny = datetime.now(tz=ZoneInfo("America/New_York"))
    return (now_ny.hour > 15) or (now_ny.hour == 15 and now_ny.minute >= 45)


def _is_inside_rth() -> Tuple[bool, str]:
    """
    RTH: 09:30 - 16:00 NY
    """
    now_ny = datetime.now(tz=ZoneInfo("America/New_York"))
    dow = int(now_ny.strftime("%u"))  # 1..7
    hhmm = int(now_ny.strftime("%H%M"))
    if dow >= 6:
        return False, f"weekend {now_ny.isoformat()}"
    if hhmm < 930 or hhmm >= 1600:
        return False, f"outside_rth {now_ny.isoformat()}"
    return True, f"inside_rth {now_ny.isoformat()}"


def close_all_via_api() -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close-all")

    resp = requests.post(f"{API_BASE}/alpaca/close-all", headers=_api_headers(), timeout=10)
    if resp.status_code not in (200, 207):
        raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close-all: {resp.text}")
    return _safe_json(resp)


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close/{symbol}")

    symbol = str(symbol).strip().upper()
    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", headers=_api_headers(), timeout=10)
    data = _safe_json(resp)
    if resp.status_code in (200, 204):
        return data if isinstance(data, dict) else {"status": "ok", "symbol": symbol}
    raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close/{symbol}: {resp.text}")


def _execute_trade_via_http(symbol: str, side: str, qty: int, alpaca_mode: Optional[str] = None) -> Dict[str, Any]:
    """
    Ejecuta /trade (solo acciones hoy). Retorna respuesta o info de error (sin romper tick).
    ✅ Preparado para dual paper/live: manda alpaca_mode (paper/live) si está.
    """
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}

    symbol = str(symbol).strip().upper()
    side = str(side).lower().strip()
    qty = int(qty)

    url = f"{API_BASE.rstrip('/')}/trade"
    payload: Dict[str, Any] = {"symbol": symbol, "side": side, "qty": qty}

    # No rompe aunque trade.py aún no lo use
    if alpaca_mode in ("paper", "live"):
        payload["alpaca_mode"] = alpaca_mode

    try:
        r = requests.post(url, headers=_api_headers(), json=payload, timeout=10)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "payload": payload}
        return {"status": "ok", "result": _safe_json(r), "payload": payload}
    except Exception as e:
        return {"status": "error", "detail": str(e), "payload": payload}


def _get_snapshot_prices() -> Dict[str, Dict[str, Any]]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/snapshot", headers=_api_headers(), timeout=5)
        data = _safe_json(resp)
        if isinstance(data, dict):
            return data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        return {}
    except Exception:
        return {}


def _process_pending_trades(snapshot_data: Dict[str, Dict[str, Any]], allow_execute: bool) -> List[Dict[str, Any]]:
    """
    allow_execute=False: solo detecta triggers, NO ejecuta /trade.
    allow_execute=True: ejecuta /trade y marca triggered.
    """
    now = datetime.utcnow()
    ejecuciones: List[Dict[str, Any]] = []
    changed = False

    # ✅ Soporta PENDING_TRADES como LISTA
    for trade in list(PENDING_TRADES):
        if getattr(trade, "status", None) != "pending":
            continue

        valid_until = getattr(trade, "valid_until", None)
        if valid_until and now > valid_until:
            trade.status = "expired"
            changed = True
            ejecuciones.append({
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "status": "expired",
                "reason": "valid_until alcanzado",
            })
            continue

        info = snapshot_data.get(trade.symbol) or {}
        price = info.get("price")
        if price is None:
            continue

        condition_met = False
        if trade.side == "buy":
            if price >= trade.trigger_price and (trade.max_price is None or price <= trade.max_price):
                condition_met = True

        if not condition_met:
            continue

        if allow_execute:
            out = _execute_trade_via_http(trade.symbol, trade.side, trade.qty)
            trade.status = "triggered"
            changed = True
            ejecuciones.append({
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "qty": trade.qty,
                "trigger_price": trade.trigger_price,
                "max_price": trade.max_price,
                "price_at_trigger": price,
                "status": "triggered",
                "trade_result": out,
            })
        else:
            ejecuciones.append({
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "qty": trade.qty,
                "trigger_price": trade.trigger_price,
                "max_price": trade.max_price,
                "price_at_trigger": price,
                "status": "trigger_detected",
            })

    # ✅ Persistencia opcional si existe save_pending_trades()
    if changed and callable(save_pending_trades):
        try:
            save_pending_trades(PENDING_TRADES)
        except Exception:
            pass

    return ejecuciones


def _get_recommendation() -> Dict[str, Any]:
    if not API_BASE:
        return {}
    try:
        r = requests.get(f"{API_BASE}/recommend", headers=_api_headers(), timeout=10)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text}
        data = _safe_json(r)
        return data if isinstance(data, dict) else {"status": "error", "detail": "recommend_non_dict"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _pick_trade_from_recommend(rec_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(rec_payload, dict):
        return None

    recs = rec_payload.get("recommendations")
    if isinstance(recs, list) and recs:
        for r in recs:
            sug = str(r.get("suggestion", "")).lower().strip()
            sym = r.get("symbol")
            if sym and sug in ("buy", "sell"):
                return {"symbol": str(sym).strip().upper(), "side": sug, "source": "recommend"}
        return None

    sug = str(rec_payload.get("suggestion", "")).lower().strip()
    sym = rec_payload.get("symbol")
    if sym and sug in ("buy", "sell"):
        return {"symbol": str(sym).strip().upper(), "side": sug, "source": "recommend"}

    return None


def _ai_symbols() -> List[str]:
    raw = os.getenv("AI_SYMBOLS", "QQQ,SPY,NVDA")
    out: List[str] = []
    for s in raw.split(","):
        s = s.strip().upper()
        if s:
            out.append(s)
    return out or ["QQQ"]


# ==============================
# ✅ PASO 2: Market context live
# ==============================
def _get_market_context(symbols: List[str]) -> Dict[str, Any]:
    """
    Llama a /snapshot/indicators para obtener bias_inferred + trend_strength por símbolo.
    """
    if not API_BASE:
        return {}

    params: Dict[str, Any] = {
        "symbols": ",".join([s.strip().upper() for s in symbols if s.strip()]),
        "timeframe": MARKET_CTX_TIMEFRAME,
        "limit": str(MARKET_CTX_LIMIT),
        "lookback_hours": str(MARKET_CTX_LOOKBACK_HOURS),
    }
    if MARKET_CTX_FEED in ("iex", "sip"):
        params["feed"] = MARKET_CTX_FEED

    try:
        r = requests.get(f"{API_BASE}/snapshot/indicators", headers=_api_headers(), params=params, timeout=15)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
        data = _safe_json(r)
        return data if isinstance(data, dict) else {"status": "error", "detail": "market_ctx_non_dict", "params": params}
    except Exception as e:
        return {"status": "error", "detail": str(e), "params": params}


def _market_gate_allows(ctx: Dict[str, Any], side: str) -> Tuple[bool, str]:
    if not MARKET_CTX_ENABLED:
        return True, "market_ctx_disabled"

    try:
        ts = int(ctx.get("trend_strength", 0) or 0)
    except Exception:
        ts = 0

    bias = str(ctx.get("bias_inferred", "neutral") or "neutral").strip().lower()
    if bias not in ("bullish", "bearish", "neutral"):
        bias = "neutral"

    if ts < MARKET_TREND_MIN:
        return False, f"market_trend_too_low ts={ts} < {MARKET_TREND_MIN}"

    side = str(side).strip().lower()
    if side == "buy" and bias == "bearish":
        return False, f"market_bias_blocks_buy bias={bias} ts={ts}"
    if side == "sell" and bias == "bullish":
        return False, f"market_bias_blocks_sell bias={bias} ts={ts}"

    return True, f"market_gate_ok bias={bias} ts={ts}"


def _get_signals_ai(symbol: str, bias: str, trend_strength: int) -> Dict[str, Any]:
    if not API_BASE:
        return {}

    bias = (bias or "neutral").strip().lower()
    if bias not in ("bullish", "bearish", "neutral"):
        bias = "neutral"

    try:
        ts = int(trend_strength)
    except Exception:
        ts = 1

    params = {
        "symbol": str(symbol).strip().upper(),
        "bias": bias,
        "trend_strength": ts,
        "near_extreme": "false",
        "prefer_spreads": "true",
    }

    try:
        r = requests.get(f"{API_BASE}/signals/ai", headers=_api_headers(), params=params, timeout=12)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
        data = _safe_json(r)
        if isinstance(data, dict):
            data.setdefault("params", params)
            return data
        return {"status": "error", "detail": "signals_ai_non_dict", "params": params}
    except Exception as e:
        return {"status": "error", "detail": str(e), "params": params}


def _pick_trade_from_signals_ai(ai_payload: Dict[str, Any], min_conf: float) -> Optional[Dict[str, Any]]:
    if not isinstance(ai_payload, dict):
        return None

    data = ai_payload.get("data", ai_payload)
    if not isinstance(data, dict):
        return None

    try:
        conf = float(data.get("confidence", 0) or 0)
    except Exception:
        conf = 0.0

    sym = data.get("symbol") or data.get("ticker")
    action = data.get("action") or data.get("side") or data.get("suggestion")

    structure = data.get("structure", {}) if isinstance(data.get("structure"), dict) else {}
    kind = str(structure.get("kind", "")).lower().strip()
    legs = structure.get("legs", [])
    legs_is_nonempty = isinstance(legs, list) and len(legs) > 0

    if (kind not in ("", "none")) or legs_is_nonempty:
        return {
            "status": "not_supported",
            "source": "signals_ai",
            "confidence": conf,
            "kind": kind or "unknown",
            "reason": "ai_signal_looks_like_options_or_non_stock_structure",
        }

    if sym and action:
        action = str(action).lower().strip()
        if action in ("buy", "sell") and conf >= min_conf:
            return {"symbol": str(sym).strip().upper(), "side": action, "source": "signals_ai", "confidence": conf}

    return None


def _summarize_ai(ai_payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ai_payload, dict):
        return {"status": "bad_ai_payload"}

    data = ai_payload.get("data", ai_payload)
    if not isinstance(data, dict):
        return {"status": ai_payload.get("status", "unknown")}

    structure = data.get("structure", {}) if isinstance(data.get("structure"), dict) else {}
    kind = str(structure.get("kind", "")).lower().strip()
    legs = structure.get("legs", [])
    legs_is_nonempty = isinstance(legs, list) and len(legs) > 0

    looks_like_options = legs_is_nonempty or (kind not in ("", "none"))

    return {
        "status": ai_payload.get("status", "ok"),
        "symbol": (data.get("symbol") or data.get("ticker")),
        "action": (data.get("action") or data.get("side") or data.get("suggestion")),
        "confidence": data.get("confidence"),
        "looks_like_options": bool(looks_like_options),
        "params_used": ai_payload.get("params"),
    }


def _cooldown_key(symbol: str, side: str) -> str:
    return f"{str(symbol).strip().upper()}|{str(side).strip().lower()}"


def _cooldown_state(symbol: str, side: str) -> Dict[str, Any]:
    now = datetime.utcnow()
    key = _cooldown_key(symbol, side)

    last_ts = _LAST_ENTRY_BY_KEY.get(key)
    if not last_ts:
        return {"allow": True, "reason": "no_last_entry_for_key", "remaining_sec": 0, "key": key}

    try:
        elapsed = (now - last_ts).total_seconds()
    except Exception:
        return {"allow": True, "reason": "bad_last_ts", "remaining_sec": 0, "key": key}

    if elapsed < AUTO_ENTRY_COOLDOWN_SEC:
        remaining = int(max(0, AUTO_ENTRY_COOLDOWN_SEC - elapsed))
        return {
            "allow": False,
            "reason": f"cooldown_active elapsed={int(elapsed)}s<{AUTO_ENTRY_COOLDOWN_SEC}s",
            "remaining_sec": remaining,
            "key": key,
            "last_ts": str(last_ts),
        }

    return {"allow": True, "reason": f"cooldown_ok elapsed={int(elapsed)}s", "remaining_sec": 0, "key": key}


def _set_last_entry(symbol: str, side: str) -> None:
    _LAST_ENTRY_BY_KEY[_cooldown_key(symbol, side)] = datetime.utcnow()


@router.get("/tick")
def monitor_tick(x_bdv_secret: Optional[str] = Header(default=None)):
    _require_agent_secret(x_bdv_secret)

    inside, rth_reason = _is_inside_rth()
    if not inside:
        return _with_build_id({"status": "skipped", "reason": rth_reason})

    config = get_config_status()
    exec_mode = str(config.get("execution_mode", "manual")).lower()
    risk_mode = str(config.get("risk_mode", "low")).lower()
    max_trades_per_day = int(config.get("max_trades_per_day", 1) or 1)
    trades_today = int(config.get("trades_today", 0) or 0)

    alpaca_mode = _get_alpaca_mode_from_config(config)
    recommend_enabled = _bool_env("RECOMMEND_AUTO_ENABLED", False)

    actions: Dict[str, Any] = {
        "closed_all": False,
        "closed_symbols": [],
        "reason_all": None,
        "pending_trades_executed": [],
        "auto_entry": {"status": "skipped", "reason": "not_evaluated"},
        "limits": {"max_trades_per_day": max_trades_per_day, "trades_today": trades_today},
        "guardrails": {
            "cooldown_sec": AUTO_ENTRY_COOLDOWN_SEC,
            "cooldown_scope": "per_symbol_and_side",
            "ai_min_conf": AI_MIN_CONFIDENCE,
            "ai_trend_min_fallback": AI_TREND_MIN,
            "ai_symbols": _ai_symbols(),
            "recommend_auto_enabled": recommend_enabled,
            "market_ctx_enabled": MARKET_CTX_ENABLED,
            "market_trend_min": MARKET_TREND_MIN,
            "market_ctx_timeframe": MARKET_CTX_TIMEFRAME,
            "market_ctx_limit": MARKET_CTX_LIMIT,
            "market_ctx_lookback_hours": MARKET_CTX_LOOKBACK_HOURS,
            "alpaca_mode": alpaca_mode,
            "ai_close_enabled": AI_CLOSE_ENABLED,
            "ai_close_min_conf": AI_CLOSE_MIN_CONF,
            "ai_close_trend_min": AI_CLOSE_TREND_MIN,
        },
        "config_echo": {"execution_mode": exec_mode, "risk_mode": risk_mode},
    }

    # ✅ Importante: ahora NO salimos si exec_mode=manual
    # Manual solo bloquea ENTRADAS; cierres/riesgo siguen funcionando.

    account, positions = get_account_and_positions()
    equity = float(account.get("equity", 0.0))
    last_equity = float(account.get("last_equity", equity))
    pnl_today = equity - last_equity

    params = get_risk_params(risk_mode)
    daily_target_abs = equity * params["daily_target"]
    daily_max_loss_abs = -equity * params["daily_max_loss"]

    actions["per_trade_params"] = {"tp_per_trade": params["tp_per_trade"], "sl_per_trade": params["sl_per_trade"]}
    actions["daily_params"] = {
        "target_pct": params["daily_target"],
        "max_loss_pct": params["daily_max_loss"],
        "pnl_today": pnl_today,
        "equity": equity,
        "last_equity": last_equity,
        "target_abs": daily_target_abs,
        "max_loss_abs": daily_max_loss_abs,
    }

    # Cierres por hora / P&L
    if positions and is_after_close_time():
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Hora límite 15:45 NY"
        actions["close_all_response"] = result

    if positions and (not actions["closed_all"]) and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result

    if positions and (not actions["closed_all"]) and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result

    # TP/SL por posición (si no cerramos todo)
    if positions and not actions["closed_all"]:
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue

            try:
                plpc = float(pos.get("unrealized_plpc", 0.0))
            except (TypeError, ValueError):
                plpc = 0.0

            if plpc >= params["tp_per_trade"]:
                resp = close_symbol_via_api(symbol)
                actions["closed_symbols"].append({"symbol": symbol, "reason": f"Take profit ({plpc:.2%})", "api_response": resp})
            elif plpc <= -params["sl_per_trade"]:
                resp = close_symbol_via_api(symbol)
                actions["closed_symbols"].append({"symbol": symbol, "reason": f"Stop loss ({plpc:.2%})", "api_response": resp})

    # =====================================================
    # ✅ AI CLOSE (solo si hay posiciones y está habilitado)
    #   - Cierra si IA da señal contraria con confianza alta
    #   - Audita en actions["ai_close"]
    # =====================================================
    actions["ai_close"] = {"enabled": AI_CLOSE_ENABLED, "attempts": [], "closed": []}

    if AI_CLOSE_ENABLED and positions and not actions["closed_all"]:
        already_closed = set([str(x.get("symbol", "")).upper() for x in actions.get("closed_symbols", []) if isinstance(x, dict)])

        pos_symbols = []
        for p in positions:
            s = str(p.get("symbol") or "").strip().upper()
            if s and s not in already_closed:
                pos_symbols.append(s)

        market_ctx_raw = _get_market_context(pos_symbols) if pos_symbols else {}
        market_data = {}
        if isinstance(market_ctx_raw, dict) and isinstance(market_ctx_raw.get("data"), dict):
            market_data = market_ctx_raw["data"]

        for pos in positions:
            symbol = str(pos.get("symbol") or "").strip().upper()
            if not symbol or symbol in already_closed:
                continue

            try:
                qty_pos = float(pos.get("qty", 0) or 0)
            except Exception:
                qty_pos = 0.0

            if qty_pos == 0:
                continue

            ctx = market_data.get(symbol, {}) if isinstance(market_data, dict) else {}
            bias = str(ctx.get("bias_inferred", "neutral") or "neutral").strip().lower()
            if bias not in ("bullish", "bearish", "neutral"):
                bias = "neutral"

            try:
                ts = int(ctx.get("trend_strength", 1) or 1)
            except Exception:
                ts = 1

            if ts < AI_CLOSE_TREND_MIN:
                actions["ai_close"]["attempts"].append({"symbol": symbol, "skip": f"trend_strength<{AI_CLOSE_TREND_MIN}", "ctx": ctx})
                continue

            ai_payload = _get_signals_ai(symbol, bias=bias, trend_strength=ts)
            summary = _summarize_ai(ai_payload)
            data = ai_payload.get("data", ai_payload) if isinstance(ai_payload, dict) else {}

            try:
                conf = float(data.get("confidence", 0) or 0)
            except Exception:
                conf = 0.0

            ai_action = str(data.get("action") or "").strip().lower()

            actions["ai_close"]["attempts"].append({
                "symbol": symbol,
                "position_qty": qty_pos,
                "ctx": ctx,
                "ai_summary": summary,
                "ai_action": ai_action,
                "ai_conf": conf,
            })

            if conf < AI_CLOSE_MIN_CONF:
                continue

            # Long + IA dice SELL => cerrar
            if qty_pos > 0 and ai_action == "sell":
                resp = close_symbol_via_api(symbol)
                actions["ai_close"]["closed"].append({"symbol": symbol, "reason": "ai_opposite_signal_for_long", "ai_conf": conf, "ctx": ctx, "api_response": resp})
                already_closed.add(symbol)

            # Short + IA dice BUY => cerrar
            elif qty_pos < 0 and ai_action == "buy":
                resp = close_symbol_via_api(symbol)
                actions["ai_close"]["closed"].append({"symbol": symbol, "reason": "ai_opposite_signal_for_short", "ai_conf": conf, "ctx": ctx, "api_response": resp})
                already_closed.add(symbol)

    # Pending trades:
    # - En auto => ejecuta
    # - En manual => solo detecta (trigger_detected) y NO ejecuta
    snapshot_data = _get_snapshot_prices()
    try:
        actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=(exec_mode == "auto"))
    except Exception:
        actions["pending_trades_executed"] = []

    # Si hay posiciones, NO hacemos auto-entry (pero ya hicimos cierres/riesgo arriba)
    if len(positions) != 0:
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist", "positions_count": len(positions)}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    # Si NO estamos en auto, aquí terminamos (sin entradas)
    if exec_mode != "auto":
        actions["auto_entry"] = {"status": "skipped", "reason": "execution_mode_not_auto", "detail": f"execution_mode='{exec_mode}' (no es 'auto')"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    if trades_today >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_trades_per_day_reached", "limits": actions["limits"]}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    # ✅ Market context + auto entry
    market_data: Dict[str, Any] = {}
    market_ctx_raw: Dict[str, Any] = {}

    syms_for_ctx = _ai_symbols()
    if MARKET_CTX_ENABLED:
        market_ctx_raw = _get_market_context(syms_for_ctx)
        if isinstance(market_ctx_raw, dict):
            md = market_ctx_raw.get("data")
            if isinstance(md, dict):
                market_data = md

    actions["market_ctx"] = market_data if market_data else (market_ctx_raw if market_ctx_raw else {"status": "skipped", "reason": "market_ctx_disabled"})

    ai_checked_summary: List[Dict[str, Any]] = []
    ai_pick: Optional[Dict[str, Any]] = None

    for sym in _ai_symbols():
        ctx = market_data.get(sym, {}) if isinstance(market_data, dict) else {}

        bias = str(ctx.get("bias_inferred", "neutral")).strip().lower()
        if bias not in ("bullish", "bearish", "neutral"):
            bias = "neutral"

        try:
            ts = int(ctx.get("trend_strength", 1) or 1)
        except Exception:
            ts = 1

        if not ctx:
            ts = AI_TREND_MIN if AI_TREND_MIN else 1

        ai_payload = _get_signals_ai(sym, bias=bias, trend_strength=ts)
        ai_checked_summary.append({"symbol": sym, "market_ctx": ctx, "summary": _summarize_ai(ai_payload)})

        pick = _pick_trade_from_signals_ai(ai_payload, min_conf=AI_MIN_CONFIDENCE)

        if isinstance(pick, dict) and pick.get("status") == "not_supported":
            continue

        if isinstance(pick, dict) and pick.get("symbol") and pick.get("side") and pick.get("source") == "signals_ai":
            if MARKET_CTX_ENABLED:
                ctx_for_pick = market_data.get(pick["symbol"], {}) if isinstance(market_data, dict) else {}
                allow_gate, gate_reason = _market_gate_allows(ctx_for_pick if isinstance(ctx_for_pick, dict) else {}, pick["side"])
                pick["market_gate"] = {"allow": allow_gate, "reason": gate_reason, "ctx": ctx_for_pick}
                if not allow_gate:
                    ai_checked_summary[-1]["market_gate_blocked"] = pick["market_gate"]
                    continue

            pick["bias_used"] = bias
            pick["trend_strength_used"] = ts
            ai_pick = pick
            break

    if ai_pick:
        cd = _cooldown_state(ai_pick["symbol"], ai_pick["side"])
        if not cd.get("allow", True):
            actions["auto_entry"] = {
                "status": "skipped",
                "reason": "cooldown",
                "detail": cd.get("reason"),
                "cooldown": cd,
                "picked": ai_pick,
                "thresholds": {"ai_min_conf": AI_MIN_CONFIDENCE},
                "signals_ai_checked": ai_checked_summary,
            }
            return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

        qty = 1
        out = _execute_trade_via_http(ai_pick["symbol"], ai_pick["side"], qty, alpaca_mode=alpaca_mode)
        if out.get("status") == "ok":
            _set_last_entry(ai_pick["symbol"], ai_pick["side"])

        actions["auto_entry"] = {
            "status": "attempted",
            "source": "signals_ai",
            "picked": ai_pick,
            "qty": qty,
            "trade_result": out,
            "cooldown": cd,
            "thresholds": {"ai_min_conf": AI_MIN_CONFIDENCE},
            "signals_ai_checked": ai_checked_summary,
            "alpaca_mode": alpaca_mode,
        }
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    # Fallback /recommend (bloqueado por defecto)
    rec = _get_recommendation()

    if not recommend_enabled:
        actions["auto_entry"] = {
            "status": "skipped",
            "reason": "no_trade_signal",
            "detail": "AI no dio entrada y /recommend está bloqueado (RECOMMEND_AUTO_ENABLED=false)",
            "signals_ai_checked": ai_checked_summary,
            "recommend": rec,
        }
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    pick = _pick_trade_from_recommend(rec if isinstance(rec, dict) else {})

    if not pick:
        actions["auto_entry"] = {
            "status": "skipped",
            "reason": "no_trade_signal",
            "detail": "AI no dio entrada y /recommend tampoco generó BUY/SELL",
            "signals_ai_checked": ai_checked_summary,
            "recommend": rec,
        }
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    if MARKET_CTX_ENABLED:
        ctx_for_pick = market_data.get(pick["symbol"], {}) if isinstance(market_data, dict) else {}
        allow_gate, gate_reason = _market_gate_allows(ctx_for_pick if isinstance(ctx_for_pick, dict) else {}, pick["side"])
        if not allow_gate:
            actions["auto_entry"] = {
                "status": "skipped",
                "reason": "market_gate",
                "detail": gate_reason,
                "picked": pick,
                "market_ctx_for_pick": ctx_for_pick,
                "recommend": rec,
                "signals_ai_checked": ai_checked_summary,
            }
            return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    cd = _cooldown_state(pick["symbol"], pick["side"])
    if not cd.get("allow", True):
        actions["auto_entry"] = {
            "status": "skipped",
            "reason": "cooldown",
            "detail": cd.get("reason"),
            "cooldown": cd,
            "picked": pick,
            "source": "recommend",
            "signals_ai_checked": ai_checked_summary,
            "recommend": rec,
        }
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    qty = 1
    out = _execute_trade_via_http(pick["symbol"], pick["side"], qty, alpaca_mode=alpaca_mode)
    if out.get("status") == "ok":
        _set_last_entry(pick["symbol"], pick["side"])

    actions["auto_entry"] = {
        "status": "attempted",
        "source": "recommend",
        "picked": pick,
        "qty": qty,
        "trade_result": out,
        "cooldown": cd,
        "recommend": rec,
        "alpaca_mode": alpaca_mode,
    }

    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})
