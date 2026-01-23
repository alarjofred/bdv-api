from enum import Enum
from fastapi import APIRouter
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


@router.get("/status", response_model=ConfigStatus)
def get_config_status() -> ConfigStatus:
    # ✅ importante: antes de responder, sincroniza
    _sync_max_trades()
    return config_state


@router.post("/execution-mode", response_model=ConfigStatus)
def set_execution_mode(payload: ExecutionModeUpdate) -> ConfigStatus:
    config_state.execution_mode = payload.mode
    # ✅ no cambia límites, pero mantiene consistencia
    _sync_max_trades()
    return config_state


@router.post("/risk-mode", response_model=ConfigStatus)
def set_risk_mode(payload: RiskModeUpdate) -> ConfigStatus:
    config_state.risk_mode = payload.mode
    _sync_max_trades()
    return config_state


@router.post("/reset-trades", response_model=ConfigStatus)
def reset_trades_today() -> ConfigStatus:
    config_state.trades_today = 0
    _sync_max_trades()
    return config_state


# ✅ FIX adicional: sincroniza SIEMPRE al arrancar el server
_sync_max_trades()
