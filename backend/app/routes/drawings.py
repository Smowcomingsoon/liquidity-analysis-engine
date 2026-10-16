"""Chart drawings saved per user.

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import auth
from app import drawings as drawings_store
from app.models import (
    User,
)

router = APIRouter()


class DrawingsIn(BaseModel):
    key: str
    drawings: list = []
    updated_at: int | None = None


@router.get("/api/drawings")


async def get_drawings(key: str = Query(..., max_length=140),
                       user: User = Depends(auth.get_current_user)) -> dict:
    """The account's drawings for one chart (drawing tools plan, phase 4)."""
    if not drawings_store.valid_key(key):
        raise HTTPException(status_code=400, detail="Invalid key")
    return drawings_store.get(user.id, key) or {"drawings": None, "updated_at": 0}


@router.put("/api/drawings")


async def put_drawings(body: DrawingsIn, user: User = Depends(auth.get_current_user)) -> dict:
    try:
        return drawings_store.put(user.id, body.key, body.drawings, body.updated_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
