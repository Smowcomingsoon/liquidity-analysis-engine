"""Usage before any score (Lorenzo, 07/10): volume, trades, buyers, holders
and the $300k floor keep tokens nobody uses out of the Top; the new formula
weighs measured parts first and DeepSeek last."""
import time

import pytest

from app.trading.onchain import leaderboard as lb
from app.trading.onchain import usage

pytestmark = pytest.mark.usage_gates


def _m(**kw):
    base = {"at": time.time(), "vol_24h": 60_000, "vol_7d_avg": 50_000, "vol_30d_avg": 40_000, "days": 30, "tx_24h": 400,
            "buyers_24h": 120, "sellers_24h": 90, "holders": 3_000, "liq": 150_000, "mcap": 2_000_000}
    return {**base, **kw}


@pytest.fixture
def snap(monkeypatch):
    store = {}
    growth = {}
    monkeypatch.setattr(usage, "latest", lambda chain, token: store.get(token))
    monkeypatch.setattr(usage, "holders_growth", lambda chain, token, days=30: growth.get(token))
    return store, growth


def _e(token="T", cap=2_000_000, top10=None, **extra):
    pillars = {"github": {"score": 70}, "site": {"score": 70},
               "tokenomics": {"score": 70, "facts": {} if top10 is None else {"top10_pct": top10}}}
    return {"chain": "solana", "token_address": token, "symbol": f"Q{token}ZQ", "market_cap": cap, "pillars": pillars, **extra}


def test_clawville_numbers_fail(snap):
    store, _ = snap
    store["T"] = _m(vol_24h=492, vol_7d_avg=712, tx_24h=12, buyers_24h=2, holders=245, mcap=47_770)
    assert "early list" in lb.usage_gate(_e(cap=47_770))
    assert lb.usage_gate(_e(cap=47_770), cap_floor=False).startswith("nobody trades it: $712 a day, 2 buyers")


def test_hidden_gems_are_not_dropped_for_low_volume(snap):
    """Lorenzo, 07/10: volume and participation matter but must not steer
    everything; hidden gems must still come out."""
    store, _ = snap
    store["T"] = _m(vol_7d_avg=4_000, vol_24h=3_000, tx_24h=40, buyers_24h=15, holders=180)
    e = _e()
    e["pillars"]["site"]["score"] = e["pillars"]["github"]["score"] = 85
    assert lb.usage_gate(e) == ""
    assert lb.hidden_gem(e)
    store["T"] = _m(vol_7d_avg=1_500, buyers_24h=12)        # thin, but 12 people bought today
    assert lb.usage_gate(e) == ""


@pytest.mark.parametrize("over, why", [
    ({"vol_7d_avg": 900, "buyers_24h": 3}, "nobody trades it"),
    ({"holders": 60}, "60 holders"),
])
def test_each_bar(snap, over, why):
    store, _ = snap
    store["T"] = _m(**over)
    assert why in lb.usage_gate(_e())


def test_unknowns_and_unmeasured_pass(snap):
    store, growth = snap
    store["T"] = _m(buyers_24h=None, holders=None, vol_7d_avg=100)
    assert lb.usage_gate(_e()) == ""                         # few dollars but buyers unknown: not proven dead
    store.pop("T")
    assert lb.usage_gate(_e()) == ""                         # not measured yet: unknown is not zero


def test_shrinking_holders_and_whales(snap):
    store, growth = snap
    store["T"] = _m()
    growth["T"] = -0.08
    assert lb.usage_gate(_e()) == ""                         # within the 10% band
    growth["T"] = -0.2
    assert "holders -20% in 30 days" in lb.usage_gate(_e())
    growth["T"] = 0.05
    assert "top 10 wallets hold 55%" in lb.usage_gate(_e(top10=55))
    assert lb.usage_gate(_e(top10=20)) == ""


def test_score_prefers_usage_over_story(snap):
    store, _ = snap
    store["USED"] = _m(vol_7d_avg=200_000, buyers_24h=600, holders=20_000)
    store["STORY"] = _m(vol_7d_avg=26_000, buyers_24h=31, holders=520)
    used = _e("USED")
    story = _e("STORY")
    story["pillars"]["site"]["score"] = 95
    story["pillars"]["github"]["score"] = 95
    assert lb.final_score(used) > lb.final_score(story)


def test_score_without_deepseek_shares_its_weight(snap):
    store, _ = snap
    store["T"] = _m()
    e = _e()
    blocks = lb.score_blocks(e)
    assert blocks["deepseek"] is None and blocks["usage"] is not None
    assert 0 < lb.final_score(e) <= 100


def test_tonight_waits_for_unmeasured_tokens(snap, monkeypatch):
    monkeypatch.setattr(lb, "usage_candidates", lambda: [_e("A"), _e("B")])
    monkeypatch.setattr(usage, "measured_today", lambda chain, token: token == "A")
    assert lb.backlog()["unmeasured"] == 1 and not lb.drained()


def test_candidates_skip_layers_and_read_the_database(snap, monkeypatch):
    rows = {"studied": [{"chain": "solana", "token": "DB1", "analyzed_at": time.time(), "score": 70},
                        {"chain": "layer", "token": "kas-kaspa", "analyzed_at": time.time(), "score": 90}],
            "ranked": [{"chain": "base", "token": "0xOLD", "analyzed_at": time.time() - 40 * 86400, "score": 80}]}
    monkeypatch.setattr(lb, "_entries", {})
    monkeypatch.setattr(lb.projects_db, "search", lambda status, **k: {"items": rows[status]})
    assert [e["token_address"] for e in lb.usage_candidates()] == ["DB1"]


def test_warm_up_measures_only_tokens_without_a_fresh_snapshot(snap, monkeypatch):
    store, _ = snap
    store["OLD"] = _m(at=time.time() - 5 * 86400)
    store["NEW"] = _m(at=time.time() - 3600)
    monkeypatch.setattr(lb, "usage_candidates", lambda: [_e("OLD"), _e("NEW"), _e("NONE")])
    seen = []

    async def fake(client, items, limit=None):
        seen.extend(e["token_address"] for e in items)
        return 0
    monkeypatch.setattr(usage, "refresh", fake)
    import asyncio
    asyncio.run(lb.warm_usage())
    assert seen == ["OLD", "NONE"]
    assert lb.usage_gate(_e("OLD")) == ""                    # stale snapshot: unknown, not a reason


