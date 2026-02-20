from enum import Enum
from typing import Optional, List, Any, Dict

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
    single = "single"
    debit_spread = "debit_spread"
    credit_spread = "credit_spread"
    none = "none"


class Direction(str, Enum):
    call = "call"
    put = "put"
    none = "none"


class Action(str, Enum):
    buy = "buy"
    sell = "sell"
    wait = "wait"


# =====================================================
# MODELOS
# =====================================================

class RiskPlan(BaseModel):
    stop_loss_pct: Optional[float] = Field(None, description="Ej: 30 = -30% sobre prima.")
    take_profit_pct: Optional[float] = Field(None, description="Ej: 50 = +50% sobre prima.")
    trailing_from_pct: Optional[float] = Field(None, description="Desde qué % activa trailing.")
    trailing_stop_pct: Optional[float] = Field(None, description="Nuevo stop tras trailing.")


class OptionStructure(BaseModel):
    kind: StructureKind = Field(..., description="single/debit_spread/credit_spread/none")
    direction: Direction = Field(Direction.none, description="call/put/none")
    legs: List[str] = Field(default_factory=list, description="Lista textual de patas.")
    days_to_expiry: Optional[int] = Field(None, description="DTE sugeridos.")
    delta_hint: Optional[str] = Field(None, description="Ej: 0.20-0.30")


class OptionSignal(BaseModel):
    symbol: str
    strategy_code: str
    human_label: str
    time_frame: str = "5m"
    bias: Bias
    confidence: float = Field(..., ge=0.0, le=1.0)
    structure: OptionStructure
    risk: RiskPlan
    notes: List[str] = Field(default_factory=list)

    action: Action = Field(Action.wait, description="buy/sell/wait (default wait)")
    params_echo: Optional[Dict[str, Any]] = Field(None, description="Echo params (audit)")


# =====================================================
# LIBRERÍA DE ESTRATEGIAS (STOCK MODE)
# =====================================================

STRATEGY_LIBRARY: Dict[str, Dict[str, Any]] = {
    # ---------- SAFE DEFAULT ----------
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

    # ---------- STOCK: TREND (más fuerte) ----------
    "trend_stock_buy": {
        "human_label": "Stock Trend Buy – momentum confirmado",
        "time_frame": "5m",
        "confidence": 0.74,  # ajustable
        "structure": {
            "kind": "none",
            "direction": "none",
            "legs": [],
            "days_to_expiry": None,
            "delta_hint": None,
        },
        "risk": {
            "stop_loss_pct": 0.35,     # referencia informativa
            "take_profit_pct": 0.60,
            "trailing_from_pct": 0.35,
            "trailing_stop_pct": 0.25,
        },
        "notes": [
            "Entrada de acción (stock) por tendencia fuerte. Evita overtrade con umbral de confianza + cooldown."
        ],
    },

    "trend_stock_sell": {
        "human_label": "Stock Trend Sell – presión bajista confirmada",
        "time_frame": "5m",
        "confidence": 0.74,  # ajustable
        "structure": {
            "kind": "none",
            "direction": "none",
            "legs": [],
            "days_to_expiry": None,
            "delta_hint": None,
        },
        "risk": {
            "stop_loss_pct": 0.35,
            "take_profit_pct": 0.60,
            "trailing_from_pct": 0.35,
            "trailing_stop_pct": 0.25,
        },
        "notes": [
            "Entrada short/SELL en acción por tendencia fuerte. (Si tu broker no permite short, se bloqueará en /trade)."
        ],
    },

    # ---------- STOCK: SCALP/MOMO (más frecuente, menor conf) ----------
    "scalp_stock_momo_buy": {
        "human_label": "Stock Scalp Buy – impulso corto plazo",
        "time_frame": "5m",
        "confidence": 0.66,  # más permisivo
        "structure": {
            "kind": "none",
            "direction": "none",
            "legs": [],
            "days_to_expiry": None,
            "delta_hint": None,
        },
        "risk": {
            "stop_loss_pct": 0.25,
            "take_profit_pct": 0.45,
            "trailing_from_pct": 0.25,
            "trailing_stop_pct": 0.18,
        },
        "notes": [
            "Señal más ligera para aumentar frecuencia. Ajusta AI_MIN_CONFIDENCE si quieres más entradas."
        ],
    },

    "scalp_stock_momo_sell": {
        "human_label": "Stock Scalp Sell – impulso bajista corto plazo",
        "time_frame": "5m",
        "confidence": 0.66,
        "structure": {
            "kind": "none",
            "direction": "none",
            "legs": [],
            "days_to_expiry": None,
            "delta_hint": None,
        },
        "risk": {
            "stop_loss_pct": 0.25,
            "take_profit_pct": 0.45,
            "trailing_from_pct": 0.25,
            "trailing_stop_pct": 0.18,
        },
        "notes": [
            "Señal ligera para SELL/short. (Si no hay short, /trade puede fallar)."
        ],
    },
}


