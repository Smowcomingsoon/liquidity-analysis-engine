"""La classifica dei progetti migliori fra quelli che il radar ha visto.

Lorenzo: "una specie di leaderboard [...] primo posto, secondo posto, terzo
posto [...] una classifica dinamica, non fissa, aggiornata in tempo reale
[...] rigoroso [...] da quanto e' che sono usciti [...] e ampliamo anche la
chain per i token lanciati su Pump Fun".

Da dove arrivano i progetti:
- i listing verificati (`listings.recent()`) e le proposte e i token provati
  dal radar (`radar.snapshot()`): hanno gia' passato `legit.assess`;
- pump.fun e i progetti fuori dai limiti del radar (tipo DREGG) li porta il
  radar stesso: qui si legge solo quello che ha salvato, una fonte sola.

Ogni progetto passa da `depth.analyze` (GitHub, sito, tokenomics). Chi viene
bocciato su un pilastro esce e non rientra; chi ha un dato in attesa resta
fuori finche' non arriva. Il punteggio si rifa' da solo: tokenomics ogni
20 minuti (i portafogli cambiano), GitHub ogni 2 ore, sito ogni 12. Cap e
liquidita' ogni minuto da DexScreener. La regola della gemma e' quella del
radar: sopra i $100k solo con tutti e tre i pilastri eccellenti.

L'eccezione (Lorenzo, 24/09: "dregg va bene anche se ha 21 gg e supera i 2
mln, progetti in quel modo voglio che siano eccezione in quanto tecnicamente
incredibili su github"): oltre i 21 giorni o i $2M si entra solo se il radar
l'ha ammesso come eccezione, con la sua stessa regola
(`legit.github_shortfall` e `legit.gem_shortfall`): una regola sola.

Il riferimento (Lorenzo, 24/09: "mi devi mettere drag al primo posto in
assoluto con menzione d'onore"): DREGG sta sempre primo, e gli altri si
ordinano per qualita' (i tre pilastri) e somiglianza tecnica a DREGG
(`benchmark.py`), 70/30.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from app.trading.onchain import benchmark, depth, discovery, legit, listings, narratives, radar, storage

logger = logging.getLogger(__name__)

_STORE_FILE = storage.data_file("onchain_leaderboard.json")

TICK_S = 10
MARKET_POLL_S = 60
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"

STALE_S = {"tokenomics": 20 * 60, "github": 2 * 3600, "site": 12 * 3600}
PENDING_RETRY_S = 3 * 60
PENDING_GIVE_UP_S = 6 * 3600       # un dato che non arriva in 6 ore non arrivera'
MAX_AGE_DAYS = radar.AGE_MAX_DAYS  # oltre, non e' piu' un progetto nuovo, salvo eccezioni
MAX_ENTRIES = 400
KEEP_EXCLUDED = 60
ANALYZE_CONCURRENCY = 3
BOARD_SIZE = 50
SIMILARITY_WEIGHT = 0.30
MIN_PILLAR = 35                    # sotto, un pilastro e' troppo debole per stare in classifica
# Progetti trovati dallo scan dedicato (discovery): l'eta' conta poco, la cap
# si' ("sotto 2-3 milioni, non oltre"), e la qualita' deve essere vera.
SERIOUS_SOURCES = ("coingecko", "dexscreener", "github")
SERIOUS_CAP_MAX_USD = discovery.CAP_MAX
SERIOUS_AGE_MAX_DAYS = 365
SERIOUS_MIN_PILLAR = 60
SERIOUS_MIN_GITHUB = 70
CAP_MAX_USD = radar.MCAP_MAX_USD

_entries: dict[str, dict] = {}     # chain:token
_excluded: list[dict] = []
_gone: dict[str, float] = {}       # chain:token bocciati: non si riesaminano
_busy: set[str] = set()
_tasks: set[asyncio.Task] = set()
_slots = asyncio.Semaphore(ANALYZE_CONCURRENCY)
_state = {"version": 0, "examined": 0, "last_tick": 0.0, "last_market": 0.0,
          "sources": {}, "board": []}
_dirty = False


def _key(chain: str, token: str) -> str:
    return f"{chain}:{token}"


# ------------------------------------------------------------------ memoria

def _load() -> None:
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        _entries.update({k: v for k, v in (raw.get("entries") or {}).items() if isinstance(v, dict)})
        _excluded.extend(x for x in raw.get("excluded") or [] if isinstance(x, dict))
        _gone.update(raw.get("gone") or {})
        _state["examined"] = int(raw.get("examined") or 0)
        _rank()


def _save() -> None:
    global _dirty
    _dirty = False
    try:
        _STORE_FILE.write_text(json.dumps({"entries": _entries, "excluded": _excluded, "gone": _gone,
                                           "examined": _state["examined"]}, ensure_ascii=False),
                               encoding="utf-8")
    except OSError:
        pass


def reset_for_tests() -> None:
    _entries.clear()
    _excluded.clear()
    _gone.clear()
    _busy.clear()
    _state.update(version=0, examined=0, last_tick=0.0, last_market=0.0, sources={}, board=[])


# ------------------------------------------------------------------- fonti

def add(item: dict, source: str, now: float | None = None) -> bool:
    """Un progetto da esaminare. False se c'e' gia' o e' gia' stato bocciato."""
    chain, token = item.get("chain"), item.get("token_address")
    if not chain or not token:
        return False
    k = _key(chain, token)
    if benchmark.is_benchmark(chain, token):
        _gone.pop(k, None)
    if k in _gone:
        return False
    now = now or time.time()
    fresh = {f: item[f] for f in ("market_cap", "liquidity_usd", "website", "github", "socials", "pair_url",
                                  "report", "scan_url", "checks", "exception", "github_verified", "description",
                                  "categories") if item.get(f)}
    if k in _entries:
        _entries[k].update(fresh)
        return False
    launched = item.get("launched_at") or item.get("created_at")
    if not launched and item.get("age_hours") is not None:
        launched = now - float(item["age_hours"]) * 3600
    _entries[k] = {
        "chain": chain, "token_address": token, "pair_address": item.get("pair_address") or "",
        "symbol": item.get("symbol") or "?", "name": item.get("name") or item.get("symbol") or "?",
        "source": source, "found_at": now, "launched_at": launched,
        "image": item.get("image"), "bonding": bool(item.get("bonding")),
        "bonding_curve": item.get("bonding_curve"), "associated_bonding_curve": item.get("associated_bonding_curve"),
        "pillars": {}, "checked_at": {}, "rank": None, **fresh,
    }
    _state["examined"] += 1
    _mark()
    return True


