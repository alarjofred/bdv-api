from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import datetime

router = APIRouter(prefix="/pending-trades", tags=["pending-trades"])


class PendingTrade(BaseModel):
    id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int
    trigger_type: Literal["price_breakout"]
    trigger_price: float
    max_price: Optional[float] = None
    valid_until: Optional[datetime] = None
    risk_mode: Literal["low", "medium", "high"] = "medium"
    status: Literal["pending", "triggered", "cancelled", "expired"] = "pending"


# "Base de datos" simple en memoria
PENDING_TRADES: List[PendingTrade] = []


@router.get("/", response_model=List[PendingTrade])
def list_pending_trades():
    """
    Lista todas las órdenes condicionales (pendientes y con otro estado).
    """
    return PENDING_TRADES


@router.post("/", response_model=PendingTrade)
def add_pending_trade(trade: PendingTrade):
    """
    Agrega una nueva orden condicional.
    """
    for t in PENDING_TRADES:
        if t.id == trade.id:
            raise HTTPException(
                status_code=400,
                detail=f"Ya existe una orden condicional con id={trade.id}",
            )

    PENDING_TRADES.append(trade)
    return trade


@router.post("/{trade_id}/cancel", response_model=PendingTrade)
def cancel_pending_trade(trade_id: str):
    """
    Marca una orden condicional como cancelada.
    """
    for t in PENDING_TRADES:
        if t.id == trade_id:
            t.status = "cancelled"
            return t

    raise HTTPException(
        status_code=404,
        detail="Orden condicional no encontrada",
    )
