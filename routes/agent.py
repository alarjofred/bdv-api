import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, List, Tuple
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

# ✅ Orquestación (suave por defecto)
AGENT_DECISION_ENABLED = os.getenv("AGENT_DECISION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "y", "on")
AGENT_DECISION_TTL_SEC = int((os.getenv("AGENT_DECISION_TTL_SEC", "120") or "120").strip() or "120")

# ✅ Regla por tramos (la que pediste)
CONF_STRONG = float((os.getenv("AGENT_CONF_STRONG", "0.75") or "0.75").strip() or "0.75")
CONF_WEAK = float((os.getenv("AGENT_CONF_WEAK", "0.66") or "0.66").strip() or "0.66")
WEAK_TREND_MIN = int((os.getenv("AGENT_WEAK_TREND_MIN", "3") or "3").strip() or "3")

# Semáforo (scan)
AGENT_STALE_GREEN_MAX_SEC = int((os.getenv("AGENT_STALE_GREEN_MAX_SEC", "120") or "120").strip() or "120")
AGENT_STALE_YELLOW_MAX_SEC = int((os.getenv("AGENT_STALE_YELLOW_MAX_SEC", "600") or "600").strip() or "600")
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
    ts = int(ctx.get("trend_strength", 1) or 1) if isinstance(ctx, dict) else 1
    return {
        "symbol": symbol,
        "bias": str(ctx.get("bias_inferred", "neutral")) if isinstance(ctx, dict) else "neutral",
        "trend_strength": ts,
        "action": action,
        "confidence": conf,
    }


def _rule_allows_trade(conf: float, ts: int) -> Tuple[bool, str]:
    if conf >= CONF_STRONG:
        return True, f"conf>=strong({CONF_STRONG})"
    if conf >= CONF_WEAK and conf < CONF_STRONG:
        if ts >= WEAK_TREND_MIN:
            return True, f"weak_conf({CONF_WEAK}-{CONF_STRONG}) AND ts>=({WEAK_TREND_MIN})"
        return False, f"weak_conf BUT ts<{WEAK_TREND_MIN}"
    return False, f"conf<{CONF_WEAK}"


@router.get("/decision")
def agent_decision(
    x_bdv_secret: Optional[str] = Header(default=None),
    exclude_symbols: Optional[str] = Query(default=None),
):
    """
    ✅ DECISIÓN ÚNICA (para orquestación):
    - La usa /monitor/tick para ejecutar
    - La usa /agent/scan para reportar (Telegram)
    - Aplica tu regla por tramos (confidence/trend_strength)
    """
    _require_agent_secret(x_bdv_secret)

    now_ny = _now_ny()
    inside_rth, rth_reason = _is_inside_rth(now_ny)

    if not inside_rth:
        return {
            "status": "ok",
            "decision": "no_trade",
            "why": rth_reason,
            "symbol": None,
            "side": None,
            "confidence": 0.0,
            "expires_in_sec": AGENT_DECISION_TTL_SEC,
            "snapshot_time_et": None,
            "sources": {"candidates": [], "skipped_symbols": []},
            "rule": {"strong": CONF_STRONG, "weak": CONF_WEAK, "weak_trend_min": WEAK_TREND_MIN},
            "excluded": [],
        }

    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido")

    if not AGENT_DECISION_ENABLED:
        return {"status": "ok", "decision": "no_trade", "why": "AGENT_DECISION_ENABLED=false"}

    excl = set()
    if exclude_symbols:
        for s in str(exclude_symbols).split(","):
            s = s.strip().upper()
            if s:
                excl.add(s)

    cfg = _get_json(f"{API_BASE}/config/status", timeout=8)
    snap = _get_json(f"{API_BASE}/snapshot", timeout=8)
    snap_time_et = _parse_snapshot_time_et(snap if isinstance(snap, dict) else {})

    symbols = [s.strip().upper() for s in AGENT_SYMBOLS.split(",") if s.strip()]
    symbols = [s for s in symbols if s not in excl]
    if not symbols:
        return {"status": "ok", "decision": "no_trade", "why": "all_symbols_excluded", "excluded": sorted(list(excl))}

    # market_ctx
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

    # candidatos por signals/ai, filtrando contexto malo o insuficiente
    candidates: List[Dict[str, Any]] = []
    skipped_symbols: List[Dict[str, Any]] = []

    for sym in symbols:
        ctx = market_ctx.get(sym, {}) if isinstance(market_ctx, dict) else {}

        status_ctx = str(ctx.get("status", "ok")).strip().lower()
        if status_ctx not in ("ok", ""):
            skipped_symbols.append({
                "symbol": sym,
                "reason": f"market_ctx_status={status_ctx}",
                "ctx": ctx,
            })
            continue

        data_quality_ok = ctx.get("data_quality_ok", True)
        if data_quality_ok is False:
            skipped_symbols.append({
                "symbol": sym,
                "reason": "data_quality_ok=false",
                "ctx": ctx,
            })
            continue

        bias = str(ctx.get("bias_inferred", "neutral")).strip().lower()
        if bias not in ("bullish", "bearish", "neutral"):
            bias = "neutral"

        try:
            ts = int(ctx.get("trend_strength", 1) or 1)
        except Exception:
            ts = 1

        if ts < 1:
            ts = 1

        ai_payload = _get_signals_ai(sym, bias=bias, trend_strength=ts)
        candidates.append(_summarize_candidate(sym, ctx, ai_payload))

    # elegir mejor buy/sell por confidence
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
            "why": "no_valid_candidates_from_market_ctx_or_signals_ai",
            "candidates": candidates,
            "skipped_symbols": skipped_symbols,
            "snapshot_time_et": snap_time_et.isoformat() if snap_time_et else None,
            "rule": {"strong": CONF_STRONG, "weak": CONF_WEAK, "weak_trend_min": WEAK_TREND_MIN},
        }

    conf = float(best.get("confidence", 0) or 0)
    ts = int(best.get("trend_strength", 1) or 1)
    allow, rule_why = _rule_allows_trade(conf, ts)

    decision_obj = {
        "decision": "trade" if allow else "no_trade",
        "symbol": best["symbol"],
        "side": best["action"],
        "confidence": conf,
        "why": f"signals_ai_best_candidate | {rule_why}",
    }

    # OpenAI puede SOLO cancelar o confirmar, pero NO puede romper tu regla dura
    if OPENAI_ENABLED and OPENAI_API_KEY:
        prompt = (
            "Responde SOLO JSON válido.\n"
            "No inventes datos. Puedes elegir 1 candidato o NO_TRADE.\n\n"
            f"RULE: strong_conf>={CONF_STRONG}, weak_conf>={CONF_WEAK} requires trend_strength>={WEAK_TREND_MIN}\n"
            f"CONFIG={cfg}\n"
            f"SNAPSHOT={snap}\n"
            f"CANDIDATES={candidates}\n"
            f"SKIPPED_SYMBOLS={skipped_symbols}\n\n"
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
                if sym not in [c["symbol"] for c in candidates]:
                    sym = best["symbol"]

                allow2, rule_why2 = _rule_allows_trade(conf2, ts)
                decision_obj = {
                    "decision": "trade" if (dec == "trade" and allow2) else "no_trade",
                    "symbol": sym,
                    "side": side,
                    "confidence": conf2,
                    "why": (str(parsed.get("why", "openai"))[:200] + f" | {rule_why2}").strip(),
                }
        except Exception:
            pass

    return {
        "status": "ok",
        "decision": decision_obj["decision"],
        "symbol": decision_obj["symbol"],
        "side": decision_obj["side"],
        "confidence": decision_obj["confidence"],
        "why": decision_obj["why"],
        "expires_in_sec": AGENT_DECISION_TTL_SEC,
        "snapshot_time_et": snap_time_et.isoformat() if snap_time_et else None,
        "sources": {"candidates": candidates, "skipped_symbols": skipped_symbols},
        "rule": {"strong": CONF_STRONG, "weak": CONF_WEAK, "weak_trend_min": WEAK_TREND_MIN},
        "excluded": sorted(list(excl)),
    }


@router.get("/scan")
def agent_scan(
    x_bdv_secret: Optional[str] = Header(default=None),
):
    """
    ✅ Reporta la MISMA decisión de /decision a Telegram (pero no ejecuta trades).
    """
    _require_agent_secret(x_bdv_secret)

    symbols = [s.strip().upper() for s in AGENT_SYMBOLS.split(",") if s.strip()]
    dec = agent_decision(x_bdv_secret=x_bdv_secret, exclude_symbols=None)

    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    note = (
        f"ET={now_et.strftime('%H:%M:%S')} | decision={dec.get('decision')} "
        f"{dec.get('symbol','')} {dec.get('side','')} conf={dec.get('confidence')} why={dec.get('why')}"
    )

    title = "TRADE" if dec.get("decision") == "trade" else "NO TRADE"
    _send_signal_telegram(symbols, title, note)

    return {"status": "ok", "decision": dec, "note": note}
