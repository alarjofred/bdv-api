# routes/monitor.py

from fastapi import APIRouter, HTTPException, Header
import os
import requests
from datetime import datetime, timedelta
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

# ✅ Anti-duplicados / cooldown simple en memoria (por proceso Render)
# Nota: si Render reinicia, se borra. Aun así ayuda contra spam.
_LAST_ENTRY: Dict[str, Any] = {
    "ts": None,       # datetime UTC
    "symbol": None,   # "SPY"
    "side": None,     # "buy"/"sell"
}
AUTO_ENTRY_COOLDOWN_SEC = int(os.getenv("AUTO_ENTRY_COOLDOWN_SEC", "900"))  # 15 min default


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    """
    Protege endpoints del agente/monitor contra llamadas externas no autorizadas.
    Si BDV_AGENT_SECRET está definido, exige header X-BDV-SECRET.
    """
    if BDV_AGENT_SECRET:
        if not x_bdv_secret or x_bdv_secret.strip() != BDV_AGENT_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _api_headers() -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if BDV_AGENT_SECRET:
        h["X-BDV-SECRET"] = BDV_AGENT_SECRET
    return h


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
        resp = requests.get(f"{API_BASE}/config/status", headers=_api_headers(), timeout=5)
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


def _is_inside_rth() -> Tuple[bool, str]:
    """
    RTH: 09:30 - 16:00 NY
    """
    now_ny = datetime.now(tz=ZoneInfo("America/New_York"))
    dow = int(now_ny.strftime("%u"))   # 1..7
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
    return resp.json()


def close_symbol_via_api(symbol: str) -> Dict[str, Any]:
    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido para /alpaca/close/{symbol}")

    symbol = str(symbol).strip().upper()
    resp = requests.post(f"{API_BASE}/alpaca/close/{symbol}", headers=_api_headers(), timeout=10)
    try:
        return resp.json()
    except Exception:
        if resp.status_code in (200, 204):
            return {"status": "ok", "symbol": symbol, "detail": "cerrado"}
        raise HTTPException(status_code=resp.status_code, detail=f"Error /alpaca/close/{symbol}: {resp.text}")


def _execute_trade_via_http(symbol: str, side: str, qty: int) -> Dict[str, Any]:
    """
    Ejecuta /trade (solo acciones hoy). Retorna respuesta o info de error (sin romper tick).
    """
    if not API_BASE:
        return {"status": "error", "detail": "API_BASE missing"}

    symbol = str(symbol).strip().upper()
    side = str(side).lower().strip()
    qty = int(qty)

    url = f"{API_BASE.rstrip('/')}/trade"
    payload = {"symbol": symbol, "side": side, "qty": qty}

    try:
        r = requests.post(url, headers=_api_headers(), json=payload, timeout=10)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "payload": payload}
        try:
            return {"status": "ok", "result": r.json(), "payload": payload}
        except Exception:
            return {"status": "ok", "result": r.text, "payload": payload}
    except Exception as e:
        return {"status": "error", "detail": str(e), "payload": payload}


def _get_snapshot_prices() -> Dict[str, Dict[str, Any]]:
    if not API_BASE:
        return {}
    try:
        resp = requests.get(f"{API_BASE}/snapshot", headers=_api_headers(), timeout=5)
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

        if allow_execute:
            out = _execute_trade_via_http(trade.symbol, trade.side, trade.qty)
            trade.status = "triggered"
            trade.triggered_at = now
            ejecuciones.append(
                {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "qty": trade.qty,
                    "trigger_price": trade.trigger_price,
                    "max_price": trade.max_price,
                    "price_at_trigger": price,
                    "status": "triggered",
                    "trade_result": out,
                }
            )
        else:
            ejecuciones.append(
                {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "qty": trade.qty,
                    "trigger_price": trade.trigger_price,
                    "max_price": trade.max_price,
                    "price_at_trigger": price,
                    "status": "trigger_detected",
                }
            )

    return ejecuciones


