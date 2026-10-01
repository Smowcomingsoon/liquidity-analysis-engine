import asyncio
import json
import time

import pytest

from app.trading.onchain import deep_recheck, leaderboard, projects_db, scout_recheck


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(deep_recheck, "_STORE", tmp_path / "deep.json")
    deep_recheck.reset_for_tests()
    monkeypatch.setattr(deep_recheck.settings, "deepseek_api_key", "test-key")
    yield
    deep_recheck.reset_for_tests()


def _row(tok):
    projects_db.upsert({"chain": "solana", "token_address": tok, "symbol": "T"}, "test", now=100)
    projects_db.record({"chain": "solana", "token_address": tok, "pillars": {}}, "studied", why="DeepSeek review: old",
                       score=40, now=100)


def _fake_ask(payload):
    async def ask(messages, **kw):
        return json.dumps(payload)
    return ask


def _fake_recheck(rc):
    async def recheck(entry, item, client, ask=None):
        return rc
    return recheck


def test_dropped_token_kept_clears_why(monkeypatch):
    _row("tok1")
    monkeypatch.setattr(scout_recheck, "recheck", _fake_recheck(
        {"confirmed": [], "unverified": [], "refuted": [{"text": "x", "note": "ok"}], "found": [], "evidence": "ev"}))
    ask = _fake_ask({"keep": True, "score": 70, "verdict": "gem", "reason": "real product",
                     "strengths": ["ships"], "problems": []})
    res = asyncio.run(deep_recheck.run("solana", "tok1", None, ask=ask))
    assert res["keep"] is True
    assert deep_recheck.get("solana", "tok1")["score"] == 70
    row = projects_db.get("solana", "tok1")
    assert not (row.get("why") or "").startswith("DeepSeek")


def test_second_call_within_cooldown(monkeypatch):
    _row("tok2")
    monkeypatch.setattr(scout_recheck, "recheck", _fake_recheck(
        {"confirmed": [], "unverified": [], "refuted": [], "found": [], "evidence": ""}))
    ask = _fake_ask({"keep": False, "score": 10, "verdict": "avoid", "reason": "nothing",
                     "strengths": [], "problems": ["no product"]})
    asyncio.run(deep_recheck.run("solana", "tok2", None, ask=ask))
    with pytest.raises(deep_recheck.TooSoon) as exc:
        asyncio.run(deep_recheck.run("solana", "tok2", None, ask=ask))
    assert exc.value.wait_s > 0


def test_daily_max(monkeypatch):
    _row("tok3")
    monkeypatch.setattr(scout_recheck, "recheck", _fake_recheck(
        {"confirmed": [], "unverified": [], "refuted": [], "found": [], "evidence": ""}))
    ask = _fake_ask({"keep": False, "score": 10, "verdict": "avoid", "reason": "nothing",
                     "strengths": [], "problems": []})
    day = time.strftime("%Y-%m-%d", time.gmtime())
    deep_recheck._state["calls"][day] = deep_recheck.DAILY_MAX
    with pytest.raises(deep_recheck.TooSoon):
        asyncio.run(deep_recheck.run("solana", "tok3", None, ask=ask))


def test_unreadable_json_counts(monkeypatch):
    _row("tok4")
    monkeypatch.setattr(scout_recheck, "recheck", _fake_recheck(
        {"confirmed": [], "unverified": [], "refuted": [], "found": [], "evidence": ""}))

    async def ask(messages, **kw):
        return "not json"

    day = time.strftime("%Y-%m-%d", time.gmtime())
    before = deep_recheck._state["calls"].get(day, 0)
    with pytest.raises(RuntimeError):
        asyncio.run(deep_recheck.run("solana", "tok4", None, ask=ask))
    assert deep_recheck._state["calls"].get(day, 0) == before + 1


def test_board_entry_gets_deep_review(monkeypatch):
    entry = {"chain": "solana", "token_address": "tok5", "name": "T", "symbol": "T",
             "pillars": {}, "judge": {"verdict": "watch", "score": 30, "problems": ["p"]}}
    leaderboard._entries["solana:tok5"] = entry
    monkeypatch.setattr(scout_recheck, "recheck", _fake_recheck(
        {"confirmed": [], "unverified": [], "refuted": [], "found": [], "evidence": ""}))
    ask = _fake_ask({"keep": True, "score": 80, "verdict": "gem", "reason": "good",
                     "strengths": [], "problems": []})
    asyncio.run(deep_recheck.run("solana", "tok5", None, ask=ask))
    assert leaderboard._entries["solana:tok5"]["review"]["deep"] is True
    leaderboard._entries.pop("solana:tok5", None)
