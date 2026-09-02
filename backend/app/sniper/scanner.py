"""DEXScreener scanner: fetch recently-profiled tokens and apply primary filters."""
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

DEXSCREENER_BASE = "https://api.dexscreener.com"
SUPPORTED_CHAINS = {"ethereum", "bsc", "base", "arbitrum", "avalanche", "solana"}

MC_MIN = 50_000
MC_MAX = 500_000
LIQUIDITY_MIN = 30_000
AGE_MIN_H = 1.0
AGE_MAX_H = 168.0
VOL_MC_RATIO_MIN = 0.15
BUY_SELL_RATIO_MIN = 1.3


@dataclass
class RawToken:
    chain_id: str
    token_address: str
    pair_address: str
    symbol: str
    name: str
    dex_id: str
    price_usd: float
    market_cap: Optional[float]
    liquidity_usd: float
    volume_h24: float
    volume_h1: float
    volume_m5: float
    buys_h1: int
    sells_h1: int
    pair_created_at_ms: int
    socials: list = field(default_factory=list)
    website: Optional[str] = None
    description: Optional[str] = None

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
    if not token.market_cap:
        return False, "market_cap non disponibile"
    if not (MC_MIN <= token.market_cap <= MC_MAX):
        return False, f"MC ${token.market_cap:,.0f}"
    if token.liquidity_usd < LIQUIDITY_MIN:
        return False, f"Liquidity ${token.liquidity_usd:,.0f}"
    age = token.age_hours
    if not (AGE_MIN_H <= age <= AGE_MAX_H):
        return False, f"Age {age:.1f}h"
    if token.volume_h24 and token.market_cap:
        ratio = token.volume_h24 / token.market_cap
        if ratio < VOL_MC_RATIO_MIN:
            return False, f"Vol/MC {ratio:.2f}"
    if token.buy_sell_ratio < BUY_SELL_RATIO_MIN:
        return False, f"B/S {token.buy_sell_ratio:.2f}"
    return True, "ok"


def _parse_pair(
    pair: dict,
    socials: list,
    website: Optional[str],
    description: Optional[str],
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


async def fetch_candidates() -> list[RawToken]:
    """Fetch recent DEXScreener token profiles and return those passing primary filters."""
    candidates: list[RawToken] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
            profiles: list[dict] = resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

        if not profiles:
            return []

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
                resp = await client.get(
                    f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(batch)}"
                )
                if resp.status_code != 200:
                    continue
                pairs: list[dict] = (resp.json().get("pairs") or [])
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
                token = _parse_pair(pair, m.get("socials", []), m.get("website"), m.get("description"))
                if token:
                    ok, _ = _passes_primary(token)
                    if ok:
                        candidates.append(token)

    return candidates
