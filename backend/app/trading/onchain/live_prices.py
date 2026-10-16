"""Live USD prices of the tokens a wallet holds, for the ticking balance.

Lorenzo, 29/09, on pump.fun's app: "il saldo delle crypto nel wallet si
aggiorna in modo frenetico costantemente [...] nel simulato vedo che
praticamente non si aggiorna mai il valore del token".

One shared cache for every screen and every user: the browser asks every
couple of seconds, the server asks DexScreener at most once per token per
TTL, in batches of 30 per chain (its /tokens/v1 endpoint, 300 calls a
minute, free, no key, every trade moves priceUsd). GeckoTerminal is the
fallback when DexScreener does not answer (30 calls a minute, so only
then). A token neither source answers keeps its last price with its age,
up to STALE_S; after that it is left out, never invented.

Lorenzo, 30/09: "il saldo delle posizioni si aggiorni piu' velocemente
possibile". Measured that day: DexScreener answers from a CDN cache up to
30 s old (max-age=30), so a 2 s poll still showed the same price for half a
minute. Now, fastest first:
- EVM chains: the pool's own state, block by block (pool_prices.py);
- Solana: Jupiter's price API (free, no key, the last swaps, 5 s CDN);
- DexScreener with a cache-busting parameter, then GeckoTerminal.
The browser gets them pushed (GET /api/onchain/live-stream, SSE) instead of
asking every 2 s.
"""
import asyncio
import time
from collections import deque

import httpx

from app import http_client

from app.trading.onchain import pool_prices

DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{addrs}"
JUPITER_PRICES = "https://lite-api.jup.ag/price/v3"
GECKO_TOKENS = "https://api.geckoterminal.com/api/v2/simple/networks/{network}/token_price/{addrs}"
TTL_S = 1.0
PUSH_S = 1.0
STALE_S = 180.0
BATCH = 30
MAX_IDS = 60
# Our own budget under DexScreener's 300/min, shared by every user.
DS_PER_MIN = 240
GT_PER_MIN = 25
JUP_PER_MIN = 30     # its CDN copy lives 5 s anyway
JUP_BATCH = 50
# per chain, one batched eth_call for all its pools; Robinhood's only RPC
# answers 429 easily and also serves balances and swaps, so it gets less
RPC_PER_MIN = {"robinhood": 30}
RPC_PER_MIN_DEFAULT = 90

CHAINS = {"solana": "solana", "robinhood": "robinhood", "base": "base", "ethereum": "eth", "bsc": "bsc", "arc": "arc"}

_cache: dict[tuple[str, str], tuple[float, float, str]] = {}   # (chain, addr) -> (price, fetched_at, source)
_calls: dict[str, deque] = {"ds": deque(), "gt": deque(), "jup": deque()}
_lock = asyncio.Lock()
_clock = time.time


def reset_for_tests() -> None:
    _cache.clear()
    _calls.clear()
    _calls.update({"ds": deque(), "gt": deque(), "jup": deque()})
    pool_prices.reset_for_tests()


def _key(chain: str, addr: str) -> tuple[str, str]:
    # EVM addresses are case-insensitive, Solana mints are not.
    return chain, addr.lower() if addr.startswith("0x") else addr


