# routes/monitor.py
from fastapi import APIRouter
from datetime import datetime
from routes.config import config_state

router = APIRouter(prefix="/monitor", tags=["monitor"])

@router.get("/tick")
def monitor_tick():
    """
    Endpoint llamado por cron jobs (Render) para revisar el mercado.
    Más adelante agregaremos lógica automática de señales.
    """

    return {
        "status": "checked",
        "message": "Monitor tick procesado correctamente.",
        "timestamp_utc": datetime.utcnow().isoformat(),
        "config": {
            "execution_mode": config_state.execution_mode,
            "risk_mode": config_state.risk_mode,
            "max_trades_per_day": config_state.max_trades_per_day,
            "trades_today": config_state.trades_today,
        }
    }
