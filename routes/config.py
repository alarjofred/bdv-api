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
# _update_max_trades()  # 👈 COMENTAR ESTA LÍNEA

def _update_max_trades():
    if config_state.risk_mode == RiskMode.low:
        config_state.max_trades_per_day = 1
    elif config_state.risk_mode == RiskMode.medium:
        config_state.max_trades_per_day = 3
    elif config_state.risk_mode == RiskMode.high:
        config_state.max_trades_per_day = 5

@router.get("/status", response_model=ConfigStatus)
def get_config_status():
    return config_state

@router.post("/execution-mode", response_model=ConfigStatus)
def set_execution_mode(mode: ExecutionMode):
    config_state.execution_mode = mode
    return config_state

@router.post("/risk-mode", response_model=ConfigStatus)
def set_risk_mode(mode: RiskMode):
    config_state.risk_mode = mode
    _update_max_trades()
    return config_state

@router.post("/reset-trades", response_model=ConfigStatus)
def reset_trades_today():
    config_state.trades_today = 0
    return config_state
