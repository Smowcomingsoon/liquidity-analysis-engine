"""The gem research in one run a night, at 22:00 Europe/Rome.

Lorenzo, 07/10: "non lasciare sempre in ascolto il bot che scansiona
costantemente [...] tutto in una tranche a fine serata". Until then five
loops ran around the clock: the board's tick every 10 s (discovery feeds,
market polls, pillars, DeepSeek judge/review/code readings, ranking,
alerts), the weekly re-check of the Top 50, outcomes every 10 minutes,
layers once a day and DeepSeek studies every 5 minutes. Each of them paid
APIs and DeepSeek all day for a board that people read in the evening.

Now, once a night and in this order:
1. the board's ticks until its queue is empty (`leaderboard.drained`) or
   RESEARCH_SHARE of the window is spent;
2. layers, outcomes once;
3. the weekly re-check of the Top for the tokens it still owes, until the
   window closes;
4. DeepSeek studies once;
then one Telegram message in Italian with what changed.

The run is remembered in `data/onchain_nightly.json`: a deploy at 22:30
resumes the night (within its window, MAX_ATTEMPTS times at most), a
server that boots at 23:00 with tonight not run runs it now, one that
boots at 15:00 waits for 22:00 (CATCHUP_S).

What stays immediate: the manual scan and the per-token re-check run their
own steps; a project added by hand is analysed at once
(`leaderboard.analyze_now`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from app.atomic_write import write_text_atomic
from app.config import settings
from app.trading.onchain import storage

logger = logging.getLogger(__name__)

ROME = ZoneInfo("Europe/Rome")
_FILE = storage.data_file("onchain_nightly.json")
START_DELAY_S = 60              # let the server come up first
CATCHUP_S = 8 * 3600            # a night missed is caught up until 06:00, not in the afternoon
MAX_ATTEMPTS = 3                # a run that keeps crashing the server stops being resumed
RESUME_MIN_S = 10 * 60          # a resumed run gets at least this, even past its window
RESEARCH_SHARE = 0.75           # the board's ticks leave the rest of the window to the re-check
MIN_TICKS = 30                  # 5 minutes: the readings spawned by the tick get their turns
SETTLE_S = 300                  # readings still running when the ticks stop
RECHECK_GAP_S = 5               # between two tokens of the re-check

_state: dict = {}
_running = False


def _load() -> None:
    try:
        _state.update(json.loads(_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        write_text_atomic(_FILE, json.dumps(_state, ensure_ascii=False))
    except OSError:
        logger.exception("nightly: state not saved")


def reset_for_tests() -> None:
    global _running
    _state.clear()
    _running = False


# --------------------------------------------------------------- the clock

def _slot_on(day, hour: int) -> float:
    return datetime(day.year, day.month, day.day, hour, tzinfo=ROME).timestamp()


def latest_slot(now_ts: float, hour: int | None = None) -> float:
    """The most recent hour:00 Europe/Rome at or before now."""
    hour = settings.onchain_nightly_hour if hour is None else hour
    today = datetime.fromtimestamp(now_ts, ROME).date()
    slot = _slot_on(today, hour)
    return slot if slot <= now_ts else _slot_on(today - timedelta(days=1), hour)


def next_run(now_ts: float, hour: int | None = None) -> float:
    """The next hour:00 Europe/Rome strictly after now (DST included)."""
    hour = settings.onchain_nightly_hour if hour is None else hour
    today = datetime.fromtimestamp(now_ts, ROME).date()
    slot = _slot_on(today, hour)
    return slot if slot > now_ts else _slot_on(today + timedelta(days=1), hour)


def due_slot(now_ts: float, state: dict, hour: int | None = None) -> float | None:
    """The night to run now, or None. Tonight counts until CATCHUP_S after
    the hour: a server booting at 23:00 runs it, one at 15:00 waits."""
    slot = latest_slot(now_ts, hour)
    if (state.get("done_slot") or 0) >= slot or now_ts - slot > CATCHUP_S:
        return None
    if state.get("slot") == slot and (state.get("attempts") or 0) >= MAX_ATTEMPTS:
        return None
    return slot


def status(now: float | None = None) -> dict:
    """For the board's meta: `nightly: {last_run, next_run, summary}`."""
    if not _state:
        _load()
    now = now or time.time()
    return {"enabled": settings.onchain_nightly_enabled, "running": _running,
            "last_run": _state.get("last_run"), "next_run": next_run(now),
            "summary": _state.get("summary")}


# --------------------------------------------------------------- the steps

