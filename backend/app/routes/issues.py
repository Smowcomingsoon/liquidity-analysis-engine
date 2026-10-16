"""Bug board: the reports people write from the app.

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import auth
from app import guest as guest_links
from app import issues as issues_store
from app.models import (
    User,
)

router = APIRouter()


class IssueIn(BaseModel):
    title: str
    detail: str = ""
    priority: str = "media"


class IssueReplyIn(BaseModel):
    text: str
    # "risolto" lo decide chi ha scritto la segnalazione, non chi ha scritto la
    # correzione: se il problema si vede ancora, la voce torna aperta.
    reopen: bool = False


class IssuePatch(BaseModel):
    status: str | None = None
    priority: str | None = None
    detail: str | None = None


@router.post("/api/issues/{issue_id}/replies")


async def reply_to_issue(
    issue_id: str, req: IssueReplyIn, user: User = Depends(auth.get_current_user)
) -> dict:
    if not any(i["id"] == issue_id for i in issues_store.list_issues()):
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        return issues_store.add_reply(issue_id, req.text, author=user.email,
                                      reopen=req.reopen)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/issues")


async def list_issues(user: User | None = Depends(auth.get_current_user_optional)) -> dict:
    items = issues_store.list_issues()
    if user is None or guest_links.is_guest(user):
        items = _mask_emails(items)
    return {
        "items": items,
        "counts": {
            "aperto": sum(1 for i in items if i.get("status") == "aperto"),
            "in corso": sum(1 for i in items if i.get("status") == "in corso"),
            "risolto": sum(1 for i in items if i.get("status") == "risolto"),
            "riaperto": sum(1 for i in items if i.get("status") == "riaperto"),
        },
    }


_EMAIL = re.compile(r"([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _mask_emails(obj):
    """The board is public (anonymous GET): authors are stored as the account
    email, so strangers see only the first two letters of it."""
    if isinstance(obj, str):
        return _EMAIL.sub(lambda m: m.group(1) + "•••", obj)
    if isinstance(obj, list):
        return [_mask_emails(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _mask_emails(v) for k, v in obj.items()}
    return obj


@router.post("/api/issues")


async def create_issue(body: IssueIn, user: User = Depends(auth.get_current_user)) -> dict:
    try:
        return issues_store.add_issue(
            title=body.title, detail=body.detail,
            priority=body.priority, author=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/issues/{issue_id}")


async def patch_issue(issue_id: str, body: IssuePatch,
                      user: User = Depends(auth.get_current_user)) -> dict:
    try:
        updated = issues_store.update_issue(
            issue_id, status=body.status, priority=body.priority, detail=body.detail
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return updated


@router.delete("/api/issues/{issue_id}")


async def remove_issue(issue_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    if not issues_store.delete_issue(issue_id):
        raise HTTPException(status_code=404, detail="Report not found")
    return {"deleted": issue_id}
