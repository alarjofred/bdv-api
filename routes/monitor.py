from fastapi import APIRouter, HTTPException, Header
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from .pending_trades import PENDING_TRADES

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
AI_MIN_CONFIDENCE = _env_float("AI_MIN_CONFIDENCE", 0.72)  # ✅ más "suave" por defecto

# ✅ “Dual support” (Market Context gate)
MARKET_CTX_ENABLED = _bool_env("MARKET_CTX_ENABLED", True)
MARKET_TREND_MIN = _env_int("MARKET_TREND_MIN", 2)
MARKET_CTX_TIMEFRAME = os.getenv("MARKET_CTX_TIMEFRAME", "5Min").strip()
MARKET_CTX_LIMIT = _env_int("MARKET_CTX_LIMIT", 200)
MARKET_CTX_LOOKBACK_HOURS = _env_int("MARKET_CTX_LOOKBACK_HOURS", 48)
MARKET_CTX_FEED = os.getenv("APCA_DATA_FEED", "").strip().lower()  # iex/sip (opcional)

# ✅ Preparado para Paper/Live “on demand”
DEFAULT_ALPACA_MODE = os.getenv("ALPACA_MODE", "paper").strip().lower()  # paper | live

# =====================================================
# ✅ NUEVO: ORQUESTACIÓN REAL (agent -> monitor)
# =====================================================
ORCH_ENABLED = _bool_env("ORCH_ENABLED", True)
ORCH_MIN_CONF = _env_float("ORCH_MIN_CONF", 0.72)  # ✅ suave por defecto
ORCH_DECISION_TIMEOUT = _env_int("ORCH_DECISION_TIMEOUT", 20)

# =====================================================
# ✅ NUEVO: CIERRE ASISTIDO POR IA (apagado por defecto)
# =====================================================
AI_CLOSE_ENABLED = _bool_env("AI_CLOSE_ENABLED", False)
AI_CLOSE_MIN_CONF = _env_float("AI_CLOSE_MIN_CONF", 0.75)
AI_CLOSE_TREND_MIN = _env_int("AI_CLOSE_TREND_MIN", 2)

# =====================================================
# ✅ NUEVO: EOD CLOSE robusto (no depende de RTH gate)
# =====================================================
EOD_CLOSE_ENABLED = _bool_env("EOD_CLOSE_ENABLED", True)


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    if BDV_AGENT_SECRET:
        if (not x_bdv_secret) or (x_bdv_secret.strip() != BDV_AGENT_SECRET):
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _api_headers() -> Dict[str, str]:
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
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret, "Accept": "application/json"}


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


def _now_ny() -> datetime:
    return datetime.now(tz=ZoneInfo("America/New_York"))


def is_after_close_time() -> bool:
    # 15:45 NY
    now_ny = _now_ny()
    return (now_ny.hour > 15) or (now_ny.hour == 15 and now_ny.minute >= 45)


def _is_inside_rth() -> Tuple[bool, str]:
    now_ny = _now_ny()
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
    resp = requests.post(f"{API_BASE}/alpaca/close-all", headers=_api_headers(), timeout=15)
    return _safe_json(resp)


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close/{symbol}")
    symbol = str(symbol).strip().upper()
    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", headers=_api_headers(), timeout=15)
    return _safe_json(resp)


def _execute_trade_via_http(symbol: str, side: str, qty: int, alpaca_mode: Optional[str] = None) -> Dict[str, Any]:
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}

    symbol = str(symbol).strip().upper()
    side = str(side).lower().strip()
    qty = int(qty)

    url = f"{API_BASE.rstrip('/')}/trade"
    payload: Dict[str, Any] = {"symbol": symbol, "side": side, "qty": qty}

    if alpaca_mode in ("paper", "live"):
        payload["alpaca_mode"] = alpaca_mode

    try:
        r = requests.post(url, headers=_api_headers(), json=payload, timeout=15)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "payload": payload}
        return {"status": "ok", "result": _safe_json(r), "payload": payload}
    except Exception as e:
        return {"status": "error", "detail": str(e), "payload": payload}


def _get_snapshot_prices() -> Dict[str, Dict[str, Any]]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/snapshot", headers=_api_headers(), timeout=8)
        data = _safe_json(resp)
        if isinstance(data, dict):
            return data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        return {}
    except Exception:
        return {}


def _iter_pending_trades():
    # soporta dict o list sin romper
    if isinstance(PENDING_TRADES, dict):
        return list(PENDING_TRADES.values())
    if isinstance(PENDING_TRADES, list):
        return list(PENDING_TRADES)
    return []


