"""
Lavori lunghi seguiti da fuori, invece che dentro una richiesta HTTP.

L'ottimizzazione "stabile" puo' durare minuti. Tenuta dentro una GET, quella
richiesta deve restare aperta per tutto il tempo, e non e' una cosa che
sopravvive alla catena: il proxy davanti all'app la chiude, il browser la
lascia cadere, e quello che torna non e' piu' JSON ma una pagina d'errore.
Safari, a quel punto, prova a leggerla come JSON e dice "The string did not
match the expected pattern" — il messaggio arrivato sulla bacheca, che non
parla di ottimizzazione perche' non e' l'ottimizzazione ad aver fallito.

Qui la richiesta apre un lavoro e torna subito. Il lavoro va avanti per conto
suo e accumula righe di log; il client le chiede quando vuole. Nessuna
richiesta lunga, e in piu' si vede cosa sta succedendo — che era la seconda
meta' della segnalazione: capire se sta lavorando.

I lavori dell'app on-chain (ricontrollo DeepSeek, scansione manuale, domanda
all'agente) hanno anche un `device`: il browser che li ha lanciati, a cui
arriva una notifica push alla fine (Lorenzo, 01/10: "qualsiasi task [...] deve
continuare in background [...] e arrivarmi una notifica al completamento").
Quelli si salvano su disco, finiti compresi, per un giorno: chi riapre l'app
la mattina dopo ritrova il risultato. Un lavoro in corso al riavvio (deploy)
non lo porta avanti nessuno: si segna "fallito" e lo si dice con la notifica,
invece di lasciarlo "in corso" per sempre.

Nessuna notifica se l'app lo sta guardando: il client che interroga il lavoro
con la pagina visibile lo segna come seguito (`watch`), e chi lascia la
pagina lo dice (`away`). Una notifica per un risultato gia' sullo schermo e'
rumore.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

State = Literal["in corso", "finito", "fallito", "annullato"]

_MAX_LOG_LINES = 400
_KEEP_FINISHED_S = 900          # un quarto d'ora: il tempo di leggere il risultato
_MAX_JOBS = 60

_KEEP_DEVICE_S = 24 * 3600      # i lavori dell'app: il risultato si ritrova il giorno dopo
_WATCH_S = 5.0                  # interrogato da una pagina visibile entro 5 s = la notifica non serve
_FILE = Path(__file__).resolve().parent.parent / "data" / "app_jobs.json"
_VIEW_RE = re.compile(r"^[\w\-:.&=%,]{0,300}$")

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_loaded = False
_notify: dict[str, Callable[[dict], tuple[str, str]]] = {}
_tasks: set[asyncio.Task] = set()


def _keep(j: dict) -> float:
    return _KEEP_DEVICE_S if j.get("device") else _KEEP_FINISHED_S


def _save() -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(_FILE, json.dumps([j for j in _jobs.values() if j.get("device")]))
    except OSError:
        logger.exception("app jobs not saved")


def _ensure_loaded() -> None:
    """Al primo uso: i lavori dell'app salvati. Quelli rimasti "in corso" li ha
    interrotti un riavvio."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        saved = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    lost = []
    for j in saved if isinstance(saved, list) else []:
        if not isinstance(j, dict) or "id" not in j:
            continue
        if j.get("state") == "in corso":
            j.update(state="fallito", error="interrupted by a server update, run it again",
                     finished_at=time.time())
            lost.append(j)
        _jobs[j["id"]] = j
    if lost:
        _save()
        for j in lost:
            _push(j)


def _prune() -> None:
    now = time.time()
    stale = [
        jid for jid, j in _jobs.items()
        if j["state"] != "in corso" and now - j["finished_at"] > _keep(j)
    ]
    for jid in stale:
        del _jobs[jid]
    if len(_jobs) > _MAX_JOBS:
        oldest = sorted(_jobs.items(), key=lambda kv: kv[1]["created_at"])
        for jid, _ in oldest[: len(_jobs) - _MAX_JOBS]:
            del _jobs[jid]


def clean_view(view: str | None) -> str:
    """Dove l'app era quando il lavoro e' partito: il frammento dell'URL
    (`gems&token=solana:ABC&tab=why`), che la notifica riapre. Solo caratteri
    da URL: e' testo che arriva dal client e finisce in un link."""
    view = (view or "").lstrip("#")
    return view if _VIEW_RE.match(view) else ""


def create(owner_id: str, kind: str, title: str, device: str = "", view: str = "",
           page: str = "/onchain-app.html") -> str:
    with _lock:
        _ensure_loaded()
        _prune()
        jid = uuid.uuid4().hex[:12]
        _jobs[jid] = {
            "id": jid,
            "owner_id": owner_id,
            "kind": kind,
            "title": title,
            "state": "in corso",
            "created_at": time.time(),
            "finished_at": 0.0,
            "log": [],
            "result": None,
            "error": "",
            "device": device,
            "view": clean_view(view),
            "page": page,
            "watched_at": 0.0,
        }
        if device:
            _save()
    return jid


