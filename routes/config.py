from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/config", tags=["config"])


class ExecutionMode(str, Enum):
    manual = "manual"
    auto = "auto"


class RiskMode(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


# ✅ Opción A: risk_mode es la única fuente de verdad del límite diario
MAX_TRADES_BY_RISK = {
    RiskMode.low: 1,
    RiskMode.medium: 3,
    RiskMode.high: 5,
}


class ConfigStatus(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.manual
    risk_mode: RiskMode = RiskMode.low

    # ✅ FIX: no puede arrancar en 0 si risk_mode default es low
    # Se mantiene, pero se sincroniza SIEMPRE con risk_mode.
    max_trades_per_day: int = 1

    trades_today: int = 0


# ESTADO GLOBAL ÚNICO
config_state = ConfigStatus()


def _sync_max_trades() -> None:
    """
    Recalcula SIEMPRE el máximo de trades por día según el risk_mode.
    Evita desincronización (ej: que LOW termine mostrando 2).
    """
    config_state.max_trades_per_day = int(MAX_TRADES_BY_RISK.get(config_state.risk_mode, 1))


class ExecutionModeUpdate(BaseModel):
    mode: ExecutionMode


class RiskModeUpdate(BaseModel):
    mode: RiskMode


def _normalize_mode_str(value: str) -> str:
    return value.strip().lower()


async def _extract_mode_from_request(
    request: Request,
    query_mode: Optional[str],
    body_obj: Optional[Any],
    dict_key_primary: str,
    dict_key_alt: str,
    allowed: set[str],
) -> str:
    """
    Extrae modo desde:
      1) Query param (?mode=auto)
      2) Body dict con claves {mode: ...} o {alt_key: ...}
      3) Body string JSON: "auto"
      4) Body raw parseado manualmente (por si FastAPI no lo parseó)
    """
    # 1) querystring (lo más robusto para cron)
    if query_mode:
        m = _normalize_mode_str(str(query_mode))
        if m in allowed:
            return m

    # 2) body ya parseado
    if isinstance(body_obj, dict):
        v = body_obj.get(dict_key_primary) or body_obj.get(dict_key_alt)
        if v is not None:
            m = _normalize_mode_str(str(v))
            if m in allowed:
                return m

    if isinstance(body_obj, str):
        m = _normalize_mode_str(body_obj)
        if m in allowed:
            return m

    # 3) raw body fallback
    raw = await request.body()
    if raw:
        text = raw.decode("utf-8", errors="ignore").strip()

        # string "auto" (sin JSON)
        if text and text[0] != "{":
            m = _normalize_mode_str(text.strip('"').strip("'"))
            if m in allowed:
                return m

        # JSON dict
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, dict):
                v = parsed.get(dict_key_primary) or parsed.get(dict_key_alt)
                if v is not None:
                    m = _normalize_mode_str(str(v))
                    if m in allowed:
                        return m
            elif isinstance(parsed, str):
                m = _normalize_mode_str(parsed)
                if m in allowed:
                    return m
        except Exception:
            pass

    raise HTTPException(
        status_code=422,
        detail=f"mode is required and must be one of: {sorted(list(allowed))}. "
               f"Send ?mode=... or JSON body."
    )


@router.get("/status", response_model=ConfigStatus)
def get_config_status() -> ConfigStatus:
    _sync_max_trades()
    return config_state


@router.post("/execution-mode", response_model=ConfigStatus)
async def set_execution_mode(
    request: Request,
    mode: Optional[str] = Query(default=None),
    payload: Optional[dict] = Body(default=None),
) -> ConfigStatus:
    # Acepta ?mode=auto (recomendado) o JSON {"mode":"auto"} / {"execution_mode":"auto"}
    m = await _extract_mode_from_request(
        request=request,
        query_mode=mode,
        body_obj=payload,
        dict_key_primary="mode",
        dict_key_alt="execution_mode",
        allowed={"auto", "manual"},
    )
    config_state.execution_mode = ExecutionMode(m)
    _sync_max_trades()
    return config_state


@router.post("/risk-mode", response_model=ConfigStatus)
async def set_risk_mode(
    request: Request,
    mode: Optional[str] = Query(default=None),
    payload: Optional[dict] = Body(default=None),
) -> ConfigStatus:
    # Acepta ?mode=low|medium|high o JSON {"mode":"low"} / {"risk_mode":"low"}
    m = await _extract_mode_from_request(
        request=request,
        query_mode=mode,
        body_obj=payload,
        dict_key_primary="mode",
        dict_key_alt="risk_mode",
        allowed={"low", "medium", "high"},
    )
    config_state.risk_mode = RiskMode(m)
    _sync_max_trades()
    return config_state


@router.post("/reset-trades", response_model=ConfigStatus)
def reset_trades_today() -> ConfigStatus:
    config_state.trades_today = 0
    _sync_max_trades()
    return config_state


# ✅ FIX adicional: sincroniza SIEMPRE al arrancar el server
_sync_max_trades()