def _process_pending_trades(snapshot_data: Dict[str, Dict[str, Any]], allow_execute: bool) -> List[Dict[str, Any]]:
    now = datetime.utcnow()
    ejecuciones: List[Dict[str, Any]] = []

    for trade in _iter_pending_trades():
        if getattr(trade, "status", None) != "pending":
            continue

        valid_until = getattr(trade, "valid_until", None)
        if valid_until and now > valid_until:
            trade.status = "expired"
            trade.expired_at = now
            ejecuciones.append({"id": trade.id, "symbol": trade.symbol, "side": trade.side, "status": "expired"})
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
            trade.triggered_at = now
            ejecuciones.append({"id": trade.id, "symbol": trade.symbol, "side": trade.side, "status": "triggered", "trade_result": out})
        else:
            ejecuciones.append({"id": trade.id, "symbol": trade.symbol, "side": trade.side, "status": "trigger_detected"})

    return ejecuciones


# ==============================
# Market context
# ==============================
def _get_market_context(symbols: List[str]) -> Dict[str, Any]:
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
        r = requests.get(f"{API_BASE}/snapshot/indicators", headers=_api_headers(), params=params, timeout=20)
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


# ==============================
# Signals AI (fallback)
# ==============================
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
        r = requests.get(f"{API_BASE}/signals/ai", headers=_api_headers(), params=params, timeout=15)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
        data = _safe_json(r)
        if isinstance(data, dict):
            data.setdefault("params", params)
            return data
        return {"status": "error", "detail": "signals_ai_non_dict", "params": params}
    except Exception as e:
        return {"status": "error", "detail": str(e), "params": params}


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

    # si viene estructura de options, no soportado por /trade
    if (kind not in ("", "none")) or legs_is_nonempty:
        return {"status": "not_supported", "source": "signals_ai", "confidence": conf, "reason": "options_structure"}

    if sym and action:
        action = str(action).lower().strip()
        if action in ("buy", "sell") and conf >= min_conf:
            return {"symbol": str(sym).strip().upper(), "side": action, "source": "signals_ai", "confidence": conf}

    return None


def _cooldown_key(symbol: str, side: str) -> str:
    return f"{str(symbol).strip().upper()}|{str(side).strip().lower()}"


def _cooldown_state(symbol: str, side: str) -> Dict[str, Any]:
    now = datetime.utcnow()
    key = _cooldown_key(symbol, side)

    last_ts = _LAST_ENTRY_BY_KEY.get(key)
    if not last_ts:
        return {"allow": True, "reason": "no_last_entry_for_key", "remaining_sec": 0, "key": key}

    elapsed = (now - last_ts).total_seconds()
    if elapsed < AUTO_ENTRY_COOLDOWN_SEC:
        remaining = int(max(0, AUTO_ENTRY_COOLDOWN_SEC - elapsed))
        return {"allow": False, "reason": f"cooldown_active {int(elapsed)}s<{AUTO_ENTRY_COOLDOWN_SEC}s", "remaining_sec": remaining, "key": key}

    return {"allow": True, "reason": f"cooldown_ok elapsed={int(elapsed)}s", "remaining_sec": 0, "key": key}


def _set_last_entry(symbol: str, side: str) -> None:
    _LAST_ENTRY_BY_KEY[_cooldown_key(symbol, side)] = datetime.utcnow()


