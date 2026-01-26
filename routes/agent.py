# routes/agent.py

import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, Header, HTTPException

from .telegram_notify import send_alert

router = APIRouter(prefix="/agent", tags=["agent"])

# Base URL de TU API (Render)
API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# Seguridad básica
BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()

# OpenAI (para análisis profundo)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
OPENAI_ENABLED = os.getenv("OPENAI_ENABLED", "0").strip() in ("1", "true", "True", "yes", "YES")

# Símbolos y Telegram
AGENT_SYMBOLS = os.getenv("AGENT_SYMBOLS", "QQQ,SPY,NVDA")
AGENT_SEND_TELEGRAM = os.getenv("AGENT_SEND_TELEGRAM", "1").strip() not in ("0", "false", "False", "no", "NO")

# Semáforo y tolerancias
AGENT_STALE_GREEN_MAX_SEC = int(os.getenv("AGENT_STALE_GREEN_MAX_SEC", "120"))
AGENT_STALE_YELLOW_MAX_SEC = int(os.getenv("AGENT_STALE_YELLOW_MAX_SEC", "600"))  # 10 min
AGENT_ALLOW_YELLOW_SUMMARY = os.getenv("AGENT_ALLOW_YELLOW_SUMMARY", "1").strip() in ("1", "true", "True", "yes", "YES")


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    if BDV_AGENT_SECRET:
        if (not x_bdv_secret) or (x_bdv_secret.strip() != BDV_AGENT_SECRET):
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _get_json(url: str, timeout: int = 8) -> Dict[str, Any]:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    # Tu API a veces responde {"data": {...}} y a veces directo
    return data.get("data", data)


def _parse_snapshot_time_et(snapshot: Dict[str, Any]) -> Optional[datetime]:
    """
    Soporta 2 formatos:
    1) {"time": "..."} en root
    2) {"QQQ": {"time": "..."}, "SPY": {...}}  (tu caso real)
    """
    # 1) Root time/timestamp
    t = snapshot.get("time") or snapshot.get("timestamp")

    # 2) Buscar dentro del primer símbolo que tenga time/timestamp
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
    # RTH 9:30–16:00 ET
    h, m = et_dt.hour, et_dt.minute
    after_open = (h > 9) or (h == 9 and m >= 30)
    before_close = (h < 16) or (h == 16 and m == 0)
    return after_open and before_close


def _call_openai_bdv(prompt: str) -> str:
    """
    Llama OpenAI Responses API para análisis profundo.
    """
    if not OPENAI_API_KEY:
        return "OPENAI_DISABLED: falta OPENAI_API_KEY"

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "input": prompt,
    }

    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    j = r.json()

    # Caso simple
    if isinstance(j, dict) and j.get("output_text"):
        return str(j["output_text"]).strip()

    # Fallback robusto
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
            "note": (note or "")[:3500],  # safety para Telegram
        },
    )


@router.get("/scan")
def agent_scan(
    x_bdv_secret: Optional[str] = Header(default=None),
    force_analysis: int = 0,  # 1 = pide análisis a OpenAI aunque sea YELLOW (pero NUNCA ejecuta trades)
):
    """
    AGENTE (solo alerta + análisis):
    - Valida datos (config + snapshot)
    - Semáforo: GREEN / YELLOW / RED
    - Si GREEN: puede pedir análisis profundo (OpenAI) y mandar Telegram
    - Si YELLOW: manda resumen (y opcional análisis si force_analysis=1)
    - Si RED: manda error

    IMPORTANTE:
    - La ejecución automática NO ocurre aquí.
    - La ejecución automática (paper/live) queda en /monitor/tick (cron).
    """
    _require_agent_secret(x_bdv_secret)

    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido")

    symbols = [s.strip() for s in AGENT_SYMBOLS.split(",") if s.strip()]

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

    # Regla dura de límite diario
    if max_trades and trades_today >= max_trades:
        note = f"NO TRADE: límite diario alcanzado {trades_today}/{max_trades}. exec_mode={exec_mode} risk_mode={risk_mode}"
        _send_signal_telegram(symbols, "NO TRADE", note)
        return {"status": "yellow", "reason": "límite diario alcanzado", "config": cfg}

    # 2) snapshot
    try:
        snap = _get_json(f"{API_BASE}/snapshot", timeout=8)  # normalmente devuelve el dict interno "data"
    except Exception as e:
        _send_signal_telegram(symbols, "RED: API ERROR", f"/snapshot falló: {e}")
        return {"status": "red", "reason": "error API /snapshot", "error": str(e), "config": cfg}

    snap_time_et = _parse_snapshot_time_et(snap if isinstance(snap, dict) else {})
    if not snap_time_et:
        _send_signal_telegram(symbols, "RED: BAD DATA", "snapshot.time no existe o timestamp inválido")
        return {"status": "red", "reason": "sin snapshot.time / timestamp inválido", "config": cfg, "snapshot": snap}

    # Tiempo y edad del dato
    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    age_sec = int((now_et - snap_time_et).total_seconds())
    if age_sec < 0:
        # si por cualquier razón el reloj/parse genera futuro, clamp para no romper semáforo
        age_sec = 0
    in_rth = _is_rth(snap_time_et)  # ✅ usa la hora REAL del dato

    # Semáforo
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

    # RED duro (demasiado viejo)
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

    # YELLOW: no es operable para trade, pero sí enviamos resumen (para que no “muera” en NO TRADE)
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

    # GREEN (o YELLOW con force_analysis=1): pedir análisis a OpenAI si hay key y está habilitado o forzado
    wants_openai = bool(OPENAI_API_KEY) and (OPENAI_ENABLED or force_analysis == 1)

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
            f"SYMBOLS: {symbols}\n\n"
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
        telegram_note = base_ctx + "\n\n" + analysis_text
        _send_signal_telegram(symbols, "ANÁLISIS BDV", telegram_note)

        return {
            "status": "ok",
            "light": light,
            "config": cfg,
            "snapshot_time_et": snap_time_et.isoformat(),
            "age_sec": age_sec,
            "in_rth": in_rth,
            "note": base_ctx,
            "analysis": analysis_text,
        }

    # Sin OpenAI: solo manda estado
    _send_signal_telegram(symbols, "GREEN: DATA OK" if light == "green" else "YELLOW: DATA OK", base_ctx)
    return {
        "status": "ok",
        "light": light,
        "config": cfg,
        "snapshot_time_et": snap_time_et.isoformat(),
        "age_sec": age_sec,
        "in_rth": in_rth,
        "note": base_ctx,
    }
