"""Traction and valuation for the Gem Finder, from DefiLlama.

Request: "ci sono progetti migliori ma non li trovo, dobbiamo fare un
algoritmo piu' profondo". The three pillars (GitHub, site, tokenomics) say
how serious a project *looks*; none of them says whether anyone *uses* it,
or whether its price is low for what it does. A gem is exactly that: real
use at a small valuation.

DefiLlama is free and lists what can be measured on-chain: TVL, fees over
30 days, TVL change over 7 days, and for each protocol its token address,
chains, GitHub organisations and market cap. Two uses:

1. Source: Solana and Robinhood Chain protocols in the Gem Finder's cap band
   enter the board's queue (`candidates`), GitHub included. They are
   projects with something on-chain by construction, not launches.
2. Lens: for any project on the board, `lookup` gives traction (0-100:
   TVL, fees, TVL growth) and value (0-100: market cap against yearly fees
   and against TVL; lower multiples score higher). A project DefiLlama does
   not list gets None, not zero: most microcaps are not there, and missing
   data is not a verdict.

The lists are refreshed every REFRESH_S and kept in memory only.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable

import httpx

from app.trading.onchain import projects_db

logger = logging.getLogger(__name__)

PROTOCOLS = "https://api.llama.fi/protocols"
FEES = "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
REFRESH_S = 6 * 3600
CHAIN_NAMES = {"solana": ("Solana",), "robinhood": ("Robinhood", "Robinhood Chain"), "base": ("Base",), "ethereum": ("Ethereum",),
               "arc": ("Arc",)}
CAP_MIN = 50_000
CAP_MAX = 10_000_000             # leaderboard.BOARD_CAP_MAX_USD (was $3M until 07/10)
TVL_MIN_UNPRICED = 50_000        # no market cap on DefiLlama: enter only with real TVL
_SKIP_CATEGORIES = {"CEX", "Chain", "Bridge", "Liquid Staking", "Canonical Bridge"}

_index: dict[str, dict] = {}     # "chain:token" -> protocol + fees
_state = {"at": 0.0, "protocols": 0, "listed": 0, "error": ""}


def reset_for_tests() -> None:
    _index.clear()
    _state.update(at=0.0, protocols=0, listed=0, error="")


def status() -> dict:
    return {"at": _state["at"] or None, "listed": _state["listed"], "error": _state["error"] or None}


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ramp(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _token(chain: str, address: str | None) -> str | None:
    """DefiLlama writes "solana:<mint>", "robinhood:0x..", or a bare 0x for
    Ethereum."""
    if address and chain == "ethereum" and address.startswith("0x") and ":" not in address:
        return address.lower()
    if not address or ":" not in address:
        return None
    prefix, token = address.split(":", 1)
    if prefix.lower() != chain or not token or token == "-":
        return None
    return token.lower() if token.startswith("0x") else token


def build_index(protocols: list[dict], fees: list[dict]) -> dict[str, dict]:
    by_slug = {str(f.get("slug") or "").lower(): f for f in fees or [] if isinstance(f, dict)}
    by_id = {str(f.get("defillamaId") or ""): f for f in fees or [] if isinstance(f, dict)}
    out: dict[str, dict] = {}
    for p in protocols or []:
        if not isinstance(p, dict):
            continue
        for chain in CHAIN_NAMES:
            token = _token(chain, p.get("address"))
            if not token:
                continue
            f = by_slug.get(str(p.get("slug") or "").lower()) or by_id.get(str(p.get("id") or "")) or {}
            out[f"{chain}:{token}"] = {
                "chain": chain, "token_address": token, "name": p.get("name"), "symbol": p.get("symbol"),
                "slug": p.get("slug"), "url": p.get("url"), "category": p.get("category"),
                "chains": p.get("chains") or [], "github": p.get("github") or [], "twitter": p.get("twitter"),
                "description": p.get("description"), "logo": p.get("logo"),
                "tvl": _num(p.get("tvl")), "change_7d": _num(p.get("change_7d")), "mcap": _num(p.get("mcap")),
                "fees_24h": _num(f.get("total24h")), "fees_30d": _num(f.get("total30d")),
            }
    return out


def lookup(chain: str, token: str) -> dict | None:
    return _index.get(f"{chain}:{token.lower() if token.startswith('0x') else token}")


def judge(p: dict | None, mcap: float | None) -> dict | None:
    """{traction, value, facts, reasons} or None when DefiLlama has nothing."""
    if not p:
        return None
    tvl, f30, ch = p.get("tvl"), p.get("fees_30d"), p.get("change_7d")
    if not tvl and not f30:
        return None
    reasons: list[str] = []
    pts = 0.0
    if tvl:
        pts += 40 * _ramp(math.log10(max(tvl, 1)), 4, 7)          # $10k -> 0, $10M -> full
        reasons.append(f"TVL ${tvl / 1e6:,.2f}M" if tvl >= 1e6 else f"TVL ${tvl / 1e3:,.0f}k")
    if f30:
        pts += 40 * _ramp(math.log10(max(f30, 1)), 3, 6)          # $1k/month -> 0, $1M -> full
        reasons.append(f"fees ${f30 / 1e3:,.0f}k in 30 days")
    if ch is not None and tvl:
        pts += 20 * _ramp(ch, -10, 20)
        if ch >= 10:
            reasons.append(f"TVL {ch:+.0f}% in 7 days")
    mcap = mcap or p.get("mcap")
    facts = {"tvl": tvl, "fees_30d": f30, "tvl_change_7d": ch, "category": p.get("category"),
             "slug": p.get("slug"), "mcap_to_fees": None, "mcap_to_tvl": None}
    value_parts = []
    if mcap and f30:
        multiple = mcap / (f30 * 365 / 30)
        facts["mcap_to_fees"] = round(multiple, 1)
        value_parts.append(1 - _ramp(math.log10(max(multiple, 0.01)), 1, 2.3))   # 10x -> full, 200x -> 0
        if multiple <= 20:
            reasons.append(f"valued at {multiple:.0f}x yearly fees")
    if mcap and tvl:
        multiple = mcap / tvl
        facts["mcap_to_tvl"] = round(multiple, 2)
        value_parts.append(1 - _ramp(math.log10(max(multiple, 0.001)), -0.3, 0.7))  # 0.5x -> full, 5x -> 0
        if multiple <= 1:
            reasons.append(f"market cap {multiple:.2f}x its TVL")
    value = round(100 * sum(value_parts) / len(value_parts), 1) if value_parts else None
    return {"traction": round(pts, 1), "value": value, "facts": facts, "reasons": reasons[:4]}


def candidates(index: dict[str, dict], band: bool = True) -> list[dict]:
    """Protocols worth a look: our chains, in band (or with real TVL when
    DefiLlama has no market cap), not a CEX, bridge or chain. With
    band=False, every protocol on our chains (for the database)."""
    out = []
    for p in index.values():
        if p.get("category") in _SKIP_CATEGORIES:
            continue
        mcap, tvl = p.get("mcap"), p.get("tvl") or 0
        if not band:
            pass
        elif mcap:
            if not (CAP_MIN <= mcap <= CAP_MAX):
                continue
        elif tvl < TVL_MIN_UNPRICED:
            continue
        gh = [f"https://github.com/{g}" for g in p.get("github") or [] if isinstance(g, str) and g]
        tw = p.get("twitter")
        out.append({"chain": p["chain"], "token_address": p["token_address"], "symbol": p.get("symbol") or "?",
                    "name": p.get("name") or "", "website": p.get("url"), "market_cap": mcap,
                    "github": gh[0] if gh else None, "description": p.get("description"),
                    "image": p.get("logo"), "categories": [p["category"]] if p.get("category") else [],
                    "socials": [{"type": "twitter", "url": f"https://x.com/{tw}"}] if tw else []})
    return out


async def poll(client: httpx.AsyncClient, add: Callable[[dict, str], bool], now: float | None = None) -> int:
    """Refresh the lists (every REFRESH_S) and queue the candidates."""
    now = now or time.time()
    if now - _state["at"] < REFRESH_S:
        return 0
    _state["at"] = now
    r = await client.get(PROTOCOLS, timeout=60.0)
    if r.status_code != 200:
        _state["error"] = f"protocols {r.status_code}"
        raise RuntimeError(f"DefiLlama {r.status_code}")
    try:
        fr = await client.get(FEES, timeout=60.0)
        fees = (fr.json() or {}).get("protocols") or [] if fr.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        fees = []          # traction without fees is still traction
    protocols = r.json()
    _index.clear()
    _index.update(build_index(protocols, fees))
    _state.update(protocols=len(protocols or []), listed=len(_index), error="")
    projects_db.upsert_many(candidates(_index, band=False), "defillama", now)
    added = 0
    for item in candidates(_index):
        added += bool(add(item, "defillama"))
    return added
