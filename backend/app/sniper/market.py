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


from app import http_client

from .scanner import (
    GECKO_NETWORKS,
    RawToken,
    _passes_primary,
    fetch_dexscreener_boosted,
    fetch_market_chain,
    fetch_pumpfun_trending,
)
from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_MARKET_FILE = _DATA_DIR / "sniper_market.json"
_LOG_FILE = _DATA_DIR / "sniper_market_log.json"

LOG_MAX_ENTRIES = 2000
# A live screener, not an archive: tokens no source has returned for a week
# are dropped (the next scan brings back any that are still trading).
STORE_MAX_AGE_S = 7 * 86400


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
    market_cap: float | None
    liquidity_usd: float
    volume_h24: float
    age_hours: float
    buy_sell_ratio: float
    last_updated: float = field(default_factory=time.time)


_store: dict[str, MarketToken] = {}  # key = f"{chain_id}:{token_address}"
_last_scan: float = 0.0
_scan_lock = asyncio.Lock()
_subscribers: list[asyncio.Queue] = []
_log: list[dict] = []


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


def _prune(now: float | None = None) -> int:
    now = now or time.time()
    stale = [k for k, t in _store.items() if now - t.last_updated > STORE_MAX_AGE_S]
    for k in stale:
        del _store[k]
    return len(stale)


