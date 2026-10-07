"""Usage before any score (Lorenzo, 07/10): volume, trades, buyers, holders
and the $300k floor keep tokens nobody uses out of the Top; the new formula
weighs measured parts first and DeepSeek last."""
import pytest

from app.trading.onchain import leaderboard as lb
from app.trading.onchain import usage

pytestmark = pytest.mark.usage_gates


def _m(**kw):
    base = {"vol_24h": 60_000, "vol_7d_avg": 50_000, "vol_30d_avg": 40_000, "days": 30, "tx_24h": 400,
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
    assert lb.usage_gate(_e(cap=47_770), cap_floor=False).startswith("volume $712")


@pytest.mark.parametrize("over, why", [
    ({"vol_7d_avg": 10_000}, "volume $10,000 a day"),
    ({"vol_7d_avg": 30_000}, "of the cap"),            # 30k on a 5M cap is 0.6%
    ({"tx_24h": 50}, "50 trades"),
    ({"buyers_24h": 10}, "10 buyers"),
    ({"holders": 300}, "300 holders"),
])
def test_each_bar(snap, over, why):
    store, _ = snap
    store["T"] = _m(**over)
    assert why in lb.usage_gate(_e(cap=5_000_000 if "of the cap" in why else 2_000_000))


def test_passes_and_unknowns_are_not_zero(snap):
    store, growth = snap
    store["T"] = _m(buyers_24h=None, holders=None)
    assert lb.usage_gate(_e()) == ""
    store["T"] = _m(vol_7d_avg=None, vol_24h=None)
    assert lb.usage_gate(_e()) == "volume unknown"
    store.pop("T")
    assert lb.usage_gate(_e()) == "usage not measured yet"


def test_shrinking_holders_and_whales(snap):
    store, growth = snap
    store["T"] = _m()
    growth["T"] = -0.1
    assert "holders -10% in 30 days" in lb.usage_gate(_e())
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
