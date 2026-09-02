"""Token discovery: merges DEXScreener's "latest profiles" feed (rich
metadata — website, description, socials — but a single global feed
dominated by whichever chain is hot right now) with GeckoTerminal's
per-network "new pools" feed (queried once per supported chain, so every
chain actually gets explored every scan instead of only whichever one
happens to dominate DEXScreener's global feed at that moment)."""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
SUPPORTED_CHAINS = {"ethereum", "bsc", "base", "arbitrum", "avalanche", "solana", "polygon", "tron", "ton", "sui"}

# Our chain_id -> GeckoTerminal network id
GECKO_NETWORKS = {
    "ethereum": "eth",
    "bsc": "bsc",
    "base": "base",
    "arbitrum": "arbitrum",
    "avalanche": "avax",
    "solana": "solana",
    "polygon": "polygon_pos",
    "tron": "tron",
    "ton": "ton",
    "sui": "sui-network",
}

# Deliberately loose: real-world scans showed the excluded pool is bimodal
# (near-worthless launches seconds old at one end, already-established
# tokens surfacing via a fresh LP at the other) — these thresholds still
# cut both of those, just with more headroom for genuinely early but not
# yet fully "seasoned" projects in between.
MC_MIN = 1_000
MC_MAX = 15_000_000
LIQUIDITY_MIN = 500
AGE_MAX_H = 720.0


@dataclass
class RawToken:
    chain_id: str
    token_address: str
    pair_address: str
    symbol: str
    name: str
    dex_id: str
    price_usd: float
    market_cap: float | None
    liquidity_usd: float
    volume_h24: float
    volume_h1: float
    volume_m5: float
    buys_h1: int
    sells_h1: int
    pair_created_at_ms: int
    socials: list = field(default_factory=list)
    website: str | None = None
    description: str | None = None

    @property
    def age_hours(self) -> float:
        return (time.time() * 1000 - self.pair_created_at_ms) / 3_600_000

    @property
    def buy_sell_ratio(self) -> float:
        return self.buys_h1 / max(self.sells_h1, 1)

    @property
    def pair_url(self) -> str:
        return f"https://dexscreener.com/{self.chain_id}/{self.pair_address}"


def _passes_primary(token: RawToken) -> tuple[bool, str]:
    mc = token.market_cap
    # Only filter on MC when the data is actually present
    if mc is not None:
        if mc < MC_MIN:
            return False, f"MC ${mc:,.0f} troppo basso"
        if mc > MC_MAX:
            return False, f"MC ${mc:,.0f} troppo alto"
    if token.liquidity_usd < LIQUIDITY_MIN:
        return False, f"Liquidity ${token.liquidity_usd:,.0f}"
    age = token.age_hours
    if age > AGE_MAX_H:
        return False, f"Troppo vecchio {age:.0f}h"
    return True, "ok"


def _parse_pair(
    pair: dict,
    socials: list,
    website: str | None,
    description: str | None,
) -> Optional["RawToken"]:
    try:
        chain = pair.get("chainId", "")
        if chain not in SUPPORTED_CHAINS:
            return None
        base = pair.get("baseToken", {})
        txns_h1 = pair.get("txns", {}).get("h1", {})
        vol = pair.get("volume", {})
        liq = pair.get("liquidity", {})
        mc = pair.get("marketCap") or pair.get("fdv")
        created = pair.get("pairCreatedAt")
        if not created:
            return None
        return RawToken(
            chain_id=chain,
            token_address=(base.get("address") or "").lower(),
            pair_address=(pair.get("pairAddress") or "").lower(),
            symbol=base.get("symbol", ""),
            name=base.get("name", ""),
            dex_id=pair.get("dexId", ""),
            price_usd=float(pair.get("priceUsd") or 0),
            market_cap=float(mc) if mc else None,
            liquidity_usd=float((liq.get("usd") or 0)),
            volume_h24=float(vol.get("h24") or 0),
            volume_h1=float(vol.get("h1") or 0),
            volume_m5=float(vol.get("m5") or 0),
            buys_h1=int(txns_h1.get("buys") or 0),
            sells_h1=int(txns_h1.get("sells") or 0),
            pair_created_at_ms=int(created),
            socials=socials,
            website=website,
            description=description,
        )
    except Exception:
        return None


