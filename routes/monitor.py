from fastapi import APIRouter
import datetime
import os
from typing import List

import pytz

# Si usas alpaca_trade_api:
#   pip install alpaca-trade-api
# y descomenta esta línea:
from alpaca_trade_api import REST


router = APIRouter(prefix="/monitor", tags=["monitor"])

# ============================================
# CONFIGURACIÓN ALPACA – AJUSTA A TU PROYECTO
# ============================================

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


def get_alpaca_client() -> REST:
    """
    Devuelve el cliente de Alpaca.
    Si en tu proyecto ya tienes otra función/objeto para esto,
    puedes reemplazar esta función por la tuya.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("[BDV][Monitor] Faltan llaves de Alpaca en variables de entorno.")
    return REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)


# ==========================
# PARÁMETROS DE GESTIÓN BDV
# ==========================

TP_PCT = 0.12    # 12% de ganancia (0.12 = 12 %)
SL_PCT = -0.05   # -5% de pérdida
MAX_RUNNERS = 1  # máximo 1 posición "runner" para el día siguiente


# ==========================
# FUNCIONES AUXILIARES TIEMPO
# ==========================

def _get_ny_time() -> datetime.datetime:
    """Hora actual en Nueva York (para saber cierre de mercado)."""
    tz = pytz.timezone("America/New_York")
    return datetime.datetime.now(tz)


def _is_near_market_close(minutes: int = 10) -> bool:
    """
    Devuelve True si estamos cerca del cierre de mercado (X minutos).
    Se asume horario regular 9:30–16:00 NY.
    """
    now = _get_ny_time()
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = market_close - now
    return datetime.timedelta(minutes=0) < delta <= datetime.timedelta(minutes=minutes)


# ==========================
# GESTIÓN DE POSICIONES BDV
# ==========================

def manage_open_positions(alpaca_client: REST) -> None:
    """
    Lógica de gestión de posiciones:
    - TP en 10 %
    - SL en -3 %
    - Cerca del cierre:
        - cerrar casi todo
        - dejar a lo sumo 1 runner (mejor PnL >= 10 %)
    """

    try:
        positions = alpaca_client.list_positions()
    except Exception as e:
        print(f"[BDV][Monitor] Error al listar posiciones: {e}")
        return

    if not positions:
        return

    # 1) TP / SL INTRADÍA
    for pos in positions:
        try:
            symbol = pos.symbol
            # unrealized_plpc suele ser string "0.1234" => 12.34 %
            pnl_pct = float(pos.unrealized_plpc)  # proporción, no en %
        except Exception as e:
            print(f"[BDV][Monitor] Error leyendo posición: {e}")
            continue

        # Take Profit (TP)
        if pnl_pct >= TP_PCT:
            print(f"[BDV][TP] Cerrando {symbol} por TP {pnl_pct:.2%}")
            try:
                alpaca_client.close_position(symbol)
            except Exception as e:
                print(f"[BDV][TP] Error al cerrar {symbol}: {e}")
            continue

        # Stop Loss (SL)
        if pnl_pct <= SL_PCT:
            print(f"[BDV][SL] Cerrando {symbol} por SL {pnl_pct:.2%}")
            try:
                alpaca_client.close_position(symbol)
            except Exception as e:
                print(f"[BDV][SL] Error al cerrar {symbol}: {e}")

    # Releer posiciones después de TP/SL
    try:
        positions = alpaca_client.list_positions()
    except Exception as e:
        print(f"[BDV][Monitor] Error al relistar posiciones: {e}")
        return

    if not positions:
        return

    # 2) LÓGICA CERCA DEL CIERRE DE MERCADO
    if not _is_near_market_close(minutes=10):
        # Si NO estamos cerca del cierre, no hacemos nada extra
        return

    enriched = []
    for pos in positions:
        try:
            pnl_pct = float(pos.unrealized_plpc)
            enriched.append((pnl_pct, pos))
        except Exception as e:
            print(f"[BDV][Monitor] Error enriqueciendo posición: {e}")
            continue

    if not enriched:
        return

    # Ordenar de mayor PnL a menor
    enriched.sort(key=lambda x: x[0], reverse=True)

    runners_kept = 0
    for idx, (pnl_pct, pos) in enumerate(enriched):
        symbol = pos.symbol

        # Regla simple de cierre al final del día:
        # - Si tiene ≥ 10 % y aún no hemos alcanzado el límite de runners,
        #   podemos dejar ESTA posición abierta como posible "runner".
        # - TODO lo demás se cierra al cierre.
        keep = False
        if pnl_pct >= TP_PCT and runners_kept < MAX_RUNNERS:
            keep = True
            runners_kept += 1

        if keep:
            print(f"[BDV][Runner] Manteniendo {symbol} con ganancia {pnl_pct:.2%} para posible capitalización mañana.")
            continue

        # Cerrar el resto al cierre
        print(f"[BDV][Close EOD] Cerrando {symbol} al cierre con PnL {pnl_pct:.2%}")
        try:
            alpaca_client.close_position(symbol)
        except Exception as e:
            print(f"[BDV][Close EOD] Error al cerrar {symbol}: {e}")


# ==========================
# ENDPOINT /monitor/tick
# ==========================

@router.get("/tick")
async def monitor_tick():
    """
    Tick del monitor BDV:
    - Aquí podrías tener ya tu lógica actual de:
      * revisar señales AI
      * revisar configuración BDV
      * etc.

    - Al final, llama a manage_open_positions() para aplicar:
      * TP 10 %
      * SL -3 %
      * cierre al final del día
      * dejar máximo 1 runner
    """
    # 1) Lógica previa del monitor (si tenías algo, puedes insertarlo aquí)
    #    Ejemplo:
    #    - verificar /config/status
    #    - llamar a /signals/ai
    #    - registrar logs
    #    IMPORTANTE: si tenías código aquí, no lo borres, insértalo encima
    #    de la parte de Alpaca.

    # 2) Gestión de posiciones con Alpaca
    try:
        alpaca_client = get_alpaca_client()
    except Exception as e:
        print(f"[BDV][Monitor] Error creando cliente Alpaca: {e}")
        return {
            "status": "error",
            "detail": "No se pudo crear el cliente de Alpaca. Revisa las llaves en variables de entorno."
        }

    manage_open_positions(alpaca_client)

    return {
        "status": "ok",
        "detail": "Monitor tick ejecutado con gestión de posiciones (TP/SL/EOD) BDV."
    }
