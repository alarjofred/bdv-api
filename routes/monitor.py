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


AUTO_ENTRY_COOLDOWN_SEC = _env_int("AUTO_ENTRY_COOLDOWN_SEC", 1800)  # 30 min
AI_TREND_MIN = _env_int("AI_TREND_MIN", 2)  # fallback si market_ctx falla
AI_MIN_CONFIDENCE = _env_float("AI_MIN_CONFIDENCE", 0.75)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false")
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


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
def _get_market_context() -> Dict[str, Any]:
    """
    Llama a /snapshot/indicators para obtener bias_inferred + trend_strength por símbolo.
    """
    if not API_BASE:
        return {}
    try:
        r = requests.get(f"{API_BASE}/snapshot/indicators", headers=_api_headers(), timeout=12)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text}
        data = _safe_json(r)
        return data if isinstance(data, dict) else {"status": "error", "detail": "market_ctx_non_dict"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_signals_ai(symbol: str, bias: str, trend_strength: int) -> Dict[str, Any]:
    """
    Llama a /signals/ai usando bias+trend_strength inferidos del mercado (live).
    Si market_ctx falla, caller puede pasar fallback neutral/1.
    """
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
    """
    ✅ kind="none" NO debe marcarse como opciones
    """
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
        "params_used": data.get("params_echo") or ai_payload.get("params"),
    }


def _cooldown_key(symbol: str, side: str) -> str:
    return f"{str(symbol).strip().upper()}|{str(side).strip().lower()}"


def _cooldown_state(symbol: str, side: str) -> Dict[str, Any]:
    """
    Cooldown por símbolo+side (per_symbol_and_side).
    """
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
        },
        "config_echo": {"execution_mode": exec_mode, "risk_mode": risk_mode},
    }

    if exec_mode != "auto":
        actions["auto_entry"] = {
            "status": "skipped",
            "reason": "execution_mode_not_auto",
            "detail": f"execution_mode='{exec_mode}' (no es 'auto')",
        }
        return _with_build_id({"status": "skipped", "reason": actions["auto_entry"]["detail"], "actions": actions})

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
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist_close_logic"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    if positions and pnl_today >= daily_target_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Meta diaria alcanzada"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist_daily_target"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    if positions and pnl_today <= daily_max_loss_abs:
        result = close_all_via_api()
        actions["closed_all"] = True
        actions["reason_all"] = "Pérdida diaria máxima alcanzada"
        actions["close_all_response"] = result
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist_daily_max_loss"}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    # TP/SL por posición
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

    # AUTO ENTRY: SOLO si NO hay posiciones
    if len(positions) != 0:
        actions["auto_entry"] = {"status": "skipped", "reason": "positions_exist", "positions_count": len(positions)}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": len(positions), "actions": actions})

    if trades_today >= max_trades_per_day:
        actions["auto_entry"] = {"status": "skipped", "reason": "max_trades_per_day_reached", "limits": actions["limits"]}
        return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})

    # ✅ PASO 2: leer contexto live (bias + trend_strength)
    market_ctx = _get_market_context()
    market_data = {}
    if isinstance(market_ctx, dict):
        market_data = market_ctx.get("data", {}) if isinstance(market_ctx.get("data"), dict) else {}
    actions["market_ctx"] = market_data if market_data else market_ctx  # para auditoría

    # Intento con /signals/ai
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

        # fallback suave si market_ctx no trajo datos
        if not ctx:
            ts = AI_TREND_MIN if AI_TREND_MIN else 1

        ai_payload = _get_signals_ai(sym, bias=bias, trend_strength=ts)
        ai_checked_summary.append({"symbol": sym, "market_ctx": ctx, "summary": _summarize_ai(ai_payload)})

        pick = _pick_trade_from_signals_ai(ai_payload, min_conf=AI_MIN_CONFIDENCE)

        if isinstance(pick, dict) and pick.get("status") == "not_supported":
            continue

        if isinstance(pick, dict) and pick.get("symbol") and pick.get("side") and pick.get("source") == "signals_ai":
            # añade trazabilidad del contexto usado
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
        out = _execute_trade_via_http(ai_pick["symbol"], ai_pick["side"], qty)
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
    out = _execute_trade_via_http(pick["symbol"], pick["side"], qty)
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
    }

    return _with_build_id({"status": "ok", "mode": exec_mode, "risk_mode": risk_mode, "positions_count": 0, "actions": actions})
