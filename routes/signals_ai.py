# routes/signals_ai.py
from enum import Enum
from typing import Optional, Literal, List

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/signals", tags=["signals_ai"])


# ==========
# ENUMS BASE
# ==========

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


# ==========
# MODELOS
# ==========

class RiskPlan(BaseModel):
    stop_loss_pct: Optional[float] = Field(
        None,
        description="Porcentaje de pérdida máxima permitido sobre la prima (ej: 30 = -30%)"
    )
    take_profit_pct: Optional[float] = Field(
        None,
        description="Porcentaje objetivo de ganancia sobre la prima (ej: 50 = +50%)"
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
            "Ej: ['BUY CALL ATM', 'SELL CALL OTM +2']. "
            "No son órdenes directas, solo plan."
        )
    )
    days_to_expiry: Optional[int] = Field(
        None,
        description="Días hasta vencimiento sugeridos (2-5 típico)."
    )
    delta_hint: Optional[str] = Field(
        None,
        description="Orientación de delta, ej: '0.40-0.60'."
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


# ==========
# LÓGICA DE SELECCIÓN
# ==========

def _build_trend_call_debit_spread(symbol: str) -> OptionSignal:
    return OptionSignal(
        symbol=symbol,
        strategy_code="trend_pullback_call_debit_spread",
        human_label="Tendencia alcista + pullback sano (CALL Debit Spread)",
        time_frame="5m",
        bias=Bias.bullish,
        confidence=0.75,
        structure=OptionStructure(
            kind=StructureKind.debit_spread,
            direction=Direction.call,
            legs=[
                "BUY CALL ATM (delta 0.40-0.60, vencimiento 2-5 días)",
                "SELL CALL OTM (+2 a +4 strikes sobre el ATM, mismo vencimiento)"
            ],
            days_to_expiry=3,
            delta_hint="0.40-0.60"
        ),
        risk=RiskPlan(
            stop_loss_pct=50.0,      # perder máx 50% del spread
            take_profit_pct=70.0,    # objetivo 70% del valor máximo
            trailing_from_pct=None,
            trailing_stop_pct=None
        ),
        notes=[
            "Usar solo si EMA 9 > EMA 21 y precio > VWAP en 5m.",
            "Esperar pullback suave hacia EMA 21 o VWAP con volumen decreciente.",
            "Entrar en la primera vela verde fuerte que cierra de nuevo sobre EMA 9.",
            "Debit spread reduce coste y suaviza el impacto de la volatilidad implícita."
        ]
    )


def _build_trend_put_debit_spread(symbol: str) -> OptionSignal:
    return OptionSignal(
        symbol=symbol,
        strategy_code="trend_pullback_put_debit_spread",
        human_label="Tendencia bajista + pullback a resistencia (PUT Debit Spread)",
        time_frame="5m",
        bias=Bias.bearish,
        confidence=0.75,
        structure=OptionStructure(
            kind=StructureKind.debit_spread,
            direction=Direction.put,
            legs=[
                "BUY PUT ATM (delta 0.40-0.60, vencimiento 2-5 días)",
                "SELL PUT OTM (-2 a -4 strikes bajo el ATM, mismo vencimiento)"
            ],
            days_to_expiry=3,
            delta_hint="0.40-0.60"
        ),
        risk=RiskPlan(
            stop_loss_pct=50.0,
            take_profit_pct=70.0,
            trailing_from_pct=None,
            trailing_stop_pct=None
        ),
        notes=[
            "Usar solo si EMA 9 < EMA 21 y precio < VWAP en 5m.",
            "Esperar pullback alcista hacia EMA 21 o VWAP con volumen decreciente.",
            "Entrar en la primera vela roja fuerte que rechaza esa zona y cierra bajo EMA 9."
        ]
    )


def _build_bull_put_credit_spread(symbol: str) -> OptionSignal:
    return OptionSignal(
        symbol=symbol,
        strategy_code="bull_put_credit_spread",
        human_label="Soporte fuerte + sobreventa (Bull Put Credit Spread)",
        time_frame="5m",
        bias=Bias.bullish,
        confidence=0.7,
        structure=OptionStructure(
            kind=StructureKind.credit_spread,
            direction=Direction.put,
            legs=[
                "SELL PUT OTM bajo soporte clave (1-2 strikes OTM)",
                "BUY PUT más abajo para protección (3-5 strikes bajo el vendido)"
            ],
            days_to_expiry=5,
            delta_hint="0.20-0.30 en la pata vendida"
        ),
        risk=RiskPlan(
            stop_loss_pct=100.0,     # riesgo máx = ancho del spread - prima cobrada
            take_profit_pct=50.0,    # cerrar cuando se capture 50% de la prima
            trailing_from_pct=None,
            trailing_stop_pct=None
        ),
        notes=[
            "Usar cuando el precio testeó soporte fuerte y muestra vela de rechazo con volumen.",
            "Ganas si el precio se mantiene por encima del strike vendido hasta el vencimiento.",
            "Adecuado para SPY/QQQ en zonas soportivas fuertes."
        ]
    )


def _build_bear_call_credit_spread(symbol: str) -> OptionSignal:
    return OptionSignal(
        symbol=symbol,
        strategy_code="bear_call_credit_spread",
        human_label="Resistencia fuerte + sobrecompra (Bear Call Credit Spread)",
        time_frame="5m",
        bias=Bias.bearish,
        confidence=0.7,
        structure=OptionStructure(
            kind=StructureKind.credit_spread,
            direction=Direction.call,
            legs=[
                "SELL CALL OTM sobre resistencia clave (1-2 strikes OTM)",
                "BUY CALL más arriba para protección (3-5 strikes sobre el vendido)"
            ],
            days_to_expiry=5,
            delta_hint="0.20-0.30 en la pata vendida"
        ),
        risk=RiskPlan(
            stop_loss_pct=100.0,
            take_profit_pct=50.0,
            trailing_from_pct=None,
            trailing_stop_pct=None
        ),
        notes=[
            "Usar cuando el precio testeó resistencia fuerte y mostró rechazo claro.",
            "Ganas si el precio se mantiene por debajo del strike vendido hasta el vencimiento.",
            "Especialmente útil en SPY cuando respeta niveles diarios importantes."
        ]
    )


def _build_no_trade(symbol: str, bias: Bias) -> OptionSignal:
    return OptionSignal(
        symbol=symbol,
        strategy_code="no_trade",
        human_label="Sin ventaja clara (NO TRADE)",
        time_frame="5m",
        bias=bias,
        confidence=0.0,
        structure=OptionStructure(
            kind=StructureKind.none,
            direction=Direction.none,
            legs=[],
            days_to_expiry=None,
            delta_hint=None
        ),
        risk=RiskPlan(
            stop_loss_pct=None,
            take_profit_pct=None,
            trailing_from_pct=None,
            trailing_stop_pct=None
        ),
        notes=[
            "Condiciones de mercado sin clara tendencia ni extremo: mejor no operar.",
            "Volatilidad baja, precio pegado a VWAP o rangos muy estrechos.",
            "Regla inspirada en gestión profesional: evitar trades basura para proteger capital."
        ]
    )


# ==========
# ENDPOINT
# ==========

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
    Genera una señal profesional de opciones para QQQ/SPY/NVDA basada en:
    - Sesgo (bullish/bearish/neutral)
    - Fuerza de tendencia (1-3)
    - Si está en zona extrema (near_extreme + extreme_side)
    - Preferencia por spreads para reducir riesgo.

    No usa datos de mercado en tiempo real: asume que el llamador (GPT/otro endpoint)
    ya evaluó las condiciones técnicas y pasa el contexto correcto.
    """

    # 1) Escenario de NO TRADE (rango, sin fuerza, ni soporte/resistencia claros)
    if bias == Bias.neutral and not near_extreme:
        return _build_no_trade(symbol, bias)

    # 2) Tendencia fuerte → preferir debit spreads direccionales
    if bias == Bias.bullish and trend_strength >= 2:
        if prefer_spreads:
            return _build_trend_call_debit_spread(symbol)
        # versión simple single call (por si en el futuro quieres habilitarla)
        signal = _build_trend_call_debit_spread(symbol)
        signal.structure.kind = StructureKind.single
        signal.structure.legs = ["BUY CALL ATM (delta 0.40-0.60, vencimiento 2-5 días)"]
        signal.risk.stop_loss_pct = 30.0
        signal.risk.take_profit_pct = 50.0
        return signal

    if bias == Bias.bearish and trend_strength >= 2:
        if prefer_spreads:
            return _build_trend_put_debit_spread(symbol)
        signal = _build_trend_put_debit_spread(symbol)
        signal.structure.kind = StructureKind.single
        signal.structure.legs = ["BUY PUT ATM (delta 0.40-0.60, vencimiento 2-5 días)"]
        signal.risk.stop_loss_pct = 30.0
        signal.risk.take_profit_pct = 50.0
        return signal

    # 3) Zonas extremas → credit spreads de alta probabilidad
    if near_extreme and extreme_side is not None:
        if extreme_side == ExtremeSide.support and bias in (Bias.bullish, Bias.neutral):
            return _build_bull_put_credit_spread(symbol)
        if extreme_side == ExtremeSide.resistance and bias in (Bias.bearish, Bias.neutral):
            return _build_bear_call_credit_spread(symbol)

    # 4) Si nada encaja bien → NO TRADE
    return _build_no_trade(symbol, bias)