def collect_saved() -> int:
    """Quello che il radar ha gia' verificato e salvato, piu' il riferimento."""
    snap = radar.snapshot()
    n = add(benchmark.seed_item(), "riferimento")
    for item in listings.recent():
        n += add(item, "listing")
    for item in (snap.get("proposals") or []) + (snap.get("tested") or []):
        n += add(item, "radar")
    return n


def is_big(item: dict, now: float | None = None) -> bool:
    """Fuori dai limiti del radar: puo' entrare solo come eccezione."""
    now = now or time.time()
    launched = item.get("launched_at")
    return (float(item.get("market_cap") or 0) > CAP_MAX_USD
            or bool(launched and now - launched > MAX_AGE_DAYS * 86400))


def apply_market(pairs: list[dict]) -> None:
    """Cap, liquidita', variazione e data del pool piu' liquido di ogni token."""
    best: dict[str, dict] = {}
    for p in pairs:
        token = (p.get("baseToken") or {}).get("address")
        k = _key(p.get("chainId") or "", token or "")
        if k not in _entries:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if k not in best or liq > float((best[k].get("liquidity") or {}).get("usd") or 0):
            best[k] = p
        created = (p.get("pairCreatedAt") or 0) / 1000
        e = _entries[k]
        if created and (not e.get("launched_at") or created < e["launched_at"]):
            e["launched_at"] = created
    for k, p in best.items():
        e = _entries[k]
        cap = p.get("marketCap") or p.get("fdv")
        e.update(market_cap=float(cap) if cap else e.get("market_cap"),
                 liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
                 change_24h=(p.get("priceChange") or {}).get("h24"), pair_url=p.get("url") or e.get("pair_url"),
                 dex=p.get("dexId"), bonding=(p.get("dexId") == "pumpfun"))
        if not e.get("pair_address") or e.get("bonding"):
            e["pair_address"] = p.get("pairAddress") or e.get("pair_address")
    _mark()


