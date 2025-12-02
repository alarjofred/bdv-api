from fastapi import APIRouter, HTTPException
import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

router = APIRouter(prefix="/monitor", tags=["monitor"])

# URL pública de tu API (Render)
API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# URL de trading de Alpaca (paper/live)
TRADING_URL = os.getenv("APCA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")


def get_alpaca_headers() -> Dict[str, str]:
    """
    Construye los headers necesarios para autenticar contra Alpaca.
    """
    api_key = os.getenv("APCA_API_KEY_ID")
    api_secret = os.getenv("APCA_API_SECRET_KEY")

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="Faltan las variables de entorno APCA_API_KEY_ID o APCA_API_SECRET_KEY",
        )

    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def get_config_status() -> Dict[str, Any]:
    """
    Lee /config/status desde tu propia API para saber execution_mode y risk_mode.
    Si falla, devuelve un dict vacío.
    """
    if not API_BASE:
        return {}

    try:
        resp = requests.get(f"{API_BASE}/config/status", timeout=5)
        data = resp.json()
        # Puede venir envuelto en {"data": {...}} o directo.
        return data.get("data", data)
    except Exception:
        return {}


def get_account_and_positions() -> (Dict[str, Any], List[Dict[str, Any]]):
    """
    Obtiene la cuenta y las posiciones actuales en Alpaca.
    """
    headers = get_alpaca_headers()

    # Cuenta
    acc_resp = requests.get(f"{TRADING_URL}/v2/account", headers=headers, timeout=5)
    if acc_resp.status_code != 200:
        raise HTTPException(
            status_code=acc_resp.status_code,
            detail=f"Error al leer cuenta: {acc_resp.text}",
        )
    account = acc_resp.json()

    # Posiciones
    pos_resp = requests.get(f"{TRADING_URL}/v2/positions", headers=headers, timeout=5)
    if pos_resp.status_code not in (200, 404):
        raise HTTPException(
            status_code=pos_resp.status_code,
            detail=f"Error al leer posiciones: {pos_resp.text}",
        )

    if pos_resp.status_code == 404:
        positions: List[Dict[str, Any]] = []
    else:
        positions = pos_resp.json()

    return account, positions


def get_risk_params(risk_mode: str) -> Dict[str, float]:
    """
    Define parámetros de riesgo por modo:
    - tp_per_trade: take profit por trade (en fracción, 0.20 = 20%)
    - sl_per_trade: stop loss por trade
    - daily_target: meta diaria (fracción de la equity)
    - daily_max_loss: pérdida máxima diaria (fracción de la equity)
    """
    risk_mode = (risk_mode or "low").lower()

    if risk_mode == "high":
        return {
            "tp_per_trade": 0.30,
            "sl_per_trade": 0.15,
            "daily_target": 0.05,
            "daily_max_loss": 0.02,
        }
    if risk_mode == "medium":
        return {
            "tp_per_trade": 0.20,
            "sl_per_trade": 0.10,
            "daily_target": 0.03,
            "daily_max_loss": 0.015,
        }
    # low por defecto
    return {
        "tp_per_trade": 0.15,
        "sl_per_trade": 0.08,
        "daily_target": 0.02,
        "daily_max_loss": 0.01,
    }


def is_after_close_time() -> bool:
    """
    Devuelve True si la hora actual (aprox ET) es >= 15:45.
    Aproximación: ET = UTC-5. Es suficiente para la lógica interna.
    """
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc + timedelta(hours=-5)
    return (now_et.hour > 15) or (now_et.hour == 15 and now_et.minute >= 45)


def close_all_via_api() -> Dict[str, Any]:
    """
    Llama a tu propio endpoint POST /alpaca/close-all.
    """
    if not API_BASE:
        raise HTTPException(
            status_code=500,
            detail="No está definido RENDER_EXTERNAL_URL para llamar /alpaca/close-all",
        )

    resp = requests.post(f"{API_BASE}/alpaca/close-all", timeout=10)
    if resp.status_code not in (200, 207):
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error en /alpaca/close-all: {resp.text}",
        )
    return resp.json()


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    """
    Llama a tu propio endpoint POST /alpaca/close/{symbol}.
    """
    if not API_BASE:
        raise HTTPException(
            status_code=500,
            detail="No está definido RENDER_EXTERNAL_URL para llamar /alpaca/close/{symbol}",
        )

    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", timeout=10)

    # Si Alpaca dice que no hay posición, devolvemos el JSON igualmente.
    try:
        return resp.json()
    except Exception:
        # Respuesta sin JSON
        if resp.status_code in (200, 204):
            return {"status": "ok", "symbol": symbol, "detail": "cerrado"}
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error en /alpaca/close/{symbol}: {resp.text}",
        )


