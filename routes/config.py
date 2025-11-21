from fastapi import APIRouter, HTTPException
from typing import Literal
from pydantic import BaseModel

router = APIRouter()

# -------------------------------------------------------
#  Estado en memoria (se reinicia si Render reinicia el servicio)
# -------------------------------------------------------
EXECUTION_MODE: Literal["manual", "auto"] = "manual"
RISK_MODE: Literal["low", "medium", "high"] = "medium"
MAX_TRADES_PER_DAY: int = 3
TRADES_TODAY: int = 0  # por ahora sólo informativo


# -------------------------------------------------------
#  Modelos de respuesta (para que el OpenAPI quede limpio)
# -------------------------------------------------------
class ConfigStatusResponse(BaseModel):
    execution_mode: Literal["manual", "auto"]
    risk_mode: Literal["low", "medium", "high"]
    max_trades_per_day: int
    trades_today: int


class ExecutionModeResponse(BaseModel):
    status: str
    execution_mode: Literal["manual", "auto"]
    message: str


class RiskModeResponse(BaseModel):
    status: str
    risk_mode: Literal["low", "medium", "high"]
    max_trades_per_day: int
    message: str


# -------------------------------------------------------
#  GET /config/status
# -------------------------------------------------------
@router.get("/config/status", response_model=ConfigStatusResponse)
def get_config_status():
    """
    Devuelve la configuración actual del sistema BDV API.
    """
    return {
        "execution_mode": EXECUTION_MODE,
        "risk_mode": RISK_MODE,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "trades_today": TRADES_TODAY,
    }


# -------------------------------------------------------
#  POST /config/execution-mode
#  mode: "manual" | "auto"
# -------------------------------------------------------
@router.post("/config/execution-mode", response_model=ExecutionModeResponse)
def set_execution_mode(mode: Literal["manual", "auto"]):
    """
    Cambia el modo de ejecución:
      - manual: sólo señales, tú confirmas.
      - auto: la API puede enviar órdenes a Alpaca (según reglas).
    """
    global EXECUTION_MODE

    if mode not in ("manual", "auto"):
        raise HTTPException(status_code=422, detail="Invalid execution mode")

    EXECUTION_MODE = mode

    return {
        "status": "ok",
        "execution_mode": EXECUTION_MODE,
        "message": f"Modo de ejecución cambiado a '{EXECUTION_MODE}'",
    }


# -------------------------------------------------------
#  POST /config/risk-mode
#  mode: "low" | "medium" | "high"
# -------------------------------------------------------
@router.post("/config/risk-mode", response_model=RiskModeResponse)
def set_risk_mode(mode: Literal["low", "medium", "high"]):
    """
    Cambia el modo de riesgo y ajusta el número máximo
    de trades por día.
    """
    global RISK_MODE, MAX_TRADES_PER_DAY

    if mode == "low":
        MAX_TRADES_PER_DAY = 2
    elif mode == "medium":
        MAX_TRADES_PER_DAY = 3
    elif mode == "high":
        MAX_TRADES_PER_DAY = 5
    else:
        raise HTTPException(status_code=422, detail="Invalid risk mode")

    RISK_MODE = mode

    return {
        "status": "ok",
        "risk_mode": RISK_MODE,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "message": f"Modo de riesgo cambiado a '{RISK_MODE}'",
    }