async def _poll_market(client: httpx.AsyncClient) -> None:
    by_chain: dict[str, list[str]] = {}
    for e in _entries.values():
        by_chain.setdefault(e["chain"], []).append(e["token_address"])
    for chain, tokens in by_chain.items():
        for i in range(0, len(tokens), 30):
            r = await client.get(DEX_TOKENS.format(chain=chain, addresses=",".join(tokens[i:i + 30])),
                                 timeout=10.0)
            if r.status_code == 200 and isinstance(r.json(), list):
                apply_market(r.json())


# ---------------------------------------------------------------- analisi

def due(e: dict, now: float) -> set[str]:
    """I pilastri da rifare adesso."""
    out = set()
    for k, every in STALE_S.items():
        p = e["pillars"].get(k)
        at = e["checked_at"].get(k, 0)
        if p is None or (p.get("pending") and now - at >= PENDING_RETRY_S) or now - at >= every:
            out.add(k)
    return out


def apply_analysis(k: str, result: dict, now: float | None = None) -> None:
    now = now or time.time()
    e = _entries.get(k)
    if e is None:
        return
    for name, pillar in result.items():
        e["pillars"][name] = pillar
        e["checked_at"][name] = now
        if name == "github" and (pillar.get("facts") or {}).get("url"):
            e["github"] = pillar["facts"]["url"]
    e["overall"] = depth.overall(e["pillars"])
    e["similarity"] = benchmark.similarity(benchmark.signals(
        (e["pillars"].get("github") or {}).get("facts"),
        (e["pillars"].get("site") or {}).get("facts", {}).get("tech_terms")))
    gh = (e["pillars"].get("github") or {}).get("facts") or {}
    site_keys = (e["pillars"].get("site") or {}).get("facts", {}).get("narratives") or []
    more = narratives.match(e.get("description"), e.get("categories"), gh.get("topics"), gh.get("description"))
    e["narratives"] = list(dict.fromkeys([*site_keys, *more]))
    if e["overall"]["fatal"] and not benchmark.is_benchmark(e["chain"], e["token_address"]):
        _exclude(k, e["overall"]["fatal"], now)
    _mark()


def _exclude(k: str, reason: str, now: float) -> None:
    e = _entries.pop(k, None)
    _gone[k] = now
    if e is None:
        return
    _excluded.insert(0, {"chain": e["chain"], "token_address": e["token_address"], "symbol": e["symbol"],
                         "name": e["name"], "source": e["source"], "reason": reason, "at": now,
                         "was_rank": e.get("rank")})
    del _excluded[KEEP_EXCLUDED:]


async def _analyze(k: str, skip: set[str], client: httpx.AsyncClient) -> None:
    try:
        async with _slots:
            e = _entries.get(k)
            if e is None:
                return
            item = {**e, "links": [s.get("url") for s in e.get("socials") or [] if s.get("url")]}
            result = await depth.analyze(item, client, skip=skip)
        apply_analysis(k, result)
    except Exception:
        logger.exception("classifica: analisi fallita su %s", k)
    finally:
        _busy.discard(k)


def _prune(now: float) -> None:
    for k, e in list(_entries.items()):
        launched = e.get("launched_at")
        pending_since = e.get("found_at", now)
        limit = (radar.EXC_AGE_MAX_DAYS if exceptional(e)
                 else SERIOUS_AGE_MAX_DAYS if e.get("source") in SERIOUS_SOURCES else MAX_AGE_DAYS)
        if benchmark.is_benchmark(e["chain"], e["token_address"]):
            continue
        if launched and now - launched > limit * 86400:
            _entries.pop(k)
        elif (e.get("overall") or {}).get("pending") and now - pending_since > PENDING_GIVE_UP_S:
            # A slow service (GitHub out of requests) is not the project's
            # fault: drop it from the queue, but let it come back.
            _entries.pop(k)
    if len(_entries) > MAX_ENTRIES:
        for k in sorted(_entries, key=lambda k: _entries[k].get("found_at", 0))[:len(_entries) - MAX_ENTRIES]:
            _entries.pop(k)
    for k, at in list(_gone.items()):
        if now - at > 30 * 86400:
            del _gone[k]


