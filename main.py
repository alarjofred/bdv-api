# ---------------------------------
# IMPORT DE ROUTERS (con protección)
# ---------------------------------
from routes.test_alpaca import router as test_alpaca_router
from routes.recommend import router as recommend_router
from routes.signals import router as signals_router
from routes.config import router as config_router
from routes.monitor import router as monitor_router
from routes.signals_ai import router as signals_ai_router
from routes.alpaca_close import router as alpaca_close_router
from routes.agent import router as agent_router  # ✅ AGENTE
from routes import trade
from routes import telegram_notify
from routes import pending_trades

# Estos pueden fallar si el archivo no existe / tiene error.
try:
    from routes import analysis
except Exception as e:
    analysis = None
    print(f"[WARN] No se pudo importar routes.analysis: {e}")

try:
    from routes import candles
except Exception as e:
    candles = None
    print(f"[WARN] No se pudo importar routes.candles: {e}")
