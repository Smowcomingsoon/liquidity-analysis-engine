"""Read-only guest links (admin).

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import auth
from app import guest as guest_links
from app.models import (
    User,
)
from app.web_common import _is_https, _require_admin

router = APIRouter()


class GuestLinkRequest(BaseModel):
    label: str = ""
    days: int = guest_links.DEFAULT_DAYS


def _guest_link_out(link: guest_links.GuestLink, request: Request) -> dict:
    base = f"{'https' if _is_https(request) else 'http'}://{request.headers.get('host', request.url.netloc)}"
    return {
        "id": link.id,
        "label": link.label,
        "created_at": link.created_at,
        "expires_at": link.expires_at,
        "active": link.active(),
        "url": f"{base}/ospite?t={link.token}" if link.active() else None,
    }


@router.get("/api/guest-links")


async def guest_links_list(request: Request, user: User = Depends(auth.get_current_user)) -> list[dict]:
    _require_admin(user)
    return [_guest_link_out(link, request) for link in guest_links.list_links()]


@router.post("/api/guest-links")


async def guest_links_create(
    req: GuestLinkRequest, request: Request, user: User = Depends(auth.get_current_user)
) -> dict:
    _require_admin(user)
    return _guest_link_out(guest_links.create_link(user.email, req.label, req.days), request)


@router.delete("/api/guest-links/{link_id}")


async def guest_links_revoke(link_id: str, request: Request, user: User = Depends(auth.get_current_user)) -> dict:
    _require_admin(user)
    link = guest_links.revoke_link(link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    return _guest_link_out(link, request)
