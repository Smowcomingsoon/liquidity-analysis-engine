"""Background jobs a browser started, and their results.

Moved out of app/main.py (overhaul plan, phase 5); same paths, same code.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import auth
from app import jobs as jobs_store
from app.models import (
    User,
)
from app.web_common import _device

router = APIRouter()


@router.post("/api/jobs/{job_id}/cancel")


async def cancel_job(
    job_id: str, user: User | None = Depends(auth.get_current_user_optional)
) -> dict:
    """Ferma una ricerca in corso.

    Segna soltanto la richiesta: il lavoro se ne accorge al backtest successivo,
    quindi si ferma entro una frazione di secondo invece che a fase finita.
    """
    if not jobs_store.request_cancel(job_id, owner_id=user.id if user else None):
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return {"ok": True}


@router.get("/api/jobs/{job_id}")


async def job_status(
    job_id: str,
    since: int = Query(0, ge=0),
    visible: bool = False,
    user: User | None = Depends(auth.get_current_user_optional),
) -> dict:
    """`visible=1`: the page asking is on screen, so no push for this job if it
    ends in the next seconds (jobs.watch)."""
    job = jobs_store.get(job_id, owner_id=user.id if user else None, since=since)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    if visible:
        jobs_store.watch(job_id)
    return job


@router.post("/api/jobs/{job_id}/away")


async def job_away(job_id: str, user: User | None = Depends(auth.get_current_user_optional)) -> dict:
    """The page left the screen (sendBeacon on pagehide/visibilitychange):
    the job's end goes out as a push even if it was polled a second ago."""
    if jobs_store.get(job_id, owner_id=user.id if user else None) is not None:
        jobs_store.away(job_id)
    return {"ok": True}


@router.get("/api/jobs")


async def jobs_of_device(request: Request, user: User | None = Depends(auth.get_current_user_optional)) -> dict:
    """This browser's jobs of the last day (X-Device): what the app resumes
    when it is reopened without tapping the notification."""
    device = _device(request)
    return {"jobs": jobs_store.recent(device, owner_id=user.id if user else None) if device else []}
