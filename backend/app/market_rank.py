"""Market-cap rank of a crypto ticker, for the YOUR TICKERS list.

Board issue d46f02f7 asked for "la posizione del ticker nel cryptorank".
CryptoRank's API needs a key the server does not have; CoinPaprika's
`/v1/tickers` gives the same thing (global rank by market cap) for every
listed coin in one keyless call. The table is cached for a few hours: a
rank barely moves within a day and the payload is a few MB.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app import http_client

logger = logging.getLogger(__name__)

PAPRIKA_TICKERS = "https://api.coinpaprika.com/v1/tickers"
TTL_S = 6 * 3600
RETRY_S = 10 * 60
# Longest first: "FDUSD" must not be read as "...USD".
_QUOTES = ("FDUSD", "USDT", "USDC", "BUSD", "USD", "EUR", "TRY", "BTC", "ETH")

_cache: dict = {"at": 0.0, "ranks": {}}
_lock = asyncio.Lock()


def ranks_from_tickers(tickers: list[dict] | None) -> dict[str, int]:
    """symbol -> best (lowest) rank. Several coins share a symbol; the
    biggest one is almost always the one an exchange pair means."""
    out: dict[str, int] = {}
    for t in tickers or []:
        sym = str(t.get("symbol") or "").upper().strip()
        try:
            rank = int(t.get("rank") or 0)
        except (TypeError, ValueError):
            continue
        if not sym or rank <= 0:
            continue
        if sym not in out or rank < out[sym]:
            out[sym] = rank
    return out


def rank_for(symbol: str, ranks: dict[str, int]) -> int | None:
    """Rank of an exchange pair ("BTCUSDT", "ETH/USDT"), a bare ticker
    ("WBTC") or a DEX pair ("BONK@solana:addr")."""
    sym = str(symbol or "").upper().strip()
    if "@" in sym:
        sym = sym.split("@", 1)[0]
    for sep in ("/", "-", "_", ":"):
        if sep in sym:
            sym = sym.split(sep, 1)[0]
            break
    if not sym:
        return None
    if sym in ranks:
        return ranks[sym]
    for q in _QUOTES:
        if sym.endswith(q) and len(sym) > len(q):
            base = sym[: -len(q)]
            if base in ranks:
                return ranks[base]
    return None


async def ranks() -> dict[str, int]:
    """Cached symbol -> rank table; the last good table survives a failed
    refresh, and a failure is retried after a few minutes, not every call."""
    if time.time() < _cache["at"]:
        return _cache["ranks"]
    async with _lock:
        now = time.time()
        if now < _cache["at"]:  # another caller refreshed while we waited
            return _cache["ranks"]
        table: dict[str, int] = {}
        try:
            async with http_client.pooled(timeout=20) as client:
                resp = await client.get(PAPRIKA_TICKERS)
                resp.raise_for_status()
                table = ranks_from_tickers(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("market_rank: CoinPaprika tickers failed: %s", exc)
        if table:
            _cache["ranks"] = table
        # "at" holds the time of the next refresh.
        _cache["at"] = now + (TTL_S if table else RETRY_S)
        return _cache["ranks"]
