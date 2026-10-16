"""Price alerts on a ticker.

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import app.price_alerts as price_alerts_store
from app import auth
from app.models import (
    AssetType,
    CryptoExchange,
    PriceAlert,
    PriceAlertCondition,
    User,
)

router = APIRouter()


class PriceAlertCreateRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    condition: PriceAlertCondition
    price: float
    note: str = ""
    once_only: bool = True


@router.get("/api/price-alerts", response_model=list[PriceAlert])


async def list_price_alerts(user: User = Depends(auth.get_current_user)) -> list[PriceAlert]:
    return price_alerts_store.list_alerts(user.id)


@router.post("/api/price-alerts", response_model=PriceAlert)


async def create_price_alert(
    req: PriceAlertCreateRequest, user: User = Depends(auth.get_current_user)
) -> PriceAlert:
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    return price_alerts_store.add_alert(
        user.id, symbol, req.asset_type, req.exchange,
        req.condition, req.price, req.note, req.once_only,
    )


@router.delete("/api/price-alerts/{alert_id}")


async def delete_price_alert(alert_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    a = price_alerts_store.get_alert(alert_id)
    if a is None or a.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    price_alerts_store.remove_alert(alert_id)
    return {"ok": True}


class PriceAlertActiveRequest(BaseModel):
    active: bool


@router.patch("/api/price-alerts/{alert_id}/active", response_model=PriceAlert)


async def set_price_alert_active(
    alert_id: str, req: PriceAlertActiveRequest, user: User = Depends(auth.get_current_user)
) -> PriceAlert:
    a = price_alerts_store.get_alert(alert_id)
    if a is None or a.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    return price_alerts_store.set_active(alert_id, req.active)