# -------------------------------------------------------------- classifica

def exceptional(e: dict) -> bool:
    """La regola del radar per le eccezioni, sui controlli che ha salvato."""
    checks = e.get("checks") or []
    gh = next((c for c in checks if c.get("key") == "github"), None)
    return bool(gh) and not legit.github_shortfall(gh) and not legit.gem_shortfall(checks, e.get("report"))


def qualifies(e: dict, now: float | None = None) -> str:
    """"" se entra in classifica, altrimenti perche' no."""
    if benchmark.is_benchmark(e.get("chain"), e.get("token_address")):
        return ""   # il metro di paragone c'e' sempre, i suoi numeri si vedono comunque
    o = e.get("overall") or {}
    pillars = e.get("pillars") or {}
    if not o or o.get("fatal"):
        return o.get("fatal") or "to be examined"
    if o.get("pending"):
        return o["pending"]
    if any((pillars.get(k) or {}).get("score") is None for k in depth.WEIGHTS):
        return "incomplete analysis"
    weak = [k for k in depth.WEIGHTS if pillars[k]["score"] < MIN_PILLAR]
    if weak:
        return f"{weak[0]} too weak ({pillars[weak[0]]['score']})"
    if e.get("source") in SERIOUS_SOURCES:
        return _serious_shortfall(e, pillars, now)
    if is_big(e, now):
        launched = e.get("launched_at")
        if (float(e.get("market_cap") or 0) > radar.EXC_MCAP_MAX_USD
                or (launched and (now or time.time()) - launched > radar.EXC_AGE_MAX_DAYS * 86400)):
            return "beyond the limits even for an exception"
        if not exceptional(e):
            return f"over {MAX_AGE_DAYS} days or ${CAP_MAX_USD / 1e6:.0f}M without an exceptional GitHub"
        # Anche l'analisi profonda deve confermare che il codice e' eccezionale.
        if pillars["github"]["score"] < legit.GEM_MIN_SCORE:
            return f"radar exception, but deep GitHub score only {pillars['github']['score']}"
        return ""
    cap = float(e.get("market_cap") or 0)
    if cap > legit.GEM_CAP_USD and any(pillars[k]["score"] < legit.GEM_MIN_SCORE for k in depth.WEIGHTS):
        return "above $100k without excellent pillars"
    return ""


def _serious_shortfall(e: dict, pillars: dict, now: float | None) -> str:
    launched = e.get("launched_at")
    if float(e.get("market_cap") or 0) > SERIOUS_CAP_MAX_USD:
        return f"over ${SERIOUS_CAP_MAX_USD / 1e6:.0f}M"
    if launched and (now or time.time()) - launched > SERIOUS_AGE_MAX_DAYS * 86400:
        return f"over {SERIOUS_AGE_MAX_DAYS} days"
    if pillars["github"]["score"] < SERIOUS_MIN_GITHUB:
        return f"GitHub only {pillars['github']['score']}"
    weak = [k for k in depth.WEIGHTS if pillars[k]["score"] < SERIOUS_MIN_PILLAR]
    return f"{weak[0]} only {pillars[weak[0]]['score']}" if weak else ""


def final_score(e: dict) -> float:
    """Qualita' (i tre pilastri) e somiglianza tecnica a DREGG."""
    q = (e.get("overall") or {}).get("score") or 0
    sim = (e.get("similarity") or {}).get("score") or 0
    base = (1 - SIMILARITY_WEIGHT) * q + SIMILARITY_WEIGHT * sim
    return round(min(100.0, base + narratives.bonus(e.get("narratives") or [])), 1)


