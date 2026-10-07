"""The nightly research run (nightly.py): the clock, the catch-up after a
restart, the loop choice in lifespan and the window that stops the run."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.trading.onchain import nightly

ROME = ZoneInfo("Europe/Rome")


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ROME).timestamp()


def rome(t):
    return datetime.fromtimestamp(t, ROME)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(nightly, "_FILE", tmp_path / "onchain_nightly.json")
    monkeypatch.setattr(settings, "onchain_nightly_hour", 22)
    monkeypatch.setattr(settings, "onchain_nightly_max_hours", 2.0)
    nightly.reset_for_tests()
    yield
    nightly.reset_for_tests()


def test_next_run_around_the_hour():
    assert nightly.next_run(ts(2026, 10, 7, 21, 59)) == ts(2026, 10, 7, 22)
    # exactly at 22:00 the next one is tomorrow's
    assert nightly.next_run(ts(2026, 10, 7, 22)) == ts(2026, 10, 8, 22)
    assert nightly.next_run(ts(2026, 10, 7, 23, 30)) == ts(2026, 10, 8, 22)
    assert nightly.next_run(ts(2026, 10, 8, 1)) == ts(2026, 10, 8, 22)


@pytest.mark.parametrize("before,after", [((2026, 10, 24), (2026, 10, 25)),   # summer time ends
                                          ((2026, 3, 28), (2026, 3, 29))])    # summer time starts
def test_next_run_across_dst(before, after):
    nxt = nightly.next_run(ts(*before, 22, 30))
    assert rome(nxt).hour == 22 and rome(nxt).date() == datetime(*after).date()
    # the day is 25 or 23 hours long: the local hour stays 22:00
    assert nxt - ts(*before, 22) in (25 * 3600, 23 * 3600)


def test_boot_after_the_hour_runs_tonight_now():
    slot = ts(2026, 10, 7, 22)
    assert nightly.due_slot(ts(2026, 10, 7, 23), {}) == slot
    assert nightly.due_slot(ts(2026, 10, 8, 1), {"done_slot": ts(2026, 10, 6, 22)}) == slot
    # tonight already done: nothing until tomorrow
    assert nightly.due_slot(ts(2026, 10, 7, 23), {"done_slot": slot}) is None
    # a boot in the afternoon does not catch up last night: it waits for 22:00
    assert nightly.due_slot(ts(2026, 10, 8, 15), {}) is None
    assert nightly.due_slot(ts(2026, 10, 7, 21, 59), {}) is None
    # an interrupted run is resumed, but not forever
    assert nightly.due_slot(ts(2026, 10, 7, 22, 40), {"slot": slot, "attempts": 1}) == slot
    assert nightly.due_slot(ts(2026, 10, 7, 22, 40), {"slot": slot, "attempts": nightly.MAX_ATTEMPTS}) is None


def test_loop_plan_follows_the_flag():
    assert nightly.loop_plan(True, True) == ["nightly"]
    assert nightly.loop_plan(True, False) == ["leaderboard", "sweep", "outcomes", "layers", "studies"]
    assert nightly.loop_plan(False, True) == ["studies"]
    assert nightly.loop_plan(False, False) == ["studies"]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeSteps:
    """Never drained: only the window can stop the ticks."""

    def __init__(self, clock, drained_after=None, recheck_keys=()):
        self.clock, self.ticks, self.drained_after = clock, 0, drained_after
        self.order, self.left = [], list(recheck_keys)
        self.c = {"discovered": 0, "skipped": 0, "examined": 0, "analysed": 0}

    async def tick(self):
        self.ticks += 1
        self.c["analysed"] += 2
        self.clock.t += 20          # a tick takes time too

    def drained(self):
        return self.drained_after is not None and self.ticks >= self.drained_after

    def backlog(self):
        return {"running": 0, "unanalysed": 0, "database": 0 if self.drained() else 7, "discovery": 0}

    async def settle(self, timeout):
        self.order.append("settle")

    def counters(self):
        return dict(self.c)

    async def layers(self):
        self.order.append("layers")
        return 3

    async def outcomes(self):
        self.order.append("outcomes")

    def next_recheck(self, keys):
        return self.left[0] if self.left else None

    async def recheck(self, key):
        self.order.append("recheck")
        self.left.pop(0)
        self.clock.t += 600
        return "done"

    async def studies(self):
        self.order.append("studies")
        return 1

    def deepseek_calls(self, start, end):
        return 4


def test_window_stops_the_run_at_max_hours():
    clock = Clock(ts(2026, 10, 7, 22))
    deadline = clock.t + 2 * 3600
    steps = FakeSteps(clock, recheck_keys=[f"solana:T{i}" for i in range(50)])
    tops = iter([[{"chain": "solana", "token_address": "A", "symbol": "AAA"},
                  {"chain": "solana", "token_address": "B", "symbol": "BBB"}],
                 [{"chain": "solana", "token_address": "B", "symbol": "BBB"},
                  {"chain": "base", "token_address": "C", "symbol": "CCC"}]])
    last = []

    def top():
        last[:] = next(tops, last)
        return last
    s = asyncio.run(nightly.run_once(top, steps, deadline, clock=clock, sleep=clock.sleep))
    assert s["stopped"] == "window"
    # the ticks used their share, the re-check the rest; studies once at the end
    assert clock.t - deadline < 700
    assert s["ticks"] == int(2 * 3600 * nightly.RESEARCH_SHARE // 30)    # 20 s of work + 10 s of pause
    assert steps.order[:3] == ["settle", "layers", "outcomes"] and steps.order[-1] == "studies"
    assert 0 < s["rechecked"] < 50
    assert s["entered"] == ["CCC"] and s["left"] == ["AAA"]
    assert s["analysed"] == 2 * s["ticks"] and s["deepseek_calls"] == 4 and s["layers"] == 3
    text = nightly.text_it(s)
    assert "Entrati nelle arene: CCC" in text and "Usciti dalle arene: AAA" in text
    assert "Fermata al limite di 2 ore: restano 7 lavori" in text


def test_drained_queue_ends_the_ticks_early():
    clock = Clock(ts(2026, 10, 7, 22))
    steps = FakeSteps(clock, drained_after=nightly.MIN_TICKS + 5)
    s = asyncio.run(nightly.run_once(lambda: [], steps, clock.t + 7200, clock=clock, sleep=clock.sleep))
    assert s["stopped"] == "drained" and s["ticks"] == nightly.MIN_TICKS + 5


def test_run_night_remembers_the_night_and_sends_one_message(monkeypatch):
    clock = Clock(ts(2026, 10, 7, 23))
    sent = []

    async def send(text):
        sent.append(text)
    slot = nightly.due_slot(clock.t, nightly._state)
    s = asyncio.run(nightly.run_night(slot, lambda: [], steps_factory=lambda client: FakeSteps(clock, drained_after=1),
                                      clock=clock, sleep=clock.sleep, send=send))
    assert len(sent) == 1 and sent[0].startswith("Ricerca serale finita")
    assert nightly.due_slot(clock.t, nightly._state) is None
    # a restart reads the night back from the file
    nightly.reset_for_tests()
    nightly._load()
    assert nightly._state["done_slot"] == slot and nightly._state["summary"]["ticks"] == s["ticks"]
    st = nightly.status(clock.t)
    assert st["last_run"] == s["ended"] and st["next_run"] == ts(2026, 10, 8, 22)


def test_backlog_counts_what_tonight_still_owes(monkeypatch):
    from app.trading.onchain import discovery, leaderboard as lb
    lb.reset_for_tests()
    monkeypatch.setattr(discovery, "status", lambda: {"queue": 0})
    try:
        assert lb.drained()
        lb.add({"chain": "solana", "token_address": "Tok1", "symbol": "TOK", "website": "https://tok.io"}, "manual")
        assert lb.backlog()["unanalysed"] == 1 and not lb.drained()
        lb._entries[lb._key("solana", "Tok1")]["pillars"] = {"site": {"score": 50}}
        assert lb.backlog()["unanalysed"] == 0
    finally:
        lb.reset_for_tests()


def test_a_project_added_by_hand_is_analysed_at_once(monkeypatch):
    """With the nightly run no tick comes before 22:00: analyze_now does the step."""
    from app.trading.onchain import leaderboard as lb
    lb.reset_for_tests()
    seen = []

    async def fake_analyze(k, skip, client):
        seen.append((k, skip))
        lb._busy.discard(k)
    monkeypatch.setattr(lb, "_analyze", fake_analyze)
    monkeypatch.setattr(lb, "_save", lambda: None)       # the board's file lives on the volume
    lb.add({"chain": "base", "token_address": "0xabc", "symbol": "ABC", "website": "https://abc.io"}, "manual")

    async def go():
        assert lb.analyze_now("base", "0xabc")
        assert not lb.analyze_now("base", "0xabc")       # already running
        assert not lb.analyze_now("base", "0xmissing")   # not in the working set
        await asyncio.gather(*list(lb._tasks))
    try:
        asyncio.run(go())
        assert seen == [(lb._key("base", "0xabc"), set())] and not lb._busy
        assert not lb.analyze_now("base", "0xabc")       # no running loop: nothing started
    finally:
        lb.reset_for_tests()
