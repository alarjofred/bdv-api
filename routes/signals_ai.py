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
    "no_trade": {
        "human_label": "Sin ventaja clara (NO TRADE)",
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
            "Condiciones de mercado sin ventaja estadística clara: mejor no operar.",
            "Primera defensa del capital: evitar trades basura.",
        ],
    },

    # 1) SCALP CALL MOMENTUM
    "scalp_call_momo": {
        "human_label": "Scalp CALL momentum alcista",
        "time_frame": "1m-5m",
        "confidence": 0.65,
        "structure": {
            "kind": "single",
            "direction": "call",
            "legs": ["BUY CALL ligeramente OTM, vencimiento 0–1 días."],
            "days_to_expiry": 1,
            "delta_hint": "0.25-0.35",
        },
        "risk": {
            "stop_loss_pct": 25.0,
            "take_profit_pct": 30.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Scalping en dirección de momentum alcista fuerte.",
            "Sólo cuando hay ruptura de rango y volumen acompañando.",
        ],
    },

    # 2) SCALP PUT MOMENTUM
    "scalp_put_momo": {
        "human_label": "Scalp PUT momentum bajista",
        "time_frame": "1m-5m",
        "confidence": 0.65,
        "structure": {
            "kind": "single",
            "direction": "put",
            "legs": ["BUY PUT ligeramente OTM, vencimiento 0–1 días."],
            "days_to_expiry": 1,
            "delta_hint": "0.25-0.35",
        },
        "risk": {
            "stop_loss_pct": 25.0,
            "take_profit_pct": 30.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Scalping en dirección de momentum bajista claro.",
            "Sólo cuando se rompe soporte intradía con confirmación.",
        ],
    },

    # 3) INTRADAY CALL DEBIT SPREAD
    "intraday_call_spread": {
        "human_label": "CALL debit spread intradía a favor de tendencia",
        "time_frame": "5m-15m",
        "confidence": 0.7,
        "structure": {
            "kind": "debit_spread",
            "direction": "call",
            "legs": [
                "BUY CALL cercano al dinero (delta ~0.35-0.45).",
                "SELL CALL más OTM para abaratar el coste."
            ],
            "days_to_expiry": 3,
            "delta_hint": "0.35-0.45 pierna larga",
        },
        "risk": {
            "stop_loss_pct": 35.0,
            "take_profit_pct": 40.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Seguir tendencia alcista clara con riesgo limitado por el spread.",
        ],
    },

    # 4) INTRADAY PUT DEBIT SPREAD
    "intraday_put_spread": {
        "human_label": "PUT debit spread intradía a favor de tendencia bajista",
        "time_frame": "5m-15m",
        "confidence": 0.7,
        "structure": {
            "kind": "debit_spread",
            "direction": "put",
            "legs": [
                "BUY PUT cercano al dinero (delta ~0.35-0.45).",
                "SELL PUT más OTM para abaratar el coste."
            ],
            "days_to_expiry": 3,
            "delta_hint": "0.35-0.45 pierna larga",
        },
        "risk": {
            "stop_loss_pct": 35.0,
            "take_profit_pct": 40.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Aprovechar caídas ordenadas dentro de tendencia bajista.",
        ],
    },

    # 5) SWING CALL TREND
    "swing_call_trend": {
        "human_label": "CALL swing siguiendo tendencia alcista (1–3 días)",
        "time_frame": "15m-1h",
        "confidence": 0.75,
        "structure": {
            "kind": "single",
            "direction": "call",
            "legs": ["BUY CALL algo ITM para mayor estabilidad (delta ~0.40-0.55)."],
            "days_to_expiry": 5,
            "delta_hint": "0.40-0.55",
        },
        "risk": {
            "stop_loss_pct": 30.0,
            "take_profit_pct": 50.0,
            "trailing_from_pct": 30.0,
            "trailing_stop_pct": 20.0,
        },
        "notes": [
            "Tendencias alcistas claras confirmadas en marcos mayores.",
        ],
    },

    # 6) PREMIUM PUT CREDIT SPREAD
    "premium_put_credit_spread": {
        "human_label": "PUT credit spread en soporte fuerte (venta de prima, riesgo limitado)",
        "time_frame": "15m-1h",
        "confidence": 0.8,
        "structure": {
            "kind": "credit_spread",
            "direction": "put",   # Direction enum sólo admite call/put/none
            "legs": [
                "SELL PUT OTM cerca de soporte fuerte (delta ~0.20-0.30).",
                "BUY PUT más abajo para limitar el riesgo."
            ],
            "days_to_expiry": 7,
            "delta_hint": "0.20-0.30 en la pata vendida",
        },
        "risk": {
            "stop_loss_pct": 50.0,
            "take_profit_pct": 60.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Venta de prima con riesgo limitado en zonas de soporte clave.",
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
    Si no hay ventaja clara, devuelve 'no_trade'.
    Symbol no se usa aún, pero se deja por si luego
    diferenciamos lógica QQQ/SPY/NVDA.
    """

    # 1) Sin fuerza de tendencia → NO TRADE
    if trend_strength <= 1:
        return "no_trade"

    # 2) Zona de extremo + sesgo alcista → venta de PUT credit spread
    if near_extreme and bias == "bullish" and prefer_spreads:
        return "premium_put_credit_spread"

    # 3) Sesgo alcista
    if bias == "bullish":
        if prefer_spreads:
            return "intraday_call_spread"
        else:
            return "scalp_call_momo"

    # 4) Sesgo bajista
    if bias == "bearish":
        if prefer_spreads:
            return "intraday_put_spread"
        else:
            return "scalp_put_momo"

    # 5) Sesgo neutral con fuerza alta → swing
    if bias == "neutral" and trend_strength >= 3:
        return "swing_call_trend"

    # Por defecto: NO TRADE
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
    extreme_side: Optional[ExtremeSide] = Query(  # reservado para futuros ajustes
        None,
        description="support si está en soporte, resistance si está en resistencia."
    ),
    prefer_spreads: bool = Query(
        True,
        description="True para priorizar spreads (debit/credit) en lugar de opciones simples."
    )
):
    """
    Genera una señal profesional de opciones para QQQ/SPY/NVDA basada en:
    - Sesgo (bullish/bearish/neutral)
    - Fuerza de tendencia (1–3)
    - Si está en zona extrema (near_extreme + extreme_side)
    - Preferencia por spreads para reducir riesgo.

    No usa datos de mercado en tiempo real: se asume que el llamador (GPT/otro endpoint)
    ya evaluó las condiciones técnicas y pasa el contexto correcto.
    """

    strategy_code = choose_strategy_code(
        symbol=symbol,
        bias=bias.value,
        trend_strength=trend_strength,
        near_extreme=near_extreme,
        prefer_spreads=prefer_spreads,
    )

    return build_ai_signal_response(symbol=symbol, bias=bias, strategy_code=strategy_code)
