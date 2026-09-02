"""Market screener: a broad, on-demand pull of every pool GeckoTerminal will
hand an anonymous caller across all supported chains (not just brand-new
ones, unlike the sniper's scan). No on-chain scoring here — that pipeline
does one Etherscan-family call per criterion per candidate, which is fine
for the sniper's handful of primary-filtered survivors but would blow
through explorer rate limits applied to thousands of tokens. This is pure
browse/filter data: price, market cap, liquidity, volume, age.

Persisted to disk (backend/data/sniper_market.json), same convention as
cache.py, so the last pull survives a reload/redeploy without re-fetching."""
import asyncio
import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from .scanner import GECKO_NETWORKS, RawToken, fetch_market_chain

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_MARKET_FILE = _DATA_DIR / "sniper_market.json"


@dataclass
class MarketToken:
    chain_id: str
    token_address: str
    pair_address: str
    symbol: str
    name: str
    dex_id: str
    pair_url: str
    price_usd: float
    market_cap: Optional[float]
    liquidity_usd: float
    volume_h24: float
    age_hours: float
    buy_sell_ratio: float
    last_updated: float = field(default_factory=time.time)


_store: dict[str, MarketToken] = {}  # key = f"{chain_id}:{token_address}"
_last_scan: float = 0.0
_scan_lock = asyncio.Lock()
_subscribers: list[asyncio.Queue] = []


def _key(chain_id: str, token_address: str) -> str:
    return f"{chain_id}:{token_address}"


def _load() -> None:
    if not _MARKET_FILE.exists():
        return
    try:
        raw = json.loads(_MARKET_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for item in raw:
        try:
            t = MarketToken(**item)
            _store[_key(t.chain_id, t.token_address)] = t
        except (TypeError, ValueError):
            continue


def _save() -> None:
    payload = [dataclasses.asdict(t) for t in _store.values()]
    try:
        _MARKET_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("Market screener: failed to persist store: %s", exc)


_load()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def scan_in_progress() -> bool:
    return _scan_lock.locked()


def _broadcast(event: dict) -> None:
    event.setdefault("ts", time.time())
    for q in list(_subscribers):
        q.put_nowait(event)


def last_scan_ts() -> float:
    return _last_scan


def get_tokens(
    chain: Optional[str] = None,
    query: Optional[str] = None,
    sort: str = "liquidity",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[MarketToken], int]:
    """Filter/sort/paginate the current market store. Returns (page, total_matching)."""
    items = list(_store.values())
    if chain and chain != "all":
        items = [t for t in items if t.chain_id == chain]
    if query:
        q = query.strip().lower()
        items = [t for t in items if q in t.symbol.lower() or q in t.name.lower()]

    key_fn = {
        "liquidity": lambda t: t.liquidity_usd,
        "volume": lambda t: t.volume_h24,
        "market_cap": lambda t: t.market_cap or 0,
        "age": lambda t: -t.age_hours,
    }.get(sort, lambda t: t.liquidity_usd)
    items.sort(key=key_fn, reverse=True)

    total = len(items)
    return items[offset : offset + limit], total


def _to_market_token(token: RawToken) -> MarketToken:
    return MarketToken(
        chain_id=token.chain_id,
        token_address=token.token_address,
        pair_address=token.pair_address,
        symbol=token.symbol,
        name=token.name,
        dex_id=token.dex_id,
        pair_url=token.pair_url,
        price_usd=token.price_usd,
        market_cap=token.market_cap,
        liquidity_usd=token.liquidity_usd,
        volume_h24=token.volume_h24,
        age_hours=token.age_hours,
        buy_sell_ratio=token.buy_sell_ratio,
        last_updated=time.time(),
    )


async def run_market_scan() -> int:
    """Pull every pool GeckoTerminal will hand us across all supported
    chains, replacing the store. Returns the resulting token count."""
    global _last_scan
    async with _scan_lock:
        t0 = time.time()
        _broadcast({"type": "market_scan_start"})
        added = 0
        async with httpx.AsyncClient(timeout=20.0) as client:
            for chain_id in GECKO_NETWORKS:
                _broadcast({"type": "market_chain_start", "chain": chain_id})
                try:
                    tokens = await fetch_market_chain(client, chain_id)
                except Exception as exc:
                    logger.warning("Market screener fetch error (%s): %s", chain_id, exc)
                    tokens = []
                for token in tokens:
                    _store[_key(token.chain_id, token.token_address)] = _to_market_token(token)
                    added += 1
                _broadcast({
                    "type": "market_chain_done",
                    "chain": chain_id,
                    "count": len(tokens),
                })

        _last_scan = time.time()
        _save()
        _broadcast({
            "type": "market_scan_end",
            "total": len(_store),
            "duration": time.time() - t0,
        })
        logger.info("Market screener scan complete: %d tokens stored", len(_store))
        return added