def link(job: dict) -> str:
    """L'indirizzo che la notifica apre: la pagina, il lavoro, la vista."""
    return f"{job.get('page') or '/onchain-app.html'}?job={job['id']}" + (f"#{job['view']}" if job.get("view") else "")


def on_done(kind: str, describe: Callable[[dict], tuple[str, str]]) -> None:
    """Titolo e testo della notifica di un tipo di lavoro, dal lavoro finito."""
    _notify[kind] = describe


def _push(job: dict) -> None:
    if not job.get("device"):
        return
    if time.time() - job.get("watched_at", 0.0) < _WATCH_S:
        return
    try:
        title, body = _notify.get(job["kind"], lambda j: (j["title"], ""))(job)
    except Exception:
        logger.exception("job notification text")
        title, body = job["title"], ""
    if job["state"] == "fallito" and not body:
        body = job.get("error") or "Failed"
    from app.notify import web_push
    web_push.send(job["device"], title, body, link(job), tag=f"job-{job['id']}")


def watch(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["watched_at"] = time.time()


def away(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["watched_at"] = 0.0


def run(job_id: str, work: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    """Il lavoro su un task asyncio che non dipende dalla richiesta: la
    risposta HTTP torna subito, il client puo' chiudere l'app."""
    async def go() -> None:
        try:
            finish(job_id, await work())
        except Exception as exc:
            logger.exception("app job %s failed", job_id)
            fail(job_id, getattr(exc, "detail", None) if isinstance(getattr(exc, "detail", None), str)
                 else f"{type(exc).__name__}: {exc}"[:300])

    task = asyncio.create_task(go())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def recent(device: str, owner_id: str | None = None, limit: int = 10) -> list[dict]:
    """I lavori di un browser, piu' recenti prima, senza log e risultato."""
    with _lock:
        _ensure_loaded()
        _prune()
        mine = [j for j in _jobs.values() if device and j.get("device") == device
                and (owner_id is None or j["owner_id"] in ("", owner_id))]
        mine.sort(key=lambda j: j["created_at"], reverse=True)
        return [{"id": j["id"], "kind": j["kind"], "title": j["title"], "state": j["state"],
                 "view": j.get("view", ""), "created_at": j["created_at"], "finished_at": j["finished_at"]}
                for j in mine[:limit]]


def log(job_id: str, message: str) -> None:
    """Una riga di avanzamento. Chiamata dal thread di lavoro, quindi sotto lock.

    Il tetto sulle righe non e' cosmetico: una ricerca puo' emetterne migliaia,
    e una lista che cresce senza limite dentro un processo che serve anche le
    altre richieste e' un modo lento di far cadere il server. Si tengono le
    ultime, che sono quelle che dicono a che punto siamo.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["log"].append({"t": round(time.time() - job["created_at"], 1), "m": message[:300]})
        if len(job["log"]) > _MAX_LOG_LINES:
            del job["log"][: len(job["log"]) - _MAX_LOG_LINES]


def finish(job_id: str, result: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["state"] = "finito"
        job["result"] = result
        job["finished_at"] = time.time()
        if job.get("device"):
            _save()
            _push(job)


def fail(job_id: str, error: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["state"] = "fallito"
        job["error"] = error[:600]
        job["finished_at"] = time.time()
        if job.get("device"):
            _save()
            _push(job)


def cancelled(job_id: str) -> dict | None:
    """Fermata su richiesta: non e' un fallimento e non va raccontata come tale.

    Un lavoro fermato apposta che compare come "fallito" fa cercare un guasto
    che non c'e'.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job["state"] = "annullato"
        job["finished_at"] = time.time()
        return job


def get(job_id: str, owner_id: str | None = None, since: int = 0) -> dict | None:
    """Lo stato piu' le righe di log dalla posizione `since` in poi.

    Il client manda quante ne ha gia': cosi' ogni richiesta porta solo le
    novita' invece di ritrasmettere tutto il log a ogni giro.
    """
    with _lock:
        _ensure_loaded()
        job = _jobs.get(job_id)
        if job is None:
            return None
        if owner_id is not None and job["owner_id"] not in ("", owner_id):
            return None
        lines = job["log"][max(0, since):]
        return {
            "id": job["id"],
            "kind": job["kind"],
            "title": job["title"],
            "state": job["state"],
            "elapsed_s": round((job["finished_at"] or time.time()) - job["created_at"], 1),
            "log": lines,
            "log_total": len(job["log"]),
            "result": job["result"] if job["state"] == "finito" else None,
            "error": job["error"],
            "view": job.get("view", ""),
        }


def cancel_requested(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        return bool(job and job.get("cancel"))


def request_cancel(job_id: str, owner_id: str | None = None) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or (owner_id is not None and job["owner_id"] not in ("", owner_id)):
            return False
        job["cancel"] = True
        return True