def _save() -> None:
    _prune()
    payload = [dataclasses.asdict(t) for t in _store.values()]
    try:
        write_text_atomic(_MARKET_FILE, json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("Market screener: failed to persist store: %s", exc)


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
        write_text_atomic(_LOG_FILE, json.dumps(_log[-LOG_MAX_ENTRIES:]), encoding="utf-8")
    except OSError as exc:
        logger.warning("Market screener: failed to persist log: %s", exc)


def get_log(limit: int = 500) -> list[dict]:
    return _log[-limit:]


_load()
if _prune():
    _save()
_load_log()


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
    _log.append(event)
    if len(_log) > LOG_MAX_ENTRIES:
        del _log[: len(_log) - LOG_MAX_ENTRIES]
    for q in list(_subscribers):
        q.put_nowait(event)


def last_scan_ts() -> float:
    return _last_scan


def get_tokens(
    chain: str | None = None,
    query: str | None = None,
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
    """Pull every pool from GeckoTerminal, DexScreener boosted feed, and
    Pump.fun trending feed across all supported chains. Returns token count."""
    global _last_scan
    async with _scan_lock:
        t0 = time.time()
        _broadcast({"type": "market_scan_start"})
        added = 0
        async with http_client.pooled(timeout=20.0) as client:
            # GeckoTerminal: full pool catalog per chain
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

            # DexScreener boosted/trending tokens
            _broadcast({"type": "market_source_start", "source": "dexscreener_boosted"})
            try:
                boosted = await fetch_dexscreener_boosted(client)
            except Exception as exc:
                logger.warning("DexScreener boosted fetch error: %s", exc)
                boosted = []
            new_boosted = 0
            for token in boosted:
                k = _key(token.chain_id, token.token_address)
                if k not in _store:
                    new_boosted += 1
                _store[k] = _to_market_token(token)
            added += new_boosted
            _broadcast({
                "type": "market_source_done",
                "source": "dexscreener_boosted",
                "count": len(boosted),
                "new": new_boosted,
            })

            # Pump.fun trending Solana meme coins
            _broadcast({"type": "market_source_start", "source": "pump_fun"})
            try:
                pumpfun = await fetch_pumpfun_trending(client)
            except Exception as exc:
                logger.warning("Pump.fun fetch error: %s", exc)
                pumpfun = []
            new_pumpfun = 0
            for token in pumpfun:
                k = _key(token.chain_id, token.token_address)
                if k not in _store:
                    new_pumpfun += 1
                _store[k] = _to_market_token(token)
            added += new_pumpfun
            _broadcast({
                "type": "market_source_done",
                "source": "pump_fun",
                "count": len(pumpfun),
                "new": new_pumpfun,
            })

        _last_scan = time.time()
        _save()
        _broadcast({
            "type": "market_scan_end",
            "total": len(_store),
            "duration": time.time() - t0,
        })
        _save_log()
        logger.info("Market screener scan complete: %d tokens stored", len(_store))
        return added


_import_lock = asyncio.Lock()


def import_in_progress() -> bool:
    return _import_lock.locked()


def _market_to_raw(t: MarketToken) -> RawToken:
    """Convert a MarketToken back to a RawToken so the scorer pipeline can run on it."""
    pair_created_at_ms = int((time.time() - t.age_hours * 3600) * 1000)
    sells = max(1, round(10 / max(t.buy_sell_ratio, 0.01)))
    buys = round(t.buy_sell_ratio * sells)
    return RawToken(
        chain_id=t.chain_id,
        token_address=t.token_address,
        pair_address=t.pair_address,
        symbol=t.symbol,
        name=t.name,
        dex_id=t.dex_id,
        price_usd=t.price_usd,
        market_cap=t.market_cap,
        liquidity_usd=t.liquidity_usd,
        volume_h24=t.volume_h24,
        volume_h1=0.0,
        volume_m5=0.0,
        buys_h1=buys,
        sells_h1=sells,
        pair_created_at_ms=pair_created_at_ms,
    )


async def run_import() -> int:
    """Apply the sniper's primary filter + on-chain scoring to every token in
    the market store. Tokens that pass are added to the sniper candidate
    archive. Progress is broadcast on the market SSE stream so the market
    console can show it in real time. Returns the number of new candidates."""
    from .cache import (
        Candidate,
        _save_candidates,
    )
    from .cache import (
        _store as sniper_store,
    )
    from .scorer import TokenScore, score

    async with _import_lock:
        t0 = time.time()
        raw_tokens = [_market_to_raw(t) for t in list(_store.values())]
        _broadcast({"type": "import_start", "total": len(raw_tokens)})

        passing: list[RawToken] = []
        for token in raw_tokens:
            ok, reason = _passes_primary(token)
            _broadcast({
                "type": "primary",
                "symbol": token.symbol,
                "chain": token.chain_id,
                "address": token.token_address,
                "passed": ok,
                "reason": None if ok else reason,
            })
            if ok:
                passing.append(token)

        added = 0
        async with http_client.pooled(timeout=10.0) as client:
            for token in passing:
                _broadcast({
                    "type": "evaluating",
                    "symbol": token.symbol,
                    "chain": token.chain_id,
                    "address": token.token_address,
                })
                try:
                    s: TokenScore = await score(token, client)
                except Exception as exc:
                    logger.warning("Import score error %s: %s", token.symbol, exc)
                    _broadcast({
                        "type": "error",
                        "symbol": token.symbol,
                        "chain": token.chain_id,
                        "message": str(exc),
                    })
                    continue

                # Market import uses a looser gate: save everything that passed
                # primary filters. Score is computed for display only — without
                # an Etherscan API key, on-chain tokenomics criteria score 0 and
                # the normal threshold would reject every candidate.
                _broadcast({
                    "type": "result",
                    "symbol": token.symbol,
                    "chain": token.chain_id,
                    "address": token.token_address,
                    "score": s.total,
                    "score_max": s.max,
                    "passed": True,
                    "reason": None,
                })

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
                    first_seen=sniper_store[token.token_address].first_seen
                    if token.token_address in sniper_store
                    else time.time(),
                    last_updated=time.time(),
                )
                sniper_store[token.token_address] = c
                added += 1

        if added:
            _save_candidates()
        _broadcast({
            "type": "import_end",
            "added": added,
            "total": len(sniper_store),
            "scanned": len(passing),
            "fetched": len(raw_tokens),
            "duration": time.time() - t0,
        })
        _save_log()
        logger.info("Market import complete: %d new sniper candidates", added)
        return added
