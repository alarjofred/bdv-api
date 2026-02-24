import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, List, Tuple
from fastapi import APIRouter, Header, HTTPException

from .telegram_notify import send_alert

router = APIRouter(prefix="/agent", tags=["agent"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()

# OpenAI (análisis profundo opcional — NO ejecuta trades)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
OPENAI_ENABLED = os.getenv("OPENAI_ENABLED", "0").strip() in ("1", "true", "True", "yes", "YES")

# Símbolos y Telegram
AGENT_SYMBOLS = os.getenv("AGENT_SYMBOLS", "QQQ,SPY,NVDA")
AGENT_SEND_TELEGRAM = os.getenv("AGENT_SEND_TELEGRAM", "1").strip() not in ("0", "false", "False", "no", "NO")

# Semáforo y tolerancias
AGENT_STALE_GREEN_MAX_SEC = int(os.getenv("AGENT_STALE_GREEN_MAX_SEC", "120"))
AGENT_STALE_YELLOW_MAX_SEC = int(os.getenv("AGENT_STALE_YELLOW_MAX_SEC", "600"))
AGENT_ALLOW_YELLOW_SUMMARY = os.getenv("AGENT_ALLOW_YELLOW_SUMMARY", "1").strip() in ("1", "true", "True", "yes", "YES")

# =====================================================
# ✅ DOBLE UMBRAL (más operaciones pero filtradas)
# =====================================================
AGENT_DECISION_HARD_CONF = float(os.getenv("AGENT_DECISION_HARD_CONF", "0.75"))
AGENT_DECISION_SOFT_CONF = float(os.getenv("AGENT_DECISION_SOFT_CONF", "0.66"))
AGENT_DECISION_SOFT_TREND_MIN = int(os.getenv("AGENT_DECISION_SOFT_TREND_MIN", "3"))

# Market context (si snapshot/indicators existe)
AGENT_MARKET_CTX_ENABLED = os.getenv("AGENT_MARKET_CTX_ENABLED", "1").strip().lower() in ("1", "true", "yes", "y", "on")
AGENT_MARKET_CTX_TIMEFRAME = os.getenv("AGENT_MARKET_CTX_TIMEFRAME", "5Min").strip()
AGENT_MARKET_CTX_LIMIT = int(os.getenv("AGENT_MARKET_CTX_LIMIT", "200"))
AGENT_MARKET_CTX_LOOKBACK_HOURS = int(os.getenv("AGENT_MARKET_CTX_LOOKBACK_HOURS", "48"))
AGENT_MARKET_CTX_FEED = os.getenv("APCA_DATA_FEED", "").strip().lower()  # iex/sip opcional


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    if BDV_AGENT_SECRET:
        if (not x_bdv_secret) or (x_bdv_secret.strip() != BDV_AGENT_SECRET):
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _get_json(url: str, timeout: int = 8) -> Dict[str, Any]:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("data", data)


def _parse_snapshot_time_et(snapshot: Dict[str, Any]) -> Optional[datetime]:
    t = snapshot.get("time") or snapshot.get("timestamp")

    if not t and isinstance(snapshot, dict):
        for _, v in snapshot.items():
            if isinstance(v, dict) and (v.get("time") or v.get("timestamp")):
                t = v.get("time") or v.get("timestamp")
                break

    if not t:
        return None

    try:
        s = str(t).replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
        return dt_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None


def _is_rth(et_dt: datetime) -> bool:
    h, m = et_dt.hour, et_dt.minute
    after_open = (h > 9) or (h == 9 and m >= 30)
    before_close = (h < 16) or (h == 16 and m == 0)
    return after_open and before_close


def _call_openai_bdv(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_DISABLED: falta OPENAI_API_KEY"

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {"model": OPENAI_MODEL, "input": prompt}

    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    j = r.json()

    if isinstance(j, dict) and j.get("output_text"):
        return str(j["output_text"]).strip()

    out = j.get("output", [])
    chunks: List[str] = []
    for item in out if isinstance(out, list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and "text" in c:
                chunks.append(str(c["text"]))
    return "\n".join(chunks).strip() or "OPENAI_OK_BUT_EMPTY"


def _send_signal_telegram(symbols: List[str], title: str, note: str):
    if not AGENT_SEND_TELEGRAM:
        return
    send_alert(
        "signal",
        {
            "symbol": ",".join(symbols) if symbols else "BDV",
            "bias": "neutral",
            "suggestion": title,
            "target": "",
            "stop": "",
            "note": (note or "")[:3500],
        },
    )


def _get_market_context(symbols: List[str]) -> Dict[str, Any]:
    if not API_BASE or not symbols or not AGENT_MARKET_CTX_ENABLED:
        return {}

    params: Dict[str, Any] = {
        "symbols": ",".join([s.strip().upper() for s in symbols if s.strip()]),
        "timeframe": AGENT_MARKET_CTX_TIMEFRAME,
        "limit": str(AGENT_MARKET_CTX_LIMIT),
        "lookback_hours": str(AGENT_MARKET_CTX_LOOKBACK_HOURS),
    }
    if AGENT_MARKET_CTX_FEED in ("iex", "sip"):
        params["feed"] = AGENT_MARKET_CTX_FEED

    try:
        r = requests.get(f"{API_BASE}/snapshot/indicators", params=params, timeout=12)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
        j = r.json()
        return j.get("data", j) if isinstance(j, dict) else {}
    except Exception as e:
        return {"status": "error", "detail": str(e), "params": params}


def _get_signals_ai(symbol: str, bias: str, trend_strength: int) -> Dict[str, Any]:
    if not API_BASE:
        return {}
    params = {
        "symbol": str(symbol).strip().upper(),
        "bias": (bias or "neutral").strip().lower(),
        "trend_strength": int(trend_strength),
        "near_extreme": "false",
        "prefer_spreads": "true",
    }
    try:
        r = requests.get(f"{API_BASE}/signals/ai", params=params, timeout=12)
        if r.status_code != 200:
            return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
        j = r.json()
        if isinstance(j, dict):
            j.setdefault("params", params)
        return j if isinstance(j, dict) else {"status": "error", "detail": "signals_ai_non_dict", "params": params}
    except Exception as e:
        return {"status": "error", "detail": str(e), "params": params}


def _double_threshold_allows(conf: float, ts: int) -> Tuple[bool, str]:
    # Regla 1: muy segura
    if conf >= AGENT_DECISION_HARD_CONF:
        return True, f"hard_ok conf>={AGENT_DECISION_HARD_CONF}"
    # Regla 2: medio segura pero mercado fuerte
    if conf >= AGENT_DECISION_SOFT_CONF and ts >= AGENT_DECISION_SOFT_TREND_MIN:
        return True, f"soft_ok conf>={AGENT_DECISION_SOFT_CONF} AND ts>={AGENT_DECISION_SOFT_TREND_MIN}"
    # Regla 3: no
    return False, f"blocked conf<{AGENT_DECISION_SOFT_CONF} OR (conf<{AGENT_DECISION_HARD_CONF} AND ts<{AGENT_DECISION_SOFT_TREND_MIN})"


@router.get("/scan")
def agent_scan(
    x_bdv_secret: Optional[str] = Header(default=None),
    force_analysis: int = 0,
):
    _require_agent_secret(x_bdv_secret)

    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido")

    symbols = [s.strip().upper() for s in AGENT_SYMBOLS.split(",") if s.strip()]

    # 1) config/status
    try:
        cfg = _get_json(f"{API_BASE}/config/status", timeout=8)
    except Exception as e:
        _send_signal_telegram(symbols, "RED: API ERROR", f"/config/status falló: {e}")
        return {"status": "red", "reason": "error API /config/status", "error": str(e)}

    exec_mode = str(cfg.get("execution_mode", "manual")).lower()
    risk_mode = str(cfg.get("risk_mode", "low")).lower()
    max_trades = int(cfg.get("max_trades_per_day", 0) or 0)
    trades_today = int(cfg.get("trades_today", 0) or 0)

    if max_trades and trades_today >= max_trades:
        note = f"NO TRADE: límite diario alcanzado {trades_today}/{max_trades}. exec_mode={exec_mode} risk_mode={risk_mode}"
        _send_signal_telegram(symbols, "NO TRADE", note)
        return {"status": "yellow", "reason": "límite diario alcanzado", "config": cfg}

    # 2) snapshot
    try:
        snap = _get_json(f"{API_BASE}/snapshot", timeout=8)
    except Exception as e:
        _send_signal_telegram(symbols, "RED: API ERROR", f"/snapshot falló: {e}")
        return {"status": "red", "reason": "error API /snapshot", "error": str(e), "config": cfg}

    snap_time_et = _parse_snapshot_time_et(snap if isinstance(snap, dict) else {})
    if not snap_time_et:
        _send_signal_telegram(symbols, "RED: BAD DATA", "snapshot.time no existe o timestamp inválido")
        return {"status": "red", "reason": "sin snapshot.time / timestamp inválido", "config": cfg, "snapshot": snap}

    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    age_sec = int((now_et - snap_time_et).total_seconds())
    if age_sec < 0:
        age_sec = 0
    in_rth = _is_rth(snap_time_et)

    if in_rth and age_sec <= AGENT_STALE_GREEN_MAX_SEC:
        light = "green"
    elif age_sec <= AGENT_STALE_YELLOW_MAX_SEC:
        light = "yellow"
    else:
        light = "red"

    base_ctx = (
        f"SEMAFORO={light.upper()} | exec_mode={exec_mode} risk_mode={risk_mode} "
        f"| trades_today={trades_today}/{max_trades} | snapshot={snap_time_et.strftime('%H:%M:%S')} ET | age={age_sec}s | in_rth={in_rth}"
    )

    if light == "red":
        _send_signal_telegram(symbols, "RED: NO DATA", base_ctx)
        return {
            "status": "red",
            "reason": "snapshot demasiado viejo o inválido",
            "config": cfg,
            "snapshot_time_et": snap_time_et.isoformat(),
            "age_sec": age_sec,
            "in_rth": in_rth,
            "note": base_ctx,
        }

    if light == "yellow" and AGENT_ALLOW_YELLOW_SUMMARY and not force_analysis:
        _send_signal_telegram(symbols, "YELLOW: RESUMEN", base_ctx)
        return {
            "status": "yellow",
            "reason": "data OK pero no operable (stale o fuera de RTH)",
            "config": cfg,
            "snapshot_time_et": snap_time_et.isoformat(),
            "age_sec": age_sec,
            "in_rth": in_rth,
            "note": base_ctx,
        }

    # =====================================================
    # ✅ NUEVO: Decision auditable usando market_ctx + signals/ai
    # =====================================================
    market_ctx = _get_market_context(symbols)
    decisions: List[Dict[str, Any]] = []

    for sym in symbols:
        ctx = market_ctx.get(sym, {}) if isinstance(market_ctx, dict) else {}
        bias = str(ctx.get("bias_inferred", "neutral") or "neutral").strip().lower()
        if bias not in ("bullish", "bearish", "neutral"):
            bias = "neutral"
        try:
            ts = int(ctx.get("trend_strength", 1) or 1)
        except Exception:
            ts = 1

        ai_payload = _get_signals_ai(sym, bias=bias, trend_strength=ts)
        data = ai_payload.get("data", ai_payload) if isinstance(ai_payload, dict) else {}
        action = str((data.get("action") or data.get("side") or data.get("suggestion") or "")).strip().lower()

        try:
            conf = float(data.get("confidence", 0) or 0)
        except Exception:
            conf = 0.0

        allow = False
        reason = "no_signal"
        if action in ("buy", "sell"):
            allow, reason = _double_threshold_allows(conf, ts)

        decisions.append({
            "symbol": sym,
            "bias": bias,
            "trend_strength": ts,
            "ai_action": action,
            "ai_conf": conf,
            "allow_trade": allow,
            "allow_reason": reason,
            "ai_payload_status": ai_payload.get("status") if isinstance(ai_payload, dict) else None,
        })

    thresholds = {
        "hard_conf": AGENT_DECISION_HARD_CONF,
        "soft_conf": AGENT_DECISION_SOFT_CONF,
        "soft_trend_min": AGENT_DECISION_SOFT_TREND_MIN,
    }

    # OpenAI opcional (texto) para Telegram
    wants_openai = bool(OPENAI_API_KEY) and (OPENAI_ENABLED or force_analysis == 1)
    analysis_text = None
    if wants_openai:
        prompt = (
            "Eres BDV OPCIONES LIVE. Analiza SIN operar.\n"
            "Reglas duras:\n"
            "- No inventes datos.\n"
            "- Sin option chain: NO strikes exactos.\n"
            "- Usa SOLO estos datos provistos.\n"
            "- Si no hay ventaja -> NO TRADE.\n\n"
            f"CONFIG: {cfg}\n"
            f"SNAPSHOT_DATA: {snap}\n"
            f"MARKET_CTX: {market_ctx}\n"
            f"SIGNALS_AI_DECISIONS: {decisions}\n"
            f"THRESHOLDS: {thresholds}\n\n"
            "Entrega en bullets:\n"
            "1) Estado del sistema (OK/ERROR)\n"
            "2) Estado operable (SI/NO) + motivo\n"
            "3) Sesgo por símbolo (bullish/bearish/neutral)\n"
            "4) Condición exacta de entrada (SI y SOLO SI)\n"
            "5) Invalidación\n"
            "6) Estrategia (CALL/PUT/NO TRADE) por símbolo\n"
            f"7) Hora snapshot ET = {snap_time_et.strftime('%H:%M:%S')} ET\n"
        )
        analysis_text = _call_openai_bdv(prompt)

    # Telegram resumen
    trade_candidates = [d for d in decisions if d.get("allow_trade")]
    note = base_ctx + f"\n\nTHRESHOLDS: {thresholds}\nCANDIDATES: {trade_candidates[:3]}"
    if analysis_text:
        note += "\n\n" + analysis_text
        _send_signal_telegram(symbols, "ANÁLISIS BDV + DECISIÓN", note)
    else:
        _send_signal_telegram(symbols, "DECISIÓN BDV", note)

    return {
        "status": "ok",
        "light": light,
        "config": cfg,
        "snapshot_time_et": snap_time_et.isoformat(),
        "age_sec": age_sec,
        "in_rth": in_rth,
        "note": base_ctx,
        "thresholds": thresholds,
        "market_ctx": market_ctx,
        "decisions": decisions,
        "analysis": analysis_text,
    }