@router.get("/tick")
def monitor_tick():
    """
    Monitoriza posiciones abiertas y aplica la estrategia de salidas automáticas (Opción A):

    - Respeta execution_mode de /config/status (solo actúa en modo 'auto')
    - Cierra TODO por:
        * meta diaria alcanzada
        * pérdida diaria máxima
        * hora límite (15:45 ET)
    - Cierra símbolos individuales por:
        * take profit por trade
        * stop loss por trade

    Devuelve un resumen de las acciones ejecutadas en este 'tick'.
    """
    # 1) Leer configuración
    config = get_config_status()
    exec_mode = str(config.get("execution_mode", "manual")).lower()
    risk_mode = str(config.get("risk_mode", "low")).lower()

    if exec_mode != "auto":
        return {
            "status": "skipped",
            "reason": f"execution_mode='{exec_mode}' (no es 'auto')",
            "config": {"execution_mode": exec_mode, "risk_mode": risk_mode},
        }

    # 2) Leer cuenta y posiciones
    account, positions = get_account_and_positions()
    equity = float(account.get("equity", 0.0))
    last_equity = float(account.get("last_equity", equity))
    pnl_today = equity - last_equity  # P&L aproximado del día

    params = get_risk_params(risk_mode)
    daily_target_abs = equity * params["daily_target"]
    daily_max_loss_abs = -equity * params["daily_max_loss"]

    actions: Dict[str, Any] = {
        "closed_all": False,
        "closed_symbols": [],
        "reason_all": None,
        "per_trade_params": {
            "tp_per_trade": params["tp_per_trade"],
            "sl_per_trade": params["sl_per_trade"],
        },
        "daily_params": {
            "target_pct": params["daily_target"],
            "max_loss_pct": params["daily_max_loss"],
            "pnl_today": pnl_today,
        },
    }

    # 3) Regla de hora límite (hard close)
    if positions and is_after_close_time():
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Hora límite 15:45 ET"
        actions["close_all_response"] = result
        return {
            "status": "ok",
            "mode": exec_mode,
            "risk_mode": risk_mode,
            "actions": actions,
        }

    # 4) Reglas de P&L diario
    if positions and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result
        return {
            "status": "ok",
            "mode": exec_mode,
            "risk_mode": risk_mode,
            "actions": actions,
        }

    if positions and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result
        return {
            "status": "ok",
            "mode": exec_mode,
            "risk_mode": risk_mode,
            "actions": actions,
        }

    # 5) Gestión por trade (take profit / stop loss)
    for pos in positions:
        symbol = pos.get("symbol")
        if not symbol:
            continue

        # Alpaca devuelve unrealized_plpc como fracción: 0.10 = +10%
        try:
            plpc = float(pos.get("unrealized_plpc", 0.0))
        except (TypeError, ValueError):
            plpc = 0.0

        if plpc >= params["tp_per_trade"]:
            resp = close_symbol_via_api(symbol)
            actions["closed_symbols"].append(
                {
                    "symbol": symbol,
                    "reason": f"Take profit alcanzado ({plpc:.2%})",
                    "api_response": resp,
                }
            )
        elif plpc <= -params["sl_per_trade"]:
            resp = close_symbol_via_api(symbol)
            actions["closed_symbols"].append(
                {
                    "symbol": symbol,
                    "reason": f"Stop loss alcanzado ({plpc:.2%})",
                    "api_response": resp,
                }
            )

    return {
        "status": "ok",
        "mode": exec_mode,
        "risk_mode": risk_mode,
        "positions_count": len(positions),
        "actions": actions,
    }
