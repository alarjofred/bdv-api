# routes/monitor.py

from datetime import datetime
from fastapi import APIRouter
from . import config as config_module  # usamos el mismo estado global de config

router = APIRouter(prefix="/monitor", tags=["monitor"])

@router.get("/tick")
def monitor_tick():
    """
    Endpoint llamado por el CRON/daemon.
    Por ahora:
    - Lee la config actual (auto/manual, riesgo, límites).
    - Decide si "podría" operar o no.
    - Más adelante: aquí conectamos lógica de señales y /trade.
    """
    cfg = config_module.config_state  # estado global definido en routes/config.py

    now_utc = datetime.utcnow().isoformat()

    # 1) Si no está en modo auto, no hacemos nada.
    if cfg.execution_mode != "auto":
        return {
            "status": "skipped",
            "reason": "execution_mode is not auto",
            "timestamp_utc": now_utc,
            "config": cfg
        }

    # 2) Respetar límite de trades por día según riesgo
    if cfg.max_trades_per_day is not None and cfg.trades_today >= cfg.max_trades_per_day:
        return {
            "status": "skipped",
            "reason": "max_trades_per_day reached",
            "timestamp_utc": now_utc,
            "config": cfg
        }

    # 3) Aquí en el futuro haremos:
    #    - leer mercado (snapshot)
    #    - generar señal
    #    - si hay señal, ejecutar trade y aumentar trades_today
    #
    # Por ahora solo respondemos que el "tick" fue procesado y que
    # el sistema está listo para operar automáticamente.

    return {
        "status": "checked",
        "message": "Monitor tick procesado. Lógica de señales aún no implementada.",
        "timestamp_utc": now_utc,
        "config": cfg
    }
