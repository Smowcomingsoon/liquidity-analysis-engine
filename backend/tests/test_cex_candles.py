"""Exchange candles and prices for layer chains (cex_candles.py). No network:
the provider classes and httpx are monkeypatched."""
from __future__ import annotations

import asyncio

import httpx
import pandas as pd

from app.providers.base import ProviderError
from app.trading.onchain import cex_candles


def _df(rows):
    idx = pd.to_datetime([r[0] for r in rows], unit="s")
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                         "volume": [r[5] for r in rows]}, index=idx)


def test_parse_pool():
    assert cex_candles.parse_pool("mexc:KASUSDT") == ("mexc", "KASUSDT")
    assert cex_candles.parse_pool("binance:BTCUSDT") == ("binance", "BTCUSDT")
    assert cex_candles.parse_pool("KASUSDT") is None
    assert cex_candles.parse_pool("kraken:KASUSDT") is None
    assert cex_candles.parse_pool("mexc:kasusdt") is None
    assert cex_candles.parse_pool("mexc:KAS") is None
    assert cex_candles.parse_pool(None) is None


def test_hour_6_merges_from_1h(monkeypatch):
    rows = [[i * 3600, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10.0] for i in range(12)]
    calls = {}

    class Fake:
        async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
            calls["interval"] = interval
            return _df(rows)

    monkeypatch.setattr(cex_candles, "_provider", lambda ex: Fake())
    out = asyncio.run(cex_candles.candles("mexc:KASUSDT", "hour", 6, 1e12, 10))
    assert calls["interval"] == "1h"
    assert len(out) == 2
    assert out[0][0] == 0 and out[0][1] == 1.0 and out[0][2] == 7.0 and out[0][3] == 0.5 and out[0][4] == 6.5
    assert out[0][5] == 60.0
    assert cex_candles._native("hour", 12) == ("4h", 43200) and cex_candles._native("day", 7) == ("1d", 604800)
    assert out[1][0] == 6 * 3600


def test_before_and_limit_cut(monkeypatch):
    rows = [[i * 60, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(20)]

    class Fake:
        async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
            return _df(rows)

    monkeypatch.setattr(cex_candles, "_provider", lambda ex: Fake())
    out = asyncio.run(cex_candles.candles("mexc:KASUSDT", "minute", 1, 10 * 60, 3))
    assert [r[0] for r in out] == [7 * 60, 8 * 60, 9 * 60]


def test_unknown_symbol_is_empty(monkeypatch):
    class Fake:
        async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
            raise ProviderError("Invalid symbol")

    monkeypatch.setattr(cex_candles, "_provider", lambda ex: Fake())
    assert asyncio.run(cex_candles.candles("mexc:NOPEUSDT", "minute", 1, 1e12, 10)) == []


def test_network_error_is_none(monkeypatch):
    class Fake:
        async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(cex_candles, "_provider", lambda ex: Fake())
    assert asyncio.run(cex_candles.candles("mexc:KASUSDT", "minute", 1, 1e12, 10)) is None


def test_price(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "symbol=KASUSDT" in str(req.url)
        return httpx.Response(200, json={"symbol": "KASUSDT", "price": "0.1234"})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def fake_client(*a, **k):
        k["transport"] = transport
        return real(*a, **k)

    monkeypatch.setattr(cex_candles.httpx, "AsyncClient", fake_client)
    assert asyncio.run(cex_candles.price("mexc:KASUSDT")) == 0.1234
    assert asyncio.run(cex_candles.price("nope")) is None


def test_strategy_lab_backtests_layer_entries():
    from app.trading.onchain import strategy_lab
    e = {"chain": "layer", "token_address": "kaspa", "pair_address": "mexc:KASUSDT"}
    assert strategy_lab.next_due([e], 1e10) is e
    assert strategy_lab._net("layer") == "cex"
