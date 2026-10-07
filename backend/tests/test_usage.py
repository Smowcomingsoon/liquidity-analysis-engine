import asyncio
import time

import httpx
import pytest

from app.trading.onchain import storage, usage

TOKEN = "Epht7Fw4Sgh6fdcJj6afWXuNcAUmLLMc3MSthUqELiZA"
POOL = "wa71bmn5zdjAP8GDQdn4ioXQWrRHnruFRP7AC22wWwQ"


@pytest.fixture(autouse=True)
def _private_store(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    usage.reset_for_tests()
    yield
    usage.reset_for_tests()


def _pair(token, pool, liq, vol=491.64, buys=5, sells=7, mcap=47770.0):
    return {"chainId": "solana", "pairAddress": pool,
            "baseToken": {"address": token, "symbol": "CLAW"},
            "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
            "txns": {"h24": {"buys": buys, "sells": sells}, "h1": {"buys": 0, "sells": 1}},
            "volume": {"h24": vol, "h6": 10.0}, "liquidity": {"usd": liq, "base": 1, "quote": 2},
            "fdv": 50000.0, "marketCap": mcap}


def _pool_payload(buyers=2, sellers=6):
    return {"data": {"id": f"solana_{POOL}", "type": "pool", "attributes": {
        "name": "CLAW / SOL", "transactions": {
            "h1": {"buys": 0, "sells": 1, "buyers": 0, "sellers": 1},
            "h24": {"buys": 5, "sells": 7, "buyers": buyers, "sellers": sellers}}}}}


def _ohlcv_payload(vols):
    now = int(time.time()) // 86400 * 86400
    rows = [[now - i * 86400, 1, 1, 1, 1, v] for i, v in enumerate(reversed(vols))]
    return {"data": {"attributes": {"ohlcv_list": list(reversed(rows))}}}   # newest first, as Gecko does


def _info_payload(count=245, top10="48.0145"):
    return {"data": {"attributes": {"holders": {"count": count, "distribution_percentage": {
        "top_10": top10, "11_30": "10.1", "31_50": "5.0", "rest": "36.9"}, "last_updated": "x"}}}}


class Fake:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for frag, resp in self.routes:
            if frag in url:
                return resp(request) if callable(resp) else httpx.Response(200, json=resp)
        return httpx.Response(404, json={})


def _run(fake, coro_fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            return await coro_fn(c)
    return asyncio.run(go())


def _full_routes(pairs):
    return [("api.dexscreener.com", pairs),
            ("/ohlcv/day", _ohlcv_payload([100.0] * 23 + [70.0] * 7)),
            (f"/pools/{POOL}", _pool_payload()),
            ("/info", _info_payload())]


def test_measure_parses_every_source():
    pairs = [_pair(TOKEN, "small", 100.0, vol=1.0), _pair(TOKEN, POOL, 22058.27)]
    m = _run(Fake(_full_routes(pairs)), lambda c: usage.measure(c, "solana", TOKEN))
    assert m["pool"] == POOL                       # most liquid pair
    assert m["vol_24h"] == 491.64 and m["tx_24h"] == 12
    assert m["liq"] == 22058.27 and m["mcap"] == 47770.0
    assert (m["buyers_24h"], m["sellers_24h"]) == (2, 6)
    assert m["days"] == 30 and m["vol_7d_avg"] == pytest.approx(70.0)
    assert m["vol_30d_avg"] == pytest.approx((23 * 100 + 7 * 70) / 30)
    assert m["holders"] == 245 and m["top10_pct"] == pytest.approx(48.0145) and m["top10_raw"] is True
    assert m["sources"] == ["dexscreener", "geckoterminal_pool", "geckoterminal_ohlcv", "geckoterminal_info"]


def test_measure_uses_given_pool_and_fdv_fallback():
    pairs = [_pair(TOKEN, "big", 1e6), _pair(TOKEN, POOL, 10.0, mcap=None)]
    m = _run(Fake(_full_routes(pairs)), lambda c: usage.measure(c, "solana", TOKEN, pool=POOL))
    assert m["pool"] == POOL and m["liq"] == 10.0 and m["mcap"] == 50000.0


def test_missing_sources_are_none_not_zero():
    fake = Fake([("api.dexscreener.com", lambda r: httpx.Response(500)),
                 ("/info", _info_payload())])
    m = _run(fake, lambda c: usage.measure(c, "solana", TOKEN))
    assert m["holders"] == 245 and m["sources"] == ["geckoterminal_info"]
    for f in ("vol_24h", "tx_24h", "liq", "mcap", "buyers_24h", "sellers_24h",
              "vol_7d_avg", "vol_30d_avg", "days", "pool"):
        assert m[f] is None, f


def test_no_source_returns_none():
    fake = Fake([("api.dexscreener.com", [])])
    assert _run(fake, lambda c: usage.measure(c, "solana", TOKEN)) is None


def test_one_snapshot_per_day_and_pruning():
    now = time.time()
    usage.record({"chain": "solana", "token": TOKEN, "at": now - 3600 * 0.1, "holders": 10})
    usage.record({"chain": "solana", "token": TOKEN, "at": now, "holders": 11})
    assert len(usage.history("solana", TOKEN)) == 1
    assert usage.latest("solana", TOKEN)["holders"] == 11
    usage.record({"chain": "solana", "token": TOKEN, "at": now - 61 * 86400, "holders": 1})
    assert len(usage.history("solana", TOKEN)) == 1          # older than 60 days: pruned
    usage.record({"chain": "solana", "token": TOKEN, "at": now - 5 * 86400, "holders": 5})
    h = usage.history("solana", TOKEN)
    assert [s["holders"] for s in h] == [5, 11]               # oldest first
    usage.reset_for_tests()                                   # survives a reload from disk
    assert [s["holders"] for s in usage.history("solana", TOKEN)] == [5, 11]


def test_holders_growth():
    now = time.time()
    assert usage.holders_growth("solana", TOKEN) is None
    usage.record({"chain": "solana", "token": TOKEN, "at": now, "holders": 300})
    assert usage.holders_growth("solana", TOKEN) is None      # nothing older
    usage.record({"chain": "solana", "token": TOKEN, "at": now - 29 * 86400, "holders": 200})
    usage.record({"chain": "solana", "token": TOKEN, "at": now - 10 * 86400, "holders": 250})
    assert usage.holders_growth("solana", TOKEN, days=30) == pytest.approx(0.5)
    assert usage.holders_growth("solana", TOKEN, days=7) == pytest.approx(0.2)


def test_evm_keys_ignore_case():
    usage.record({"chain": "base", "token": "0xABC", "at": time.time(), "holders": 3})
    assert usage.latest("base", "0xabc")["holders"] == 3


def test_refresh_batches_dexscreener_and_skips_today():
    other = "Other111111111111111111111111111111111111111"
    pairs = [_pair(TOKEN, POOL, 22058.27), _pair(other, "otherpool", 5000.0, vol=9.0)]
    fake = Fake(_full_routes(pairs))
    items = [{"chain": "solana", "token_address": TOKEN, "pair_address": POOL},
             {"chain": "solana", "token_address": other, "pair_address": "otherpool"},
             {"chain": "solana", "token_address": TOKEN, "pair_address": POOL}]   # duplicate
    assert _run(fake, lambda c: usage.refresh(c, items)) == 2
    dex = [u for u in fake.calls if "dexscreener" in u]
    assert len(dex) == 1 and TOKEN in dex[0] and other in dex[0]
    assert usage.latest("solana", other)["vol_24h"] == 9.0
    fake.calls.clear()
    assert _run(fake, lambda c: usage.refresh(c, items)) == 0  # done today
    assert fake.calls == []


def test_refresh_respects_max_calls():
    fake = Fake(_full_routes([_pair(TOKEN, POOL, 1.0)]))
    items = [{"chain": "solana", "token_address": TOKEN, "pair_address": POOL},
             {"chain": "solana", "token_address": "Zzz", "pair_address": None}]
    assert _run(fake, lambda c: usage.refresh(c, items, max_calls=1)) == 1
    assert usage.latest("solana", "Zzz") is None