# =====================================================
# HELPERS (NORMALIZACIÓN)
# =====================================================

def _normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return s if s in ("QQQ", "SPY", "NVDA") else "QQQ"


def _normalize_legs(value: Any) -> List[str]:
    """
    Garantiza List[str] SIEMPRE.
    - None -> []
    - list -> [str(x)...]
    - str/int/float/dict/otros -> []
    """
    if value is None:
        return []
    if isinstance(value, list):
        out: List[str] = []
        for x in value:
            if x is None:
                continue
            out.append(str(x))
        return out
    return []


def _safe_kind(value: Any) -> str:
    v = str(value).strip() if value is not None else "none"
    return v if v in ("single", "debit_spread", "credit_spread", "none") else "none"


def _safe_direction(value: Any) -> str:
    v = str(value).strip() if value is not None else "none"
    return v if v in ("call", "put", "none") else "none"


def _infer_action(strategy_code: str, bias: Bias, confidence: float, kind: StructureKind) -> Action:
    """
    STOCK MODE: kind será "none", así que NO puede bloquear la acción.
    Anti-overtrading: solo opera si AI_ENABLE_STOCK_ACTION=true y confidence >= AI_MIN_CONFIDENCE.
    """
    if strategy_code == "no_trade" or confidence <= 0:
        return Action.wait

    import os
    enable_actions = os.getenv("AI_ENABLE_STOCK_ACTION", "false").lower() in ("1", "true", "yes")
    min_conf = float(os.getenv("AI_MIN_CONFIDENCE", "0.65"))

    if (not enable_actions) or (confidence < min_conf):
        return Action.wait

    if bias == Bias.bullish:
        return Action.buy
    if bias == Bias.bearish:
        return Action.sell

    return Action.wait


# =====================================================
# LÓGICA DE ELECCIÓN DE ESTRATEGIA (STOCK MODE)
# =====================================================

def choose_strategy_code(symbol: str, bias: str, trend_strength: int, near_extreme: bool, prefer_spreads: bool) -> str:
    """
    STOCK MODE (B):
    - Solo devuelve strategies que YA existen en STRATEGY_LIBRARY.
    - Gatilho “más fácil”:
        trend_strength <= 1 => no_trade
        trend_strength >= 2 => trend_* (si bias bullish/bearish)
    - Neutral => no_trade
    """
    if trend_strength <= 1:
        return "no_trade"

    if bias == "bullish":
        return "trend_stock_buy" if trend_strength >= 2 else "scalp_stock_momo_buy"

    if bias == "bearish":
        return "trend_stock_sell" if trend_strength >= 2 else "scalp_stock_momo_sell"

    return "no_trade"


def build_ai_signal_response(symbol: str, bias: Bias, strategy_code: str, params_echo: Optional[Dict[str, Any]] = None) -> OptionSignal:
    strategy = STRATEGY_LIBRARY.get(strategy_code, STRATEGY_LIBRARY["no_trade"])

    struct_dict = dict(strategy.get("structure") or {})
    struct_dict["legs"] = _normalize_legs(struct_dict.get("legs"))
    struct_dict["kind"] = _safe_kind(struct_dict.get("kind"))
    struct_dict["direction"] = _safe_direction(struct_dict.get("direction"))

    risk_dict = strategy.get("risk")
    if not isinstance(risk_dict, dict):
        risk_dict = {}

    notes_val = strategy.get("notes", [])
    if not isinstance(notes_val, list):
        notes_val = []
    notes_list = [str(x) for x in notes_val if x is not None]

    confidence = float(strategy.get("confidence", 0.0) or 0.0)

    structure = OptionStructure(**struct_dict)
    risk = RiskPlan(**risk_dict)
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
        notes=notes_list,
        action=action,
        params_echo=params_echo,
    )


# =====================================================
# ENDPOINT PRINCIPAL /signals/ai
# =====================================================

@router.get("/ai", response_model=OptionSignal)
def generate_ai_signal(
    symbol: str = Query("QQQ", pattern="^(QQQ|SPY|NVDA)$"),
    bias: Bias = Query(Bias.bullish),
    trend_strength: int = Query(1, ge=1, le=3),
    near_extreme: bool = Query(False),
    extreme_side: Optional[ExtremeSide] = Query(None),
    prefer_spreads: bool = Query(True),
):
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

    signal = build_ai_signal_response(symbol=sym, bias=bias, strategy_code=strategy_code, params_echo=params_echo)

    # Telegram (opcional: evitar spam en WAIT)
    try:
        import os
        notify_wait = os.getenv("AI_NOTIFY_WAIT", "false").lower() in ("1", "true", "yes")
        if signal.action != Action.wait or notify_wait:
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
