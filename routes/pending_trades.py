from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import datetime
import json
import os

router = APIRouter(prefix="/pending-trades", tags=["pending-trades"])

# Ruta CORRECTA con permisos de escritura en Render
DATA_DIR = "/opt/render/project/src/data"
os.makedirs(DATA_DIR, exist_ok=True)

PENDING_TRADES_FILE = f"{DATA_DIR}/pending_trades.json"


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


# ---------------------------
# Funciones de persistencia
# ---------------------------

def load_pending_trades() -> List[PendingTrade]:
    if not os.path.isfile(PENDING_TRADES_FILE):
        return []

    with open(PENDING_TRADES_FILE, "r") as f:
        try:
            data = json.load(f)
            return [PendingTrade(**item) for item in data]
        except:
            return []


def save_pending_trades(data: List[PendingTrade]):
    with open(PENDING_TRADES_FILE, "w") as f:
        json.dump([t.dict() for t in data], f, indent=4)


# Cargar en memoria al iniciar
PENDING_TRADES: List[PendingTrade] = load_pending_trades()


# ---------------------------
# ENDPOINTS
# ---------------------------

@router.get("/", response_model=List[PendingTrade])
def list_pending_trades():
    return PENDING_TRADES


@router.post("/", response_model=PendingTrade)
def add_pending_trade(trade: PendingTrade):
    for t in PENDING_TRADES:
        if t.id == trade.id:
            raise HTTPException(
                status_code=400,
                detail=f"Ya existe una orden condicional con id={trade.id}",
            )

    PENDING_TRADES.append(trade)
    save_pending_trades(PENDING_TRADES)
    return trade


@router.post("/{trade_id}/cancel", response_model=PendingTrade)
def cancel_pending_trade(trade_id: str):
    for t in PENDING_TRADES:
        if t.id == trade_id:
            t.status = "cancelled"
            save_pending_trades(PENDING_TRADES)
            return t

    raise HTTPException(
        status_code=404,
        detail="Orden condicional no encontrada",
    )