def _rank() -> None:
    ranked = sorted((e for e in _entries.values() if not qualifies(e)),
                    key=lambda e: (not benchmark.is_benchmark(e["chain"], e["token_address"]),
                                   -final_score(e), -(e.get("launched_at") or 0)))[:BOARD_SIZE]
    order = [_key(e["chain"], e["token_address"]) for e in ranked]
    now = time.time()
    for i, e in enumerate(ranked, 1):
        if e.get("rank") != i:
            e["rank_before"], e["rank"], e["moved_at"] = e.get("rank"), i, now
    for k, e in _entries.items():
        if k not in order and e.get("rank") is not None:
            e["rank_before"], e["rank"], e["moved_at"] = e["rank"], None, now
    if order != _state["board"]:
        _state["board"] = order


def _mark() -> None:
    global _dirty
    _dirty = True
    _state["version"] += 1
    _rank()


def _public(e: dict) -> dict:
    keep = ("chain", "token_address", "pair_address", "symbol", "name", "source", "launched_at", "found_at",
            "market_cap", "liquidity_usd", "change_24h", "website", "github", "socials", "pair_url", "scan_url",
            "image", "bonding", "rank", "rank_before", "moved_at", "overall", "pillars", "checked_at")
    top = benchmark.is_benchmark(e["chain"], e["token_address"])
    return {**{k: e.get(k) for k in keep}, "exception": is_big(e) and not top, "score": final_score(e),
            "similarity": e.get("similarity"), "benchmark": top,
            "narratives": narratives.labels(e.get("narratives") or []), "honor": benchmark.HONOR if top else None}


def board() -> dict:
    entries = [_entries[k] for k in _state["board"] if k in _entries]
    waiting = sum(1 for k, e in _entries.items() if k not in _state["board"])
    return {
        "version": _state["version"], "updated_at": _state["last_tick"] or None,
        "entries": [_public(e) for e in entries],
        "examined": _state["examined"], "analyzing": len(_busy), "waiting": waiting,
        "excluded": _excluded[:20], "excluded_total": len(_gone),
        "sources": _state["sources"], "discovery": discovery.status(),
        "github_rate": legit.github_rate or None,
        "rules": {"weights": depth.WEIGHTS, "similarity_weight": SIMILARITY_WEIGHT, "benchmark": benchmark.NAME, "min_pillar": MIN_PILLAR, "gem_cap_usd": legit.GEM_CAP_USD,
                  "gem_min_score": legit.GEM_MIN_SCORE, "cap_max_usd": CAP_MAX_USD, "max_age_days": MAX_AGE_DAYS,
                  "exception_cap_usd": radar.EXC_MCAP_MAX_USD, "exception_age_days": radar.EXC_AGE_MAX_DAYS},
    }


# -------------------------------------------------------------------- giro

async def _step(name: str, fn, *args) -> None:
    try:
        await fn(*args)
        _state["sources"][name] = {"ok": True, "at": time.time()}
    except Exception as exc:
        _state["sources"][name] = {"ok": False, "at": time.time(), "error": type(exc).__name__}
        logger.info("classifica: %s non riuscito: %s", name, exc)


async def tick(client: httpx.AsyncClient, now: float | None = None) -> None:
    now = now or time.time()
    _state["last_tick"] = now
    try:
        collect_saved()
    except Exception:
        logger.exception("classifica: lettura del radar fallita")
    await _step("progetti", discovery.tick, client, add, now)
    if now - _state["last_market"] >= MARKET_POLL_S and _entries:
        _state["last_market"] = now
        await _step("dexscreener", _poll_market, client)
    _prune(now)
    # I nuovi prima (nessun pilastro), poi i piu' vecchi da rinfrescare.
    todo = sorted(((k, due(e, now)) for k, e in _entries.items() if k not in _busy),
                  key=lambda kv: (bool(_entries[kv[0]]["pillars"]), -_entries[kv[0]].get("found_at", 0)))
    for k, need in todo:
        if not need or len(_busy) >= ANALYZE_CONCURRENCY * 2:
            continue
        _busy.add(k)
        task = asyncio.create_task(_analyze(k, set(STALE_S) - need, client))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    if _dirty:
        _save()


START_DELAY_S = 20


async def leaderboard_loop() -> None:
    await asyncio.sleep(START_DELAY_S)
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0 (LiquidityEngine)"}) as client:
        while True:
            try:
                await tick(client)
            except Exception:
                logger.exception("classifica: giro fallito")
            await asyncio.sleep(TICK_S)


_load()
