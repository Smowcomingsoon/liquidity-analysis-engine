"""Candles and prices for layer chains (layers.py) from the exchange.

A layer chain has no DEX pool: GeckoTerminal and DexScreener cannot give
candles or prices. Its pool id everywhere is the string
"<exchange>:<pair>", e.g. "mexc:KASUSDT"; the GeckoTerminal network name
for them is "cex".
"""
from __future__ import annotations

import re

import httpx  # noqa: F401  (tests patch this module's httpx.AsyncClient)

from app import http_client

from app.providers.base import ProviderError

PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
EXCHANGES = ("mexc", "binance")
PRICE_URL = {"mexc": "https://api.mexc.com/api/v3/ticker/price?symbol={pair}",
             "binance": "https://api.binance.com/api/v3/ticker/price?symbol={pair}"}
# Native interval for a (tf, aggregate) pair. The exchange has no 2h/6h/8h/12h
# bars: those are merged from the next smaller native step.
_NATIVE = {("minute", 1): "1m", ("minute", 5): "5m", ("minute", 15): "15m",
           ("hour", 1): "1h", ("hour", 4): "4h", ("day", 1): "1d"}
_STEP_S = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def parse_pool(pool) -> tuple[str, str] | None:
    """\"mexc:KASUSDT\" -> (\"mexc\", \"KASUSDT\"); anything else -> None."""
    if not isinstance(pool, str) or ":" not in pool:
        return None
    exchange, _, pair = pool.partition(":")
    if exchange not in EXCHANGES or not PAIR_RE.match(pair):
        return None
    return exchange, pair


def _native(tf: str, agg: int) -> tuple[str, int] | None:
    """(native interval, seconds per output bar) for a (tf, aggregate)."""
    if (tf, agg) in _NATIVE:
        return _NATIVE[(tf, agg)], _STEP_S[_NATIVE[(tf, agg)]]
    if tf == "hour" and agg % 4 == 0:
        return "4h", agg * 3600
    if tf == "hour":
        return "1h", agg * 3600
    if tf == "day":
        return "1d", agg * 86400
    return None


def _merge(rows: list[list], step: int) -> list[list]:
    """Merge consecutive bars into buckets aligned to t // step * step."""
    out: list[list] = []
    for r in rows:
        t = int(r[0])
        bucket = t // step * step
        if out and out[-1][0] == bucket:
            last = out[-1]
            last[2] = max(last[2], r[2])
            last[3] = min(last[3], r[3])
            last[4] = r[4]
            last[5] += r[5]
        else:
            out.append([bucket, r[1], r[2], r[3], r[4], r[5]])
    return out


def _provider(exchange: str):
    if exchange == "mexc":
        from app.providers.mexc import MexcProvider
        return MexcProvider()
    from app.providers.binance import BinanceProvider
    return BinanceProvider()


async def candles(pool, tf: str, agg: int, before: float, limit: int) -> list | None:
    """Same contract as alert_track._candles: rows [t, o, h, l, c, v] (t int,
    the rest floats), [] when the pair is unknown, None on network error."""
    parsed = parse_pool(pool)
    if parsed is None:
        return []
    exchange, pair = parsed
    native = _native(tf, agg)
    if native is None:
        return []
    interval, step = native
    # Fetch enough native bars to fill `limit` output bars after the merge.
    want = limit * max(1, step // _STEP_S[interval])
    try:
        df = await _provider(exchange).fetch_ohlcv(pair, interval, want)
    except ProviderError as exc:
        text = str(exc).lower()
        # "does not recognize" (MEXC), "invalid symbol" (Binance), "no data":
        # the pair is not there. A broken response is a network problem.
        if "recognize" in text or "invalid symbol" in text or "no data" in text:
            return []
        return None
    except Exception:
        return None
    rows = [[int(ts.timestamp()), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
            for ts, r in df.iterrows()]
    rows.sort(key=lambda r: r[0])
    if step > _STEP_S[interval]:
        rows = _merge(rows, step)
    rows = [r for r in rows if r[0] < before]
    return rows[-limit:]


async def price(pool) -> float | None:
    """Last price from the exchange ticker. None on error."""
    parsed = parse_pool(pool)
    if parsed is None:
        return None
    exchange, pair = parsed
    try:
        async with http_client.pooled(timeout=4.0) as client:
            r = await client.get(PRICE_URL[exchange].format(pair=pair))
        if r.status_code != 200:
            return None
        return float(r.json()["price"])
    except Exception:
        return None
