from fastapi import APIRouter, HTTPException, Header
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from .pending_trades import PENDING_TRADES

router = APIRouter(prefix="/monitor", tags=["monitor"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
TRADING_URL = _raw_trading[:-3] if _raw_trading.endswith("/v2") else _raw_trading

BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()
BUILD_ID = os.getenv("BUILD_ID", "unknown")

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


AUTO_ENTRY_COOLDOWN_SEC = _env_int("AUTO_ENTRY_COOLDOWN_SEC", 1800)

# Orquestación
ORCH_ENABLED = _bool_env("ORCH_ENABLED", True)
ORCH_DECISION_ENDPOINT = os.getenv("ORCH_DECISION_ENDPOINT", "/agent/decision").strip() or "/agent/decision"

# ✅ Máximo de posiciones abiertas simultáneamente
# 0 => usa max_trades_per_day del config (medium=3)
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 0)

# EOD close window (para permitir cierre aunque tick llegue tarde)
EOD_CLOSE_ENABLED = _bool_env("EOD_CLOSE_ENABLED", True)
EOD_CLOSE_HHMM = _env_int("EOD_CLOSE_HHMM", 1545)  # 15:45 ET


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
        resp = requests.get(f"{API_BASE}/config/status", headers=_api_headers(), timeout=5)
        data = _safe_json(resp)
        return data.get("data", data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_alpaca_mode_from_config(config: Dict[str, Any]) -> str:
    mode = str(config.get("alpaca_mode", "") or "").strip().lower()
    if mode in ("paper", "live"):
        return mode
    mode = str(os.getenv("ALPACA_MODE", "paper")).strip().lower()
    return mode if mode in ("paper", "live") else "paper"


def get_account_and_positions() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    headers = get_alpaca_headers()

    acc_resp = requests.get(f"{TRADING_URL}/v2/account", headers=headers, timeout=8)
    if acc_resp.status_code != 200:
        raise HTTPException(status_code=acc_resp.status_code, detail=f"Error cuenta: {acc_resp.text}")
    account = acc_resp.json()

    pos_resp = requests.get(f"{TRADING_URL}/v2/positions", headers=headers, timeout=8)
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


def _is_weekday_ny() -> bool:
    now = _now_ny()
    dow = int(now.strftime("%u"))  # 1..7
    return dow < 6


def is_after_close_time() -> bool:
    now_ny = _now_ny()
    hhmm = int(now_ny.strftime("%H%M"))
    return hhmm >= EOD_CLOSE_HHMM


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
    if resp.status_code not in (200, 207):
        raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close-all: {resp.text}")
    return _safe_json(resp)


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close/{symbol}")

    symbol = str(symbol).strip().upper()
    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", headers=_api_headers(), timeout=15)
    data = _safe_json(resp)
    if resp.status_code in (200, 204):
        return data if isinstance(data, dict) else {"status": "ok", "symbol": symbol}
    raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close/{symbol}: {resp.text}")


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


def _process_pending_trades(snapshot_data: Dict[str, Dict[str, Any]], allow_execute: bool) -> List[Dict[str, Any]]:
    now = datetime.utcnow()
    ejecuciones: List[Dict[str, Any]] = []

    for trade in list(PENDING_TRADES.values()):
        if trade.status != "pending":
            continue

        if trade.valid_until and now > trade.valid_until:
            trade.status = "expired"
            trade.expired_at = now
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
            trade.triggered_at = now
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

    return ejecuciones


def _get_agent_decision(exclude_symbols: List[str]) -> Dict[str, Any]:
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}
    params = {}
    if exclude_symbols:
        params["exclude"] = ",".join([s.strip().upper() for s in exclude_symbols if s.strip()])
    try:
        r = requests.get(
            f"{API_BASE}{ORCH_DECISION_ENDPOINT}",
            headers=_api_headers(),
            params=params,
            timeout=20,
        )
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
        j = r.json()
        return j if isinstance(j, dict) else {"status": "error", "detail": "decision_non_dict"}
    except Exception as e:
        return {"status": "error", "detail": str(e), "params": params}


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
            "orch_enabled": ORCH_ENABLED,
            "max_open_positions": (MAX_OPEN_POSITIONS if MAX_OPEN_POSITIONS > 0 else max_trades_per_day),
            "eod_close_enabled": EOD_CLOSE_ENABLED,
            "eod_close_hhmm": EOD_CLOSE_HHMM,
            "alpaca_mode": alpaca_mode,
        },
        "config_echo": {"execution_mode": exec_mode, "risk_mode": risk_mode},
    }

    # Si no es auto, no hace nada (incluye cierres). Esto respeta tu “manual=yo mando”.
    if exec_mode != "auto":
        actions["auto_entry"] = {"status": "skipped", "reason": "execution_mode_not_auto"}
        return _with_build_id({"status": "skipped", "reason": "execution_mode!='auto'", "actions": actions})

    # Cargar cuenta/posiciones SIEMPRE para permitir cierre EOD aunque tick llegue tarde
    account, positions = get_account_and_positions()
    open_symbols = sorted(list({str(p.get("symbol", "")).upper() for p in positions if p.get("symbol")}))
    actions["positions_count"] = len(positions)
    actions["open_symbols"] = open_symbols

    inside_rth, rth_reason = _is_inside_rth()

    # ✅ CIERRE EOD incluso si ya pasó de las 16:00 (si tick corre tarde)
    if EOD_CLOSE_ENABLED and positions and _is_weekday_ny() and is_after_close_time():
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "EOD close >= 15:45 ET"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "eod_close"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # Si no estamos en RTH, no abrimos trades ni pending triggers
    if not inside_rth:
        actions["auto_entry"] = {"status": "skipped", "reason": rth_reason}
        return _with_build_id({"status": "skipped", "reason": rth_reason, "actions": actions})

    # Risk metrics diarios
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
    }
    actions["per_trade_params"] = {"tp_per_trade": params["tp_per_trade"], "sl_per_trade": params["sl_per_trade"]}

    # Cierres por meta / pérdida diaria
    if positions and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "daily_target"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    if positions and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "daily_max_loss"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # TP/SL por posición
    for pos in positions:
        symbol = str(pos.get("symbol") or "").strip().upper()
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

    # Pending trades (solo en RTH)
    snapshot_data = _get_snapshot_prices()
    try:
        actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=True)
    except Exception:
        actions["pending_trades_executed"] = []

    # ========= AUTO ENTRY (ORQUESTADO) =========
    max_open_positions = MAX_OPEN_POSITIONS if MAX_OPEN_POSITIONS > 0 else max_trades_per_day

    if trades_today >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_trades_per_day_reached", "limits": actions["limits"]}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    if len(open_symbols) >= max_open_positions:
        actions["auto_entry"] = {
            "status": "skipped",
            "reason": "max_open_positions_reached",
            "open_symbols": open_symbols,
            "max_open_positions": max_open_positions,
        }
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    if not ORCH_ENABLED:
        actions["auto_entry"] = {"status": "skipped", "reason": "ORCH_ENABLED=false"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    # ✅ pide decisión excluyendo símbolos ya abiertos (para permitir NVDA + QQQ + SPY)
    dec = _get_agent_decision(exclude_symbols=open_symbols)
    actions["orchestrator_decision"] = dec

    if dec.get("decision") != "trade":
        actions["auto_entry"] = {"status": "skipped", "reason": "decision_no_trade", "detail": dec.get("why")}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    symbol = str(dec.get("symbol") or "").strip().upper()
    side = str(dec.get("side") or "").strip().lower()

    if not symbol or side not in ("buy", "sell"):
        actions["auto_entry"] = {"status": "skipped", "reason": "bad_decision_payload", "decision": dec}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    if symbol in open_symbols:
        actions["auto_entry"] = {"status": "skipped", "reason": "symbol_already_open", "symbol": symbol, "open_symbols": open_symbols}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    cd = _cooldown_state(symbol, side)
    if not cd.get("allow", True):
        actions["auto_entry"] = {"status": "skipped", "reason": "cooldown", "cooldown": cd, "picked": {"symbol": symbol, "side": side}}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})

    qty = 1
    out = _execute_trade_via_http(symbol, side, qty, alpaca_mode=alpaca_mode)
    if out.get("status") == "ok":
        _set_last_entry(symbol, side)

    actions["auto_entry"] = {
        "status": "attempted",
        "source": "agent/decision",
        "picked": {"symbol": symbol, "side": side, "confidence": dec.get("confidence"), "why": dec.get("why")},
        "qty": qty,
        "trade_result": out,
        "cooldown": cd,
        "alpaca_mode": alpaca_mode,
    }

    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})
