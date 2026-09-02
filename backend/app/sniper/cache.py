"""In-memory sniper candidate store and background polling loop."""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .scanner import RawToken, fetch_candidates
from .scorer import TokenScore, score

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300  # 5 min

SCORE_MIN = 8
TOKENOMICS_MIN = 2
UTILITY_MIN = 1


@dataclass
class Candidate:
    # identity
    chain_id: str
    token_address: str
    pair_address: str
    symbol: str
    name: str
    dex_id: str
    pair_url: str
    # market
    price_usd: float
    market_cap: Optional[float]
    liquidity_usd: float
    volume_h24: float
    volume_h1: float
    age_hours: float
    buy_sell_ratio: float
    # links
    website: Optional[str]
    socials: list = field(default_factory=list)
    # score
    score_total: int = 0
    score_max: int = 13
    score_items: list = field(default_factory=list)
    # metadata
    first_seen: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


_store: dict[str, Candidate] = {}  # key = token_address
_last_scan: float = 0.0
_scan_lock = asyncio.Lock()
_subscribers: list[asyncio.Queue] = []


def get_candidates() -> list[Candidate]:
    """Return all candidates sorted by score descending."""
    return sorted(_store.values(), key=lambda c: c.score_total, reverse=True)


def subscribe() -> asyncio.Queue:
    """Register a new console listener; returns a queue fed with scan log events."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def scan_in_progress() -> bool:
    return _scan_lock.locked()


def _broadcast(event: dict) -> None:
    for q in list(_subscribers):
        q.put_nowait(event)


def _passes_threshold(s: TokenScore) -> bool:
    tok = sum(i.earned for i in s.items if i.category == "Tokenomics")
    uti = sum(i.earned for i in s.items if i.category == "Utilità")
    return s.total >= SCORE_MIN and tok >= TOKENOMICS_MIN and uti >= UTILITY_MIN


def _fail_reason(s: TokenScore) -> str:
    tok = sum(i.earned for i in s.items if i.category == "Tokenomics")
    uti = sum(i.earned for i in s.items if i.category == "Utilità")
    if tok < TOKENOMICS_MIN:
        return f"tokenomics {tok}/{TOKENOMICS_MIN} min"
    if uti < UTILITY_MIN:
        return f"utilità {uti}/{UTILITY_MIN} min"
    return f"score {s.total}/{SCORE_MIN} min"


async def run_scan() -> int:
    """Fetch candidates, score them, update store. Returns count of added/updated."""
    global _last_scan
    async with _scan_lock:
        t0 = time.time()
        _broadcast({"type": "scan_start", "ts": t0})
        try:
            tokens: list[RawToken] = await fetch_candidates()
        except Exception as exc:
            logger.error("Sniper fetch error: %s", exc)
            _broadcast({"type": "fetch_error", "message": str(exc)})
            _broadcast({"type": "scan_end", "added": 0, "total": len(_store),
                        "scanned": 0, "duration": time.time() - t0})
            return 0

        _broadcast({"type": "fetched", "count": len(tokens)})

        added = 0
        async with httpx.AsyncClient(timeout=10.0) as client:
            for token in tokens:
                _broadcast({
                    "type": "evaluating",
                    "symbol": token.symbol,
                    "chain": token.chain_id,
                    "address": token.token_address,
                })
                try:
                    s: TokenScore = await score(token, client)
                except Exception as exc:
                    logger.warning("Sniper score error %s: %s", token.symbol, exc)
                    _broadcast({
                        "type": "error",
                        "symbol": token.symbol,
                        "chain": token.chain_id,
                        "message": str(exc),
                    })
                    continue

                passed = _passes_threshold(s)
                _broadcast({
                    "type": "result",
                    "symbol": token.symbol,
                    "chain": token.chain_id,
                    "address": token.token_address,
                    "score": s.total,
                    "score_max": s.max,
                    "passed": passed,
                    "reason": None if passed else _fail_reason(s),
                })

                if not passed:
                    continue

                c = Candidate(
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
                    volume_h1=token.volume_h1,
                    age_hours=token.age_hours,
                    buy_sell_ratio=token.buy_sell_ratio,
                    website=token.website,
                    socials=token.socials,
                    score_total=s.total,
                    score_max=s.max,
                    score_items=[
                        {
                            "category": i.category,
                            "criterion": i.criterion,
                            "earned": i.earned,
                            "max": i.max,
                            "auto": i.auto,
                            "note": i.note,
                        }
                        for i in s.items
                    ],
                    first_seen=_store[token.token_address].first_seen
                    if token.token_address in _store
                    else time.time(),
                    last_updated=time.time(),
                )
                _store[token.token_address] = c
                added += 1

        _last_scan = time.time()
        logger.info("Sniper scan complete: %d candidates stored", len(_store))
        _broadcast({
            "type": "scan_end",
            "added": added,
            "total": len(_store),
            "scanned": len(tokens),
            "duration": time.time() - t0,
        })
        return added


async def polling_loop() -> None:
    """Background loop: scan every POLL_INTERVAL_SECONDS."""
    while True:
        try:
            await run_scan()
        except Exception as exc:
            logger.error("Sniper polling error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def last_scan_ts() -> float:
    return _last_scan
