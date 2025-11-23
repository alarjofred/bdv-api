# routes/signals_ai.py
from enum import Enum
from typing import Optional, Literal, List

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/signals", tags=["signals_ai"])

# ===== Estrategias profesionales para /signals/ai =====

STRATEGY_LIBRARY = {
    "no_trade": {
        "human_label": "Sin ventaja clara (NO TRADE)",
        "time_frame": "5m",
        "confidence": 0.0,
        "structure": {
            "kind": "none",
            "direction": "none",
            "legs": 1,
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
            "Regla inspirada en la gestión profesional: la primera defensa del capital es no entrar en trades basura.",
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
            "legs": 1,
            "days_to_expiry": 1,      # 0–1 días (muy corto plazo)
            "delta_hint": 0.25,       # OTM ligero para aprovechar movimiento
        },
        "risk": {
            "stop_loss_pct": 25.0,    # cortar rápido
            "take_profit_pct": 30.0,  # buscar 20–30%
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Operación de scalping en dirección de momentum alcista.",
            "Sólo válida cuando hay ruptura clara de rango y volumen acompañando.",
            "Pensada para 1–2 movimientos rápidos, no para mantener.",
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
            "legs": 1,
            "days_to_expiry": 1,
            "delta_hint": 0.25,
        },
        "risk": {
            "stop_loss_pct": 25.0,
            "take_profit_pct": 30.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Operación de scalping en dirección de momentum bajista.",
            "Sólo válida cuando se rompe soporte intradía con confirmación.",
        ],
    },

    # 3) INTRADAY CALL SPREAD
    "intraday_call_spread": {
        "human_label": "CALL debit spread intradía a favor de tendencia",
        "time_frame": "5m-15m",
        "confidence": 0.7,
        "structure": {
            "kind": "debit_spread",
            "direction": "call",
            "legs": 2,
            "days_to_expiry": 3,      # 1–3 días
            "delta_hint": 0.35,       # pierna larga entre 0.30–0.40
        },
        "risk": {
            "stop_loss_pct": 35.0,    # el spread limita el riesgo
            "take_profit_pct": 40.0,  # objetivo 30–40%
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Estrategia para seguir tendencia alcista clara con riesgo limitado.",
            "Se compra un CALL cercano al dinero y se vende uno más OTM para abaratar costo.",
        ],
    },

    # 4) INTRADAY PUT SPREAD
    "intraday_put_spread": {
        "human_label": "PUT debit spread intradía a favor de tendencia bajista",
        "time_frame": "5m-15m",
        "confidence": 0.7,
        "structure": {
            "kind": "debit_spread",
            "direction": "put",
            "legs": 2,
            "days_to_expiry": 3,
            "delta_hint": 0.35,
        },
        "risk": {
            "stop_loss_pct": 35.0,
            "take_profit_pct": 40.0,
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Estrategia para caídas ordenadas dentro de una tendencia bajista.",
            "Se busca aprovechar el movimiento con riesgo máximo limitado desde el inicio.",
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
            "legs": 1,
            "days_to_expiry": 5,      # 3–7 días
            "delta_hint": 0.4,        # algo más ITM para tener más estabilidad
        },
        "risk": {
            "stop_loss_pct": 30.0,
            "take_profit_pct": 50.0,  # objetivo más ambicioso
            "trailing_from_pct": 30.0,
            "trailing_stop_pct": 20.0,
        },
        "notes": [
            "Estrategia para tendencias alcistas claras con confirmación en marcos mayores.",
            "Se prioriza calidad de la tendencia frente a velocidad del movimiento.",
        ],
    },

    # 6) PREMIUM PUT CREDIT SPREAD
    "premium_put_credit_spread": {
        "human_label": "PUT credit spread en soporte fuerte (venta de prima, riesgo limitado)",
        "time_frame": "15m-1h",
        "confidence": 0.8,
        "structure": {
            "kind": "credit_spread",
            "direction": "bull_put",  # beneficio si el precio se mantiene por encima de un nivel
            "legs": 2,
            "days_to_expiry": 7,      # 5–10 días típicamente
            "delta_hint": 0.2,        # short leg con delta baja (probabilidad alta de expirar OTM)
        },
        "risk": {
            "stop_loss_pct": 50.0,    # se asume riesgo limitado al ancho del spread
            "take_profit_pct": 60.0,  # cerrar cuando se captura 50–60% de la prima
            "trailing_from_pct": None,
            "trailing_stop_pct": None,
        },
        "notes": [
            "Estrategia de venta de prima con riesgo limitado: se vende PUT cerca de soporte fuerte y se compra otro PUT más abajo.",
            "Requiere contexto: soporte técnico y volatilidad implícita relativamente alta.",
        ],
    },
}


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
    """

    # 1) Sin fuerza de tendencia → NO TRADE
    if trend_strength <= 1:
        return "no_trade"

    # 2) Zona de extremo → preferimos venta de prima con protección
    if near_extreme and bias == "bullish":
        # Precio en soporte fuerte + sesgo alcista → venta de PUT credit spread
        return "premium_put_credit_spread"

    # 3) Sesgo alcista
    if bias == "bullish":
        if prefer_spreads:
            # Tendencia alcista intradía: CALL spread
            return "intraday_call_spread"
        else:
            # Momentum fuerte: scalp CALL
            return "scalp_call_momo"

    # 4) Sesgo bajista
    if bias == "bearish":
        if prefer_spreads:
            # Caída estructurada: PUT spread
            return "intraday_put_spread"
        else:
            # Momentum bajista: scalp PUT
            return "scalp_put_momo"

    # 5) Sesgo neutral → sólo swing si la fuerza es muy alta (por ejemplo en ruptura mayor)
    if bias == "neutral" and trend_strength >= 3:
        return "swing_call_trend"

    # Por defecto: no trade
    return "no_trade"


def build_ai_signal_response(symbol: str, bias: str, strategy_code: str) -> dict:
    """
    Construye el JSON final a partir de STRATEGY_LIBRARY.
    """
    strategy = STRATEGY_LIBRARY.get(strategy_code, STRATEGY_LIBRARY["no_trade"])

    return {
        "symbol": symbol,
        "strategy_code": strategy_code,
        "human_label": strategy["human_label"],
        "time_frame": strategy["time_frame"],
        "bias": bias,
        "confidence": strategy["confidence"],
        "structure": strategy["structure"],
        "risk": strategy["risk"],
        "notes": strategy["notes"],
    }

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
    - Fuerza de tendencia (1–3)
    - Si está en zona extrema (near_extreme + extreme_side)
    - Preferencia por spreads para reducir riesgo.

    No usa datos de mercado en tiempo real: asume que el llamador (GPT/otro endpoint)
    ya evaluó las condiciones técnicas y pasa el contexto correcto.
    """

    def make_signal(
        *,
        strategy_code: str,
        human_label: str,
        confidence: float,
        kind: str,
        direction: str,
        legs: int,
        days_to_expiry: Optional[int],
        delta_hint: Optional[float],
        stop_loss_pct: Optional[float],
        take_profit_pct: Optional[float],
        trailing_from_pct: Optional[float],
        trailing_stop_pct: Optional[float],
        notes: list[str],
    ) -> OptionSignal:
        return OptionSignal(
            symbol=symbol,
            strategy_code=strategy_code,
            human_label=human_label,
            time_frame="5m",
            bias=bias,
            confidence=confidence,
            structure=OptionStructure(
                kind=kind,
                direction=direction,
                legs=legs,
                days_to_expiry=days_to_expiry,
                delta_hint=delta_hint,
            ),
            risk=RiskSettings(
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                trailing_from_pct=trailing_from_pct,
                trailing_stop_pct=trailing_stop_pct,
            ),
            notes=notes,
        )

    # 1) NO TRADE — sin ventaja clara
    if trend_strength == 1 and not near_extreme and extreme_side is None:
        return make_signal(
            strategy_code="no_trade",
            human_label="Sin ventaja clara (NO TRADE)",
            confidence=0.0,
            kind="none",
            direction="none",
            legs=0,
            days_to_expiry=None,
            delta_hint=None,
            stop_loss_pct=None,
            take_profit_pct=None,
            trailing_from_pct=None,
            trailing_stop_pct=None,
            notes=[
                "Condiciones de mercado sin clara tendencia ni extremo: mejor no operar.",
                "Volatilidad baja o precio pegado a VWAP / rango estrecho.",
                "Regla profesional: evitar trades basura para proteger capital.",
            ],
        )

    # 2) SCALP MOMENTUM CALL (direccional rápido)
    if (
        bias == Bias.bullish
        and trend_strength >= 2
        and not near_extreme
        and extreme_side is None
        and not prefer_spreads
    ):
        return make_signal(
            strategy_code="scalp_momo_call",
            human_label="Scalp CALL momentum intradía",
            confidence=0.7,
            kind="single",
            direction="call",
            legs=1,
            days_to_expiry=0,     # 0–1 días, vencimiento muy cercano
            delta_hint=0.35,      # ATM–ligeramente OTM
            stop_loss_pct=0.30,
            take_profit_pct=0.60,
            trailing_from_pct=None,
            trailing_stop_pct=None,
            notes=[
                "Sesgo alcista con momentum y sin sobrecompra extrema.",
                "Estructura simple para ejecución rápida en opciones.",
                "Cerrar parcial si el impulso se frena o rompe VWAP en contra.",
            ],
        )

    # 3) SCALP MOMENTUM PUT (direccional rápido)
    if (
        bias == Bias.bearish
        and trend_strength >= 2
        and not near_extreme
        and extreme_side is None
        and not prefer_spreads
    ):
        return make_signal(
            strategy_code="scalp_momo_put",
            human_label="Scalp PUT momentum intradía",
            confidence=0.7,
            kind="single",
            direction="put",
            legs=1,
            days_to_expiry=0,
            delta_hint=0.35,
            stop_loss_pct=0.30,
            take_profit_pct=0.60,
            trailing_from_pct=None,
            trailing_stop_pct=None,
            notes=[
                "Sesgo bajista con momentum claro y sin sobreventa extrema.",
                "Estrategia agresiva, tamaño de posición moderado.",
                "Salir si pierde fuerza el movimiento o entra volumen en contra.",
            ],
        )

    # 4) CREDIT PUT SPREAD — comprar el retroceso en soporte en mercado alcista
    if (
        bias == Bias.bullish
        and (near_extreme or extreme_side == ExtremeSide.support)
        and prefer_spreads
    ):
        return make_signal(
            strategy_code="credit_put_spread",
            human_label="Credit PUT spread en soporte (bullish)",
            confidence=0.8,
            kind="spread",
            direction="put",
            legs=2,
            days_to_expiry=3,     # 3–7 días típicamente
            delta_hint=0.25,      # short strike algo OTM
            stop_loss_pct=0.40,
            take_profit_pct=0.60,
            trailing_from_pct=None,
            trailing_stop_pct=None,
            notes=[
                "Sesgo alcista general; el precio corrige hacia zona de soporte.",
                "Se vende PUT OTM y se compra PUT más abajo para limitar riesgo.",
                "Estrategia inspirada en gestión de probabilidad + colchón de precio.",
            ],
        )

    # 5) CREDIT CALL SPREAD — vender el rebote en resistencia en mercado bajista
    if (
        bias == Bias.bearish
        and (near_extreme or extreme_side == ExtremeSide.resistance)
        and prefer_spreads
    ):
        return make_signal(
            strategy_code="credit_call_spread",
            human_label="Credit CALL spread en resistencia (bearish)",
            confidence=0.8,
            kind="spread",
            direction="call",
            legs=2,
            days_to_expiry=3,
            delta_hint=0.25,
            stop_loss_pct=0.40,
            take_profit_pct=0.60,
            trailing_from_pct=None,
            trailing_stop_pct=None,
            notes=[
                "Sesgo bajista; el precio rebota hacia una zona de resistencia relevante.",
                "Se vende CALL OTM y se compra CALL más arriba para acotar riesgo.",
                "Adecuado cuando hay techo claro y volatilidad razonable.",
            ],
        )

    # 6) DEBIT CALL SPREAD — ruptura alcista fuerte (tendencia 3)
    if (
        bias == Bias.bullish
        and trend_strength == 3
        and not near_extreme
        and prefer_spreads
    ):
        return make_signal(
            strategy_code="debit_call_spread_breakout",
            human_label="Debit CALL spread en ruptura alcista",
            confidence=0.75,
            kind="spread",
            direction="call",
            legs=2,
            days_to_expiry=5,
            delta_hint=0.30,
            stop_loss_pct=0.35,
            take_profit_pct=0.80,
            trailing_from_pct=0.50,
            trailing_stop_pct=0.30,
            notes=[
                "Ruptura alcista con confirmación de tendencia fuerte (3/3).",
                "Se usa spread de débito para mejorar relación riesgo/beneficio.",
                "Trailing sobre beneficios para capturar extensiones sin regalar mucho.",
            ],
        )

    # 7) DEBIT PUT SPREAD — ruptura bajista fuerte (tendencia 3)
    if (
        bias == Bias.bearish
        and trend_strength == 3
        and not near_extreme
        and prefer_spreads
    ):
        return make_signal(
            strategy_code="debit_put_spread_breakdown",
            human_label="Debit PUT spread en ruptura bajista",
            confidence=0.75,
            kind="spread",
            direction="put",
            legs=2,
            days_to_expiry=5,
            delta_hint=0.30,
            stop_loss_pct=0.35,
            take_profit_pct=0.80,
            trailing_from_pct=0.50,
            trailing_stop_pct=0.30,
            notes=[
                "Ruptura bajista con tendencia fuerte y confirmada.",
                "Spread de débito para limitar riesgo con potencial de buen recorrido.",
                "Adecuado cuando el precio rompe soporte con volumen y continuidad.",
            ],
        )

    # Fallback por seguridad: NO TRADE
    return make_signal(
        strategy_code="no_trade",
        human_label="Sin configuración clara (fallback NO TRADE)",
        confidence=0.0,
        kind="none",
        direction="none",
        legs=0,
        days_to_expiry=None,
        delta_hint=None,
        stop_loss_pct=None,
        take_profit_pct=None,
        trailing_from_pct=None,
        trailing_stop_pct=None,
        notes=[
            "No se cumple ninguna condición de setup profesional.",
            "Regla de oro: si no hay ventaja clara, mejor no operar.",
        ],
    )

