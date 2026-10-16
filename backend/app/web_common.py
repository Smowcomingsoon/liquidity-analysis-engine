"""Small helpers shared by app.main and the route modules in app/routes/.

Moved out of main.py (overhaul plan, phase 5) so a route module can use
them without importing main, which includes the routers.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app import guest as guest_links
from app.config import email_allowlist, settings
from app.models import User
from app.trading.futures import registry as futures_registry

logger = logging.getLogger("app.main")

# L'elenco degli exchange futures vive nel registro degli adattatori: qui
# c'e' solo il nome vecchio, perche' non ci sia una seconda lista che possa
# essere in disaccordo con la prima. Il perimetro (leva accessibile
# dall'Europa) e' spiegato in app/trading/futures/registry.py.
SUPPORTED_FUTURES_EXCHANGES_ORDERED = futures_registry.SUPPORTED_FUTURES_EXCHANGES

# Audit B04, rest: a one-shot task (a Telegram message) nobody holds can be
# garbage-collected before it runs, and its error is never seen. Hold it
# until it ends and log what it raised.
_fired: set[asyncio.Task] = set()


def _fire(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _fired.add(task)
    task.add_done_callback(_fired_done)
    return task


def _fired_done(task: asyncio.Task) -> None:
    _fired.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("background send failed", exc_info=task.exception())


def _telegram_allowed(user: User) -> bool:
    allowed = email_allowlist(settings.telegram_allowed_emails)
    return not allowed or user.email.lower() in allowed


def _user_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "telegram_chat_id": user.telegram_chat_id,
        "telegram_signal_chat_id": getattr(user, "telegram_signal_chat_id", ""),
        "guest": guest_links.is_guest(user),
    }


def _client_ip(request: Request) -> str:
    # Fly's edge proxy sets this; request.client.host would otherwise be
    # the internal proxy address, not the real caller.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    # Read the header directly rather than trusting uvicorn's own
    # proxy-header handling (which needs explicit --forwarded-allow-ips
    # config to kick in) — Fly's proxy always sets this correctly.
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded_proto == "https" if forwarded_proto else request.url.scheme == "https"


def _device(request: Request) -> str:
    """The browser that launched a job (X-Device, a random id the app keeps):
    the one its push notification goes to."""
    from app.notify import web_push
    d = request.headers.get("x-device", "")
    return d if web_push.valid_device(d) else ""


def _is_admin(user: User) -> bool:
    allowed = email_allowlist(settings.admin_emails)
    return not guest_links.is_guest(user) and user.email.lower() in allowed


def _require_admin(user: User) -> None:
    allowed = email_allowlist(settings.admin_emails)
    if guest_links.is_guest(user) or user.email.lower() not in allowed:
        raise HTTPException(status_code=403, detail="Only an admin can manage guest links.")


class WatchlistStrategyToggleRequest(BaseModel):
    strategy_name: str
    enabled: bool
