from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal, Optional, Dict, List
from datetime import datetime

router = APIRouter(prefix="/pending-trades", tags=["pending-trades"])


class PendingTrade(BaseModel):
    id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int

    # 🔔 Disparo por precio
    trigger_price: float                  # precio a partir del cual se activa (ej: rompe 445)
    max_price: Optional[float] = None     # precio máximo aceptable (ej: hasta 446)

    # ⏰ Vigencia (opcional)
    valid_until: Optional[datetime] = None

    status: Literal["pending", "triggered", "cancelled", "expired"] = "pending"


# "Base de datos" simple en memoria
PENDING_TRADES: Dict[str, PendingTrade] = {}


@router.get("/", response_model=List[PendingTrade])
def list_pending_trades():
    """
    Lista todas las órdenes condicionales (pendientes + históricas).
    """
    return list(PENDING_TRADES.values())


@router.post("/", response_model=PendingTrade)
def add_pending_trade(trade: PendingTrade):
    """
    Agrega una nueva orden condicional.

    La idea es que el GPT llame aquí cuando tú le digas:
    'BDV, tienes autorización para entrar en QQQ si rompe 445 hasta 446 hoy'.
    """
    if trade.id in PENDING_TRADES:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe una orden condicional con id={trade.id}",
        )

    PENDING_TRADES[trade.id] = trade
    return trade


@router.post("/{trade_id}/cancel", response_model=PendingTrade)
def cancel_pending_trade(trade_id: str):
    """
    Cancela una orden condicional (status = cancelled).
    """
    existing = PENDING_TRADES.get(trade_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Orden condicional no encontrada")

    existing.status = "cancelled"
    return existing
