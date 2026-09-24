"""
Listing appena nati con liquidita', quasi in tempo reale.

Lorenzo: "voglio che io sia informato sui listing di roba interessante dal
punto di vista di liquidita' con tempi rapidissimi rispetto all'uscita [...]
deve essere proprio quasi istantaneo".

GeckoTerminal da solo non basta: misurato il 23/09, il pool piu' nuovo della
sua lista `new_pools` aveva gia' 7-8 minuti, e la risposta resta in cache 60 s.
Quindi tre fonti, dalla piu' rapida:

- Robinhood Chain: i log `PoolCreated` (Uniswap v3) e `PairCreated` (v2) letti
  dal nodo ogni `CHAIN_POLL_S` secondi, qualunque factory li emetta;
- Solana: i token pump.fun appena "laureati" (pool PumpSwap con liquidita'
  vera), dal feed pubblico di pump.fun, secondi dopo la laurea;
- entrambe: `new_pools` di GeckoTerminal ogni minuto, per tutto il resto
  (Raydium, Meteora, altri DEX).

Un pool appena creato ha liquidita' zero: la liquidita' arriva dopo. Ogni
pool trovato resta "in attesa" per `PENDING_TTL_S` e viene riletto su
DexScreener (30 pool per richiesta) finche' non supera le soglie o scade.
Solo allora parte l'avviso, una volta per ticker.

Superate le soglie, il token passa dal filtro di legittimita' (legit.py):
Lorenzo vuole "token legittimi con progetti seri", cap sotto i $100k, e il
link al sito. Chi non ha ancora sito o report di sicurezza resta in attesa
e viene riprovato ogni `VET_RETRY_S`; chi e' bocciato non torna piu'.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from app.providers.base import ProviderError
from app.providers.dexscreener import DexScreenerProvider, make_symbol
from app.trading.onchain import legit
from app.trading.onchain.storage import data_file

logger = logging.getLogger(__name__)

_STORE_FILE = data_file("onchain_listings.json")

ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/pairs"
PUMPFUN_COINS = "https://frontend-api-v3.pump.fun/coins"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"

# Soglie dell'avviso: regolabili qui.
LIQ_MIN_USD = 8_000
# Lorenzo: "anche uno o due milioni di market cap potrebbe andare bene però
# [...] GitHub, sito, tokenomics tante cose devono essere perfette". Sopra
# legit.GEM_CAP_USD passa solo una pagella eccellente.
MCAP_MAX_USD = 2_000_000
LIQ_TO_MCAP_MIN = 0.10
MAX_AGE_S = 6 * 3600          # oltre, non e' piu' un listing
PENDING_TTL_S = 2 * 3600      # quanto si aspetta che arrivi la liquidita'
MAX_PENDING = 300
KEEP_LISTINGS = 300
NOTIFY_MAX_AGE_S = 60 * 60    # il sito arriva spesso dopo il pool: si aspetta un po' di piu'
VET_RETRY_S = 120             # un token "in attesa" (niente sito, niente report) si riprova
VET_CONCURRENCY = 4
CHART_MIN_BARS = 2            # un listing si mostra solo se il suo grafico c'e' gia'
CHART_RETRY_S = 15            # GeckoTerminal ha le candele di un pool nuovo dopo 1-5 minuti
KEEP_REJECTED = 500   # fuori dall'app si avvisa solo di cio' che e' davvero appena uscito

TICK_S = 3
CHAIN_POLL_S = 3
PUMPFUN_POLL_S = 8
ENRICH_FRESH_S = 3            # pool trovati negli ultimi FRESH_WINDOW_S
ENRICH_ALL_S = 20             # tutti gli altri in attesa
FRESH_WINDOW_S = 180
GECKO_POLL_S = 60
_MAX_LOG_BLOCKS = 5_000

POOL_CREATED_V3 = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
PAIR_CREATED_V2 = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

_NOT_A_TOKEN = {
    "SOL", "WSOL", "USDC", "USDT", "USDG", "WETH", "ETH", "DAI", "USDS", "PYUSD",
    "USD1", "USDE", "BTC", "WBTC", "CBBTC", "JITOSOL", "MSOL",
}

SOURCE_LABELS = {"chain": "catena", "pumpfun": "pump.fun", "gecko": "GeckoTerminal"}


@dataclass
class _Pending:
    chain: str
    pair: str
    source: str
    detected_at: float = field(default_factory=time.time)
    links: dict = field(default_factory=dict)
    next_vet_at: float = 0.0
    vetting: bool = False
    waiting_for: str = ""
    verdict: dict | None = None   # esito "ok" del filtro, tenuto mentre si aspetta il grafico
    symbol: str = ""


_pending: dict[str, _Pending] = {}   # chain:pair
_done: dict[str, float] = {}          # chain:pair -> quando e' stato chiuso (avvisato o scaduto)
_alerted_tickers: dict[str, float] = {}  # chain:TICKER -> quando
_listings: list[dict] = []
_rejected: list[dict] = []
_tasks: set[asyncio.Task] = set()
_vet_slots = asyncio.Semaphore(VET_CONCURRENCY)
_http: httpx.AsyncClient | None = None
_subscribers: list[asyncio.Queue] = []
_callbacks: list[Callable[[dict], Awaitable[None]]] = []
_state = {"last_block": 0, "last_tick": 0.0, "sources": {}}
_arrivals: list[float] = []   # quando sono comparsi i pool nuovi, ultimi 10 minuti
ARRIVALS_WINDOW_S = 600


def _key(chain: str, pair: str) -> str:
    return f"{chain}:{pair}"


# ------------------------------------------------------------------ memoria

def _load() -> None:
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        _listings.extend(x for x in raw.get("listings", []) if isinstance(x, dict) and _passes_today(x))
        _rejected.extend(x for x in raw.get("rejected", []) if isinstance(x, dict))
        _alerted_tickers.update(raw.get("tickers") or {})


def _passes_today(item: dict) -> bool:
    """I listing salvati prima del filtro (niente controlli, cap oltre il
    tetto: JEANCOIN a $532k) non restano in lista solo perche' erano gia' li'."""
    return bool(item.get("checks")) and float(item.get("market_cap") or 0) <= MCAP_MAX_USD


