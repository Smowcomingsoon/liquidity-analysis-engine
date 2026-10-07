"""Gem Finder: use and price from DefiLlama, and a track record of what the
board ranked. Request: "ci sono progetti migliori ma non li trovo, dobbiamo
fare un algoritmo piu' profondo"."""
from __future__ import annotations

import asyncio

import pytest

from app.trading.onchain import gem_track, research, traction
from app.trading.onchain import leaderboard as lb

T0 = 1_790_000_000.0
D = 86400.0
MINT = "So1Proto1111111111111111111111111111111111"
PROTOCOLS = [
    {"name": "Proto", "symbol": "PRT", "slug": "proto", "id": "1", "address": f"solana:{MINT}",
     "chains": ["Solana"], "category": "Dexs", "tvl": 2_000_000, "change_7d": 15.0, "mcap": 1_500_000,
     "github": ["proto-labs"], "url": "https://proto.fi", "twitter": "protofi"},
    {"name": "Big", "symbol": "BIG", "slug": "big", "id": "2", "address": "solana:BigMint",
     "chains": ["Solana"], "category": "Dexs", "tvl": 9e8, "mcap": 5e8},
    {"name": "Eth", "symbol": "E", "slug": "eth", "id": "3", "address": "arbitrum:0xabc", "chains": ["Arbitrum"], "tvl": 1e6},
    {"name": "Cex", "symbol": "C", "slug": "cex", "id": "4", "address": "solana:CexMint", "category": "CEX",
     "tvl": 1e6, "mcap": 1e6},
]
FEES = [{"slug": "proto", "defillamaId": "1", "total24h": 3_000, "total30d": 90_000}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(gem_track, "_STORE_FILE", tmp_path / "track.json")
    lb.reset_for_tests()
    yield
    lb.reset_for_tests()


def test_index_keeps_our_chains_and_joins_fees():
    idx = traction.build_index(PROTOCOLS, FEES)
    assert set(idx) == {f"solana:{MINT}", "solana:BigMint", "solana:CexMint"}
    assert idx[f"solana:{MINT}"]["fees_30d"] == 90_000


def test_candidates_are_in_band_and_not_cex():
    got = traction.candidates(traction.build_index(PROTOCOLS, FEES))
    assert [c["symbol"] for c in got] == ["PRT"]
    assert got[0]["github"] == "https://github.com/proto-labs" and got[0]["website"] == "https://proto.fi"


def test_judge_scores_use_and_cheapness_with_reasons():
    p = traction.build_index(PROTOCOLS, FEES)[f"solana:{MINT}"]
    j = traction.judge(p, 1_500_000)
    assert j["facts"]["mcap_to_fees"] == pytest.approx(1_500_000 / (90_000 * 365 / 30), abs=0.1)
    assert j["facts"]["mcap_to_tvl"] == 0.75
    assert 50 < j["traction"] < 100 and j["value"] > 50
    assert any("TVL" in r for r in j["reasons"]) and any("yearly fees" in r for r in j["reasons"])
    dear = traction.judge(p, 60_000_000)
    assert dear["value"] < j["value"]
    assert traction.judge(None, 1e6) is None
    assert traction.judge({"tvl": None, "fees_30d": None}, 1e6) is None


def test_poll_queues_candidates_once_per_refresh():
    class R:
        def __init__(self, body):
            self.status_code, self._body = 200, body

        def json(self):
            return self._body

    class Client:
        async def get(self, url, timeout=None):
            return R(PROTOCOLS if "protocols" in url else {"protocols": FEES})

    added = []
    assert asyncio.run(traction.poll(Client(), lambda item, src: added.append((item["symbol"], src)) or True, T0)) == 1
    assert added == [("PRT", "defillama")]
    assert asyncio.run(traction.poll(Client(), lambda *a: True, T0 + 60)) == 0
    assert traction.lookup("solana", MINT)["tvl"] == 2_000_000


def test_traction_lifts_the_final_score_and_is_public():
    e = {"chain": "solana", "token_address": MINT, "market_cap": 1_500_000,
         "overall": {"score": 70}, "similarity": {"score": 0}, "pillars": {}}
    before = lb.final_score(e)
    traction._index.update(traction.build_index(PROTOCOLS, FEES))
    after = lb.final_score(e)
    assert lb.traction_of(e) and after > before          # DefiLlama's use feeds the usage block (07/10)


def test_track_records_returns_at_checkpoints_and_splits_top_from_rest():
    for i, (rank, end) in enumerate([(1, 3.0), (2, 2.5), (15, 0.5), (20, 0.8)]):
        e = {"chain": "solana", "token_address": f"t{i}", "symbol": f"T{i}", "price_usd": "1.0"}
        assert gem_track.enter(e, 80, rank, T0)
        assert not gem_track.enter(e, 80, rank, T0 + 1)
        gem_track.sample("solana", f"t{i}", 1.1, T0 + 3 * D)
        gem_track.sample("solana", f"t{i}", end, T0 + 8 * D)
    st = gem_track.stats()
    week = next(h for h in st["horizons"] if h["days"] == 7)
    assert week["days"] == 7 and week["all"]["n"] == 4
    assert week["top"]["median"] == pytest.approx(1.75) and week["top"]["doubled"] == 1.0
    assert week["rest"]["median"] == pytest.approx(-0.35) and week["rest"]["positive"] == 0.0
    assert next(h for h in st["horizons"] if h["days"] == 30)["all"]["n"] == 0
    assert not gem_track.enter({"chain": "solana", "token_address": "np", "price_usd": None}, 1, 1, T0)


def test_menu_text_is_not_a_summary():
    junk = ("Skip to content dregg proof-native semantic computer the idea idea receipts worlds operations "
            "network build contents status horizon $DREGG Public proof record 01 / dregg")
    assert research.summary_line(junk) == ""
    assert research.summary_line("Skip to content A DEX for tokenized stocks. More here.") == \
        "A DEX for tokenized stocks."
