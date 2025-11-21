# routes/monitor.py

from fastapi import APIRouter
from datetime import datetime
from . import config  # para leer execution_mode, risk, trades_today, etc.
# from .alpaca_client import place_order, get_market_data ... (según tu estructura)

router = APIRouter(prefix="/monitor", tags=["monitor"])

@router.get("/tick")
def monitor_tick():
    """
    Endpoint que será llamado por el CRON/daemon.
    1) Verifica config (auto / riesgo / límite).
    2) Revisa si el mercado está abierto.
    3) Genera señal y, si aplica, ejecuta trade.
    """
    # 1. Leer config actual
    cfg = config.config_state  # usamos el estado global que ya tienes

    if cfg.execution_mode != "auto":
        return {"status": "skipped", "reason": "execution_mode is not auto"}

    # 2. Respetar límite de trades por día
    if cfg.trades_today >= cfg.max_trades_per_day:
        return {"status": "skipped", "reason": "max_trades_per_day reached"}

    # 3. (Opcional) Verificar horario de mercado (ejemplo simple, se puede usar la API de Alpaca)
    now_utc = datetime.utcnow().time()
    # Aquí podrías limitar por hora ET, etc. Por ahora lo dejamos simple.

    # 4. Lógica de señal (placeholder)
    #    Aquí deberías llamar a tu lógica /signals/generate o a una función interna
    signal = {
        "symbol": "QQQ",
        "side": "buy",
        "qty": 1,
        "reason": "Reglas BDV: ejemplo placeholder"
    }

    # Si no hay señal válida, devolvemos skip
    if not signal:
        return {"status": "skipped", "reason": "no signal"}

    # 5. Ejecutar trade usando tu lógica actual (/trade interno o cliente Alpaca)
    #    Aquí lo ideal es reutilizar la función que ya usas en /trade.
    #    Ejemplo genérico:
    # trade_response = place_order(symbol=signal["symbol"], side=signal["side"], qty=signal["qty"])

    # Simulamos una respuesta:
    trade_response = {"alpaca_status": "ok", "order_id": "demo-order-id"}

    # 6. Actualizar contador de trades y log
    cfg.trades_today += 1

    # (Aquí deberías también escribir en tu trades-log)

    return {
        "status": "executed",
        "symbol": signal["symbol"],
        "side": signal["side"],
        "qty": signal["qty"],
        "risk_mode": cfg.risk_mode,
        "execution_mode": cfg.execution_mode,
        "trade_response": trade_response
    }
