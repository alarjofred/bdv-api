# routes/agent.py

import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional
from fastapi import APIRouter, Header, HTTPException

from .telegram_notify import send_alert

router = APIRouter(prefix="/agent", tags=["agent"])

API_BASE = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")  # ajusta luego
OPENAI_ENABLED = os.getenv("OPENAI_ENABLED", "0").strip() in ("1", "true", "True", "yes", "YES")

AGENT_SYMBOLS = os.getenv("AGENT_SYMBOLS", "QQQ,SPY,NVDA")
AGENT_SEND_TELEGRAM = os.getenv("AGENT_SEND_TELEGRAM", "1").strip() not in ("0", "false", "False", "no", "NO")


def _require_agent_secret(x_bdv_secret: Optional[str]) -> None:
    if BDV_AGENT_SECRET:
        if not x_bdv_secret or x_bdv_secret.strip() != BDV_AGENT_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


def _get_json(url: str, timeout: int = 8) -> Dict[str, Any]:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("data", data)


def _parse_snapshot_time_et(snapshot: Dict[str, Any]) -> Optional[datetime]:
    t = snapshot.get("time") or snapshot.get("timestamp")
    if not t:
        return None
    try:
        # ISO parse. Si viene con Z, datetime.fromisoformat necesita reemplazo.
        s = str(t).replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            # si viene naive asumimos UTC
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
    Llama Responses API (OpenAI) para análisis profundo.
    Endpoint: POST https://api.openai.com/v1/responses :contentReference[oaicite:1]{index=1}
    Auth: Authorization: Bearer OPENAI_API_KEY :contentReference[oaicite:2]{index=2}
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
    # helper típico: output_text puede no venir; extraemos texto de forma robusta
    if "output_text" in j and j["output_text"]:
        return j["output_text"]
    # fallback: buscar texto en output items
    out = j.get("output", [])
    chunks = []
    for item in out:
        for c in item.get("content", []) if isinstance(item, dict) else []:
            if c.get("type") in ("output_text", "text") and "text" in c:
                chunks.append(c["text"])
    return "\n".join(chunks).strip() or "OPENAI_OK_BUT_EMPTY"


@router.get("/scan")
def agent_scan(x_bdv_secret: Optional[str] = Header(default=None)):
    """
    SCAN (manual y auto):
    - Valida datos (config + snapshot) con reglas BDV
    - En manual: SOLO alertas (Telegram + opcional análisis GPT)
    - En auto: sigue siendo SOLO alertas aquí (la ejecución queda en /monitor/tick)
    """
    _require_agent_secret(x_bdv_secret)

    if not API_BASE:
        raise HTTPException(status_code=500, detail="RENDER_EXTERNAL_URL no definido")

    # 1) config/status
    try:
        cfg = _get_json(f"{API_BASE}/config/status", timeout=8)
    except Exception as e:
        return {"status": "no_trade", "reason": "error API /config/status", "error": str(e)}

    exec_mode = str(cfg.get("execution_mode", "manual")).lower()
    risk_mode = str(cfg.get("risk_mode", "low")).lower()
    max_trades = int(cfg.get("max_trades_per_day", 0) or 0)
    trades_today = int(cfg.get("trades_today", 0) or 0)

    if max_trades and trades_today >= max_trades:
        # Regla dura BDV
        msg = {
            "symbol": "BDV",
            "bias": "neutral",
            "suggestion": "NO TRADE",
            "target": "",
            "stop": "",
            "note": f"límite diario alcanzado: {trades_today}/{max_trades} (exec_mode={exec_mode}, risk_mode={risk_mode})",
        }
        if AGENT_SEND_TELEGRAM:
            send_alert("signal", msg)
        return {"status": "no_trade", "reason": "límite diario alcanzado", "config": cfg}

    # 2) snapshot
    try:
        snap = _get_json(f"{API_BASE}/snapshot", timeout=8)
    except Exception as e:
        return {"status": "no_trade", "reason": "error API /snapshot", "error": str(e), "config": cfg}

    # Tu /snapshot devuelve dict por símbolos; pero también puede traer meta "time".
    # Asumimos que "snap" puede incluir "time" o que venga dentro del root.
    snap_time_et = _parse_snapshot_time_et(snap if isinstance(snap, dict) else {})
    if not snap_time_et:
        return {"status": "no_trade", "reason": "sin snapshot.time / timestamp inválido", "config": cfg}

    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    in_rth = _is_rth(now_et)
    age_sec = int((now_et - snap_time_et).total_seconds())

    if not in_rth:
        # Fuera de RTH: por regla BDV no operable
        msg = {
            "symbol": "BDV",
            "bias": "neutral",
            "suggestion": "NO TRADE",
            "target": "",
            "stop": "",
            "note": f"fuera de RTH. snapshot={snap_time_et.strftime('%H:%M:%S')} ET",
        }
        if AGENT_SEND_TELEGRAM:
            send_alert("signal", msg)
        return {
            "status": "no_trade",
            "reason": "fuera de RTH",
            "snapshot_time_et": snap_time_et.isoformat(),
            "age_sec": age_sec,
            "config": cfg,
        }

    if age_sec > 120:
        msg = {
            "symbol": "BDV",
            "bias": "neutral",
            "suggestion": "NO TRADE",
            "target": "",
            "stop": "",
            "note": f"snapshot stale: {age_sec}s (>120s). snapshot={snap_time_et.strftime('%H:%M:%S')} ET",
        }
        if AGENT_SEND_TELEGRAM:
            send_alert("signal", msg)
        return {
            "status": "no_trade",
            "reason": "snapshot stale (>120s)",
            "snapshot_time_et": snap_time_et.isoformat(),
            "age_sec": age_sec,
            "config": cfg,
        }

    # Datos OK → LISTO PARA ANÁLISIS
    symbols = [s.strip() for s in AGENT_SYMBOLS.split(",") if s.strip()]
    base_note = f"DATA OK. exec_mode={exec_mode} risk_mode={risk_mode} snapshot={snap_time_et.strftime('%H:%M:%S')} ET age={age_sec}s"

    # Opción: en esta primera fase, solo avisamos "DATA OK" o pedimos análisis profundo a OpenAI
    if OPENAI_ENABLED and OPENAI_API_KEY:
        prompt = (
            "Eres BDV OPCIONES LIVE. Analiza SIN operar.\n"
            "Reglas duras: si data OK, entrega formato BDV. No inventes. No option chain.\n\n"
            f"CONFIG: {cfg}\n"
            f"SNAPSHOT_RAW: {snap}\n"
            f"SYMBOLS: {symbols}\n"
            "Entrega: Estado de datos + Config + Sesgo + Escenarios + Niveles + Estrategia (CALL/PUT/NO TRADE) + Hora snapshot ET.\n"
        )
        analysis_text = _call_openai_bdv(prompt)
        if AGENT_SEND_TELEGRAM:
            send_alert("signal", {"symbol": ",".join(symbols), "bias": "neutral", "suggestion": "ANÁLISIS", "target": "", "stop": "", "note": analysis_text})
        return {"status": "ok", "data": "OK", "config": cfg, "snapshot_time_et": snap_time_et.isoformat(), "age_sec": age_sec, "analysis": analysis_text}

    # Sin OpenAI: solo mensaje base
    if AGENT_SEND_TELEGRAM:
        send_alert("signal", {"symbol": ",".join(symbols), "bias": "neutral", "suggestion": "DATA OK", "target": "", "stop": "", "note": base_note})

    return {"status": "ok", "data": "OK", "config": cfg, "snapshot_time_et": snap_time_et.isoformat(), "age_sec": age_sec, "note": base_note}
