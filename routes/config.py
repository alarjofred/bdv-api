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
    max_trades_per_day: int = 0
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
        # aquí fijamos el límite de 4 trades intradía como acordamos
        config_state.max_trades_per_day = 4
    elif config_state.risk_mode == RiskMode.high:
        config_state.max_trades_per_day = 5


# ─────────────────────────────
#   MODELOS PARA EL REQUEST BODY
# ─────────────────────────────

class ExecutionModeUpdate(BaseModel):
    mode: ExecutionMode


class RiskModeUpdate(BaseModel):
    mode: RiskMode


# ─────────────────────────────
#           ENDPOINTS
# ─────────────────────────────

@router.get("/status", response_model=ConfigStatus)
def get_config_status() -> ConfigStatus:
    """
    Devuelve el estado de configuración actual.
    """
    return config_state


@router.post("/execution-mode", response_model=ConfigStatus)
def set_execution_mode(payload: ExecutionModeUpdate) -> ConfigStatus:
    """
    Cambia el modo de ejecución (manual / auto).
    Espera un JSON como: { "mode": "auto" }
    """
    config_state.execution_mode = payload.mode
    return config_state


@router.post("/risk-mode", response_model=ConfigStatus)
def set_risk_mode(payload: RiskModeUpdate) -> ConfigStatus:
    """
    Cambia el modo de riesgo (low / medium / high)
    y actualiza max_trades_per_day en consecuencia.
    Espera un JSON como: { "mode": "medium" }
    """
    config_state.risk_mode = payload.mode
    _update_max_trades()
    return config_state


@router.post("/reset-trades", response_model=ConfigStatus)
def reset_trades_today() -> ConfigStatus:
    """
    Reinicia el contador de trades del día a 0.
    """
    config_state.trades_today = 0
    return config_state
