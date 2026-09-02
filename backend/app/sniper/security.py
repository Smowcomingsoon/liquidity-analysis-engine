"""Free public security checks — no API key required.

Sources:
  GoPlus Security (api.gopluslabs.io): EVM honeypot, tax, mint, ownership.
    Covers ETH, BSC, Base, Arbitrum, Polygon, Avalanche, Tron.
  Rugcheck.xyz: Solana rug-pull risk score and flag list.
  Honeypot.is (api.honeypot.is): EVM honeypot simulation (transaction-based).
    Covers ETH, BSC, Polygon, Arbitrum, Base, Avalanche.
  CoinGecko (api.coingecko.com): token metadata enrichment (website, socials,
    description, community metrics). No API key for basic endpoints.
  Jupiter Price (price.jup.ag): Solana token price cross-validation.
  DefiLlama Coins API: cross-validates price / market-cap for any chain.
"""
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
HONEYPOTIS_BASE = "https://api.honeypot.is/v2"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
JUPITER_PRICE_BASE = "https://price.jup.ag/v6"
DEFILLAMA_BASE = "https://coins.llama.fi"

# GoPlus numeric chain IDs
GOPLUS_CHAIN_IDS: dict[str, str] = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "arbitrum": "42161",
    "polygon": "137",
    "avalanche": "43114",
    "tron": "728126428",
}

# Honeypot.is chain IDs (numeric)
HONEYPOTIS_CHAIN_IDS: dict[str, str] = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "base": "8453",
    "avalanche": "43114",
}

# CoinGecko platform slugs per chain
COINGECKO_PLATFORMS: dict[str, str] = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "base": "base",
    "arbitrum": "arbitrum-one",
    "polygon": "polygon-pos",
    "avalanche": "avalanche",
    "solana": "solana",
}

# DefiLlama chain slugs
DEFILLAMA_CHAINS: dict[str, str] = {
    "ethereum": "ethereum",
    "bsc": "bsc",
    "base": "base",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "avalanche": "avax",
    "solana": "solana",
}


@dataclass
class GoplusResult:
    is_honeypot: bool | None = None   # None → chain not supported / API down
    buy_tax: float | None = None      # 0–100 %
    sell_tax: float | None = None
    is_mintable: bool | None = None
    owner_renounced: bool | None = None   # owner == 0x0000…
    can_take_back_ownership: bool | None = None
    trading_cooldown: bool | None = None
    is_blacklisted: bool | None = None
    is_open_source: bool | None = None
    holder_count: int | None = None
    top_holder_pct: float | None = None   # 0–100


@dataclass
class RugcheckResult:
    score: int | None = None          # 0=safe, higher=riskier
    risks: list = field(default_factory=list)   # [{"name":…,"level":"warn"|"danger"}]
    rugged: bool = False


@dataclass
class HoneypotIsResult:
    is_honeypot: bool | None = None
    simulation_success: bool = False
    buy_tax: float | None = None      # 0–100 %
    sell_tax: float | None = None
    transfer_tax: float | None = None


@dataclass
class CoingeckoResult:
    description: str | None = None
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    github: str | None = None
    twitter_followers: int | None = None
    coingecko_score: float | None = None


@dataclass
class DefiLlamaPrice:
    price_usd: float | None = None
    market_cap_usd: float | None = None
    confidence: float | None = None   # 0–1


# ── GoPlus ────────────────────────────────────────────────────────────────────

async def check_goplus(
    chain_id: str, token_address: str, client: httpx.AsyncClient
) -> GoplusResult | None:
    """Call GoPlus Security API. Returns None when chain is unsupported or
    the API is unreachable."""
    try:
        if chain_id == "solana":
            url = f"{GOPLUS_BASE}/solana/token_security"
        elif chain_id in GOPLUS_CHAIN_IDS:
            url = f"{GOPLUS_BASE}/token_security/{GOPLUS_CHAIN_IDS[chain_id]}"
        else:
            return None

        r = await client.get(url, params={"contract_addresses": token_address}, timeout=8.0)
        data = r.json()
        if data.get("code") != 1:
            return None

        result_map: dict = data.get("result") or {}
        # GoPlus keys are lowercase addresses
        info = (
            result_map.get(token_address.lower())
            or result_map.get(token_address)
        )
        if not info:
            return None

        # Owner renounced ↔ owner address is zero or empty
        owner = (info.get("owner_address") or "").lower().strip()
        owner_renounced = owner in (
            "", "0x0000000000000000000000000000000000000000"
        )

        # Top-holder percent (GoPlus exposes holder list for EVM)
        holders = info.get("holders") or []
        top_pct: float | None = None
        if holders:
            try:
                top_pct = float(holders[0].get("percent", 0)) * 100
            except (TypeError, ValueError):
                pass

        return GoplusResult(
            is_honeypot=_b(info.get("is_honeypot")),
            buy_tax=_pct(info.get("buy_tax")),
            sell_tax=_pct(info.get("sell_tax")),
            is_mintable=_b(info.get("is_mintable")),
            owner_renounced=owner_renounced,
            can_take_back_ownership=_b(info.get("can_take_back_ownership")),
            trading_cooldown=_b(info.get("trading_cooldown")),
            is_blacklisted=_b(info.get("is_blacklisted")),
            is_open_source=_b(info.get("is_open_source")),
            holder_count=_int(info.get("holder_count")),
            top_holder_pct=top_pct,
        )
    except Exception as exc:
        logger.debug("GoPlus check failed %s/%s: %s", chain_id, token_address, exc)
        return None


