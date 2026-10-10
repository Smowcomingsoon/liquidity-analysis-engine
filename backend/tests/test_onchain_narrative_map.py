"""DeepSeek maps the project database to the narrative catalogue."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.notify import deepseek_desk as desk
from app.trading.onchain import narrative_map as nm, projects_db as db

T0 = 1_790_000_000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(nm, "_FILE", tmp_path / "narrative_map.json")
    monkeypatch.setattr(desk, "note", lambda *a, **k: None)
    nm.reset_for_tests()
    yield
    nm.reset_for_tests()


def test_parse_keeps_valid_keys_and_caps_three():
    raw = 'sure: {"p1": ["rwa", "nope", "rwa", "depin", "privacy", "btcfi"], "p2": [], "p9": ["rwa"]} done'
    out = nm.parse(raw, ["p1", "p2"], {"rwa", "depin", "privacy", "btcfi"})
    assert out == {"p1": ["rwa", "depin", "privacy"], "p2": []}
    assert nm.parse("not json", ["p1"], {"rwa"}) == {}


def _seed(n):
    for i in range(n):
        db.upsert({"chain": "solana", "token_address": f"T{i}", "symbol": f"S{i}", "name": f"Proj {i}"}, "test", T0)


def test_step_writes_every_row_and_respects_daily_max():
    _seed(3)
    calls = []

    async def ask(messages, **kw):
        calls.append(kw)
        assert '<project id="p3">' in messages[1]["content"]
        return json.dumps({"p1": ["rwa", "made_up"], "p2": ["depin"]})

    out = asyncio.run(nm.step(T0, ask=ask))
    assert len(out) == 3 and calls[0]["kind"] == "narratives" and calls[0]["json_mode"]
    assert db.get("solana", "T0")["narratives"] == ["rwa"] and db.get("solana", "T1")["narratives"] == ["depin"]
    t2 = db.get("solana", "T2")
    assert t2["narratives"] == [] and t2["narr_at"] == T0
    assert db.narrative_progress() == {"mapped": 3, "total": 3}
    assert db.need_narratives(10) == []

    _seed(5)  # T3, T4 new
    assert asyncio.run(nm.step(T0 + 10, ask=ask)) == {}          # gap not met
    nm._state["calls"][nm.time.strftime("%Y-%m-%d", nm.time.gmtime(T0))] = nm.DAILY_MAX
    assert asyncio.run(nm.step(T0 + nm.EVERY_S, ask=ask)) == {}  # daily cap
    assert len(calls) == 1


def test_rows_mapped_before_the_catalogue_grew_are_asked_again():
    _seed(2)
    db.set_narratives("solana", "T0", ["rwa"], nm.CATALOGUE_AT - 86400)     # old catalogue
    db.set_narratives("solana", "T1", ["depin"], nm.CATALOGUE_AT + 60)      # already today's
    assert [r["token"] for r in db.need_narratives(10, before=nm.CATALOGUE_AT)] == ["T0"]

    async def ask(messages, **kw):
        return json.dumps({"p1": ["dex_amm"]})

    out = asyncio.run(nm.step(nm.CATALOGUE_AT + 3600, ask=ask))
    assert out == {"solana:T0": ["rwa", "dex_amm"]}                         # old keys kept
