# routes/signals_ai.py
from enum import Enum
from typing import Optional, List

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/signals", tags=["signals_ai"])

# =====================================================
# ENUMS
# =====================================================

class Bias(str, Enum):
    bullish = "bullish"
    bearish = "bearish"
    neutral = "neutral"


class ExtremeSide(str, Enum):
    support = "support"
    resistance = "resistance"


class StructureKind(str, Enum):
    single = "single"          # opción simple (CALL/PUT)
    debit_spread = "debit_spread"
    credit_spread = "credit_spread"
    none = "none"              # para "no trade"


class Direction(str, Enum):
    call = "call"
    put = "put"
    none = "none"


# =====================================================
# MODELOS
# =====================================================

class RiskPlan(BaseModel):
    stop_loss_pct: Optional[float] = Field(
        None,
        description="Porcentaje de pérdida máxima permitido sobre la prima (ej: 30 = -30%)."
    )
    take_profit_pct: Optional[float] = Field(
        None,
        description="Porcentaje objetivo de ganancia sobre la prima (ej: 50 = +50%)."
    )
    trailing_from_pct: Optional[float] = Field(
        None,
        description="Desde qué % de ganancia se activa el trailing stop."
    )
    trailing_stop_pct: Optional[float] = Field(
        None,
        description="Nuevo stop una vez activado el trailing (ej: 10 = stop en +10%)."
    )


class OptionStructure(BaseModel):
    kind: StructureKind = Field(
        ...,
        description="Estructura: single, debit_spread, credit_spread o none."
    )
    direction: Direction = Field(
        Direction.none,
        description="CALL, PUT o none."
    )
    legs: List[str] = Field(
        default_factory=list,
        description=(
            "Descripción textual de las patas de la estrategia. "
            "Ej: ['BUY CALL ATM', 'SELL CALL OTM +2']. No son órdenes directas, solo plan."
        )
    )
    days_to_expiry: Optional[int] = Field(
        None,
        description="Días hasta vencimiento sugeridos."
    )
    delta_hint: Optional[str] = Field(
        None,
        description="Orientación de delta, ej: '0.20-0.30' o '0.40-0.60'."
    )


class OptionSignal(BaseModel):
    symbol: str = Field(..., description="Ticker: QQQ, SPY, NVDA.")
    strategy_code: str = Field(..., description="Código interno de la estrategia.")
    human_label: str = Field(..., description="Nombre explicativo para humanos.")
    time_frame: str = Field("5m", description="Marco temporal principal usado para la señal.")
    bias: Bias
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confianza 0–1.")
    structure: OptionStructure
    risk: RiskPlan
    notes: List[str] = Field(default_factory=list)


# =====================================================
# LIBRERÍA DE ESTRATEGIAS PROFESIONALES
# =====================================================

STRATEGY_LIBRARY = {
    # (... tu librería completa intacta ...)

    "no_trade": {
        "human_label": "Sin operación – contexto no favorable",
        "time_frame": "5m",
        "confidence": 0.0,
        "structure": {
            "kind": "none",
            "direction": "none",
            "legs": [],
            "days_to_expiry": None,
            "delta_hint": None,
        },
        "risk": {
            "stop_loss_pct": None,
            "take_profit_pct": None,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "No se detectó una ventaja clara de trading en este momento. Mantenerse fuera del mercado."
        ],
    },
}


# =====================================================
# LÓGICA DE ELECCIÓN DE ESTRATEGIA
# =====================================================

def choose_strategy_code(
    symbol: str,
    bias: str,
    trend_strength: int,
    near_extreme: bool,
    prefer_spreads: bool,
) -> str:
    """
    Selecciona un strategy_code profesional según el contexto.
    """
    # (contenido intacto)
    if trend_strength <= 1:
        return "no_trade"

    if near_extreme and bias == "bullish" and prefer_spreads:
        return "premium_put_credit_spread"

    if bias == "bullish":
        return "intraday_call_spread" if prefer_spreads else "scalp_call_momo"

    if bias == "bearish":
        return "intraday_put_spread" if prefer_spreads else "scalp_put_momo"

    if bias == "neutral" and trend_strength >= 3:
        return "swing_call_trend"

    return "no_trade"


def build_ai_signal_response(symbol: str, bias: Bias, strategy_code: str) -> OptionSignal:
    """
    Construye el objeto OptionSignal final a partir de STRATEGY_LIBRARY.
    """
    strategy = STRATEGY_LIBRARY.get(strategy_code, STRATEGY_LIBRARY["no_trade"])

    structure = OptionStructure(**strategy["structure"])
    risk = RiskPlan(**strategy["risk"])

    return OptionSignal(
        symbol=symbol,
        strategy_code=strategy_code,
        human_label=strategy["human_label"],
        time_frame=strategy["time_frame"],
        bias=bias,
        confidence=strategy["confidence"],
        structure=structure,
        risk=risk,
        notes=strategy["notes"],
    )


# =====================================================
# ENDPOINT PRINCIPAL /signals/ai
# =====================================================

@router.get("/ai", response_model=OptionSignal)
def generate_ai_signal(
    symbol: str = Query(..., regex="^(QQQ|SPY|NVDA)$", description="Símbolo: QQQ, SPY o NVDA."),
    bias: Bias = Query(..., description="Sesgo actual: bullish, bearish o neutral."),
    trend_strength: int = Query(
        1,
        ge=1,
        le=3,
        description="Fuerza de la tendencia (1 = débil, 3 = fuerte)."
    ),
    near_extreme: bool = Query(
        False,
        description="True si el precio está en zona de sobrecompra/sobreventa fuerte."
    ),
    extreme_side: Optional[ExtremeSide] = Query(
        None,
        description="support si está en soporte, resistance si está en resistencia."
    ),
    prefer_spreads: bool = Query(
        True,
        description="True para priorizar spreads (debit/credit) en lugar de opciones simples."
    )
):
    """
    Genera una señal profesional de opciones para QQQ/SPY/NVDA.
    """

    strategy_code = choose_strategy_code(
        symbol=symbol,
        bias=bias.value,
        trend_strength=trend_strength,
        near_extreme=near_extreme,
        prefer_spreads=prefer_spreads,
    )

    signal = build_ai_signal_response(symbol=symbol, bias=bias, strategy_code=strategy_code)

    # 🔔 Notificación Telegram añadida
    try:
        from routes.telegram_notify import send_alert
        send_alert("signal", {
            "symbol": signal.symbol,
            "bias": signal.bias.value,
            "suggestion": signal.human_label,
            "target": f"{signal.risk.take_profit_pct}%",
            "stop": f"-{signal.risk.stop_loss_pct}%",
            "note": signal.notes[0] if signal.notes else ""
        })
    except Exception as e:
        print(f"[WARN] No se pudo enviar notificación Telegram: {e}")

    return signal
