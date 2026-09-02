"""Sniper candidate archive, scan-log history and background polling loop.

Candidates and the scan log are persisted to disk (backend/data/sniper_*.json,
same convention as favorites.py / watchlist.py) so a deploy or restart doesn't
wipe the archive — every token that ever passed the score threshold, and the
console log that shows it happening, both survive a reload of the site."""
import asyncio
import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from .scanner import RawToken, fetch_all_tokens
from .scorer import TokenScore, score

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300  # 5 min

SCORE_MIN = 2
TOKENOMICS_MIN = 0
UTILITY_MIN = 0

LOG_MAX_ENTRIES = 5000

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CANDIDATES_FILE = _DATA_DIR / "sniper_candidates.json"
_LOG_FILE = _DATA_DIR / "sniper_log.json"


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
_log: list[dict] = []


def _load_candidates() -> None:
    if not _CANDIDATES_FILE.exists():
        return
    try:
        raw = json.loads(_CANDIDATES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for item in raw:
        try:
            c = Candidate(**item)
            _store[c.token_address] = c
        except (TypeError, ValueError):
            continue


def _save_candidates() -> None:
    payload = [dataclasses.asdict(c) for c in _store.values()]
    try:
        _CANDIDATES_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("Sniper: failed to persist candidates: %s", exc)


def _load_log() -> None:
    if not _LOG_FILE.exists():
        return
    try:
        raw = json.loads(_LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(raw, list):
        _log.extend(raw[-LOG_MAX_ENTRIES:])


def _save_log() -> None:
    try:
        _LOG_FILE.write_text(json.dumps(_log[-LOG_MAX_ENTRIES:]), encoding="utf-8")
    except OSError as exc:
        logger.warning("Sniper: failed to persist log: %s", exc)


_load_candidates()
_load_log()


def get_candidates() -> list[Candidate]:
    """Return all candidates sorted by score descending."""
    return sorted(_store.values(), key=lambda c: c.score_total, reverse=True)


def get_log(limit: int = 500) -> list[dict]:
    """Return the most recent persisted scan-log events, oldest first."""
    return _log[-limit:]


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
    event.setdefault("ts", time.time())
    _log.append(event)
    if len(_log) > LOG_MAX_ENTRIES:
        del _log[: len(_log) - LOG_MAX_ENTRIES]
    for q in list(_subscribers):
        q.put_nowait(event)


def passes_threshold(s: TokenScore) -> bool:
    tok = sum(i.earned for i in s.items if i.category == "Tokenomics")
    uti = sum(i.earned for i in s.items if i.category == "Utilità")
    return s.total >= SCORE_MIN and tok >= TOKENOMICS_MIN and uti >= UTILITY_MIN


def fail_reason(s: TokenScore) -> str:
    tok = sum(i.earned for i in s.items if i.category == "Tokenomics")
    uti = sum(i.earned for i in s.items if i.category == "Utilità")
    if tok < TOKENOMICS_MIN:
        return f"tokenomics {tok}/{TOKENOMICS_MIN} min"
    if uti < UTILITY_MIN:
        return f"utilità {uti}/{UTILITY_MIN} min"
    return f"score {s.total}/{SCORE_MIN} min"


# private aliases kept for backward compat inside this module
_passes_threshold = passes_threshold
_fail_reason = fail_reason


async def run_scan() -> int:
    """Fetch candidates, score them, update store. Returns count of added/updated."""
    global _last_scan
    async with _scan_lock:
        t0 = time.time()
        _broadcast({"type": "scan_start", "ts": t0})
        try:
            fetch_result = await fetch_all_tokens()
        except Exception as exc:
            logger.error("Sniper fetch error: %s", exc)
            _broadcast({"type": "fetch_error", "message": str(exc)})
            _broadcast({"type": "scan_end", "added": 0, "total": len(_store),
                        "scanned": 0, "duration": time.time() - t0})
            _save_log()
            return 0

        all_tokens = fetch_result.results
        _broadcast({
            "type": "fetched",
            "count": fetch_result.total_profiles,
            "supported": len(all_tokens),
            "unsupported_chain": fetch_result.unsupported_chain,
            "per_chain_new_pools": fetch_result.per_chain_new_pools,
        })

        tokens: list[RawToken] = []
        for token, primary_ok, primary_reason in all_tokens:
            _broadcast({
                "type": "primary",
                "symbol": token.symbol,
                "chain": token.chain_id,
                "address": token.token_address,
                "passed": primary_ok,
                "reason": None if primary_ok else primary_reason,
            })
            if primary_ok:
                tokens.append(token)

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
            "fetched": len(all_tokens),
            "scanned": len(tokens),
            "duration": time.time() - t0,
        })
        if added:
            _save_candidates()
        _save_log()
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