# ── Rugcheck ──────────────────────────────────────────────────────────────────

async def check_rugcheck(mint: str, client: httpx.AsyncClient) -> RugcheckResult | None:
    """Check Solana token rug risk via Rugcheck.xyz (free, no key)."""
    try:
        r = await client.get(
            f"{RUGCHECK_BASE}/tokens/{mint}/report/summary", timeout=8.0
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict):
            return None
        risks = data.get("risks") or []
        return RugcheckResult(
            score=_int(data.get("score")),
            risks=risks,
            rugged=bool(data.get("rugged")),
        )
    except Exception as exc:
        logger.debug("Rugcheck failed %s: %s", mint, exc)
        return None


# ── Honeypot.is ───────────────────────────────────────────────────────────────

async def check_honeypot_is(
    chain_id: str, token_address: str, client: httpx.AsyncClient
) -> HoneypotIsResult | None:
    """Simulate buy/sell via Honeypot.is (free, no key).
    Covers ETH/BSC/Polygon/Arbitrum/Base/Avalanche — different simulation
    engine from GoPlus, useful as a second opinion."""
    chain_num = HONEYPOTIS_CHAIN_IDS.get(chain_id)
    if not chain_num:
        return None
    try:
        r = await client.get(
            f"{HONEYPOTIS_BASE}/IsHoneypot",
            params={"address": token_address, "chainID": chain_num},
            timeout=8.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        sim = data.get("simulationResult") or {}
        token_info = data.get("token") or {}
        return HoneypotIsResult(
            is_honeypot=data.get("isHoneypot"),
            simulation_success=bool(data.get("simulationSuccess")),
            buy_tax=_pct(sim.get("buyTax")),
            sell_tax=_pct(sim.get("sellTax")),
            transfer_tax=_pct(sim.get("transferTax") or token_info.get("transferTax")),
        )
    except Exception as exc:
        logger.debug("Honeypot.is check failed %s/%s: %s", chain_id, token_address, exc)
        return None


# ── CoinGecko ─────────────────────────────────────────────────────────────────

async def check_coingecko(
    chain_id: str, token_address: str, client: httpx.AsyncClient
) -> CoingeckoResult | None:
    """Fetch token metadata from CoinGecko by contract address (free, no key).
    Returns description, website, social links, and community size."""
    platform = COINGECKO_PLATFORMS.get(chain_id)
    if not platform:
        return None
    try:
        r = await client.get(
            f"{COINGECKO_BASE}/coins/{platform}/contract/{token_address}",
            timeout=10.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("error"):
            return None

        links = data.get("links") or {}
        desc_raw = (data.get("description") or {}).get("en") or ""
        # Strip HTML tags from description
        import re
        desc = re.sub(r"<[^>]+>", "", desc_raw).strip() or None

        homepage = next(
            (u for u in (links.get("homepage") or []) if u), None
        )
        twitter_handle = links.get("twitter_screen_name") or None
        telegram_handle = links.get("telegram_channel_identifier") or None
        github_repos = links.get("repos_url", {}).get("github") or []
        github_url = next((u for u in github_repos if u), None)

        community = data.get("community_data") or {}
        twitter_followers = _int(community.get("twitter_followers"))

        scores = data.get("coingecko_score")

        return CoingeckoResult(
            description=desc,
            website=homepage,
            twitter=f"https://twitter.com/{twitter_handle}" if twitter_handle else None,
            telegram=f"https://t.me/{telegram_handle}" if telegram_handle else None,
            github=github_url,
            twitter_followers=twitter_followers,
            coingecko_score=_float(scores),
        )
    except Exception as exc:
        logger.debug("CoinGecko check failed %s/%s: %s", chain_id, token_address, exc)
        return None


# ── Jupiter Price (Solana) ────────────────────────────────────────────────────

async def check_jupiter_price(
    mint: str, client: httpx.AsyncClient
) -> float | None:
    """Fetch Solana token price from Jupiter Aggregator (free, no key).
    Returns USD price or None when the token is not listed on Jupiter."""
    try:
        r = await client.get(
            f"{JUPITER_PRICE_BASE}/price",
            params={"ids": mint},
            timeout=6.0,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data") or {}
        info = data.get(mint)
        if not info:
            return None
        return _float(info.get("price"))
    except Exception as exc:
        logger.debug("Jupiter price check failed %s: %s", mint, exc)
        return None


# ── DefiLlama Coins ───────────────────────────────────────────────────────────

async def check_defillama_price(
    chain_id: str, token_address: str, client: httpx.AsyncClient
) -> DefiLlamaPrice | None:
    """Cross-validate token price and MC via DefiLlama Coins API (free, no key)."""
    slug = DEFILLAMA_CHAINS.get(chain_id)
    if not slug:
        return None
    try:
        coin_key = f"{slug}:{token_address}"
        r = await client.get(
            f"{DEFILLAMA_BASE}/prices/current/{coin_key}", timeout=6.0
        )
        if r.status_code != 200:
            return None
        coins: dict = (r.json().get("coins") or {})
        info = coins.get(coin_key)
        if not info:
            return None
        return DefiLlamaPrice(
            price_usd=_float(info.get("price")),
            market_cap_usd=_float(info.get("marketCap")),
            confidence=_float(info.get("confidence")),
        )
    except Exception as exc:
        logger.debug("DefiLlama price failed %s/%s: %s", chain_id, token_address, exc)
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _b(v) -> bool | None:
    if v is None:
        return None
    return str(v) == "1"


def _pct(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
