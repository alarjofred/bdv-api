# routes/monitor.py

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


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    """
    Protege endpoints del agente/monitor contra llamadas externas no autorizadas.
    Si BDV_AGENT_SECRET está definido, exige header X-BDV-SECRET.
    """
    if BDV_AGENT_SECRET:
        if not x_bdv_secret or x_bdv_secret.strip() != BDV_AGENT_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def get_alpaca_headers() -> Dict[str, str]:
    api_key = os.getenv("APCA_API_KEY_ID")
    api_secret = os.getenv("APCA_API_SECRET_KEY")
    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="Faltan APCA_API_KEY_ID o APCA_API_SECRET_KEY",
        )
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def get_config_status() -> Dict[str, Any]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/config/status", timeout=5)
        data = resp.json()
        return data.get("data", data)
    except Exception:
        return {}


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


def close_all_via_api() -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close-all")

    resp = requests.post(f"{API_BASE}/alpaca/close-all", timeout=10)
    if resp.status_code not in (200, 207):
        raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close-all: {resp.text}")
    return resp.json()


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close/{symbol}")

    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", timeout=10)
    try:
        return resp.json()
    except Exception:
        if resp.status_code in (200, 204):
            return {"status": "ok", "symbol": symbol, "detail": "cerrado"}
        raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close/{symbol}: {resp.text}")


def _execute_trade_via_http(symbol: str, side: str, qty: int) -> None:
    """
    Ejecuta /trade (solo se usará cuando allow_execute=True).
    """
    if not API_BASE:
        return

    url = f"{API_BASE.rstrip('/')}/trade"
    payload = {"symbol": symbol, "side": side, "qty": qty}
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
    except Exception:
        # no romper monitor
        pass


def _get_snapshot_prices() -> Dict[str, Dict[str, Any]]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/snapshot", timeout=5)
        data = resp.json()
        return data.get("data", {})
    except Exception:
        return {}


def _process_pending_trades(snapshot_data: Dict[str, Dict[str, Any]], allow_execute: bool) -> List[Dict[str, Any]]:
    """
    allow_execute=False: solo detecta triggers y marca, NO ejecuta /trade.
    allow_execute=True: ejecuta /trade.
    """
    now = datetime.utcnow()
    ejecuciones: List[Dict[str, Any]] = []

    for trade in list(PENDING_TRADES.values()):
        if trade.status != "pending":
            continue

        if trade.valid_until and now > trade.valid_until:
            trade.status = "expired"
            trade.expired_at = now
            ejecuciones.append(
                {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "status": "expired",
                    "reason": "valid_until alcanzado",
                }
            )
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

        # Trigger detectado
        if allow_execute:
            _execute_trade_via_http(trade.symbol, trade.side, trade.qty)
            trade.status = "triggered"
            trade.triggered_at = now
            status = "triggered"
        else:
            # En manual/scan: NO ejecutar
            status = "trigger_detected"

        ejecuciones.append(
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "qty": trade.qty,
                "trigger_price": trade.trigger_price,
                "max_price": trade.max_price,
                "price_at_trigger": price,
                "status": status,
            }
        )

    return ejecuciones


@router.get("/tick")
def monitor_tick(x_bdv_secret: Optional[str] = Header(default=None)):
    """
    EJECUCIÓN / GESTIÓN (solo en auto):
    - close por hora / P&L / tp/sl
    - pending trades ejecuta /trade SOLO si auto
    Protegido por X-BDV-SECRET si BDV_AGENT_SECRET está definido.
    """
    _require_agent_secret(x_bdv_secret)

    config = get_config_status()
    exec_mode = str(config.get("execution_mode", "manual")).lower()
    risk_mode = str(config.get("risk_mode", "low")).lower()

    if exec_mode != "auto":
        return {
            "status": "skipped",
            "reason": f"execution_mode='{exec_mode}' (no es 'auto')",
            "config": {"execution_mode": exec_mode, "risk_mode": risk_mode},
        }

    # Solo en auto tocamos Alpaca
    account, positions = get_account_and_positions()
    equity = float(account.get("equity", 0.0))
    last_equity = float(account.get("last_equity", equity))
    pnl_today = equity - last_equity

    params = get_risk_params(risk_mode)
    daily_target_abs = equity * params["daily_target"]
    daily_max_loss_abs = -equity * params["daily_max_loss"]

    actions: Dict[str, Any] = {
        "closed_all": False,
        "closed_symbols": [],
        "reason_all": None,
        "per_trade_params": {"tp_per_trade": params["tp_per_trade"], "sl_per_trade": params["sl_per_trade"]},
        "daily_params": {"target_pct": params["daily_target"], "max_loss_pct": params["daily_max_loss"], "pnl_today": pnl_today},
        "pending_trades_executed": [],
    }

    if positions and is_after_close_time():
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Hora límite 15:45 NY"
        actions["close_all_response"] = result
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions}

    if positions and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions}

    if positions and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "actions": actions}

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

    # Pending trades SOLO en auto ejecutan
    snapshot_data = _get_snapshot_prices()
    try:
        actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=True)
    except Exception:
        actions["pending_trades_executed"] = []

    return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions}