def _budget(name: str, per_min: int) -> bool:
    now = _clock()
    q = _calls.setdefault(name, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_min:
        return False
    q.append(now)
    return True


def parse_ids(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        chain, _, addr = part.strip().partition(":")
        if chain in CHAINS and addr.isalnum() and 32 <= len(addr) <= 44:
            k = _key(chain, addr)
            if k not in out:
                out.append(k)
    return out[:MAX_IDS]


def _best_prices(pairs: list[dict], chain: str) -> dict[tuple[str, str], float]:
    """Per base token, the price of its deepest pool (remembered for
    pool_prices, which reads that pool on-chain)."""
    best: dict[tuple[str, str], tuple[float, float, dict]] = {}
    for p in pairs or []:
        try:
            if p.get("chainId") != chain:
                continue
            k = _key(chain, p["baseToken"]["address"])
            price = float(p["priceUsd"])
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if price > 0 and (k not in best or liq > best[k][1]):
            best[k] = (price, liq, p)
    for k, v in best.items():
        pool_prices.note_pair(k, v[2])
    return {k: v[0] for k, v in best.items()}


async def _dexscreener(client: httpx.AsyncClient, chain: str, addrs: list[str]) -> dict[tuple[str, str], float]:
    if not _budget("ds", DS_PER_MIN):
        return {}
    try:
        # the parameter skips the CDN copy, up to 30 s old
        r = await client.get(DEXSCREENER_TOKENS.format(chain=chain, addrs=",".join(addrs)),
                             params={"t": int(_clock() * 1000)})
        if r.status_code != 200:
            return {}
        return _best_prices(r.json(), chain)
    except (httpx.HTTPError, ValueError):
        return {}


async def _gecko(client: httpx.AsyncClient, chain: str, addrs: list[str]) -> dict[tuple[str, str], float]:
    if not _budget("gt", GT_PER_MIN):
        return {}
    try:
        r = await client.get(GECKO_TOKENS.format(network=CHAINS[chain], addrs=",".join(addrs)))
        if r.status_code != 200:
            return {}
        prices = r.json()["data"]["attributes"]["token_prices"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return {}
    out = {}
    for addr, v in (prices or {}).items():
        try:
            if v is not None and float(v) > 0:
                out[_key(chain, addr)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


async def _jupiter(client: httpx.AsyncClient, addrs: list[str]) -> dict[tuple[str, str], float] | None:
    """Solana prices from Jupiter; None when our budget is spent (the
    cached price stays, rather than switching source for one tick)."""
    if not _budget("jup", JUP_PER_MIN):
        return None
    try:
        r = await client.get(JUPITER_PRICES, params={"ids": ",".join(addrs)})
        if r.status_code != 200:
            return {}
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return {}
    out = {}
    for addr, v in (data or {}).items():
        try:
            price = float(v["usdPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if price > 0:
            out[("solana", addr)] = price
    return out


async def _onchain(client: httpx.AsyncClient, chain: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
    if not _budget(f"rpc:{chain}", RPC_PER_MIN.get(chain, RPC_PER_MIN_DEFAULT)):
        return {}
    return await pool_prices.read(client, chain, keys)


async def _refresh(keys: list[tuple[str, str]]) -> None:
    async with http_client.pooled(timeout=4.0) as client:
        # 0. layer chains: no DEX pool, the price comes from the exchange
        from app.trading.onchain import cex_candles, projects_db
        layer = [k for k in keys if k[0] == "layer"]
        if layer:
            now = _clock()
            for _, addr in layer:
                row = await asyncio.to_thread(projects_db.get, "layer", addr)
                cex = (row.get("overall") or {}).get("cex") if row else None
                if not cex or not cex.get("exchange") or not cex.get("pair"):
                    continue
                p = await cex_candles.price(f"{cex['exchange']}:{cex['pair']}")
                if p is not None:
                    _cache[("layer", addr)] = (p, now, "cex")
        keys = [k for k in keys if k[0] != "layer"]
        # 1. the fast sources: pools on-chain (EVM), Jupiter (Solana)
        evm: dict[str, list[tuple[str, str]]] = {}
        for k in keys:
            if pool_prices.usable(k):
                evm.setdefault(k[0], []).append(k)
        sol = [k[1] for k in keys if k[0] == "solana"]
        tasks = [_onchain(client, c, ks) for c, ks in evm.items()]
        tasks += [_jupiter(client, sol[i:i + JUP_BATCH]) for i in range(0, len(sol), JUP_BATCH)]
        done: set[tuple[str, str]] = set()
        results = await asyncio.gather(*tasks)
        now = _clock()
        for n, got in enumerate(results):
            src = "onchain" if n < len(evm) else "jupiter"
            if got is None:     # Jupiter budget: keep what we have this tick
                batch = sol[(n - len(evm)) * JUP_BATCH:(n - len(evm) + 1) * JUP_BATCH]
                done.update(("solana", a) for a in batch if ("solana", a) in _cache)
                continue
            for k, price in got.items():
                _cache[k] = (price, now, src)
                done.add(k)
        # 2. the rest from DexScreener; it also (re)learns each EVM token's
        # pool and quote price, once a minute, without replacing the
        # on-chain price of that tick
        by_chain: dict[str, list[str]] = {}
        for chain, addr in keys:
            if (chain, addr) not in done or pool_prices.needs_meta((chain, addr)):
                by_chain.setdefault(chain, []).append(addr)
        jobs = []
        for chain, addrs in by_chain.items():
            for i in range(0, len(addrs), BATCH):
                jobs.append((chain, addrs[i:i + BATCH]))
        found = await asyncio.gather(*(_dexscreener(client, c, a) for c, a in jobs))
        now = _clock()
        seen = set()
        for got in found:
            for k, price in got.items():
                seen.add(k)
                if k not in done:
                    _cache[k] = (price, now, "dexscreener")
        missing = [(c, [a for a in batch if (c, a) not in seen and (c, a) not in done]) for c, batch in jobs]
        missing = [(c, a) for c, a in missing if a]
        if missing:
            for got in await asyncio.gather(*(_gecko(client, c, a) for c, a in missing)):
                for k, price in got.items():
                    _cache[k] = (price, _clock(), "geckoterminal")


async def prices(keys: list[tuple[str, str]]) -> dict[str, dict]:
    """{"chain:addr": {"p": price, "age": seconds, "src": source}} for every
    key with a price no older than STALE_S."""
    stale = [k for k in keys if k not in _cache or _clock() - _cache[k][1] >= TTL_S]
    if stale:
        async with _lock:   # one fetch at a time: the next caller finds it cached
            stale = [k for k in stale if k not in _cache or _clock() - _cache[k][1] >= TTL_S]
            if stale:
                await _refresh(stale)
    now = _clock()
    out = {}
    for k in keys:
        hit = _cache.get(k)
        if hit and now - hit[1] < STALE_S:
            out[f"{k[0]}:{k[1]}"] = {"p": hit[0], "age": round(now - hit[1], 1), "src": hit[2]}
    return out