def _save() -> None:
    try:
        _STORE_FILE.write_text(json.dumps({"listings": _listings, "rejected": _rejected,
                                           "tickers": _alerted_tickers},
                                          ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


_load()


def recent() -> list[dict]:
    return [x for x in _listings if _passes_today(x)]


def rejected() -> list[dict]:
    return list(_rejected)


def pulse(now: float | None = None) -> dict:
    """Quello che il radar sta facendo adesso, per far vedere che e' vivo."""
    now = now or time.time()
    del _arrivals[:sum(1 for t in _arrivals if now - t > ARRIVALS_WINDOW_S)]
    checking = sorted({p.symbol for p in _pending.values() if p.symbol and (p.vetting or p.waiting_for)})
    return {"at": now, "arrivals_10m": len(_arrivals), "last_arrival": _arrivals[-1] if _arrivals else None,
            "pending": len(_pending), "checking": checking[:6], "rejected": len(_rejected),
            "last_tick": _state["last_tick"]}


def status() -> dict:
    return {
        "pending": len(_pending),
        "vetting": sum(1 for p in _pending.values() if p.waiting_for or p.vetting),
        "rejected": len(_rejected),
        "recent_rejected": _rejected[:8],
        "ai_review": bool(legit.settings.anthropic_api_key),
        "last_tick": _state["last_tick"],
        "sources": dict(_state["sources"]),
        "thresholds": {"liq_min_usd": LIQ_MIN_USD, "mcap_max_usd": MCAP_MAX_USD,
                       "liq_to_mcap_min": LIQ_TO_MCAP_MIN, "max_age_h": MAX_AGE_S / 3600,
                       "requires": ["sito proprio", "social", "controllo anti-rug"]},
    }


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def on_listing(fn: Callable[[dict], Awaitable[None]]) -> None:
    _callbacks.append(fn)


def reset_for_tests() -> None:
    _pending.clear()
    _done.clear()
    _alerted_tickers.clear()
    _listings.clear()
    _rejected.clear()
    _arrivals.clear()
    _subscribers.clear()
    _state.update(last_block=0, last_tick=0.0, sources={})


# ------------------------------------------------------------------- scoperta

def add_pending(chain: str, pair: str, source: str, links: dict | None = None) -> bool:
    k = _key(chain, pair)
    if pair and links and k in _pending:
        _pending[k].links = legit.merge_links(_pending[k].links, links)
    if not pair or k in _pending or k in _done:
        return False
    if len(_pending) >= MAX_PENDING:  # si sacrifica il piu' vecchio, mai il nuovo
        oldest = min(_pending, key=lambda x: _pending[x].detected_at)
        del _pending[oldest]
        _done[oldest] = time.time()
    _pending[k] = _Pending(chain, pair, source, links=links or {})
    _arrivals.append(time.time())
    return True


def pools_from_logs(logs: list[dict]) -> list[str]:
    """Indirizzo del pool da un log PoolCreated (ultima parola dei dati) o
    PairCreated (prima parola)."""
    pools = []
    for log in logs:
        data = (log.get("data") or "")[2:]
        topic = (log.get("topics") or [""])[0]
        if topic == POOL_CREATED_V3 and len(data) >= 128:
            pools.append("0x" + data[-40:])
        elif topic == PAIR_CREATED_V2 and len(data) >= 64:
            pools.append("0x" + data[24:64])
    return pools


def pools_from_pumpfun(coins: list[dict], now: float | None = None) -> list[tuple[str, dict]]:
    """(pool, link a sito e social dichiarati su pump.fun)."""
    now = now or time.time()
    out = []
    for c in coins:
        if not c.get("complete"):
            continue
        created = (c.get("created_timestamp") or 0) / 1000
        pool = c.get("pump_swap_pool") or c.get("pool_address") or c.get("raydium_pool")
        if pool and now - created <= MAX_AGE_S:
            out.append((pool, legit.links_from_pumpfun(c)))
    return out


def evaluate_pair(pair: dict, now: float | None = None) -> dict | None:
    """Un pair DexScreener che supera le soglie, come voce di listing."""
    now = now or time.time()
    base = pair.get("baseToken") or {}
    symbol = (base.get("symbol") or "").strip()
    if not symbol or symbol.upper() in _NOT_A_TOKEN:
        return None
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    cap = pair.get("marketCap") or pair.get("fdv")
    created = (pair.get("pairCreatedAt") or 0) / 1000
    if liq < LIQ_MIN_USD or not cap or float(cap) > MCAP_MAX_USD or liq / float(cap) < LIQ_TO_MCAP_MIN:
        return None
    if created and now - created > MAX_AGE_S:
        return None
    return {
        "chain": pair.get("chainId"),
        "symbol": symbol,
        "name": base.get("name") or symbol,
        "token_address": base.get("address") or "",
        "pair_address": pair.get("pairAddress") or "",
        "pair_url": pair.get("url") or "",
        "dex": pair.get("dexId") or "",
        "liquidity_usd": liq,
        "market_cap": float(cap),
        "created_at": created or None,
    }


def _is_expired(p: _Pending, now: float) -> bool:
    return now - p.detected_at > PENDING_TTL_S


def _push(event: str, item: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait((event, item))
        except asyncio.QueueFull:
            pass


def update_listing(chain: str, pair: str, **fields) -> dict | None:
    """Aggiunge a un listing gia' pubblicato (il backtest del radar) e lo
    rispinge a chi guarda."""
    item = next((x for x in _listings if x.get("chain") == chain and x.get("pair_address") == pair), None)
    if item is None:
        return None
    item.update(fields)
    _save()
    _push("update", item)
    return item


async def _publish(item: dict) -> None:
    _listings.insert(0, item)
    del _listings[KEEP_LISTINGS:]
    _save()
    _push("listing", item)
    fresh = item.get("created_at") and item["found_at"] - item["created_at"] <= NOTIFY_MAX_AGE_S
    for fn in list(_callbacks) if fresh else []:
        try:
            await fn(item)
        except Exception:
            logger.exception("listings: callback fallita")


def note_rejected(item: dict, reason: str, now: float, verdict: dict | None = None,
                  source: str = "listing") -> dict | None:
    """Lorenzo: "scorrendo giu' voglio una sezione che mi faccia vedere tutti
    quelli che sono stati scartati". Uno per token, col perche' e i controlli."""
    token = item.get("token_address") or item.get("pair_address")
    if any(r.get("chain") == item.get("chain") and (r.get("token_address") or r.get("pair_address")) == token
           for r in _rejected):
        return None
    verdict = verdict or {}
    entry = {"chain": item.get("chain"), "symbol": item.get("symbol"), "name": item.get("name"),
             "token_address": item.get("token_address") or "", "pair_address": item.get("pair_address") or "",
             "pair_url": item.get("pair_url") or "", "market_cap": item.get("market_cap"),
             "liquidity_usd": item.get("liquidity_usd"), "created_at": item.get("created_at"),
             "reason": reason, "at": now, "source": source,
             "checks": verdict.get("checks") or [], "website": verdict.get("website"),
             "github": verdict.get("github"), "scan_url": verdict.get("scan_url")}
    _rejected.insert(0, entry)
    del _rejected[KEEP_REJECTED:]
    _save()
    _push("rejected", entry)
    return entry


def _reject(item: dict, reason: str, now: float, verdict: dict | None = None) -> None:
    note_rejected(item, reason, now, verdict)


async def _chart_ready(item: dict) -> bool:
    """Le candele del pool ci sono gia'? Lorenzo: "non deve succedere che se
    c'e' un token rilevato, se ci clicco non mi carica il grafico. Il
    controllo deve essere fatto a monte"."""
    try:
        df = await DexScreenerProvider().fetch_ohlcv(
            make_symbol(item["symbol"], item["chain"], item["pair_address"]), "1m", 10)
    except ProviderError:
        return False
    return len(df) >= CHART_MIN_BARS


async def _vet(k: str, pending: _Pending, item: dict, pair: dict) -> dict | None:
    """Il filtro di legittimita' su un listing che ha gia' superato le soglie,
    poi l'attesa del grafico."""
    try:
        async with _vet_slots:
            verdict = pending.verdict
            if verdict is None:
                client = _http or httpx.AsyncClient(timeout=10.0)
                try:
                    verdict = await legit.assess(
                        item, legit.merge_links(pending.links, legit.links_from_dexscreener(pair)), client)
                finally:
                    if client is not _http:
                        await client.aclose()
            chart = verdict["status"] == "ok" and await _chart_ready(item)
    except Exception:
        logger.exception("listings: filtro fallito su %s", item["symbol"])
        verdict, chart = {"status": "wait", "reason": "check failed"}, False
    finally:
        pending.vetting = False
    now = time.time()
    if _pending.get(k) is not pending:
        return None  # scaduto o sostituito nel frattempo
    if verdict["status"] == "wait":
        pending.next_vet_at = now + VET_RETRY_S
        pending.waiting_for = verdict["reason"]
        return None
    if verdict["status"] == "ok" and not chart:
        pending.verdict = verdict
        pending.next_vet_at = now + CHART_RETRY_S
        pending.waiting_for = "chart not available yet"
        return None
    del _pending[k]
    _done[k] = now
    if verdict["status"] == "fail":
        _reject(item, verdict["reason"], now, verdict)
        return None
    ticker = f"{item['chain']}:{item['symbol'].upper()}"
    if ticker in _alerted_tickers:
        return None  # una copia con lo stesso nome: il primo che passa vince
    _alerted_tickers[ticker] = now
    item.update(found_at=now, source=pending.source,
                latency_s=round(now - item["created_at"]) if item["created_at"] else None,
                website=verdict["website"], socials=verdict["socials"], checks=verdict["checks"],
                ai=verdict.get("ai"), github=verdict.get("github"), scan_url=verdict.get("scan_url"),
                report=verdict.get("report"))
    await _publish(item)
    return item


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def accept_pairs(pairs: list[dict], now: float | None = None, *, wait: bool = False) -> list[dict]:
    """Manda al filtro i pending che ora superano le soglie. Il filtro gira in
    parallelo e non ferma la scoperta; `wait=True` lo aspetta (per i test)."""
    now = now or time.time()
    tasks = []
    for pair in pairs:
        chain = pair.get("chainId")
        k = _key(chain, pair.get("pairAddress") or "")
        pending = _pending.get(k)
        if pending is None or pending.vetting or now < pending.next_vet_at:
            continue
        item = evaluate_pair(pair, now)
        if item is None:
            continue
        if f"{chain}:{item['symbol'].upper()}" in _alerted_tickers:
            del _pending[k]
            _done[k] = now
            continue
        pending.vetting = True
        pending.symbol = item["symbol"]
        tasks.append(_spawn(_vet(k, pending, item, pair)))
    for k, p in list(_pending.items()):
        if _is_expired(p, now) and not p.vetting:
            del _pending[k]
            _done[k] = now
    for k, ts in list(_done.items()):
        if now - ts > 2 * PENDING_TTL_S:
            del _done[k]
    for k, ts in list(_alerted_tickers.items()):
        if now - ts > 7 * 86400:
            del _alerted_tickers[k]
    if not wait or not tasks:
        return []
    return [x for x in await asyncio.gather(*tasks) if x]


def telegram_text(item: dict, now: float | None = None, app_url: str = "") -> str:
    """Il messaggio Telegram: numeri, controlli passati e i link cliccabili
    (sito del progetto, GitHub, grafico). Lorenzo: "voglio avere anche la
    possibilita' con un link di cliccare e andare sul sito del token"."""
    now = now or time.time()
    esc = html.escape
    chain = "Solana" if item["chain"] == "solana" else "Robinhood"
    age = int((now - item["created_at"]) // 60) if item.get("created_at") else None
    lines = [f"🆕 <b>{esc(item['symbol'])}</b> · {esc(item.get('name') or '')} su {chain}",
             f"liq. ${item['liquidity_usd']:,.0f} · cap ${item['market_cap']:,.0f}"
             + (f" · nato {age} min fa" if age is not None else "")]
    passed = [c for c in item.get("checks") or [] if c.get("ok")]
    if passed:
        lines.append("✅ " + " · ".join(esc(c["label"]) for c in passed))
    ai = item.get("ai") or {}
    if ai.get("reason") and not ai.get("error"):
        lines.append(f"🤖 {ai.get('score')}/10 · {esc(ai['reason'])}")
    links = []
    if item.get("website"):
        links.append(f'<a href="{esc(item["website"], quote=True)}">🌐 Sito</a>')
    if item.get("github"):
        links.append(f'<a href="{esc(item["github"], quote=True)}">GitHub</a>')
    for s in item.get("socials") or []:
        if s.get("type") in ("twitter", "telegram"):
            links.append(f'<a href="{esc(s["url"], quote=True)}">{"X" if s["type"] == "twitter" else "Telegram"}</a>')
    if item.get("scan_url"):
        links.append(f'<a href="{esc(item["scan_url"], quote=True)}">RugCheck</a>')
    if item.get("pair_url"):
        links.append(f'<a href="{esc(item["pair_url"], quote=True)}">Grafico</a>')
    if app_url:
        links.append(f'<a href="{esc(app_url, quote=True)}">Apri in DEGEN</a>')
    lines.append(" · ".join(links))
    return "\n".join(lines)


# --------------------------------------------------------------------- fonti

async def _poll_chain(client: httpx.AsyncClient) -> None:
    async def rpc(method: str, params: list):
        r = await client.post(ROBINHOOD_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    head = int(await rpc("eth_blockNumber", []), 16)
    start = _state["last_block"] + 1 if _state["last_block"] else head
    start = max(start, head - _MAX_LOG_BLOCKS)
    if start > head:
        return
    logs = await rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(head),
                                      "topics": [[POOL_CREATED_V3, PAIR_CREATED_V2]]}])
    _state["last_block"] = head
    for pool in pools_from_logs(logs):
        add_pending("robinhood", pool, "chain")


async def _poll_pumpfun(client: httpx.AsyncClient) -> None:
    r = await client.get(PUMPFUN_COINS, params={"offset": 0, "limit": 50, "sort": "last_trade_timestamp",
                                                "order": "DESC", "complete": "true"})
    if r.status_code == 200 and isinstance(r.json(), list):
        for pool, links in pools_from_pumpfun(r.json()):
            add_pending("solana", pool, "pumpfun", links)


async def _poll_gecko(client: httpx.AsyncClient) -> None:
    for chain in ("solana", "robinhood"):
        r = await client.get(f"{GECKO_BASE}/networks/{chain}/new_pools", params={"page": 1},
                             headers={"Accept": "application/json"})
        if r.status_code == 200:
            for pool in r.json().get("data") or []:
                add_pending(chain, (pool.get("attributes") or {}).get("address") or "", "gecko")
        await asyncio.sleep(2.1)


async def _enrich(client: httpx.AsyncClient, *, fresh_only: bool) -> None:
    now = time.time()
    by_chain: dict[str, list[str]] = {}
    for p in _pending.values():
        if fresh_only and now - p.detected_at > FRESH_WINDOW_S:
            continue
        by_chain.setdefault(p.chain, []).append(p.pair)
    pairs: list[dict] = []
    for chain, pools in by_chain.items():
        for i in range(0, len(pools), 30):
            r = await client.get(f"{DEXSCREENER_PAIRS}/{chain}/{','.join(pools[i:i + 30])}")
            if r.status_code == 200:
                pairs += (r.json() or {}).get("pairs") or []
    await accept_pairs(pairs)


async def _run(name: str, every: float, fn, client: httpx.AsyncClient, now: float) -> None:
    src = _state["sources"].setdefault(name, {"last_ok": 0.0, "last_try": 0.0, "error": ""})
    if now - src["last_try"] < every:
        return
    src["last_try"] = now
    try:
        await fn(client)
        src.update(last_ok=time.time(), error="")
    except Exception as exc:  # una fonte giu' non ferma le altre
        src["error"] = str(exc)[:120]
        logger.info("listings: %s: %s", name, exc)


START_DELAY_S = 10


async def listings_loop() -> None:
    await asyncio.sleep(START_DELAY_S)
    global _http
    async with httpx.AsyncClient(timeout=10.0) as client:
        _http = client
        while True:
            now = time.time()
            _state["last_tick"] = now
            await asyncio.gather(
                _run("chain", CHAIN_POLL_S, _poll_chain, client, now),
                _run("pumpfun", PUMPFUN_POLL_S, _poll_pumpfun, client, now),
                _run("gecko", GECKO_POLL_S, _poll_gecko, client, now),
            )
            await _run("dexscreener", ENRICH_FRESH_S, lambda c: _enrich(c, fresh_only=True), client, now)
            await _run("dexscreener_all", ENRICH_ALL_S, lambda c: _enrich(c, fresh_only=False), client, now)
            await asyncio.sleep(TICK_S)