def _parse_gecko_pool(chain_id: str, pool: dict) -> Optional["RawToken"]:
    try:
        attrs = pool.get("attributes") or {}
        created = attrs.get("pool_created_at")
        if not created:
            return None
        created_ms = int(
            datetime.fromisoformat(created.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
            * 1000
        )
        pair_addr = (attrs.get("address") or "").lower()
        base_rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        # id looks like "<network>_<address>"
        base_addr = base_rel.get("id", "").split("_", 1)[-1].lower()
        dex_id = (((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}).get(
            "id", ""
        )
        symbol = (attrs.get("name") or "?/?").split(" / ")[0].strip()
        mc = attrs.get("market_cap_usd") or attrs.get("fdv_usd")
        vol = attrs.get("volume_usd") or {}
        txns_h1 = (attrs.get("transactions") or {}).get("h1") or {}
        if not base_addr or not pair_addr:
            return None
        return RawToken(
            chain_id=chain_id,
            token_address=base_addr,
            pair_address=pair_addr,
            symbol=symbol,
            name=symbol,
            dex_id=dex_id,
            price_usd=float(attrs.get("base_token_price_usd") or 0),
            market_cap=float(mc) if mc else None,
            liquidity_usd=float(attrs.get("reserve_in_usd") or 0),
            volume_h24=float(vol.get("h24") or 0),
            volume_h1=float(vol.get("h1") or 0),
            volume_m5=float(vol.get("m5") or 0),
            buys_h1=int(txns_h1.get("buys") or 0),
            sells_h1=int(txns_h1.get("sells") or 0),
            pair_created_at_ms=created_ms,
        )
    except Exception:
        return None


GECKO_PAGES_PER_CHAIN = 6  # ~20 pools/page -> up to ~120 pools/chain/scan

# GeckoTerminal's anonymous/free tier hard-caps pagination at page 10 for
# every list endpoint (page 11+ returns 401 "Upgrade to Analyst plan…") —
# this is the real ceiling, not a number we chose.
GECKO_MAX_PAGE = 10


_GECKO_PAGE_DELAY = 2.0   # GeckoTerminal free tier: ~30 req/min
_GECKO_RATE_RETRY_WAIT = 12.0  # back-off on 429 before retry


async def _fetch_gecko_pages(
    client: httpx.AsyncClient, chain_id: str, endpoint: str, pages: int
) -> list[RawToken]:
    """Paginate one GeckoTerminal list endpoint ("new_pools" or "pools") for
    one chain.  Pages are fetched sequentially with a 2-second delay to
    stay inside the free-tier rate limit (~30 req/min).  A 429 is retried
    once after a longer back-off before giving up on the current chain."""
    network = GECKO_NETWORKS[chain_id]
    tokens: list[RawToken] = []
    for page in range(1, min(pages, GECKO_MAX_PAGE) + 1):
        pools: list[dict] = []
        for attempt in range(2):  # at most one retry on 429
            try:
                resp = await client.get(
                    f"{GECKOTERMINAL_BASE}/networks/{network}/{endpoint}",
                    params={"page": page},
                )
            except Exception as exc:
                logger.warning(
                    "GeckoTerminal fetch error (%s/%s, page %d): %s",
                    chain_id, endpoint, page, exc,
                )
                return tokens

            if resp.status_code == 200:
                pools = resp.json().get("data") or []
                break
            if resp.status_code == 429 and attempt == 0:
                logger.warning(
                    "GeckoTerminal 429 on %s/%s page %d — backing off %.0fs",
                    chain_id, endpoint, page, _GECKO_RATE_RETRY_WAIT,
                )
                await asyncio.sleep(_GECKO_RATE_RETRY_WAIT)
                continue
            # Non-200, non-retried 429: stop this chain silently
            logger.warning(
                "GeckoTerminal %d on %s/%s page %d — stopping chain",
                resp.status_code, chain_id, endpoint, page,
            )
            return tokens

        for pool in pools:
            token = _parse_gecko_pool(chain_id, pool)
            if token:
                tokens.append(token)

        if page < pages:
            await asyncio.sleep(_GECKO_PAGE_DELAY)

    return tokens


async def _fetch_gecko_chain(client: httpx.AsyncClient, chain_id: str) -> list[RawToken]:
    """New pools on one chain — this is what guarantees every supported
    chain is actually explored (and with real depth, not just the first
    page) each scan, instead of relying on DEXScreener's single global
    "latest profiles" feed to happen to include it."""
    return await _fetch_gecko_pages(client, chain_id, "new_pools", GECKO_PAGES_PER_CHAIN)


async def fetch_market_chain(client: httpx.AsyncClient, chain_id: str) -> list[RawToken]:
    """Broad market pull for one chain: every pool GeckoTerminal will hand
    an anonymous caller (not just new ones), up to its hard page cap. Used
    by the separate market screener, not the sniper's every-5-minutes scan
    — this alone is ~200 requests-worth of pages across six chains and has
    no reason to run that often."""
    return await _fetch_gecko_pages(client, chain_id, "pools", GECKO_MAX_PAGE)


async def fetch_dexscreener_boosted(client: httpx.AsyncClient) -> list[RawToken]:
    """Fetch boosted tokens from DexScreener (top + latest boost feeds).
    Tokens that paid for promotion but often have genuine liquidity and volume."""
    meta: dict[str, dict] = {}
    for endpoint in ("/token-boosts/top/v1", "/token-boosts/latest/v1"):
        try:
            resp = await client.get(f"{DEXSCREENER_BASE}{endpoint}")
            items: list[dict] = resp.json() if resp.status_code == 200 else []
        except Exception:
            continue
        for p in items:
            addr = (p.get("tokenAddress") or "").lower()
            if not addr or p.get("chainId") not in SUPPORTED_CHAINS:
                continue
            if addr in meta:
                continue
            links = p.get("links") or []
            meta[addr] = {
                "website": next((lk["url"] for lk in links if lk.get("type") == "website"), None),
                "socials": [
                    {"type": lk.get("type"), "url": lk.get("url")}
                    for lk in links
                    if lk.get("type") != "website"
                ],
                "description": p.get("description"),
            }

    tokens: list[RawToken] = []
    addresses = list(meta.keys())
    for i in range(0, len(addresses), 30):
        batch = addresses[i : i + 30]
        try:
            resp = await client.get(
                f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(batch)}"
            )
            if resp.status_code != 200:
                continue
            pairs: list[dict] = resp.json().get("pairs") or []
        except Exception:
            continue

        best: dict[str, dict] = {}
        for pair in pairs:
            base_addr = (pair.get("baseToken", {}).get("address") or "").lower()
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if liq > float((best.get(base_addr, {}).get("liquidity") or {}).get("usd") or 0):
                best[base_addr] = pair

        for addr, pair in best.items():
            if pair.get("chainId") not in SUPPORTED_CHAINS:
                continue
            m = meta.get(addr, {})
            token = _parse_pair(
                pair, m.get("socials", []), m.get("website"), m.get("description")
            )
            if token:
                tokens.append(token)

    return tokens


async def fetch_pumpfun_trending(client: httpx.AsyncClient) -> list[RawToken]:
    """Fetch trending Solana meme coins from Pump.fun's public feed.
    No h1 buy/sell data available — import filter will apply B/S check separately."""
    tokens: list[RawToken] = []
    try:
        resp = await client.get(
            "https://frontend-api.pump.fun/coins",
            params={"sort": "last_trade_unix_timestamp", "order": "DESC", "limit": 200},
        )
        if resp.status_code != 200:
            return tokens
        coins: list[dict] = resp.json()
        if not isinstance(coins, list):
            return tokens
    except Exception:
        return tokens

    for coin in coins:
        try:
            mint = (coin.get("mint") or "").lower()
            if not mint:
                continue
            symbol = coin.get("symbol") or "?"
            name = coin.get("name") or symbol
            mc = coin.get("usd_market_cap")
            # virtual_sol_reserves is in lamports; approximate liquidity in USD
            sol_lamports = float(coin.get("virtual_sol_reserves") or 0)
            liquidity_usd = (sol_lamports / 1e9) * 150.0  # ~$150/SOL rough estimate
            created_ts = coin.get("created_timestamp")
            created_ms = int(created_ts * 1000) if created_ts else int(time.time() * 1000)
            tokens.append(RawToken(
                chain_id="solana",
                token_address=mint,
                pair_address=mint,
                symbol=symbol,
                name=name,
                dex_id="pump.fun",
                price_usd=0.0,
                market_cap=float(mc) if mc else None,
                liquidity_usd=liquidity_usd,
                volume_h24=0.0,
                volume_h1=0.0,
                volume_m5=0.0,
                buys_h1=0,
                sells_h1=0,
                pair_created_at_ms=created_ms,
            ))
        except Exception:
            continue

    return tokens


@dataclass
class ScanFetchResult:
    results: list[tuple[RawToken, bool, str]]
    total_profiles: int
    unsupported_chain: int
    per_chain_new_pools: int


async def _fetch_dexscreener_profiles(
    client: httpx.AsyncClient,
) -> tuple[list[RawToken], int, int]:
    """DEXScreener's global "latest profiles" feed — richer metadata
    (website/description/socials) where a token has a profile, but it's a
    single feed shared across every chain, so on any given scan it can be
    almost entirely dominated by whichever chain is hottest right now."""
    tokens: list[RawToken] = []
    unsupported_chain = 0
    try:
        resp = await client.get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
        profiles: list[dict] = resp.json() if resp.status_code == 200 else []
    except Exception:
        return [], 0, 0

    if not profiles:
        return [], 0, 0

    # Build profile metadata indexed by token address
    meta: dict[str, dict] = {}
    for p in profiles:
        addr = (p.get("tokenAddress") or "").lower()
        if not addr:
            continue
        links = p.get("links") or []
        website = next((lk["url"] for lk in links if lk.get("type") == "website"), None)
        socials = [
            {"type": lk.get("type"), "url": lk.get("url")}
            for lk in links
            if lk.get("type") != "website"
        ]
        meta[addr] = {
            "website": website,
            "socials": socials,
            "description": p.get("description"),
        }

    # Batch fetch pair data (30 addresses per request is DEXScreener's practical limit)
    addresses = list(meta.keys())
    for i in range(0, len(addresses), 30):
        batch = addresses[i : i + 30]
        try:
            resp = await client.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(batch)}")
            if resp.status_code != 200:
                continue
            pairs: list[dict] = resp.json().get("pairs") or []
        except Exception:
            continue

        # Keep only the most liquid pair per base token
        best: dict[str, dict] = {}
        for pair in pairs:
            base_addr = (pair.get("baseToken", {}).get("address") or "").lower()
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            prev_liq = float((best.get(base_addr, {}).get("liquidity") or {}).get("usd") or 0)
            if liq > prev_liq:
                best[base_addr] = pair

        for addr, pair in best.items():
            m = meta.get(addr, {})
            if pair.get("chainId") not in SUPPORTED_CHAINS:
                unsupported_chain += 1
                continue
            token = _parse_pair(pair, m.get("socials", []), m.get("website"), m.get("description"))
            if token:
                tokens.append(token)

    return tokens, len(profiles), unsupported_chain