def _get_recommendation() -> Dict[str, Any]:
    """
    Llama /recommend y devuelve el JSON (o {}).
    """
    if not API_BASE:
        return {}
    try:
        r = requests.get(f"{API_BASE}/recommend", headers=_api_headers(), timeout=10)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text}
        return r.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _pick_trade_from_recommend(rec_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Busca la primera recomendación BUY/SELL y la convierte a {symbol, side, source}.
    """
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


def _get_signals_ai() -> Dict[str, Any]:
    """
    Llama /signals/ai y devuelve el JSON.
    """
    if not API_BASE:
        return {}
    try:
        r = requests.get(f"{API_BASE}/signals/ai", headers=_api_headers(), timeout=10)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text}
        return r.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _pick_trade_from_signals_ai(ai_payload: Dict[str, Any], min_conf: float = 0.6) -> Optional[Dict[str, Any]]:
    """
    Solo devuelve pick si AI es EJECUTABLE.
    Criterio ejecutable:
      - structure.kind != "none"
      - legs no vacío
      - confidence >= min_conf
      - y que exista symbol + side sugerido para ACCIONES (buy/sell)
    Nota: Si AI describe opciones, aquí SOLO lo registramos (no ejecutamos opciones).
    """
    if not isinstance(ai_payload, dict):
        return None

    # soporte por si viene envuelto
    data = ai_payload.get("data", ai_payload)

    # Señales típicas auditadas antes
    structure = data.get("structure", {}) if isinstance(data.get("structure"), dict) else {}
    kind = str(structure.get("kind", "")).lower().strip()
    legs = structure.get("legs", []) if isinstance(structure.get("legs"), list) else data.get("legs", [])

    try:
        conf = float(data.get("confidence", 0) or 0)
    except Exception:
        conf = 0.0

    # Si no es ejecutable → None (y dejamos que monitor haga fallback)
    if kind in ("", "none") or not legs or conf < min_conf:
        return None

    # Intento de extraer una instrucción simple stock (si existiera)
    # Buscamos campos comunes: symbol + side/suggestion/action
    sym = data.get("symbol") or data.get("ticker")
    side = data.get("side") or data.get("suggestion") or data.get("action")

    if sym and side:
        side = str(side).lower().strip()
        if side in ("buy", "sell"):
            return {"symbol": str(sym).strip().upper(), "side": side, "source": "signals_ai", "confidence": conf, "kind": kind}

    # Si AI es ejecutable pero NO trae instrucción stock simple, no ejecutamos (probablemente es options)
    return {"status": "not_supported", "source": "signals_ai", "confidence": conf, "kind": kind, "reason": "ai_executable_but_not_stock_order"}


def _cooldown_allows(symbol: str, side: str) -> Tuple[bool, str]:
    """
    Evita repetir órdenes iguales cada 5 min.
    """
    now = datetime.utcnow()
    last_ts = _LAST_ENTRY.get("ts")
    last_sym = _LAST_ENTRY.get("symbol")
    last_side = _LAST_ENTRY.get("side")

    symbol = str(symbol).strip().upper()
    side = str(side).lower().strip()

    if not last_ts:
        return True, "no_last_entry"

    try:
        elapsed = (now - last_ts).total_seconds()
    except Exception:
        return True, "bad_last_ts"

    if last_sym == symbol and last_side == side and elapsed < AUTO_ENTRY_COOLDOWN_SEC:
        return False, f"cooldown_active {int(elapsed)}s<{AUTO_ENTRY_COOLDOWN_SEC}s"

    return True, f"cooldown_ok elapsed={int(elapsed)}s"


def _set_last_entry(symbol: str, side: str) -> None:
    _LAST_ENTRY["ts"] = datetime.utcnow()
    _LAST_ENTRY["symbol"] = str(symbol).strip().upper()
    _LAST_ENTRY["side"] = str(side).lower().strip()


@router.get("/tick")
def monitor_tick(x_bdv_secret: Optional[str] = Header(default=None)):
    """
    EJECUCIÓN / GESTIÓN (solo en auto):
    - close por hora / P&L / tp/sl
    - pending trades ejecuta /trade SOLO si auto
    - AUTO ENTRY: prioriza /signals/ai si es ejecutable; si no, fallback /recommend
    Protegido por X-BDV-SECRET si BDV_AGENT_SECRET está definido.
    """
    _require_agent_secret(x_bdv_secret)

    inside, rth_reason = _is_inside_rth()
    if not inside:
        return {"status": "skipped", "reason": rth_reason}

    config = get_config_status()
    exec_mode = str(config.get("execution_mode", "manual")).lower()
    risk_mode = str(config.get("risk_mode", "low")).lower()
    max_trades_per_day = int(config.get("max_trades_per_day", 1) or 1)
    trades_today = int(config.get("trades_today", 0) or 0)

    if exec_mode != "auto":
        return {
            "status": "skipped",
            "reason": f"execution_mode='{exec_mode}' (no es 'auto')",
            "config": {
                "execution_mode": exec_mode,
                "risk_mode": risk_mode,
                "max_trades_per_day": max_trades_per_day,
                "trades_today": trades_today,
            },
        }

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
        # ✅ nunca dejarlo null: default "skipped"
        "auto_entry": {"status": "skipped", "reason": "not_evaluated"},
        "limits": {"max_trades_per_day": max_trades_per_day, "trades_today": trades_today},
    }

    # 2) Cierres por hora / P&L
    if positions and is_after_close_time():
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Hora límite 15:45 NY"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist_close_logic"}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions}

    if positions and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist_daily_target"}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions}

    if positions and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist_daily_max_loss"}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions}

    # 3) TP/SL por posición
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

    # 4) Pending trades SOLO en auto ejecutan
    snapshot_data = _get_snapshot_prices()
    try:
        actions["pending_trades_executed"] = _process_pending_trades(snapshot_data, allow_execute=True)
    except Exception:
        actions["pending_trades_executed"] = []

    # 5) ✅ AUTO ENTRY: SOLO si NO hay posiciones
    if len(positions) != 0:
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist", "positions_count": len(positions)}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions}

    if trades_today >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_trades_per_day_reached"}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions}

    # 5.A) Intento con /signals/ai (prioridad 1)
    ai = _get_signals_ai()
    ai_pick = _pick_trade_from_signals_ai(ai, min_conf=float(os.getenv("AI_MIN_CONFIDENCE", "0.6")))

    # Si AI devuelve dict con status not_supported, lo registramos y fallback
    if isinstance(ai_pick, dict) and ai_pick.get("status") == "not_supported":
        actions["auto_entry"] = {"status": "skipped", "reason": "ai_not_supported_for_stock", "signals_ai": ai, "ai_pick": ai_pick}
    elif isinstance(ai_pick, dict) and ai_pick.get("symbol") and ai_pick.get("side") and ai_pick.get("source") == "signals_ai":
        # Dedupe/cooldown
        ok_cd, cd_reason = _cooldown_allows(ai_pick["symbol"], ai_pick["side"])
        if not ok_cd:
            actions["auto_entry"] = {"status": "skipped", "reason": cd_reason, "picked": ai_pick, "source": "signals_ai", "signals_ai": ai}
            return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions}

        qty = 1
        out = _execute_trade_via_http(ai_pick["symbol"], ai_pick["side"], qty)
        if out.get("status") == "ok":
            _set_last_entry(ai_pick["symbol"], ai_pick["side"])
        actions["auto_entry"] = {
            "status": "attempted",
            "source": "signals_ai",
            "picked": ai_pick,
            "qty": qty,
            "trade_result": out,
            "signals_ai": ai,
        }
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions}

    # 5.B) Fallback a /recommend (prioridad 2)
    rec = _get_recommendation()
    pick = _pick_trade_from_recommend(rec if isinstance(rec, dict) else {})

    if not pick:
        actions["auto_entry"] = {"status": "skipped", "reason": "no_trade_signal", "recommend": rec, "signals_ai": ai}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions}

    ok_cd, cd_reason = _cooldown_allows(pick["symbol"], pick["side"])
    if not ok_cd:
        actions["auto_entry"] = {"status": "skipped", "reason": cd_reason, "picked": pick, "source": "recommend", "recommend": rec, "signals_ai": ai}
        return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions}

    qty = 1
    out = _execute_trade_via_http(pick["symbol"], pick["side"], qty)
    if out.get("status") == "ok":
        _set_last_entry(pick["symbol"], pick["side"])

    actions["auto_entry"] = {
        "status": "attempted",
        "source": "recommend",
        "picked": pick,
        "qty": qty,
        "trade_result": out,
        "recommend": rec,
        "signals_ai": ai,
    }

    return {"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions}
