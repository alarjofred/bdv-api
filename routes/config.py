from enum import Enum
from typing import Optional, Set

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/config", tags=["config"])


class ExecutionMode(str, Enum):
    manual = "manual"
    auto = "auto"


class RiskMode(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


MAX_TRADES_BY_RISK = {
    RiskMode.low: 1,
    RiskMode.medium: 3,
    RiskMode.high: 5,
}


class ConfigStatus(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.manual
    risk_mode: RiskMode = RiskMode.low
    max_trades_per_day: int = 1
    trades_today: int = 0


config_state = ConfigStatus()


def _sync_max_trades() -> None:
    config_state.max_trades_per_day = int(MAX_TRADES_BY_RISK.get(config_state.risk_mode, 1))


def _normalize_mode_str(value: str) -> str:
    return value.strip().lower()


async def _extract_mode(
    request: Request,
    query_mode: Optional[str],
    dict_key_primary: str,
    dict_key_alt: str,
    allowed: Set[str],
) -> str:
    """
    Extrae modo desde:
      1) Query param (?mode=auto)  <-- lo más robusto para GitHub Actions
      2) Body raw (JSON dict {"mode":"auto"} o {"execution_mode":"auto"} o string "auto")
      3) Body raw sin JSON (auto/manual)
    IMPORTANTE: NO usamos Body(...) para evitar 422 json_invalid antes de entrar al endpoint.
    """

    # 1) querystring (preferido)
    if query_mode:
        m = _normalize_mode_str(str(query_mode))
        if m in allowed:
            return m

    # 2) raw body fallback (solo si existe)
    raw = await request.body()
    if raw:
        text = raw.decode("utf-8", errors="ignore").strip()

        # Caso: body es un string simple: auto
        if text and text[0] != "{":
            m = _normalize_mode_str(text.strip('"').strip("'"))
            if m in allowed:
                return m

        # Caso: JSON
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
            # Si el JSON viene roto, NO explotamos; seguimos a error controlado.
            pass

    raise HTTPException(
        status_code=422,
        detail=(
            f"mode is required and must be one of: {sorted(list(allowed))}. "
            f"Send ?mode=... or JSON body."
        ),
    )


@router.get("/status", response_model=ConfigStatus)
def get_config_status() -> ConfigStatus:
    _sync_max_trades()
    return config_state


@router.post("/execution-mode", response_model=ConfigStatus)
async def set_execution_mode(
    request: Request,
    mode: Optional[str] = Query(default=None),
) -> ConfigStatus:
    # Acepta:
    #  - /config/execution-mode?mode=auto (recomendado)
    #  - body {"mode":"auto"} o {"execution_mode":"auto"} o "auto"
    m = await _extract_mode(
        request=request,
        query_mode=mode,
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
) -> ConfigStatus:
    # Acepta:
    #  - /config/risk-mode?mode=low|medium|high (recomendado)
    #  - body {"mode":"low"} o {"risk_mode":"low"} o "low"
    m = await _extract_mode(
        request=request,
        query_mode=mode,
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


_sync_max_trades()
