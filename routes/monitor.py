from fastapi import APIRouter, HTTPException, Header
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple, Set

router = APIRouter(prefix="/monitor", tags=["monitor"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

_raw_trading = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
# ✅ robusto: quita /v2 sin dejar slash extra
if _raw_trading.endswith("/v2"):
    TRADING_URL = _raw_trading[:-3].rstrip("/")
else:
    TRADING_URL = _raw_trading.rstrip("/")

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
AUTO_ENTRY_MAX_PER_TICK = _env_int("AUTO_ENTRY_MAX_PER_TICK", 3)

ORCH_ENABLED = _bool_env("ORCH_ENABLED", True)

MARKET_CTX_ENABLED = str(os.getenv("MARKET_CTX_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "y", "on")
MARKET_TREND_MIN = _env_int("MARKET_TREND_MIN", 2)
MARKET_CTX_TIMEFRAME = os.getenv("MARKET_CTX_TIMEFRAME", "5Min").strip()
MARKET_CTX_LIMIT = _env_int("MARKET_CTX_LIMIT", 200)
MARKET_CTX_LOOKBACK_HOURS = _env_int("MARKET_CTX_LOOKBACK_HOURS", 48)

DEFAULT_ALPACA_MODE = os.getenv("ALPACA_MODE", "paper").strip().lower()

# ✅ Cierre EOD SOLO DENTRO DE RTH (15:45–16:00 NY)
EOD_CLOSE_ENABLED = _bool_env("EOD_CLOSE_ENABLED", True)
EOD_CLOSE_HH = _env_int("EOD_CLOSE_HH", 15)
EOD_CLOSE_MM = _env_int("EOD_CLOSE_MM", 45)


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
        resp = requests.get(f"{API_BASE}/config/status", headers=_api_headers(), timeout=8)
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

    acc_resp = requests.get(f"{TRADING_URL}/v2/account", headers=headers, timeout=10)
    if acc_resp.status_code != 200:
        raise HTTPException(status_code=acc_resp.status_code, detail=f"Error cuenta: {acc_resp.text}")
    account = acc_resp.json()

    pos_resp = requests.get(f"{TRADING_URL}/v2/positions", headers=headers, timeout=10)
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


def _in_eod_close_window(now_ny: datetime) -> bool:
    if not EOD_CLOSE_ENABLED:
        return False
    hhmm = now_ny.hour * 100 + now_ny.minute
    start = EOD_CLOSE_HH * 100 + EOD_CLOSE_MM
    return (hhmm >= start) and (hhmm < 1600)


def close_all_via_api() -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close-all")
    resp = requests.post(f"{API_BASE}/alpaca/close-all", headers=_api_headers(), timeout=20)
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
        r = requests.post(url, headers=_api_headers(), json=payload, timeout=20)
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

    elapsed = (now - last_ts).total_seconds()
    if elapsed < AUTO_ENTRY_COOLDOWN_SEC:
        remaining = int(max(0, AUTO_ENTRY_COOLDOWN_SEC - elapsed))
        return {"allow": False, "reason": f"cooldown_active {int(elapsed)}s<{AUTO_ENTRY_COOLDOWN_SEC}s", "remaining_sec": remaining, "key": key}

    return {"allow": True, "reason": f"cooldown_ok {int(elapsed)}s", "remaining_sec": 0, "key": key}


def _set_last_entry(symbol: str, side: str) -> None:
    _LAST_ENTRY_BY_KEY[_cooldown_key(symbol, side)] = datetime.utcnow()


def _open_position_symbols(positions: List[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for p in positions or []:
        s = str(p.get("symbol") or "").strip().upper()
        if s:
            out.add(s)
    return out


def _get_agent_decision(exclude_symbols: Set[str]) -> Dict[str, Any]:
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}
    try:
        params = {}
        if exclude_symbols:
            params["exclude_symbols"] = ",".join(sorted(list(exclude_symbols)))
        r = requests.get(f"{API_BASE}/agent/decision", headers=_api_headers(), params=params, timeout=20)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text}
        data = _safe_json(r)
        return data if isinstance(data, dict) else {"status": "error", "detail": "agent_decision_non_dict"}
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
        "auto_entries": [],
        "limits": {"max_trades_per_day": max_trades_per_day, "trades_today": trades_today},
        "guardrails": {
            "eod_close_enabled": EOD_CLOSE_ENABLED,
            "eod_close_window": f"{EOD_CLOSE_HH:02d}:{EOD_CLOSE_MM:02d}–15:59 NY",
            "cooldown_sec": AUTO_ENTRY_COOLDOWN_SEC,
            "auto_entry_max_per_tick": AUTO_ENTRY_MAX_PER_TICK,
            "orch_enabled": ORCH_ENABLED,
            "alpaca_mode": alpaca_mode,
        },
        "config_echo": {"execution_mode": exec_mode, "risk_mode": risk_mode},
        "rth": {"inside": inside_rth, "reason": rth_reason, "now_ny": now_ny.isoformat()},
    }

    # 1) leer posiciones
    try:
        _account, positions = get_account_and_positions()
    except Exception as e:
        return _with_build_id({"status": "error", "reason": "alpaca_account_positions_failed", "detail": str(e), "actions": actions})

    actions["positions_count"] = len(positions)
    actions["open_symbols"] = sorted(list(_open_position_symbols(positions)))

    # 2) CIERRE EOD SOLO dentro de RTH y dentro de ventana 15:45–16:00
    if positions and inside_rth and _in_eod_close_window(now_ny):
        try:
            result = close_all_via_api()
            actions["closed_all"] = True
            actions["reason_all"] = f"EOD window {EOD_CLOSE_HH:02d}:{EOD_CLOSE_MM:02d}–15:59 NY"
            actions["close_all_response"] = result
            return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})
        except Exception as e:
            return _with_build_id({"status": "error", "reason": "eod_close_failed", "detail": str(e), "actions": actions})

    # 3) si no es auto, no abrir
    if exec_mode != "auto":
        actions["auto_entries"].append({"status": "skipped", "reason": "execution_mode_not_auto"})
        return _with_build_id({"status": "skipped", "reason": f"execution_mode='{exec_mode}'", "actions": actions})

    # 4) fuera de RTH no abrir
    if not inside_rth:
        actions["auto_entries"].append({"status": "skipped", "reason": "outside_rth_no_new_entries"})
        return _with_build_id({"status": "skipped", "reason": rth_reason, "actions": actions})

    # 5) TP/SL por posición (solo dentro de RTH)
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

    # 6) Entradas: permitir múltiples símbolos distintos hasta max_trades_per_day
    open_syms = _open_position_symbols(positions)
    attempted_syms: Set[str] = set()

    for _ in range(max(1, AUTO_ENTRY_MAX_PER_TICK)):
        if trades_today >= max_trades_per_day:
            actions["auto_entries"].append({"status": "skipped", "reason": "max_trades_per_day_reached"})
            break
        if len(open_syms) >= max_trades_per_day:
            actions["auto_entries"].append({"status": "skipped", "reason": "max_concurrent_positions_reached", "open": sorted(list(open_syms))})
            break

        dec = _get_agent_decision(exclude_symbols=open_syms.union(attempted_syms)) if ORCH_ENABLED else {"status": "ok", "decision": "no_trade", "why": "ORCH_ENABLED=false"}

        if not isinstance(dec, dict) or dec.get("decision") != "trade":
            actions["auto_entries"].append({"status": "skipped", "reason": "no_trade_from_agent", "agent_decision": dec})
            break

        sym = str(dec.get("symbol") or "").strip().upper()
        side = str(dec.get("side") or "").strip().lower()

        if not sym or side not in ("buy", "sell"):
            actions["auto_entries"].append({"status": "skipped", "reason": "bad_agent_decision_payload", "agent_decision": dec})
            break

        # ✅ evita repetir símbolo ya abierto o ya intentado en este tick
        if sym in open_syms or sym in attempted_syms:
            attempted_syms.add(sym)
            actions["auto_entries"].append({"status": "skipped", "reason": "symbol_already_open_or_attempted", "symbol": sym})
            continue

        cd = _cooldown_state(sym, side)
        if not cd.get("allow", True):
            attempted_syms.add(sym)
            actions["auto_entries"].append({"status": "skipped", "reason": "cooldown", "symbol": sym, "cooldown": cd})
            continue

        out = _execute_trade_via_http(sym, side, qty=1, alpaca_mode=alpaca_mode)
        if out.get("status") == "ok":
            _set_last_entry(sym, side)
            open_syms.add(sym)
            trades_today += 1
            actions["auto_entries"].append({"status": "attempted", "symbol": sym, "side": side, "trade_result": out, "agent_decision": dec})
        else:
            attempted_syms.add(sym)
            actions["auto_entries"].append({"status": "error", "symbol": sym, "side": side, "trade_result": out, "agent_decision": dec})
            break

    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions})
