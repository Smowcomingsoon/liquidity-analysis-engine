"""Background CEX-listing enrichment for sniper candidates.

Queries CoinGecko (free tier) to determine whether each candidate token
is already listed on a major centralized exchange. Results are stored in
Candidate.cex_listed and persisted to disk.

CoinGecko free tier allows ~10–30 req/min; we add a 4-second delay between
calls and skip tokens that were already checked.
"""
import asyncio
import logging

import httpx

from app import http_client

logger = logging.getLogger(__name__)

_CG_BASE = "https://api.coingecko.com/api/v3"

_CHAIN_TO_PLATFORM = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "base": "base",
    "arbitrum": "arbitrum-one",
    "avalanche": "avalanche",
    "solana": "solana",
    "polygon": "polygon-pos",
    "tron": "tron",
    "ton": "the-open-network",
    "sui": "sui",
}

# Identifiers used by CoinGecko for major CEXs
_CEX_IDS = {
    "binance", "coinbase-exchange", "kraken", "okex", "bybit_spot",
    "gate", "kucoin", "huobi", "mexc", "bitget", "bitfinex",
}


async def check_cex_listed(chain_id: str, token_address: str, client: httpx.AsyncClient) -> bool | None:
    platform = _CHAIN_TO_PLATFORM.get(chain_id)
    if not platform:
        return False
    try:
        url = f"{_CG_BASE}/coins/{platform}/contract/{token_address}"
        r = await client.get(url, timeout=10.0)
        if r.status_code == 429:
            await asyncio.sleep(60)
            return None  # will retry next cycle
        if r.status_code != 200:
            return False
        data = r.json()
        tickers = data.get("tickers", [])
        for t in tickers:
            if t.get("market", {}).get("identifier", "") in _CEX_IDS:
                return True
        return False
    except Exception as exc:
        logger.debug("CEX check error for %s/%s: %s", chain_id, token_address, exc)
        return None  # unknown — will retry


async def enrichment_loop() -> None:
    """Continuously enrich candidates missing a cex_listed value."""
    from .cache import _save_candidates, _store

    while True:
        await asyncio.sleep(30)
        unchecked = [c for c in list(_store.values()) if c.cex_listed is None]
        if not unchecked:
            await asyncio.sleep(120)
            continue

        logger.info("CEX enricher: checking %d unchecked candidates", len(unchecked))
        async with http_client.pooled(timeout=12.0) as client:
            for c in unchecked:
                result = await check_cex_listed(c.chain_id, c.token_address, client)
                if result is not None:
                    c.cex_listed = result
                    logger.debug("CEX check %s → %s", c.symbol, result)
                await asyncio.sleep(4)  # ~15 req/min, stay under free-tier limit

        _save_candidates()
        await asyncio.sleep(300)  # wait 5 min before next enrichment pass
