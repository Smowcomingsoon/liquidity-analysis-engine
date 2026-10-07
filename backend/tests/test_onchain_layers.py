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
    yield
    layers.reset_for_tests()
    lb.reset_for_tests()


def _ticker(cid="kas-kaspa", name="Kaspa", symbol="KAS", cap=20_000_000, price=0.1, total=250_000_000, mx=None):
    return {"id": cid, "name": name, "symbol": symbol, "rank": 300, "total_supply": total, "max_supply": mx,
            "quotes": {"USD": {"price": price, "market_cap": cap}}}


def _cand(type_="L1", **kw):
    return {**layers.paprika_candidates([_ticker(**kw)], {kw.get("cid", "kas-kaspa"): type_})[0]}


_COIN = {"id": "kas-kaspa", "name": "Kaspa", "symbol": "KAS", "logo": "https://x/kas.png",
         "description": "A proof-of-work blockDAG.", "started_at": "2021-11-14T00:00:00Z",
         "tags": [{"id": "layer-1-l1", "name": "Layer 1 (L1)"}],
         "links": {"website": ["https://kaspa.org"], "source_code": ["https://github.com/kaspanet/rusty-kaspa"]}}


def test_llama_tvl_by_name():
    assert layers.llama_tvl([{"name": "Kaspa", "tvl": 5}, {"name": "", "tvl": 9}]) == {"kaspa": 5}


def test_layer_types_lowest_layer_wins():
    tags = [{"id": "layer-1-l1", "coins": ["a", "b"]}, {"id": "layer-0-l0", "coins": ["a"]},
            {"id": "layer-2-l2", "coins": ["b", "c"]}, {"id": "defi", "coins": ["d"]}]
    assert layers.layer_types(tags) == {"a": "L0", "b": "L1", "c": "L2"}


def test_paprika_candidates_band_fdv_memes():
    types = {"ok": "L1", "big": "L1", "tiny": "L1", "dil": "L1", "pepe": "L1"}
    rows = [_ticker("ok", "Alpha Chain", "ALP"), _ticker("big", "Big", "BIG", cap=900e6),
            _ticker("tiny", "Tiny", "TNY", cap=100e3), _ticker("dil", "Dil", "DIL", mx=10e9),
            _ticker("pepe", "Pepe Chain", "PEPE"), _ticker("none", "NoTag", "NT")]
    out = layers.paprika_candidates(rows, types)
    assert [c["id"] for c in out] == ["ok"]
    assert out[0]["circulating"] == 200_000_000 and out[0]["fdv"] == 25_000_000


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
    assert layers.classify(_cand(), _COIN) == ("L1", "")
    assert layers.classify(_cand("L0"), _COIN)[0] == "L0"
    meme = {**_COIN, "tags": [{"id": "meme", "name": "Meme"}]}
    assert layers.classify(_cand(), meme)[1].startswith("tag")
    assert "outside" in layers.classify({**_cand(), "market_cap": 200e6}, _COIN)[1]
    assert "FDV" in layers.classify({**_cand(), "fdv": 200e6}, _COIN)[1]
    l2b = {"base": {"stage": "Stage 1", "tvs": 1, "has_token": False}}
    base = _cand("L2", cid="base", name="Base")
    assert layers.classify(base, _COIN, l2b) == (None, "L2 without own token")