class Steps:
    """What one night does, one method per step: tests swap it for a fake."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def tick(self) -> None:
        from app.trading.onchain import leaderboard
        await leaderboard.tick(self.client)

    def drained(self) -> bool:
        from app.trading.onchain import leaderboard
        return leaderboard.drained()

    def backlog(self) -> dict:
        from app.trading.onchain import leaderboard
        return leaderboard.backlog()

    async def settle(self, timeout: float) -> None:
        """Readings the ticks spawned finish before the client closes."""
        from app.trading.onchain import leaderboard
        pending = set(leaderboard._tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        for t in pending:
            t.cancel()
        if leaderboard._dirty:
            leaderboard._save()

    def counters(self) -> dict:
        from app.trading.onchain import discovery, leaderboard
        d = discovery.status()
        return {"discovered": d.get("found") or 0, "skipped": d.get("skipped") or 0,
                "examined": leaderboard._state.get("examined") or 0, "analysed": leaderboard._counts["analysed"]}

    async def layers(self) -> int:
        from app.trading.onchain import layers
        return await layers.run(self.client)

    async def outcomes(self) -> None:
        from app.trading.onchain import outcomes
        await outcomes.tick()

    def next_recheck(self, keys: list[str]) -> str | None:
        from app.trading.onchain import deep_recheck
        return deep_recheck.next_in_sweep(keys, time.time())

    async def recheck(self, key: str) -> str:
        from app.trading.onchain import deep_recheck
        return await deep_recheck.sweep_one(key, self.client)

    async def studies(self) -> int:
        from app.notify import deepseek_studies
        return len(await deepseek_studies.tick())

    def deepseek_calls(self, start: float, end: float) -> int:
        from app.notify import deepseek_calls
        return deepseek_calls.count_between(start, end)


def _top_map(top: Callable[[], list[dict]]) -> dict[str, str]:
    try:
        return {f"{e['chain']}:{e['token_address']}": e.get("symbol") or "?" for e in top()}
    except Exception:
        logger.exception("nightly: Top not readable")
        return {}


async def _duels(top: Callable[[], list[dict]], now: float) -> int:
    """The arenas' ladder: one night of duels between peers (arenas.py)."""
    from app.trading.onchain import arenas
    return arenas.record_night(top(), now)


async def _guard(name: str, coro: Awaitable, default=None):
    try:
        return await coro
    except Exception:
        logger.exception("nightly: %s failed", name)
        return default


async def run_once(top: Callable[[], list[dict]], steps: Steps, deadline: float, *,
                   clock: Callable[[], float] = time.time, sleep=asyncio.sleep,
                   top_before: dict[str, str] | None = None) -> dict:
    """One night. Returns the summary; stops at `deadline` at the latest
    (the studies, one bounded pass, run even after it). `top_before` is the
    Top as the night found it, kept across a resume after a deploy."""
    from app.trading.onchain import leaderboard
    t0 = clock()
    before = steps.counters()
    top_before = _top_map(top) if top_before is None else top_before
    research_end = t0 + (deadline - t0) * RESEARCH_SHARE
    ticks, stopped = 0, "drained"
    while True:
        if clock() >= research_end:
            stopped = "window"
            break
        await _guard("tick", steps.tick())
        ticks += 1
        await sleep(leaderboard.TICK_S)
        if ticks >= MIN_TICKS and steps.drained():
            break
    await _guard("settle", steps.settle(max(30.0, min(SETTLE_S, deadline - clock()))))
    left = steps.backlog()
    n_layers = await _guard("layers", steps.layers(), 0)
    await _guard("outcomes", steps.outcomes())
    rechecked = 0
    keys = list(_top_map(top))
    while clock() < deadline:
        key = steps.next_recheck(keys)
        if key is None:
            break
        res = await _guard("re-check", steps.recheck(key), "down")
        if res == "down":
            break
        rechecked += res == "done"
        await sleep(RECHECK_GAP_S)
    else:
        stopped = "window"
    studies = await _guard("studies", steps.studies(), 0)
    duels = await _guard("duels", _duels(top, clock()), 0)
    t1 = clock()
    after, top_after = steps.counters(), _top_map(top)
    return {
        "started": t0, "ended": t1, "minutes": round((t1 - t0) / 60, 1), "stopped": stopped, "ticks": ticks,
        **{k: after[k] - before.get(k, 0) for k in after},
        "entered": [top_after[k] for k in top_after if k not in top_before],
        "left": [top_before[k] for k in top_before if k not in top_after],
        "top": len(top_after), "layers": n_layers or 0, "rechecked": rechecked, "studies": studies or 0,
        "duels": duels or 0,
        "deepseek_calls": steps.deepseek_calls(t0, t1), "backlog": left,
    }


