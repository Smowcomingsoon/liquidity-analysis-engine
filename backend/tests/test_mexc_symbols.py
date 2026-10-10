import asyncio

from app.providers import mexc


def _pair(symbol, status="1", api=True):
    return {"symbol": symbol, "status": status, "isSpotTradingAllowed": api,
            "baseAsset": symbol[:-4], "quoteAsset": "USDT", "fullName": symbol[:-4]}


def test_pairs_without_api_trading_are_still_searchable(monkeypatch):
    # RAINUSDT trades on MEXC with isSpotTradingAllowed false (7410bba2).
    monkeypatch.setitem(mexc._symbols_cache, "all", [
        _pair("RAINUSDT", api=False), _pair("RAINBOWUSDT"), _pair("RAINXUSDT", status="2"),
    ])
    out = asyncio.run(mexc.MexcProvider().search_symbols("rain"))
    assert [o["symbol"] for o in out] == ["RAINUSDT", "RAINBOWUSDT"]


def test_failed_refresh_keeps_last_good_list(monkeypatch):
    mexc._symbols_cache.clear()
    monkeypatch.setitem(mexc._last_good, "all", [_pair("KASUSDT")])

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("offline")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mexc.httpx, "AsyncClient", Boom)
    out = asyncio.run(mexc.MexcProvider().search_symbols("KAS"))
    assert [o["symbol"] for o in out] == ["KASUSDT"]
    assert "all" not in mexc._symbols_cache
