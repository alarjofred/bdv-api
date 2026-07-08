import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query

from .telegram_notify import send_alert


router = APIRouter(prefix="/agent", tags=["agent"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()

AGENT_SYMBOLS = os.getenv("AGENT_SYMBOLS", "QQQ,SPY,NVDA")

AGENT_SEND_TELEGRAM = os.getenv("AGENT_SEND_TELEGRAM", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

AGENT_DECISION_ENABLED = os.getenv("AGENT_DECISION_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

AGENT_DECISION_TTL_SEC = int(
    (os.getenv("AGENT_DECISION_TTL_SEC", "120") or "120").strip() or "120"
)

CONF_STRONG = float(
    (os.getenv("AGENT_CONF_STRONG", "0.75") or "0.75").strip() or "0.75"
)

CONF_WEAK = float(
    (os.getenv("AGENT_CONF_WEAK", "0.66") or "0.66").strip() or "0.66"
)

WEAK_TREND_MIN = int(
    (os.getenv("AGENT_WEAK_TREND_MIN", "3") or "3").strip() or "3"
)


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    if not BDV_AGENT_SECRET:
        return

    if not x_bdv_secret or x_bdv_secret.strip() != BDV_AGENT_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: missing/invalid X-BDV-SECRET",
        )


def _api_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if BDV_AGENT_SECRET:
        headers["X-BDV-SECRET"] = BDV_AGENT_SECRET

    return headers


def _get_json(url: str, timeout: int = 10) -> Dict[str, Any]:
    response = requests.get(
        url,
        headers=_api_headers(),
        timeout=timeout,
    )
    response.raise_for_status()

    payload = response.json()

    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            return data

    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _now_ny() -> datetime:
    return datetime.now(tz=ZoneInfo("America/New_York"))


def _is_inside_rth(now_ny: datetime) -> Tuple[bool, str]:
    day_of_week = int(now_ny.strftime("%u"))
    hhmm = int(now_ny.strftime("%H%M"))

    if day_of_week >= 6:
        return False, f"weekend {now_ny.isoformat()}"

    if hhmm < 930 or hhmm >= 1600:
        return False, f"outside_rth {now_ny.isoformat()}"

    return True, f"inside_rth {now_ny.isoformat()}"


def _parse_snapshot_time_et(snapshot: Dict[str, Any]) -> Optional[str]:
    timestamp = snapshot.get("time") or snapshot.get("timestamp")

    if not timestamp:
        for value in snapshot.values():
            if isinstance(value, dict):
                timestamp = value.get("time") or value.get("timestamp")
                if timestamp:
                    break

    if not timestamp:
        return None

    try:
        raw_value = str(timestamp).replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(raw_value)

        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))

        return dt_utc.astimezone(ZoneInfo("America/New_York")).isoformat()
    except Exception:
        return None


def _send_signal_telegram(symbols: List[str], title: str, note: str) -> None:
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

    response = requests.get(
        f"{API_BASE}/signals/ai",
        headers=_api_headers(),
        params=params,
        timeout=12,
    )

    if response.status_code != 200:
        return {
            "status": "error",
            "http": response.status_code,
            "body": response.text[:500],
            "params": params,
        }

    payload = response.json()

    if isinstance(payload, dict):
        return payload

    return {
        "status": "error",
        "detail": "signals_ai_non_dict",
        "params": params,
    }


def _summarize_candidate(
    symbol: str,
    ctx: Dict[str, Any],
    ai_payload: Dict[str, Any],
) -> Dict[str, Any]:
    data = ai_payload.get("data", ai_payload) if isinstance(ai_payload, dict) else {}

    action = str(data.get("action") or "").strip().lower()
    confidence = _safe_float(data.get("confidence", 0), 0.0)

    bias = str(ctx.get("bias_inferred", "neutral")).strip().lower()
    if bias not in ("bullish", "bearish", "neutral"):
        bias = "neutral"

    trend_strength = _safe_int(ctx.get("trend_strength", 1), 1)
    if trend_strength < 1:
        trend_strength = 1

    return {
        "symbol": symbol,
        "bias": bias,
        "trend_strength": trend_strength,
        "action": action,
        "confidence": confidence,
    }


def _normalize_candidate_action(candidate: Dict[str, Any]) -> Dict[str, Any]:
    action = str(candidate.get("action", "")).strip().lower()
    bias = str(candidate.get("bias", "")).strip().lower()
    confidence = _safe_float(candidate.get("confidence", 0), 0.0)
    trend_strength = _safe_int(candidate.get("trend_strength", 1), 1)

    if action in ("buy", "sell"):
        return candidate

    if confidence >= CONF_STRONG and trend_strength >= WEAK_TREND_MIN:
        if bias == "bullish":
            candidate["action"] = "buy"
        elif bias == "bearish":
            candidate["action"] = "sell"

    return candidate


def _rule_allows_trade(confidence: float, trend_strength: int) -> Tuple[bool, str]:
    if confidence >= CONF_STRONG:
        return True, f"conf>=strong({CONF_STRONG})"

    if CONF_WEAK <= confidence < CONF_STRONG:
        if trend_strength >= WEAK_TREND_MIN:
            return True, (
                f"weak_conf>=({CONF_WEAK}) "
                f"and trend_strength>=({WEAK_TREND_MIN})"
            )

        return False, f"weak_conf but trend_strength<{WEAK_TREND_MIN}"

    return False, f"confidence<{CONF_WEAK}"


def _base_no_trade_response(reason: str) -> Dict[str, Any]:
    return {
        "status": "ok",
        "decision": "no_trade",
        "why": reason,
        "symbol": None,
        "side": None,
        "confidence": 0.0,
        "expires_in_sec": AGENT_DECISION_TTL_SEC,
        "snapshot_time_et": None,
        "sources": {
            "candidates": [],
            "skipped_symbols": [],
        },
        "rule": {
            "strong": CONF_STRONG,
            "weak": CONF_WEAK,
            "weak_trend_min": WEAK_TREND_MIN,
        },
        "excluded": [],
    }


@router.get("/decision")
def agent_decision(
    x_bdv_secret: Optional[str] = Header(default=None),
    exclude_symbols: Optional[str] = Query(default=None),
):
    _require_agent_secret(x_bdv_secret)

    now_ny = _now_ny()
    inside_rth, rth_reason = _is_inside_rth(now_ny)

    if not inside_rth:
        return _base_no_trade_response(rth_reason)

    if not API_BASE:
        raise HTTPException(
            status_code=500,
            detail="RENDER_EXTERNAL_URL no definido",
        )

    if not AGENT_DECISION_ENABLED:
        return _base_no_trade_response("AGENT_DECISION_ENABLED=false")

    excluded_symbols = set()
    if exclude_symbols:
        for raw_symbol in str(exclude_symbols).split(","):
            symbol = raw_symbol.strip().upper()
            if symbol:
                excluded_symbols.add(symbol)

    symbols = [
        symbol.strip().upper()
        for symbol in AGENT_SYMBOLS.split(",")
        if symbol.strip()
    ]

    symbols = [
        symbol
        for symbol in symbols
        if symbol not in excluded_symbols
    ]

    if not symbols:
        response = _base_no_trade_response("all_symbols_excluded")
        response["excluded"] = sorted(list(excluded_symbols))
        return response

    snapshot = _get_json(f"{API_BASE}/snapshot", timeout=8)
    snapshot_time_et = _parse_snapshot_time_et(
        snapshot if isinstance(snapshot, dict) else {}
    )

    market_ctx: Dict[str, Any] = {}

    try:
        response = requests.get(
            f"{API_BASE}/snapshot/indicators",
            headers=_api_headers(),
            params={
                "symbols": ",".join(symbols),
                "timeframe": "5Min",
                "limit": "200",
                "lookback_hours": "48",
            },
            timeout=15,
        )

        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                market_ctx = payload["data"]
    except Exception:
        market_ctx = {}

    candidates: List[Dict[str, Any]] = []
    skipped_symbols: List[Dict[str, Any]] = []

    for symbol in symbols:
        ctx = market_ctx.get(symbol, {}) if isinstance(market_ctx, dict) else {}

        status_ctx = str(ctx.get("status", "ok")).strip().lower()
        if status_ctx not in ("ok", ""):
            skipped_symbols.append(
                {
                    "symbol": symbol,
                    "reason": f"market_ctx_status={status_ctx}",
                    "ctx": ctx,
                }
            )
            continue

        if ctx.get("data_quality_ok", True) is False:
            skipped_symbols.append(
                {
                    "symbol": symbol,
                    "reason": "data_quality_ok=false",
                    "ctx": ctx,
                }
            )
            continue

        bias = str(ctx.get("bias_inferred", "neutral")).strip().lower()
        if bias not in ("bullish", "bearish", "neutral"):
            bias = "neutral"

        trend_strength = _safe_int(ctx.get("trend_strength", 1), 1)
        if trend_strength < 1:
            trend_strength = 1

        ai_payload = _get_signals_ai(
            symbol=symbol,
            bias=bias,
            trend_strength=trend_strength,
        )

        candidate = _summarize_candidate(symbol, ctx, ai_payload)
        candidate = _normalize_candidate_action(candidate)

        candidates.append(candidate)

    best: Optional[Dict[str, Any]] = None

    for candidate in candidates:
        action = str(candidate.get("action", "")).strip().lower()

        if action not in ("buy", "sell"):
            continue

        confidence = _safe_float(candidate.get("confidence", 0), 0.0)

        if best is None:
            best = candidate
            continue

        best_confidence = _safe_float(best.get("confidence", 0), 0.0)
        if confidence > best_confidence:
            best = candidate

    if not best:
        return {
            "status": "ok",
            "decision": "no_trade",
            "why": "no_valid_candidates_from_market_ctx_or_signals_ai",
            "symbol": None,
            "side": None,
            "confidence": 0.0,
            "expires_in_sec": AGENT_DECISION_TTL_SEC,
            "snapshot_time_et": snapshot_time_et,
            "sources": {
                "candidates": candidates,
                "skipped_symbols": skipped_symbols,
            },
            "rule": {
                "strong": CONF_STRONG,
                "weak": CONF_WEAK,
                "weak_trend_min": WEAK_TREND_MIN,
            },
            "excluded": sorted(list(excluded_symbols)),
        }

    confidence = _safe_float(best.get("confidence", 0), 0.0)
    trend_strength = _safe_int(best.get("trend_strength", 1), 1)
    allow, rule_why = _rule_allows_trade(confidence, trend_strength)

    return {
        "status": "ok",
        "decision": "trade" if allow else "no_trade",
        "why": f"signals_ai_best_candidate | {rule_why}",
        "symbol": best.get("symbol"),
        "side": best.get("action"),
        "confidence": confidence,
        "expires_in_sec": AGENT_DECISION_TTL_SEC,
        "snapshot_time_et": snapshot_time_et,
        "sources": {
            "candidates": candidates,
            "skipped_symbols": skipped_symbols,
        },
        "rule": {
            "strong": CONF_STRONG,
            "weak": CONF_WEAK,
            "weak_trend_min": WEAK_TREND_MIN,
        },
        "excluded": sorted(list(excluded_symbols)),
    }


@router.get("/scan")
def agent_scan(
    x_bdv_secret: Optional[str] = Header(default=None),
):
    _require_agent_secret(x_bdv_secret)

    symbols = [
        symbol.strip().upper()
        for symbol in AGENT_SYMBOLS.split(",")
        if symbol.strip()
    ]

    decision = agent_decision(
        x_bdv_secret=x_bdv_secret,
        exclude_symbols=None,
    )

    now_et = datetime.now(tz=ZoneInfo("America/New_York"))

    note = (
        f"ET={now_et.strftime('%H:%M:%S')} | "
        f"decision={decision.get('decision')} "
        f"{decision.get('symbol', '')} "
        f"{decision.get('side', '')} "
        f"conf={decision.get('confidence')} "
        f"why={decision.get('why')}"
    )

    title = "TRADE" if decision.get("decision") == "trade" else "NO TRADE"
    _send_signal_telegram(symbols, title, note)

    return {
        "status": "ok",
        "decision": decision,
        "note": note,
    }