def test_score_tokenomics():
    assert layers.score_tokenomics(_cand(total=220_000_000))["score"] == 90
    # 20% circulating, FDV 5x the cap
    assert layers.score_tokenomics(_cand(mx=1_000_000_000, total=0, price=0.1, cap=20e6))["score"] == 15
    assert layers.score_tokenomics({"circulating": 0, "total": 0})["score"] is None


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
    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "coinpaprika.com/v1/tags" in u:
            return httpx.Response(200, json=[{"id": "layer-1-l1", "coins": ["kas-kaspa", "eth-ethereum"]}])
        if "coinpaprika.com/v1/tickers" in u:
            return httpx.Response(200, json=[_ticker(), _ticker("eth-ethereum", "Ethereum", "ETH", cap=4e11)])
        if "coinpaprika.com/v1/coins/kas-kaspa" in u:
            return httpx.Response(200, json=_COIN)
        if "coinpaprika.com/v1/coins/" in u:
            raise AssertionError("a big chain must not cost a detail call")
        if "llama.fi/v2/chains" in u:
            return httpx.Response(200, json=[{"name": "Kaspa", "tvl": 8e6}, {"name": "Ethereum", "tvl": 5e10}])
        if "historicalChainTvl" in u:
            return httpx.Response(200, json=[{"date": i, "tvl": 5e6} for i in range(40)])
        if "mexc" in u:
            return httpx.Response(200, json={"symbols": [{"symbol": "KASUSDT", "quoteAsset": "USDT", "status": "1"}]})
        return httpx.Response(200, json={})

    async def gh(repos, client, launched_at, *a, **k):
        assert repos == ["https://github.com/kaspanet/rusty-kaspa"]
        return depth._pillar("github", 70, {}, [])

    async def site(item, client, launched):
        assert item["website"] == "https://kaspa.org"
        return depth._pillar("site", 60, {}, []), []

    monkeypatch.setattr(depth, "analyze_github", gh)
    monkeypatch.setattr(research, "analyze", site)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await layers.run(client, T0)

    # a CoinGecko-era row of the same chain goes once the new one is written
    old = {"chain": "layer", "token_address": "kaspa", "symbol": "KAS", "name": "Kaspa", "cg_id": "kaspa"}
    projects_db.upsert(old, "layers", T0)
    assert asyncio.run(go()) == 1
    assert projects_db.get("layer", "kaspa") is None
    p = projects_db.get("layer", "kas-kaspa")
    assert p["status"] == "studied" and p["score"] and p["categories"][0] == "L1" and p["image"] == "https://x/kas.png"
    assert (p["overall"] or {}).get("cex") == {"exchange": "mexc", "pair": "KASUSDT"}
    assert "layer" not in projects_db.stale_market(50, T0 + 30 * 86400)
    assert projects_db.due_for_analysis(50, now=T0 + 30 * 86400) == []
    snap = layers.snapshot()
    assert [x["id"] for x in snap["found"]] == ["kas-kaspa"] and snap["last_run"] == T0


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
    def handler(req: httpx.Request) -> httpx.Response:
        if "coinpaprika" in str(req.url):
            return httpx.Response(429)
        return httpx.Response(200, json=[] if "chains" in str(req.url) else {})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await layers.run(client, T0)

    assert asyncio.run(go()) == 0
    assert layers.snapshot()["last_run"] == 0


def test_layer_token_page_reads_list_pillars(monkeypatch):
    """Layer rows keep their pillars as a list: the token page opened from
    the Top answered 500 (07/10, AttributeError 'list' object has no attribute 'get')."""
    import time

    from app import main
    monkeypatch.setattr(main.settings, "onchain_enabled", True)
    monkeypatch.setattr(main.guest_links, "is_guest", lambda user: False)
    now = time.time()
    item = {"chain": "layer", "token_address": "kas-kaspa", "symbol": "KAS", "name": "Kaspa", "market_cap": 2e7}
    projects_db.upsert(item, "layers", now)
    pillars = [depth._pillar("github", 70, {"repo": "kaspanet/rusty-kaspa"}, []), depth._pillar("site", 60, {}, [])]
    projects_db.record({**item, "pillars": pillars, "overall": {"score": 80}}, "studied", score=80, now=now)
    out = asyncio.run(main.onchain_token("layer", "kas-kaspa", object()))
    assert out["entry"]["pillars"]["github"]["score"] == 70 and out["entry"]["pillars"]["site"]["score"] == 60
    assert lb.db_entry(projects_db.get("layer", "kas-kaspa"))["pillars"]["github"]["facts"]["repo"] == "kaspanet/rusty-kaspa"
