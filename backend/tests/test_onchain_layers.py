"""Layer engine (layers.py): L0/L1/L2 chains with their own token, found
from free sources and scored into the project database."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import _top_entries
from app.trading.onchain import depth, layers, projects_db, research
from app.trading.onchain import leaderboard as lb

T0 = 1_790_000_000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    lb.reset_for_tests()
    layers.reset_for_tests()
    monkeypatch.setattr(layers, "_STORE_FILE", tmp_path / "onchain_layers.json")
    monkeypatch.setattr(layers, "CG_GAP_S", 0)
    monkeypatch.setattr(layers, "RETRY_429_S", 0)
    yield
    layers.reset_for_tests()
    lb.reset_for_tests()


def _coin(cats, cap=20_000_000, fdv=30_000_000, circ=800, total=1000, mx=None, name="Kaspa", symbol="kas"):
    return {"id": name.lower(), "symbol": symbol, "name": name, "categories": cats,
            "links": {"homepage": ["https://kaspa.org"], "repos_url": {"github": ["https://github.com/kaspanet/rusty-kaspa"]}},
            "genesis_date": "2021-11-07", "description": {"en": "A proof-of-work blockDAG."},
            "developer_data": {"commit_count_4_weeks": 120},
            "market_data": {"market_cap": {"usd": cap}, "fully_diluted_valuation": {"usd": fdv},
                            "circulating_supply": circ, "total_supply": total, "max_supply": mx}}


def test_llama_candidates_one_per_gecko_id():
    rows = [{"name": "Kaspa", "gecko_id": "kaspa", "tvl": 5}, {"name": "Kaspa EVM", "gecko_id": "kaspa", "tvl": 1},
            {"name": "NoToken", "gecko_id": None, "tvl": 9}]
    assert layers.llama_candidates(rows) == [{"id": "kaspa", "name": "Kaspa", "tvl": 5}]


def test_cg_candidates_band_and_memes():
    rows = [{"id": "a", "symbol": "aaa", "name": "Alpha Chain", "market_cap": 50_000_000},
            {"id": "big", "symbol": "big", "name": "Big", "market_cap": 900_000_000},
            {"id": "tiny", "symbol": "t", "name": "Tiny", "market_cap": 100_000}]
    assert [c["id"] for c in layers.cg_candidates(rows)] == ["a"]


def test_l2beat_index_and_llama_extras():
    summary = {"projects": {"base": {"name": "Base Chain", "slug": "base", "stage": "Stage 1",
                                     "tvs": {"breakdown": {"total": 10}, "associatedTokens": []}},
                            "old": {"name": "Old", "slug": "old", "isArchived": True}}}
    idx = layers.l2beat_index(summary)
    assert idx["base"] == {"stage": "Stage 1", "tvs": 10, "has_token": False} and "old" not in idx
    ex = layers.llama_extras([{"name": "Kaspa", "totalCirculatingUSD": {"peggedUSD": 12}}],
                             {"protocols": [{"name": "Kaspa", "category": "Chain", "total30d": 7},
                                            {"name": "Uniswap", "category": "Dexs", "total30d": 99}]})
    assert ex == {"kaspa": {"stables_usd": 12, "fees_30d": 7}}


def test_cex_symbol_prefers_mexc():
    assert layers.cex_symbol("kas", {"KASUSDT"}, {"KASUSDT"}) == {"exchange": "mexc", "pair": "KASUSDT"}
    assert layers.cex_symbol("kas", set(), {"KASUSDT"}) == {"exchange": "binance", "pair": "KASUSDT"}
    assert layers.cex_symbol("kas", set(), set()) is None


def test_classify():
    assert layers.classify(_coin(["Layer 0 (L0)", "Layer 1 (L1)"]))[0] == "L0"
    assert layers.classify(_coin(["Layer 1 (L1)"]))[0] == "L1"
    assert layers.classify(_coin(["Rollup"]))[0] == "L2"
    assert layers.classify(_coin(["Exchange-based Tokens"])) == (None, "not a layer")
    assert layers.classify(_coin(["Layer 1 (L1)", "Meme"]))[1].startswith("category")
    assert "outside" in layers.classify(_coin(["Layer 1 (L1)"], cap=200_000_000, fdv=200_000_000))[1]
    assert "outside" in layers.classify(_coin(["Layer 1 (L1)"], cap=100_000, fdv=100_000))[1]
    assert "FDV" in layers.classify(_coin(["Layer 1 (L1)"], fdv=200_000_000))[1]
    l2b = {"base": {"stage": "Stage 1", "tvs": 1, "has_token": False}}
    assert layers.classify(_coin(["Layer 2 (L2)"], name="Base"), l2b) == (None, "L2 without own token")


def test_score_tokenomics():
    assert layers.score_tokenomics(_coin([], circ=900, total=1000))["score"] == 90
    assert layers.score_tokenomics(_coin([], circ=100, total=1000, mx=1000, fdv=80_000_000))["score"] == 15
    assert layers.score_tokenomics(_coin([], circ=0, total=0))["score"] is None


def test_score_traction():
    assert layers.score_traction(10e6, 0, 0, 0, 0, None)["score"] == 35
    full = layers.score_traction(10e6, 8e6, 5e6, 20e6, 50_000, {"stage": "Stage 1"})
    assert full["score"] == 100      # 30 + 25 + 10 + 10 + 15 + 10
    weak = layers.score_traction(100e6, 1e6, 2e6, 0, 0, None)
    assert weak["score"] == 20       # 30 - 10, cap/TVL 100


def test_final_score_renormalises_missing_pillars():
    pillars = [{"key": "github", "score": None}, {"key": "site", "score": 80},
               {"key": "tokenomics", "score": 60}, {"key": "traction", "score": None}]
    assert layers.final_score(pillars) == 70.0


def test_run_records_a_layer_outside_the_gem_pipeline(monkeypatch):
    coin = _coin(["Layer 1 (L1)"])

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "llama.fi/v2/chains" in u:
            return httpx.Response(200, json=[{"name": "Kaspa", "gecko_id": "kaspa", "tvl": 8e6},
                                             {"name": "Ethereum", "gecko_id": "ethereum", "tvl": 5e10}])
        if "coins/markets" in u and "ids=" in u:
            return httpx.Response(200, json=[{"id": "kaspa", "symbol": "kas", "name": "Kaspa", "market_cap": 20e6},
                                             {"id": "ethereum", "symbol": "eth", "name": "Ethereum",
                                              "market_cap": 4e11}])
        if "coins/markets" in u:
            return httpx.Response(200, json=[])
        if "/coins/kaspa" in u:
            return httpx.Response(200, json=coin)
        if "/coins/ethereum" in u:
            raise AssertionError("a big chain must not cost a detail call")
        if "historicalChainTvl" in u:
            return httpx.Response(200, json=[{"date": i, "tvl": 5e6} for i in range(40)])
        if "mexc" in u:
            return httpx.Response(200, json={"symbols": [{"symbol": "KASUSDT", "quoteAsset": "USDT", "status": "1"}]})
        return httpx.Response(200, json={})

    async def gh(repos, client, launched_at, *a, **k):
        return depth._pillar("github", 70, {}, [])

    async def site(item, client, launched):
        return depth._pillar("site", 60, {}, []), []

    monkeypatch.setattr(depth, "analyze_github", gh)
    monkeypatch.setattr(research, "analyze", site)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await layers.run(client, T0)

    assert asyncio.run(go()) == 1
    p = projects_db.get("layer", "kaspa")
    assert p["status"] == "studied" and p["score"] and p["categories"][0] == "L1"
    assert (p["overall"] or {}).get("cex") == {"exchange": "mexc", "pair": "KASUSDT"}
    assert "layer" not in projects_db.stale_market(50, T0 + 30 * 86400)
    assert projects_db.due_for_analysis(50, now=T0 + 30 * 86400) == []
    snap = layers.snapshot()
    assert [x["id"] for x in snap["found"]] == ["kaspa"] and snap["last_run"] == T0


def test_layer_row_with_cex_shows_in_top_entries():
    import time
    now = time.time()
    for gid, sym, cex in (("kaspa", "KAS", {"exchange": "mexc", "pair": "KASUSDT"}), ("noc", "NOC", None)):
        item = {"chain": "layer", "token_address": gid, "symbol": sym, "name": sym, "market_cap": 20_000_000}
        projects_db.upsert(item, "layers", now)
        projects_db.record({**item, "pillars": [], "overall": {"score": 80, "cex": cex}}, "studied", score=80, now=now)
    out = _top_entries(50)
    kas = [e for e in out if e["token_address"] == "kaspa"]
    assert kas and kas[0]["pair_address"] == "mexc:KASUSDT" and kas[0]["market_cap"] == 20_000_000
    assert not [e for e in out if e["token_address"] == "noc"]


def test_a_run_with_no_candidates_is_not_stamped_done():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "coingecko" in str(req.url):
            return httpx.Response(429)
        return httpx.Response(200, json=[] if "chains" in str(req.url) else {})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await layers.run(client, T0)

    assert asyncio.run(go()) == 0
    assert layers.snapshot()["last_run"] == 0
    # a 429 from CoinGecko is tried twice before giving up
    assert sum("category=layer-1" in u for u in calls) == 2