async def fetch_all_tokens() -> ScanFetchResult:
    """Discover candidate tokens from two sources and return every one found,
    each tagged with its primary-filter verdict (pass/fail + reason) —
    including the ones that fail, so callers can show the full picture of
    what the scanner looked at, not just the tokens that made it through.

    DEXScreener's "latest profiles" feed alone under-represents most chains
    (it's one global feed, easily dominated by whichever chain is hottest at
    the moment) — so every supported chain is also queried directly via
    GeckoTerminal's per-network "new pools" feed, guaranteeing each one is
    actually explored on every scan."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        dex_tokens, total_profiles, unsupported_chain = await _fetch_dexscreener_profiles(client)
        gecko_lists = await asyncio.gather(
            *(_fetch_gecko_chain(client, chain_id) for chain_id in GECKO_NETWORKS)
        )

    seen: set[tuple[str, str]] = set()
    all_raw: list[RawToken] = []
    for token in dex_tokens:
        key = (token.chain_id, token.token_address)
        if key in seen:
            continue
        seen.add(key)
        all_raw.append(token)

    per_chain_new_pools = 0
    for chain_tokens in gecko_lists:
        for token in chain_tokens:
            per_chain_new_pools += 1
            key = (token.chain_id, token.token_address)
            if key in seen:
                continue
            seen.add(key)
            all_raw.append(token)

    results = [(token, *_passes_primary(token)) for token in all_raw]

    return ScanFetchResult(
        results=results,
        total_profiles=total_profiles,
        unsupported_chain=unsupported_chain,
        per_chain_new_pools=per_chain_new_pools,
    )


async def fetch_candidates() -> list[RawToken]:
    """Fetch recent DEXScreener token profiles and return those passing primary filters."""
    return [token for token, ok, _ in (await fetch_all_tokens()).results if ok]