def text_it(s: dict) -> str:
    """The Telegram message: what the night did, in Italian."""
    def names(xs: list[str]) -> str:
        return ", ".join(xs[:15]) + (f" e altri {len(xs) - 15}" if len(xs) > 15 else "") if xs else "nessuno"
    lines = [f"Ricerca serale finita in {s.get('minutes', 0):g} min.",
             f"Progetti visti dalla scoperta: {s.get('discovered', 0) + s.get('skipped', 0)} "
             f"(tenuti {s.get('discovered', 0)}), entrati in esame: {s.get('examined', 0)}, "
             f"analizzati: {s.get('analysed', 0)}.",
             f"Entrati nelle arene: {names(s.get('entered') or [])}.",
             f"Usciti dalle arene: {names(s.get('left') or [])}.",
             f"Layer aggiornati: {s.get('layers', 0)}; ricontrolli DeepSeek: {s.get('rechecked', 0)}; "
             f"studi: {s.get('studies', 0)}; duelli della scala: {s.get('duels', 0)}.",
             f"Chiamate DeepSeek: {s.get('deepseek_calls', 0)}."]
    if s.get("resumed"):
        lines.append("Ripresa dopo un riavvio del server: i conteggi coprono solo l'ultima parte, "
                     "entrati e usciti tutta la sera.")
    if s.get("stopped") == "window":
        left = sum((s.get("backlog") or {}).values())
        lines.append(f"Fermata al limite di {settings.onchain_nightly_max_hours:g} ore"
                     + (f": restano {left} lavori in coda per domani." if left else "."))
    return "\n".join(lines)


async def _send(text: str) -> None:
    from app.notify import telegram
    await telegram.send_message(text)


async def run_night(slot: float, top: Callable[[], list[dict]], *, steps_factory=Steps,
                    clock: Callable[[], float] = time.time, sleep=asyncio.sleep, send=_send) -> dict:
    """Runs the night of `slot`, remembering it so that a restart neither
    reruns nor skips it."""
    global _running
    now = clock()
    if _state.get("slot") != slot:
        _state.update(slot=slot, first_start=now, attempts=0, top_before=_top_map(top))
    _state["attempts"] = (_state.get("attempts") or 0) + 1
    _save()
    deadline = max(_state["first_start"] + settings.onchain_nightly_max_hours * 3600, now + RESUME_MIN_S)
    _running = True
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0 (LiquidityEngine)"}) as client:
            summary = await run_once(top, steps_factory(client), deadline, clock=clock, sleep=sleep,
                                     top_before=_state.get("top_before"))
    except asyncio.CancelledError:
        # A clean stop (a deploy) is not a failed try: only crashes count
        # towards MAX_ATTEMPTS, so evening deploys never drop the night.
        _state["attempts"] = max(0, (_state.get("attempts") or 1) - 1)
        _save()
        raise
    finally:
        _running = False
    summary["resumed"] = _state["first_start"] < now
    _state.update(done_slot=slot, last_run=summary["ended"], summary=summary)
    _save()
    try:
        await send(text_it(summary))
    except Exception:
        logger.exception("nightly: Telegram summary not sent")
    return summary


async def _gave_up(now: float, send) -> None:
    """One message when a night crashed MAX_ATTEMPTS times: never silent."""
    slot = latest_slot(now)
    if _state.get("slot") != slot or (_state.get("done_slot") or 0) >= slot or _state.get("gave_up") == slot \
            or (_state.get("attempts") or 0) < MAX_ATTEMPTS:
        return
    _state["gave_up"] = slot
    _save()
    try:
        await send(f"Ricerca serale interrotta: e' fallita {MAX_ATTEMPTS} volte stasera, riprova domani alle "
                   f"{settings.onchain_nightly_hour}:00. Vedi i log del server.")
    except Exception:
        logger.exception("nightly: give-up note not sent")


async def nightly_loop(top: Callable[[], list[dict]], *, clock: Callable[[], float] = time.time,
                       sleep=asyncio.sleep, steps_factory=Steps, send=_send) -> None:
    """Sleeps until the night's hour, runs it, sleeps again. `top()` gives
    the Top 50 entries (main._top_entries)."""
    from app.trading.onchain import leaderboard
    _load()
    await sleep(START_DELAY_S)
    leaderboard.startup_requeue()
    try:                            # a night due now measures usage itself
        if due_slot(clock(), _state) is None:
            await leaderboard.warm_usage()
    except Exception:
        logger.exception("nightly: usage warm-up failed")
    while True:
        try:
            now = clock()
            slot = due_slot(now, _state)
            if slot is None:
                await _gave_up(now, send)
                # At most an hour at a time: a changed clock or setting is seen soon.
                await sleep(min(max(next_run(now) - now, 1.0), 3600.0))
                continue
            await run_night(slot, top, steps_factory=steps_factory, clock=clock, sleep=sleep, send=send)
        except Exception:
            logger.exception("nightly: run failed")
            await sleep(600)


def loop_plan(radar_enabled: bool, nightly_enabled: bool) -> list[str]:
    """Which research loops the server starts (main.lifespan). The nightly
    run replaces the five around-the-clock loops; without the radar there is
    no gem research, and the studies keep their own loop as before."""
    if not radar_enabled:
        return ["studies"]
    if nightly_enabled:
        return ["nightly"]
    return ["leaderboard", "sweep", "outcomes", "layers", "studies"]
