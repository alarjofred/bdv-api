# routes/signals_ai.py

from enum import Enum
from typing import Optional, List, Any, Dict, Union

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


class Action(str, Enum):
    """
    Acción resumida para consumo de otros módulos.
    Por defecto será "wait" para evitar overtrading.
    """
    buy = "buy"
    sell = "sell"
    wait = "wait"


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

    # ✅ Campo resumido para facilitar consumo (monitor/logs/UI)
    # Por defecto: wait (no-trade)
    action: Action = Field(Action.wait, description="Resumen: buy/sell/wait (por defecto wait).")

    # ✅ Meta útil de depuración (qué params llegaron al endpoint)
    params_echo: Optional[Dict[str, Any]] = Field(
        None,
        description="Echo de params utilizados (para auditoría)."
    )


# =====================================================
# LIBRERÍA DE ESTRATEGIAS PROFESIONALES
# =====================================================

STRATEGY_LIBRARY: Dict[str, Dict[str, Any]] = {
    # ✅ Ejemplo mínimo. Mantén tu librería completa aquí (intacta),
    # solo asegúrate que 'structure.legs' sea lista de strings.
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
# HELPERS (NORMALIZACIÓN)
# =====================================================

def _normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s not in ("QQQ", "SPY", "NVDA"):
        # fallback seguro
        return "QQQ"
    return s


def _normalize_legs(value: Union[List[str], str, int, None]) -> List[str]:
    """
    En tu screenshot aparecía legs: 1 (int). Eso NO debe pasar.
    Aquí normalizamos a List[str] siempre.
    """
    if value is None:
        return []
    if isinstance(value, list):
        # filtra a strings
        out: List[str] = []
        for x in value:
            if x is None:
                continue
            out.append(str(x))
        return out
    if isinstance(value, (str, int, float)):
        # si venía un número/string raro, no lo usamos como “legs”
        return []
    return []


def _infer_action(strategy_code: str, bias: Bias, confidence: float, kind: StructureKind) -> Action:
    """
    IMPORTANTÍSIMO:
    - Por defecto NO queremos ejecutar trades por IA.
    - Así que action = wait salvo que tú habilites explícitamente un modo.
    """
    # regla segura: no_trade => wait
    if strategy_code == "no_trade" or kind == StructureKind.none or confidence <= 0:
        return Action.wait

    # Por defecto seguimos siendo conservadores: WAIT.
    # Si luego quieres habilitar BUY/SELL, lo hacemos con una ENV (te lo dejo listo):
    import os
    enable_stock_action = os.getenv("AI_ENABLE_STOCK_ACTION", "false").lower() in ("1", "true", "yes")
    min_conf = float(os.getenv("AI_MIN_CONFIDENCE", "0.75"))

    if not enable_stock_action:
        return Action.wait

    if confidence < min_conf:
        return Action.wait

    # Mapeo simple (solo si lo habilitas):
    if bias == Bias.bullish:
        return Action.buy
    if bias == Bias.bearish:
        return Action.sell
    return Action.wait


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
    # 🔒 guardrail: si no hay fuerza de tendencia, no trade
    if trend_strength <= 1:
        return "no_trade"

    # Ejemplos (ajusta según tu librería real):
    if near_extreme and bias == "bullish" and prefer_spreads:
        return "premium_put_credit_spread"

    if bias == "bullish":
        return "intraday_call_spread" if prefer_spreads else "scalp_call_momo"

    if bias == "bearish":
        return "intraday_put_spread" if prefer_spreads else "scalp_put_momo"

    if bias == "neutral" and trend_strength >= 3:
        return "swing_call_trend"

    return "no_trade"


def build_ai_signal_response(
    symbol: str,
    bias: Bias,
    strategy_code: str,
    params_echo: Optional[Dict[str, Any]] = None
) -> OptionSignal:
    """
    Construye el objeto OptionSignal final a partir de STRATEGY_LIBRARY.
    """
    strategy = STRATEGY_LIBRARY.get(strategy_code, STRATEGY_LIBRARY["no_trade"])

    # Normaliza legs SIEMPRE a lista de strings
    struct_dict = dict(strategy.get("structure", {}))
    struct_dict["legs"] = _normalize_legs(struct_dict.get("legs"))

    # Si kind no existe o es inválido, fuerza none
    if "kind" not in struct_dict:
        struct_dict["kind"] = "none"

    structure = OptionStructure(**struct_dict)
    risk = RiskPlan(**strategy.get("risk", {}))

    confidence = float(strategy.get("confidence", 0.0) or 0.0)
    action = _infer_action(strategy_code=strategy_code, bias=bias, confidence=confidence, kind=structure.kind)

    return OptionSignal(
        symbol=symbol,
        strategy_code=strategy_code,
        human_label=strategy.get("human_label", "Señal IA"),
        time_frame=strategy.get("time_frame", "5m"),
        bias=bias,
        confidence=confidence,
        structure=structure,
        risk=risk,
        notes=list(strategy.get("notes", [])) if isinstance(strategy.get("notes"), list) else [],
        action=action,
        params_echo=params_echo,
    )


# =====================================================
# ENDPOINT PRINCIPAL /signals/ai
# =====================================================

@router.get("/ai", response_model=OptionSignal)
def generate_ai_signal(
    # ✅ YA NO es obligatorio: así no hay 422 si alguien llama “pelado”
    symbol: str = Query(
        "QQQ",
        pattern="^(QQQ|SPY|NVDA)$",
        description="Símbolo: QQQ, SPY o NVDA."
    ),
    # ✅ Default para evitar 422
    bias: Bias = Query(Bias.bullish, description="Sesgo actual: bullish, bearish o neutral."),
    # ✅ Default ya existe; mantenemos rango
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
    Por defecto NO recomienda ejecutar trades automáticos (action='wait'),
    a menos que habilites AI_ENABLE_STOCK_ACTION=true y pase el umbral.
    """

    sym = _normalize_symbol(symbol)

    strategy_code = choose_strategy_code(
        symbol=sym,
        bias=bias.value,
        trend_strength=trend_strength,
        near_extreme=near_extreme,
        prefer_spreads=prefer_spreads,
    )

    params_echo = {
        "symbol": sym,
        "bias": bias.value,
        "trend_strength": trend_strength,
        "near_extreme": near_extreme,
        "extreme_side": extreme_side.value if extreme_side else None,
        "prefer_spreads": prefer_spreads,
    }

    signal = build_ai_signal_response(
        symbol=sym,
        bias=bias,
        strategy_code=strategy_code,
        params_echo=params_echo
    )

    # 🔔 Notificación Telegram (defensivo con None)
    try:
        from routes.telegram_notify import send_alert

        tp = signal.risk.take_profit_pct
        sl = signal.risk.stop_loss_pct

        send_alert("signal", {
            "symbol": signal.symbol,
            "bias": signal.bias.value,
            "action": signal.action.value,
            "strategy_code": signal.strategy_code,
            "suggestion": signal.human_label,
            "confidence": signal.confidence,
            "target": f"{tp}%" if tp is not None else "n/a",
            "stop": f"-{sl}%" if sl is not None else "n/a",
            "note": signal.notes[0] if signal.notes else ""
        })
    except Exception as e:
        print(f"[WARN] No se pudo enviar notificación Telegram: {e}")

    return signal
