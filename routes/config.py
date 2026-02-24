from fastapi import APIRouter, HTTPException, Header
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from .pending_trades import PENDING_TRADES

router = APIRouter(prefix="/monitor", tags=["monitor"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# Normaliza APCA_TRADING_URL para que NO termine en /v2 (porque abajo agregamos /v2/..)
_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
TRADING_URL = _raw_trading[:-3] if _raw_trading.endswith("/v2") else _raw_trading

BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()
BUILD_ID = os.getenv("BUILD_ID", "unknown")

# Cooldown por símbolo+side (memoria por proceso)
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


# -----------------------------
# Guardrails / thresholds
# -----------------------------
AUTO_ENTRY_COOLDOWN_SEC = _env_int("AUTO_ENTRY_COOLDOWN_SEC", 1800)

# ✅ Regla “tier” (suave pero filtrada)
ORCH_HI_CONF = _env_float("ORCH_HI_CONF", 0.75)
ORCH_LO_CONF = _env_float("ORCH_LO_CONF", 0.66)
ORCH_MID_TS_MIN = _env_int("ORCH_MID_TS_MIN", 3)

# Market context gate (opcional)
MARKET_CTX_ENABLED = _bool_env("MARKET_CTX_ENABLED", True)
MARKET_TREND_MIN = _env_int("MARKET_TREND_MIN", 2)
MARKET_CTX_TIMEFRAME = os.getenv("MARKET_CTX_TIMEFRAME", "5Min").strip()
MARKET_CTX_LIMIT = _env_int("MARKET_CTX_LIMIT", 200)
MARKET_CTX_LOOKBACK_HOURS = _env_int("MARKET_CTX_LOOKBACK_HOURS", 48)
MARKET_CTX_FEED = os.getenv("APCA_DATA_FEED", "").strip().lower()

DEFAULT_ALPACA_MODE = os.getenv("ALPACA_MODE", "paper").strip().lower()

# AI CLOSE (opcional)
AI_CLOSE_ENABLED = _bool_env("AI_CLOSE_ENABLED", False)
AI_CLOSE_MIN_CONF = _env_float("AI_CLOSE_MIN_CONF", 0.75)
AI_CLOSE_TREND_MIN = _env_int("AI_CLOSE_TREND_MIN", 2)


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
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def get_config_status() -> Dict[str, Any]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/config/status", headers=_api_headers(), timeout=6)
        data = _safe_json(resp)
        return data.get("data", data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_alpaca_mode_from_config(config: Dict[str, Any]) -> str:
    mode = str(config.get("alpaca_mode", "") or "").strip().lower()
    if mode in ("paper", "live"):
        return mode
    return DEFAULT_ALPACA_MODE if DEFAULT_ALPACA_MODE in ("paper", "live") else "paper"


def get_account_and_positions() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    headers = get_alpaca_headers()

    acc_resp = requests.get(f"{TRADING_URL}/v2/account", headers=headers, timeout=6)
    if acc_resp.status_code != 200:
        raise HTTPException(status_code=acc_resp.status_code, detail=f"Error cuenta: {acc_resp.text}")
    account = acc_resp.json()

    pos_resp = requests.get(f"{TRADING_URL}/v2/positions", headers=headers, timeout=6)
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


def _is_inside_rth(now_ny: datetime) -> Tuple[bool, str]:
    dow = int(now_ny.strftime("%u"))  # 1..7
    hhmm = int(now_ny.strftime("%H%M"))
    if dow >= 6:
        return False, f"weekend {now_ny.isoformat()}"
    if hhmm < 930 or hhmm >= 1600:
        return False, f"outside_rth {now_ny.isoformat()}"
    return True, f"inside_rth {now_ny.isoformat()}"


def is_after_close_time(now_ny: datetime) -> bool:
    # 15:45 NY
    return (now_ny.hour > 15) or (now_ny.hour == 15 and now_ny.minute >= 45)


def close_all_via_api() -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close-all")
    resp = requests.post(f"{API_BASE}/alpaca/close-all", headers=_api_headers(), timeout=12)
    return _safe_json(resp)


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close/{symbol}")
    symbol = str(symbol).strip().upper()
    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", headers=_api_headers(), timeout=12)
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


def _inc_trades_today(delta: int = 1) -> Dict[str, Any]:
    # requiere que exista /config/inc-trades (te lo dejo abajo en config.py)
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}
    try:
        r = requests.post(f"{API_BASE}/config/inc-trades?delta={int(delta)}", headers=_api_headers(), timeout=6)
        return _safe_json(r)
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_snapshot_prices() -> Dict[str, Dict[str, Any]]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/snapshot", headers=_api_headers(), timeout=6)
        data = _safe_json(resp)
        if isinstance(data, dict):
            return data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        return {}
    except Exception:
        return {}


def _pending_iterable():
    # soporta dict o list
    if isinstance(PENDING_TRADES, dict):
        return list(PENDING_TRADES.values())
    if isinstance(PENDING_TRADES, list):
        return list(PENDING_TRADES)
    return []


def _process_pending_trades(snapshot_data: Dict[str, Dict[str, Any]], allow_execute: bool) -> List[Dict[str, Any]]:
    now = datetime.utcnow()
    ejecuciones: List[Dict[str, Any]] = []

    for trade in _pending_iterable():
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
            ejecuciones.append({"id": trade.id, "symbol": trade.symbol, "side": trade.side, "qty": trade.qty, "status": "triggered", "trade_result": out})
        else:
            ejecuciones.append({"id": trade.id, "symbol": trade.symbol, "side": trade.side, "qty": trade.qty, "status": "trigger_detected"})

    return ejecuciones


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
        return {"allow": False, "reason": f"cooldown_active elapsed={int(elapsed)}s<{AUTO_ENTRY_COOLDOWN_SEC}s", "remaining_sec": remaining, "key": key}

    return {"allow": True, "reason": f"cooldown_ok elapsed={int(elapsed)}s", "remaining_sec": 0, "key": key}


def _set_last_entry(symbol: str, side: str) -> None:
    _LAST_ENTRY_BY_KEY[_cooldown_key(symbol, side)] = datetime.utcnow()


def _tier_allows(conf: float, trend_strength: int) -> Tuple[bool, str]:
    if conf >= ORCH_HI_CONF:
        return True, f"conf>={ORCH_HI_CONF}"
    if conf >= ORCH_LO_CONF:
        if trend_strength >= ORCH_MID_TS_MIN:
            return True, f"{ORCH_LO_CONF}<=conf<{ORCH_HI_CONF} AND ts>={ORCH_MID_TS_MIN}"
        return False, f"mid_conf_but_ts<{ORCH_MID_TS_MIN}"
    return False, f"conf<{ORCH_LO_CONF}"


def _get_agent_decision() -> Dict[str, Any]:
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}
    try:
        r = requests.get(f"{API_BASE}/agent/decision", headers=_api_headers(), timeout=25)
        return _safe_json(r) if isinstance(_safe_json(r), dict) else {"status": "error", "detail": "decision_non_dict"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/tick")
def monitor_tick(x_bdv_secret: Optional[str] = Header(default=None)):
    _require_agent_secret(x_bdv_secret)

    now_ny = _now_ny()
    inside_rth, rth_reason = _is_inside_rth(now_ny)

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
            "orch_hi_conf": ORCH_HI_CONF,
            "orch_lo_conf": ORCH_LO_CONF,
            "orch_mid_ts_min": ORCH_MID_TS_MIN,
            "market_ctx_enabled": MARKET_CTX_ENABLED,
            "market_trend_min": MARKET_TREND_MIN,
            "alpaca_mode": alpaca_mode,
            "ai_close_enabled": AI_CLOSE_ENABLED,
            "ai_close_min_conf": AI_CLOSE_MIN_CONF,
            "ai_close_trend_min": AI_CLOSE_TREND_MIN,
        },
        "config_echo": {"execution_mode": exec_mode, "risk_mode": risk_mode},
        "time_ny": now_ny.isoformat(),
        "inside_rth": inside_rth,
        "rth_reason": rth_reason,
    }

    # Manual => no entradas ni cierres automáticos
    if exec_mode != "auto":
        actions["auto_entry"] = {"status": "skipped", "reason": "execution_mode_not_auto", "detail": f"execution_mode='{exec_mode}'"}
        return _with_build_id({"status": "skipped", "reason": actions["auto_entry"]["detail"], "actions": actions})

    # Siempre intenta leer posiciones (porque también necesitamos cierre EOD)
    account, positions = get_account_and_positions()
    actions["positions_count"] = len(positions)

    # ✅ CIERRE EOD: si ya son >= 15:45 NY y hay posiciones, intentamos cerrar (aunque ya sea >16:00 lo intenta y audita)
    if positions and is_after_close_time(now_ny):
        try:
            result = close_all_via_api()
            actions["closed_all"] = True
            actions["reason_all"] = "EOD >= 15:45 NY"
            actions["close_all_response"] = result
        except Exception as e:
            actions["closed_all"] = False
            actions["reason_all"] = f"EOD_CLOSE_ATTEMPT_FAILED: {e}"
        # En EOD no hacemos entradas nuevas
        actions["auto_entry"] = {"status": "skipped", "reason": "eod_close_window"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # Si fuera de RTH, no abrimos entradas nuevas, pero sí procesamos pending_trades en modo seguro (sin ejecutar)
    if not inside_rth:
        snapshot_data = _get_snapshot_prices()
        try:
            actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=False)
        except Exception:
            actions["pending_trades_executed"] = []
        actions["auto_entry"] = {"status": "skipped", "reason": "outside_rth_no_entry"}
        return _with_build_id({"status": "skipped", "reason": rth_reason, "actions": actions})

    # -----------------------------
    # TP/SL por posición (si hay)
    # -----------------------------
    params = get_risk_params(risk_mode)
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

    # Pending trades: solo ejecuta si está en RTH
    snapshot_data = _get_snapshot_prices()
    try:
        actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=True)
    except Exception:
        actions["pending_trades_executed"] = []

    # -----------------------------
    # ENTRADAS: permitir hasta N posiciones (símbolos distintos)
    # -----------------------------
    open_symbols = set([str(p.get("symbol", "")).strip().upper() for p in positions if isinstance(p, dict)])
    actions["open_symbols"] = sorted(list(open_symbols))

    # límite diario
    if trades_today >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_trades_per_day_reached", "limits": actions["limits"]}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # límite de posiciones abiertas simultáneas = max_trades_per_day (como pediste)
    if len(open_symbols) >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_open_positions_reached", "open": len(open_symbols), "max": max_trades_per_day}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # ✅ ORQUESTACIÓN: el “cerebro” decide
    decision_payload = _get_agent_decision()
    actions["orchestrator"] = {"agent_decision": decision_payload}

    if not isinstance(decision_payload, dict) or decision_payload.get("status") not in ("ok", "OK"):
        actions["auto_entry"] = {"status": "skipped", "reason": "agent_decision_error"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # candidates para escoger alternativa si la “mejor” ya está abierta
    candidates = []
    try:
        candidates = decision_payload.get("sources", {}).get("candidates", []) or []
    except Exception:
        candidates = []

    def pick_best_not_open() -> Optional[Dict[str, Any]]:
        best = None
        for c in candidates if isinstance(candidates, list) else []:
            if not isinstance(c, dict):
                continue
            sym = str(c.get("symbol", "")).strip().upper()
            side = str(c.get("action", "")).strip().lower()
            if not sym or sym in open_symbols:
                continue
            if side not in ("buy", "sell"):
                continue
            try:
                conf = float(c.get("confidence", 0) or 0)
            except Exception:
                conf = 0.0
            try:
                ts = int(c.get("trend_strength", 1) or 1)
            except Exception:
                ts = 1

            ok, _ = _tier_allows(conf, ts)
            if not ok:
                continue

            if best is None or conf > float(best.get("confidence", 0) or 0):
                best = {"symbol": sym, "side": side, "confidence": conf, "trend_strength": ts}
        return best

    chosen = None

    # 1) intenta lo que dice el decision “principal”
    if str(decision_payload.get("decision", "")).lower() == "trade":
        sym = str(decision_payload.get("symbol", "")).strip().upper()
        side = str(decision_payload.get("side", "")).strip().lower()
        try:
            conf = float(decision_payload.get("confidence", 0) or 0)
        except Exception:
            conf = 0.0

        # trend_strength: lo sacamos del candidato correspondiente si existe
        ts = 1
        for c in candidates if isinstance(candidates, list) else []:
            if isinstance(c, dict) and str(c.get("symbol", "")).strip().upper() == sym:
                try:
                    ts = int(c.get("trend_strength", 1) or 1)
                except Exception:
                    ts = 1
                break

        ok, why = _tier_allows(conf, ts)
        if ok and sym and side in ("buy", "sell") and sym not in open_symbols:
            chosen = {"symbol": sym, "side": side, "confidence": conf, "trend_strength": ts, "why": f"agent_decision + tier_ok ({why})"}

    # 2) si la principal está abierta o no pasa tier, elige mejor alternativa no abierta
    if not chosen:
        alt = pick_best_not_open()
        if alt:
            chosen = {**alt, "why": "picked_best_candidate_not_open (tier_rule)"}

    if not chosen:
        actions["auto_entry"] = {"status": "skipped", "reason": "no_trade_after_filters", "open_symbols": sorted(list(open_symbols))}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # cooldown
    cd = _cooldown_state(chosen["symbol"], chosen["side"])
    if not cd.get("allow", True):
        actions["auto_entry"] = {"status": "skipped", "reason": "cooldown", "cooldown": cd, "picked": chosen}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # ejecutar
    qty = 1
    out = _execute_trade_via_http(chosen["symbol"], chosen["side"], qty, alpaca_mode=alpaca_mode)
    if out.get("status") == "ok":
        _set_last_entry(chosen["symbol"], chosen["side"])
        inc = _inc_trades_today(1)
        actions["trades_today_increment"] = inc

    actions["auto_entry"] = {"status": "attempted", "picked": chosen, "qty": qty, "trade_result": out, "cooldown": cd}
    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})
