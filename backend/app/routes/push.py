"""Web push subscriptions.

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import auth
from app.models import (
    User,
)
from app.web_common import _device

router = APIRouter()


class PushSubscribeRequest(BaseModel):
    subscription: dict


@router.get("/api/push/key")


async def push_key() -> dict:
    """The VAPID public key the service worker subscribes with (web_push.py)."""
    from app.notify import web_push
    return {"key": web_push.public_key()}


@router.post("/api/push/subscribe")


async def push_subscribe(body: PushSubscribeRequest, request: Request,
                         user: User | None = Depends(auth.get_current_user_optional)) -> dict:
    from app.notify import web_push
    device = _device(request)
    try:
        web_push.subscribe(device, body.subscription, user.id if user else "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.post("/api/push/unsubscribe")


async def push_unsubscribe(request: Request) -> dict:
    from app.notify import web_push
    device = _device(request)
    if device:
        web_push.unsubscribe(device)
    return {"ok": True}
