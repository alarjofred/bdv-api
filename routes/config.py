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


class ConfigStatus(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.manual
    risk_mode: RiskMode = RiskMode.low

    # ✅ FIX: no puede arrancar en 0 si el risk_mode default es low
    max_trades_per_day: int = 1

    trades_today: int = 0


# ESTADO GLOBAL ÚNICO
config_state = ConfigStatus()


def _update_max_trades() -> None:
    """
    Ajusta el máximo de trades por día según el modo de riesgo.
    """
    if config_state.risk_mode == RiskMode.low:
        config_state.max_trades_per_day = 1
    elif config_state.risk_mode == RiskMode.medium:
        config_state.max_trades_per_day = 3
    elif config_state.risk_mode == RiskMode.high:
        config_state.max_trades_per_day = 5


class ExecutionModeUpdate(BaseModel):
    mode: ExecutionMode


class RiskModeUpdate(BaseModel):
    mode: RiskMode


@router.get("/status", response_model=ConfigStatus)
def get_config_status() -> ConfigStatus:
    return config_state


@router.post("/execution-mode", response_model=ConfigStatus)
def set_execution_mode(payload: ExecutionModeUpdate) -> ConfigStatus:
    config_state.execution_mode = payload.mode
    return config_state


@router.post("/risk-mode", response_model=ConfigStatus)
def set_risk_mode(payload: RiskModeUpdate) -> ConfigStatus:
    config_state.risk_mode = payload.mode
    _update_max_trades()
    return config_state


@router.post("/reset-trades", response_model=ConfigStatus)
def reset_trades_today() -> ConfigStatus:
    config_state.trades_today = 0
    return config_state


# ✅ FIX adicional: recalcula SIEMPRE al arrancar el server
_update_max_trades()
