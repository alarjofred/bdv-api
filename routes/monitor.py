import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, Header, HTTPException, Query

from .telegram_notify import send_alert

router = APIRouter(prefix="/agent", tags=["agent"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()

# OpenAI (panel experto)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
OPENAI_ENABLED = os.getenv("OPENAI_ENABLED", "0").strip().lower() in ("1", "true", "yes", "y", "on")

# Símbolos y Telegram
AGENT_SYMBOLS = os.getenv("AGENT_SYMBOLS", "QQQ,SPY,NVDA")
AGENT_SEND_TELEGRAM = os.getenv("AGENT_SEND_TELEGRAM", "1").strip().lower() not in ("0", "false", "no")

# ✅ Orquestación (encendida por defecto)
AGENT_DECISION_ENABLED = os.getenv("AGENT_DECISION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "y", "on")
AGENT_DECISION_TTL_SEC = int(os.getenv("AGENT_DECISION_TTL_SEC", "120"))

# ✅ Regla por tiers (la que pediste)
AGENT_TIER1_CONF = float(os.getenv("AGENT_TIER1_CONF", "0.75"))
AGENT_TIER2_MIN_CONF = float(os.getenv("AGENT_TIER2_MIN_CONF", "0.66"))
AGENT_TIER2_MIN_TREND = int(os.getenv("AGENT_TIER2_MIN_TREND", "3"))

# Semáforo (opcional para scan)
AGENT_STALE_GREEN_MAX_SEC = int(os.getenv("AGENT_STALE_GREEN_MAX_SEC", "120"))
AGENT_STALE_YELLOW_MAX_SEC = int(os.getenv("AGENT_STALE_YELLOW_MAX_SEC", "600"))
AGENT_ALLOW_YELLOW_SUMMARY = os.getenv("AGENT_ALLOW_YELLOW_SUMMARY", "1").strip().lower() in ("1", "true", "yes", "y", "on")


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    if BDV_AGENT_SECRET:
        if (not x_bdv_secret) or (x_bdv_secret.strip() != BDV_AGENT_SECRET):
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _api_headers() -> Dict[str, str]:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if BDV_AGENT_SECRET:
        h["X-BDV-SECRET"] = BDV_AGENT_SECRET
    return h


def _get_json(url: str, timeout: int = 10) -> Dict[str, Any]:
    r = requests.get(url, headers=_api_headers(), timeout=timeout)
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


def _call_openai(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""

    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {"model": OPENAI_MODEL, "input": prompt}

    r = requests.post(url, headers=headers, json=body, timeout=35)
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
    return "\n".join(chunks).strip()


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    try:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    return None


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


def _get_signals_ai(symbol: str, bias: str, trend_strength: int) -> Dict[str, Any]:
    params = {
        "symbol": symbol,
        "bias": bias,
        "trend_strength": int(trend_strength),
        "near_extreme": "false",
        "prefer_spreads": "true",
    }
    r = requests.get(f"{API_BASE}/signals/ai", headers=_api_headers(), params=params, timeout=12)
    if r.status_code != 200:
        return {"status": "error", "http": r.status_code, "body": r.text, "params": params}
    data = r.json()
    return data if isinstance(data, dict) else {"status": "error", "detail": "signals_ai_non_dict", "params": params}


def _summarize_candidate(symbol: str, ctx: Dict[str, Any], ai_payload: Dict[str, Any]) -> Dict[str, Any]:
    data = ai_payload.get("data", ai_payload) if isinstance(ai_payload, dict) else {}
    action = str((data.get("action") or "")).strip().lower()
    try:
        conf = float(data.get("confidence", 0) or 0)
    except Exception:
        conf = 0.0
    try:
        ts = int(ctx.get("trend_strength", 2) or 2)
    except Exception:
        ts = 2

    return {
        "symbol": symbol,
        "bias": str(ctx.get("bias_inferred", "neutral")),
        "trend_strength": ts,
        "action": action,
        "confidence": conf,
    }


def _passes_tier_rule(conf: float, trend_strength: int) -> bool:
    if conf >= AGENT_TIER1_CONF:
        return True
    if conf >= AGENT_TIER2_MIN_CONF and trend_strength >= AGENT_TIER2_MIN_TREND:
        return True
    return False


@router.get("/decision")
def agent_decision(
    x_bdv_secret: Optional[str] = Header(default=None),
):
    """
    ✅ Fuente de verdad para ejecución:
    - /monitor/tick llama aquí
    - /agent/scan reporta lo mismo
    """
    _require_agent_secret(x_bdv_secret)

    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido")

    if not AGENT_DECISION_ENABLED:
        return {"status": "ok", "decision": "no_trade", "why": "AGENT_DECISION_ENABLED=false"}

    # 1) config + snapshot (para contexto y auditoría)
    cfg = _get_json(f"{API_BASE}/config/status", timeout=8)
    snap = _get_json(f"{API_BASE}/snapshot", timeout=8)
    snap_time_et = _parse_snapshot_time_et(snap if isinstance(snap, dict) else {})

    symbols = [s.strip().upper() for s in AGENT_SYMBOLS.split(",") if s.strip()]
    if not symbols:
        symbols = ["QQQ"]

    # 2) market_ctx (si existe /snapshot/indicators)
    market_ctx = {}
    try:
        r = requests.get(
            f"{API_BASE}/snapshot/indicators",
            headers=_api_headers(),
            params={"symbols": ",".join(symbols), "timeframe": "5Min", "limit": "200", "lookback_hours": "48"},
            timeout=15,
        )
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict) and isinstance(j.get("data"), dict):
                market_ctx = j["data"]
    except Exception:
        market_ctx = {}

    # 3) candidatos /signals/ai
    candidates: List[Dict[str, Any]] = []
    for sym in symbols:
        ctx = market_ctx.get(sym, {}) if isinstance(market_ctx, dict) else {}
        bias = str(ctx.get("bias_inferred", "neutral")).strip().lower()
        if bias not in ("bullish", "bearish", "neutral"):
            bias = "neutral"
        try:
            ts = int(ctx.get("trend_strength", 2) or 2)
        except Exception:
            ts = 2

        ai_payload = _get_signals_ai(sym, bias=bias, trend_strength=ts)
        candidates.append(_summarize_candidate(sym, ctx, ai_payload))

    # elige mejor por confidence
    best = None
    for c in candidates:
        if c.get("action") not in ("buy", "sell"):
            continue
        if best is None or float(c.get("confidence", 0) or 0) > float(best.get("confidence", 0) or 0):
            best = c

    if not best:
        return {
            "status": "ok",
            "decision": "no_trade",
            "why": "no_buy_sell_from_signals_ai",
            "rule": {"tier1_conf": AGENT_TIER1_CONF, "tier2_min_conf": AGENT_TIER2_MIN_CONF, "tier2_min_trend": AGENT_TIER2_MIN_TREND},
            "candidates": candidates,
            "snapshot_time_et": snap_time_et.isoformat() if snap_time_et else None,
        }

    conf = float(best.get("confidence", 0) or 0)
    ts = int(best.get("trend_strength", 2) or 2)
    allow_trade = _passes_tier_rule(conf, ts)

    decision_obj = {
        "decision": "trade" if allow_trade else "no_trade",
        "symbol": best["symbol"],
        "side": best["action"],
        "confidence": conf,
        "trend_strength": ts,
        "why": "signals_ai_best_candidate",
    }

    # 4) OpenAI puede confirmar/cancelar, PERO se aplica la misma regla dura al final
    if OPENAI_ENABLED and OPENAI_API_KEY:
        prompt = (
            "Eres BDV OPCIONES LIVE. Debes RESPONDER SOLO JSON válido.\n"
            "No inventes datos. Puedes SOLO elegir 1 candidato o NO_TRADE.\n\n"
            f"RULE:\n"
            f"- if confidence >= {AGENT_TIER1_CONF}: TRADE\n"
            f"- if {AGENT_TIER2_MIN_CONF} <= confidence < {AGENT_TIER1_CONF}: TRADE only if trend_strength >= {AGENT_TIER2_MIN_TREND}\n"
            f"- else: NO_TRADE\n\n"
            f"CONFIG={cfg}\n"
            f"SNAPSHOT={snap}\n"
            f"CANDIDATES={candidates}\n\n"
            "Devuelve exactamente:\n"
            "{\n"
            '  "decision": "trade"|"no_trade",\n'
            '  "symbol": "QQQ",\n'
            '  "side": "buy"|"sell",\n'
            '  "confidence": 0.0,\n'
            '  "why": "string"\n'
            "}\n"
        )

        try:
            out = _call_openai(prompt)
            parsed = _try_parse_json(out)
            if parsed and str(parsed.get("decision", "")).lower() in ("trade", "no_trade"):
                dec = str(parsed.get("decision")).lower()
                sym = str(parsed.get("symbol", best["symbol"])).strip().upper()
                side = str(parsed.get("side", best["action"])).strip().lower()
                try:
                    conf2 = float(parsed.get("confidence", conf) or 0)
                except Exception:
                    conf2 = conf

                if side not in ("buy", "sell"):
                    side = best["action"]
                if sym not in symbols:
                    sym = best["symbol"]

                # buscar trend_strength del símbolo elegido
                ts2 = ts
                for c in candidates:
                    if str(c.get("symbol", "")).upper() == sym:
                        try:
                            ts2 = int(c.get("trend_strength", ts) or ts)
                        except Exception:
                            ts2 = ts
                        break

                allow2 = _passes_tier_rule(conf2, ts2)

                decision_obj = {
                    "decision": "trade" if (dec == "trade" and allow2) else "no_trade",
                    "symbol": sym,
                    "side": side,
                    "confidence": conf2,
                    "trend_strength": ts2,
                    "why": str(parsed.get("why", "openai_decision")).strip()[:200],
                }
        except Exception:
            pass

    return {
        "status": "ok",
        "decision": decision_obj["decision"],
        "symbol": decision_obj["symbol"],
        "side": decision_obj["side"],
        "confidence": decision_obj["confidence"],
        "trend_strength": decision_obj["trend_strength"],
        "why": decision_obj["why"],
        "rule": {"tier1_conf": AGENT_TIER1_CONF, "tier2_min_conf": AGENT_TIER2_MIN_CONF, "tier2_min_trend": AGENT_TIER2_MIN_TREND},
        "expires_in_sec": AGENT_DECISION_TTL_SEC,
        "snapshot_time_et": snap_time_et.isoformat() if snap_time_et else None,
        "sources": {"candidates": candidates},
    }


@router.get("/scan")
def agent_scan(
    x_bdv_secret: Optional[str] = Header(default=None),
):
    """
    Reporta por Telegram la MISMA decisión que usa monitor (orquestado real).
    """
    _require_agent_secret(x_bdv_secret)

    symbols = [s.strip().upper() for s in AGENT_SYMBOLS.split(",") if s.strip()]
    dec = agent_decision(x_bdv_secret=x_bdv_secret)

    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    note = (
        f"ET={now_et.strftime('%H:%M:%S')} | decision={dec.get('decision')} "
        f"{dec.get('symbol','')} {dec.get('side','')} conf={dec.get('confidence')} "
        f"ts={dec.get('trend_strength')} why={dec.get('why')}"
    )

    title = "TRADE" if dec.get("decision") == "trade" else "NO TRADE"
    _send_signal_telegram(symbols, title, note)

    return {"status": "ok", "decision": dec, "note": note}