# ==============================
# ✅ ORQUESTACIÓN: Agent decision
# ==============================
def _call_agent_decision(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Espera un endpoint POST /agent/decision que devuelva algo tipo:
      { "status":"ok", "decision":"trade|no_trade|close", "symbol":"QQQ", "side":"buy|sell", "confidence":0.0-1.0, "reason":"..." }
    Si no existe o falla: retorna {"status":"error", ...} y monitor hace fallback.
    """
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}
    try:
        r = requests.post(f"{API_BASE}/agent/decision", headers=_api_headers(), json=payload, timeout=ORCH_DECISION_TIMEOUT)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text}
        data = _safe_json(r)
        return data if isinstance(data, dict) else {"status": "error", "detail": "agent_decision_non_dict"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _norm_decision(d: Dict[str, Any]) -> Dict[str, Any]:
    # normaliza llaves para tolerar variaciones
    decision = str(d.get("decision") or d.get("action") or d.get("result") or "").strip().lower()
    symbol = str(d.get("symbol") or d.get("ticker") or "").strip().upper()
    side = str(d.get("side") or d.get("order_side") or "").strip().lower()
    try:
        conf = float(d.get("confidence", 0) or 0)
    except Exception:
        conf = 0.0
    reason = str(d.get("reason") or d.get("note") or "").strip()
    return {"decision": decision, "symbol": symbol, "side": side, "confidence": conf, "reason": reason, "raw": d}


@router.get("/tick")
def monitor_tick(x_bdv_secret: Optional[str] = Header(default=None)):
    _require_agent_secret(x_bdv_secret)

    config = get_config_status()
    exec_mode = str(config.get("execution_mode", "manual")).lower()
    risk_mode = str(config.get("risk_mode", "low")).lower()
    max_trades_per_day = int(config.get("max_trades_per_day", 1) or 1)
    trades_today = int(config.get("trades_today", 0) or 0)
    alpaca_mode = _get_alpaca_mode_from_config(config)

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
            "market_ctx_enabled": MARKET_CTX_ENABLED,
            "market_trend_min": MARKET_TREND_MIN,
            "alpaca_mode": alpaca_mode,
            "orch_enabled": ORCH_ENABLED,
            "orch_min_conf": ORCH_MIN_CONF,
            "ai_close_enabled": AI_CLOSE_ENABLED,
            "ai_close_min_conf": AI_CLOSE_MIN_CONF,
            "ai_close_trend_min": AI_CLOSE_TREND_MIN,
            "eod_close_enabled": EOD_CLOSE_ENABLED,
        },
        "config_echo": {"execution_mode": exec_mode, "risk_mode": risk_mode},
    }

    # ✅ Siempre leemos cuenta/posiciones primero (para poder cerrar aunque esté fuera de RTH)
    account, positions = get_account_and_positions()

    # ---------------------------
    # ✅ EOD CLOSE (robusto)
    # ---------------------------
    now_ny = _now_ny()
    dow = int(now_ny.strftime("%u"))
    if EOD_CLOSE_ENABLED and positions and dow < 6 and is_after_close_time():
        # intenta cerrar aunque el cron llegue tarde
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "EOD 15:45 NY (robusto, no depende de RTH)"
        actions["close_all_response"] = result
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    # ---------------------------
    # RTH gate (para ENTRADAS)
    # ---------------------------
    inside, rth_reason = _is_inside_rth()
    actions["rth"] = {"inside": inside, "reason": rth_reason}

    # métricas diarias (para cierres por PnL)
    equity = float(account.get("equity", 0.0))
    last_equity = float(account.get("last_equity", equity))
    pnl_today = equity - last_equity
    params = get_risk_params(risk_mode)

    daily_target_abs = equity * params["daily_target"]
    daily_max_loss_abs = -equity * params["daily_max_loss"]

    actions["daily_params"] = {
        "pnl_today": pnl_today,
        "equity": equity,
        "last_equity": last_equity,
        "target_abs": daily_target_abs,
        "max_loss_abs": daily_max_loss_abs,
        "target_pct": params["daily_target"],
        "max_loss_pct": params["daily_max_loss"],
    }

    # cierres por meta/pérdida (solo si hay posiciones)
    if positions and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    if positions and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    # TP/SL por posición (solo si estamos en RTH, para evitar rechazos fuera de horario)
    if inside and positions:
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            try:
                plpc = float(pos.get("unrealized_plpc", 0.0))
            except Exception:
                plpc = 0.0

            if plpc >= params["tp_per_trade"]:
                resp = close_symbol_via_api(symbol)
                actions["closed_symbols"].append({"symbol": symbol, "reason": f"Take profit ({plpc:.2%})", "api_response": resp})
            elif plpc <= -params["sl_per_trade"]:
                resp = close_symbol_via_api(symbol)
                actions["closed_symbols"].append({"symbol": symbol, "reason": f"Stop loss ({plpc:.2%})", "api_response": resp})

    # ---------------------------
    # Pending trades (solo si auto + RTH)
    # ---------------------------
    if exec_mode == "auto" and inside:
        snapshot_data = _get_snapshot_prices()
        try:
            actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=True)
        except Exception:
            actions["pending_trades_executed"] = []

    # ---------------------------
    # Si hay posiciones, NO hacemos auto-entry
    # ---------------------------
    if len(positions) != 0:
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist", "positions_count": len(positions)}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    # ---------------------------
    # Si no estamos en RTH, no abrimos trades
    # ---------------------------
    if not inside:
        actions["auto_entry"] = {"status": "skipped", "reason": "outside_rth_no_entry", "detail": rth_reason}
        return _with_build_id({"status": "skipped", "reason": rth_reason, "actions": actions})

    # ---------------------------
    # Solo AUTO abre trades
    # ---------------------------
    if exec_mode != "auto":
        actions["auto_entry"] = {"status": "skipped", "reason": "execution_mode_not_auto", "detail": f"execution_mode='{exec_mode}'"}
        return _with_build_id({"status": "skipped", "reason": actions["auto_entry"]["detail"], "actions": actions})

    if trades_today >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_trades_per_day_reached", "limits": actions["limits"]}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    # ===========================
    # ✅ ORQUESTACIÓN REAL (agent -> monitor)
    # ===========================
    syms = [s.strip().upper() for s in os.getenv("AI_SYMBOLS", "QQQ,SPY,NVDA").split(",") if s.strip()]
    snapshot_data = _get_snapshot_prices()

    market_ctx_raw = _get_market_context(syms) if MARKET_CTX_ENABLED else {"status": "skipped", "reason": "market_ctx_disabled"}
    market_data = market_ctx_raw.get("data", {}) if isinstance(market_ctx_raw, dict) and isinstance(market_ctx_raw.get("data"), dict) else {}

    actions["market_ctx"] = market_data if market_data else market_ctx_raw

    if ORCH_ENABLED:
        decision_payload = {
            "mode": "entry",
            "symbols": syms,
            "config": {"execution_mode": exec_mode, "risk_mode": risk_mode, "max_trades_per_day": max_trades_per_day, "trades_today": trades_today},
            "snapshot": snapshot_data,
            "market_ctx": market_data,
            "alpaca_mode": alpaca_mode,
        }
        orch_raw = _call_agent_decision(decision_payload)
        actions["orchestrator"] = orch_raw

        if isinstance(orch_raw, dict) and orch_raw.get("status") == "ok":
            d = _norm_decision(orch_raw)
            # decision trade/no_trade
            if d["decision"] == "trade" and d["symbol"] and d["side"] in ("buy", "sell") and d["confidence"] >= ORCH_MIN_CONF:
                # gate por market_ctx (si está habilitado)
                if MARKET_CTX_ENABLED:
                    ctx_for_pick = market_data.get(d["symbol"], {}) if isinstance(market_data, dict) else {}
                    allow_gate, gate_reason = _market_gate_allows(ctx_for_pick if isinstance(ctx_for_pick, dict) else {}, d["side"])
                    d["market_gate"] = {"allow": allow_gate, "reason": gate_reason, "ctx": ctx_for_pick}
                    if not allow_gate:
                        actions["auto_entry"] = {"status": "skipped", "reason": "market_gate", "detail": gate_reason, "picked": d}
                        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

                cd = _cooldown_state(d["symbol"], d["side"])
                if not cd.get("allow", True):
                    actions["auto_entry"] = {"status": "skipped", "reason": "cooldown", "detail": cd.get("reason"), "picked": d, "cooldown": cd}
                    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

                qty = 1
                out = _execute_trade_via_http(d["symbol"], d["side"], qty, alpaca_mode=alpaca_mode)
                if out.get("status") == "ok":
                    _set_last_entry(d["symbol"], d["side"])

                actions["auto_entry"] = {"status": "attempted", "source": "agent_decision", "picked": d, "qty": qty, "trade_result": out, "cooldown": cd, "alpaca_mode": alpaca_mode}
                return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

            # si agent dice no_trade o conf baja, seguimos a fallback sin romper
            actions["auto_entry_orch_note"] = {"decision": d["decision"], "confidence": d["confidence"], "reason": d["reason"]}

    # ===========================
    # ✅ FALLBACK: lógica actual (signals_ai)
    # ===========================
    ai_checked_summary: List[Dict[str, Any]] = []
    ai_pick: Optional[Dict[str, Any]] = None

    for sym in syms:
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

        if isinstance(pick, dict) and pick.get("symbol") and pick.get("side"):
            if MARKET_CTX_ENABLED:
                ctx_for_pick = market_data.get(pick["symbol"], {}) if isinstance(market_data, dict) else {}
                allow_gate, gate_reason = _market_gate_allows(ctx_for_pick if isinstance(ctx_for_pick, dict) else {}, pick["side"])
                pick["market_gate"] = {"allow": allow_gate, "reason": gate_reason, "ctx": ctx_for_pick}
                if not allow_gate:
                    continue

            pick["bias_used"] = bias
            pick["trend_strength_used"] = ts
            ai_pick = pick
            break

    if not ai_pick:
        actions["auto_entry"] = {"status": "skipped", "reason": "no_trade_signal", "signals_ai_checked": ai_checked_summary}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    cd = _cooldown_state(ai_pick["symbol"], ai_pick["side"])
    if not cd.get("allow", True):
        actions["auto_entry"] = {"status": "skipped", "reason": "cooldown", "detail": cd.get("reason"), "cooldown": cd, "picked": ai_pick, "signals_ai_checked": ai_checked_summary}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    qty = 1
    out = _execute_trade_via_http(ai_pick["symbol"], ai_pick["side"], qty, alpaca_mode=alpaca_mode)
    if out.get("status") == "ok":
        _set_last_entry(ai_pick["symbol"], ai_pick["side"])

    actions["auto_entry"] = {"status": "attempted", "source": "signals_ai", "picked": ai_pick, "qty": qty, "trade_result": out, "cooldown": cd, "signals_ai_checked": ai_checked_summary, "alpaca_mode": alpaca_mode}
    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})
