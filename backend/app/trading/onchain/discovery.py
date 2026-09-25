"""Scan H24 dei progetti seri, separato dal radar.

Lorenzo (24/09): "per quanto riguarda i progetti seri voglio che anche li'
ci sia uno scan H24 che poi finisce nella leaderboard [...] vedo che hai
trovato soltanto drag perche' te l'ho detto io [...] devi trovarli, ce ne
sono tanti su Robinhood Chain e su Solana [...] low cap e micro cap".

Perche' prima non usciva niente: la classifica leggeva solo cio' che il
radar aveva salvato, e il radar guarda pool nuovi e di tendenza, cioe'
quasi solo meme coin. Un progetto con codice vero raramente e' nato ieri o
e' fra i piu' scambiati dell'ora.

Qui due fonti diverse, pensate per i progetti e non per i pool:

- CoinGecko, categorie "solana-ecosystem" e "robinhood-ecosystem", la
  fascia di capitalizzazione fra CAP_MIN e CAP_MAX. Per ogni moneta la
  scheda dice indirizzo del contratto, sito, categorie e soprattutto il
  repository GitHub: senza GitHub non si va avanti. Le categorie "Meme" si
  scartano subito.
- DexScreener, profili token appena compilati: chi paga per pubblicare
  sito e social su DexScreener. Si tengono quelli con un sito proprio e un
  nome non da meme; il GitHub lo cerca poi l'analisi nell'HTML del sito.

Quello che passa entra in classifica (`leaderboard.add`) e da li' fa la
stessa analisi profonda di tutti. CoinGecko gratuito regge poche richieste
al minuto: una ogni CG_GAP_S secondi, schede ricontrollate ogni settimana.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import time
from typing import Callable

import httpx

from app.sniper.quality import _looks_like_meme
from app.trading.onchain import legit, narratives, storage

logger = logging.getLogger(__name__)

_STORE_FILE = storage.data_file("onchain_discovery.json")

COINGECKO = "https://api.coingecko.com/api/v3"
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
# categoria CoinGecko e pagine da 250 (ordinate per cap decrescente) che
# coprono la fascia CAP_MIN-CAP_MAX
CG_CATEGORIES = {"solana": ("solana-ecosystem", range(3, 9)), "robinhood": ("robinhood-ecosystem", range(1, 5))}
CG_PLATFORMS = {"solana": ("solana",), "robinhood": ("robinhood",)}
# Lorenzo (24/09): "sotto uno barra due, tre milioni di market cap, non oltre"
CAP_MIN = 50_000
NARRATIVE_PAGES = 8
CAP_MAX = 3_000_000
# azioni tokenizzate, stablecoin, token avvolti: non sono progetti
_NOT_PROJECT = ("meme", "stock", "stablecoin", "wrapped", "bridged", "etf")
CG_GAP_S = 12                # 5 richieste al minuto: sotto il limite gratuito
CG_SWEEP_S = 6 * 3600        # un giro completo delle categorie
CG_DETAIL_TTL_S = 7 * 86400
PROFILES_POLL_S = 120
PROFILES_PER_POLL = 12

_state = {"cg_queue": [], "cg_last_sweep": 0.0, "cg_next_at": 0.0, "profiles_at": 0.0,
          "gh_queue": [], "gh_query": 0, "gh_search_at": 0.0, "gh_repo_at": 0.0,
          "found": 0, "skipped": 0, "running": False}
_chain_ids: dict[str, set[str]] = {}   # CoinGecko ids with a contract on our chains
_seen: dict[str, dict] = {}   # "cg:<chain>:<id>" o "dx:<chain>:<token>" -> {at, ok, why}


def _load() -> None:
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        _seen.update({k: v for k, v in (raw.get("seen") or {}).items() if isinstance(v, dict)})
        _state["cg_last_sweep"] = float(raw.get("cg_last_sweep") or 0)


def _save() -> None:
    try:
        _STORE_FILE.write_text(json.dumps({"seen": _seen, "cg_last_sweep": _state["cg_last_sweep"]},
                                          ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def reset_for_tests() -> None:
    _seen.clear()
    _chain_ids.clear()
    _state.update(cg_queue=[], cg_last_sweep=0.0, cg_next_at=0.0, profiles_at=0.0, gh_queue=[], gh_query=0,
                  gh_search_at=0.0, gh_repo_at=0.0, found=0, skipped=0, running=False)


def status() -> dict:
    return {"queue": len(_state["cg_queue"]) + len(_state["gh_queue"]), "found": _state["found"], "skipped": _state["skipped"],
            "last_sweep": _state["cg_last_sweep"] or None, "seen": len(_seen)}


def _note(key: str, ok: bool, why: str, now: float) -> None:
    _seen[key] = {"at": now, "ok": ok, "why": why}
    _state["found" if ok else "skipped"] += 1


# ------------------------------------------------------------- CoinGecko

def chain_index(rows: list[dict]) -> dict[str, set[str]]:
    """From /coins/list?include_platform=true: which ids live on which chain."""
    out: dict[str, set[str]] = {c: set() for c in CG_PLATFORMS}
    for r in rows or []:
        plats = r.get("platforms") or {}
        for chain, keys in CG_PLATFORMS.items():
            if any(plats.get(k) for k in keys):
                out[chain].add(r.get("id"))
    return out


def market_candidates(chain: str, rows: list[dict]) -> list[dict]:
    """Dalla lista /coins/markets: le monete nella fascia di cap, nome non da meme."""
    out = []
    for r in rows or []:
        cap = float(r.get("market_cap") or 0)
        if not (CAP_MIN <= cap <= CAP_MAX):
            continue
        if _looks_like_meme(r.get("symbol") or "", r.get("name") or ""):
            continue
        out.append({"id": r.get("id"), "chain": chain, "symbol": (r.get("symbol") or "").upper(),
                    "name": r.get("name") or "", "market_cap": cap, "image": r.get("image")})
    return [x for x in out if x["id"]]


def from_coin_detail(chain: str, coin: dict, base: dict | None = None) -> tuple[dict | None, str]:
    """(voce per la classifica, perche' no). Serve: contratto sulla chain,
    niente categoria meme, sito proprio. Il GitHub, se CoinGecko non lo ha,
    lo cerca l'analisi del sito."""
    platforms = coin.get("platforms") or {}
    token = next((platforms[p] for p in CG_PLATFORMS[chain] if platforms.get(p)), None)
    if not token:
        return None, "no contract on this chain"
    cats = [c for c in coin.get("categories") or [] if c]
    bad = next((c for c in cats if any(w in c.lower() for w in _NOT_PROJECT)), None)
    if bad:
        return None, f"category {bad}"
    links = coin.get("links") or {}
    website = legit.pick_website(links.get("homepage") or [])
    if not website:
        return None, "no own website"
    repos = [u for u in (links.get("repos_url") or {}).get("github") or [] if legit.github_targets([u])]
    socials = []
    if links.get("twitter_screen_name"):
        socials.append({"type": "twitter", "url": f"https://x.com/{links['twitter_screen_name']}"})
    if links.get("telegram_channel_identifier"):
        socials.append({"type": "telegram", "url": f"https://t.me/{links['telegram_channel_identifier']}"})
    b = base or {}
    return {
        "chain": chain, "token_address": token, "symbol": b.get("symbol") or (coin.get("symbol") or "").upper(),
        "name": b.get("name") or coin.get("name") or "", "website": website, "github": repos[0] if repos else None,
        "socials": socials, "market_cap": b.get("market_cap"), "image": b.get("image"),
        "categories": cats[:6], "description": ((coin.get("description") or {}).get("en") or "")[:400],
        "launched_at": _genesis(coin.get("genesis_date")),
    }, ""


def _genesis(date: str | None) -> float | None:
    try:
        return time.mktime(time.strptime(date, "%Y-%m-%d")) if date else None
    except ValueError:
        return None


async def _cg_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    r = await client.get(f"{COINGECKO}{path}", params=params, timeout=15.0,
                         headers={"Accept": "application/json"})
    if r.status_code == 429:
        raise RuntimeError("CoinGecko: troppe richieste")
    r.raise_for_status()
    return r.json()


async def _cg_step(client: httpx.AsyncClient, add: Callable[[dict, str], bool], now: float) -> None:
    """Una richiesta a CoinGecko: una pagina della categoria o una scheda."""
    if now < _state["cg_next_at"]:
        return
    _state["cg_next_at"] = now + CG_GAP_S
    q = _state["cg_queue"]
    if not q:
        if now - _state["cg_last_sweep"] < CG_SWEEP_S:
            return
        _state["cg_last_sweep"] = now
        # Narratives first (hotter, smaller lists), then the chain ecosystems.
        q.append(("index",))
        q.extend(("npage", cat, 1) for cat in narratives.coingecko_categories())
        q.extend(("page", chain, cat, p) for chain, (cat, pages) in CG_CATEGORIES.items() for p in pages)
        return
    job = q.pop(0)
    if job[0] == "index":
        rows = await _cg_get(client, "/coins/list", {"include_platform": "true"})
        _chain_ids.clear()
        _chain_ids.update(chain_index(rows if isinstance(rows, list) else []))
        return
    if job[0] == "npage":
        _, cat, page = job
        rows = await _cg_get(client, "/coins/markets", {"vs_currency": "usd", "category": cat,
                                                       "order": "market_cap_desc", "per_page": 250,
                                                       "page": page})
        rows = rows if isinstance(rows, list) else []
        fresh = []
        for chain in CG_CATEGORIES:
            ids = _chain_ids.get(chain) or set()
            fresh += [("coin", chain, c) for c in market_candidates(chain, [r for r in rows if r.get("id") in ids])
                      if now - (_seen.get(f"cg:{chain}:{c['id']}") or {}).get("at", 0) >= CG_DETAIL_TTL_S]
        q[0:0] = fresh
        # sorted by cap: another page only while the band is not yet below CAP_MIN
        if len(rows) == 250 and float(rows[-1].get("market_cap") or 0) >= CAP_MIN and page < NARRATIVE_PAGES:
            q.insert(len(fresh), ("npage", cat, page + 1))
        return
    if job[0] == "page":
        _, chain, cat, page = job
        rows = await _cg_get(client, "/coins/markets", {"vs_currency": "usd", "category": cat,
                                                       "order": "market_cap_desc", "per_page": 250,
                                                       "page": page})
        fresh = [("coin", chain, c) for c in market_candidates(chain, rows if isinstance(rows, list) else [])
                 if now - (_seen.get(f"cg:{chain}:{c['id']}") or {}).get("at", 0) >= CG_DETAIL_TTL_S]
        q[0:0] = fresh          # le schede di questa pagina prima delle pagine successive
        return
    _, chain, base = job
    coin = await _cg_get(client, f"/coins/{base['id']}", {"localization": "false", "tickers": "false",
                                                         "market_data": "false", "community_data": "false",
                                                         "developer_data": "false"})
    item, why = from_coin_detail(chain, coin, base)
    _note(f"cg:{chain}:{base['id']}", item is not None, why, now)
    if item:
        add(item, "coingecko")
    _save()


# ----------------------------------------------------------- DexScreener

def profile_candidates(profiles: list[dict]) -> list[dict]:
    out = []
    for p in profiles or []:
        chain = p.get("chainId")
        if chain not in CG_CATEGORIES or not p.get("tokenAddress"):
            continue
        links = p.get("links") or []
        website = legit.pick_website([x.get("url") or "" for x in links
                                      if (x.get("label") or x.get("type") or "").lower() in ("website", "")])
        if not website:
            continue
        desc = p.get("description") or ""
        if _looks_like_meme("", desc[:80]) or "meme" in desc.lower():
            continue
        github = next((x["url"] for x in links if "github.com" in (x.get("url") or "")), None)
        socials = [{"type": (x.get("type") or "").lower(), "url": x["url"]} for x in links
                   if x.get("type") and x.get("url")]
        out.append({"chain": chain, "token_address": p["tokenAddress"], "website": website, "github": github,
                    "socials": socials, "description": desc[:400], "image": p.get("icon")})
    return out


async def _poll_profiles(client: httpx.AsyncClient, add: Callable[[dict, str], bool], now: float) -> None:
    if now - _state["profiles_at"] < PROFILES_POLL_S:
        return
    _state["profiles_at"] = now
    r = await client.get(DEX_PROFILES, timeout=10.0)
    r.raise_for_status()
    n = 0
    for c in profile_candidates(r.json()):
        k = f"dx:{c['chain']}:{c['token_address']}"
        if k in _seen or n >= PROFILES_PER_POLL:
            continue
        n += 1
        _note(k, True, "", now)
        add(c, "dexscreener")


# ---------------------------------------------------------------- GitHub
#
# Reversed channel: start from the code. Serious small projects put their
# contract address in the README; the address both finds the token and
# proves the repository is theirs. Only the search costs GitHub API quota
# (one call every GH_SEARCH_S); READMEs come from raw.githubusercontent.

GH_QUERIES = (
    '"pump.fun" in:readme',
    '"dexscreener.com/solana" in:readme',
    '"dexscreener.com/robinhood" in:readme',
    '"robinhood chain" token in:readme',
    '"contract address" solana in:readme',
    '"CA:" solana in:readme',
    '"token" "solana" "mint address" in:readme',
    '"birdeye.so/token" in:readme',
    '"robinhood chain" in:readme,description',
)
GH_SEARCH_S = 60
GH_REPO_S = 6
GH_FRESH_DAYS = 120
GH_QUEUE_MAX = 300
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"
_SOL_ADDR = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_EVM_ADDR = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
# Program ids and famous mints: in many READMEs, never a project's own token.
_KNOWN = {
    "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "11111111111111111111111111111111", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "0x0000000000000000000000000000000000000000",
}


def readme_addresses(text: str) -> dict[str, list[str]]:
    """Contract addresses in a README, by chain family, in order of appearance."""
    sol, evm = [], []
    for m in _SOL_ADDR.findall(_EVM_ADDR.sub(" ", text or "")):
        # a base58 word with no digit and no mixed case is a word, not a key
        if m not in _KNOWN and m not in sol and any(c.isdigit() for c in m) and not m.islower():
            sol.append(m)
    for m in _EVM_ADDR.findall(text or ""):
        if m not in _KNOWN and m.lower() not in {x.lower() for x in evm}:
            evm.append(m)
    return {"solana": sol[:10], "robinhood": evm[:10]}


def search_candidates(items: list[dict], now: float) -> list[dict]:
    """Repositories worth reading: own code, pushed recently, not forks."""
    out = []
    for r in items or []:
        full = r.get("full_name")
        if not full or r.get("fork") or r.get("archived") or not r.get("size"):
            continue
        pushed = _iso(r.get("pushed_at"))
        if pushed and now - pushed > GH_FRESH_DAYS * 86400:
            continue
        if f"gh:{full}" in _seen:
            continue
        out.append({"full_name": full, "html_url": r.get("html_url") or f"https://github.com/{full}",
                    "homepage": r.get("homepage") or "", "default_branch": r.get("default_branch") or "HEAD"})
    return out


def _iso(s: str | None) -> float | None:
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) if s else None
    except ValueError:
        return None


def token_from_pairs(chain: str, pairs: list[dict], repo: dict) -> tuple[dict | None, str]:
    """The README's token, from DexScreener: the most liquid pool, cap in band."""
    best = None
    for p in pairs or []:
        if p.get("chainId") != chain:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if best is None or liq > float((best.get("liquidity") or {}).get("usd") or 0):
            best = p
    if best is None:
        return None, "no pool"
    base = best.get("baseToken") or {}
    cap = float(best.get("marketCap") or best.get("fdv") or 0)
    if not (CAP_MIN <= cap <= CAP_MAX):
        return None, f"cap ${cap / 1e6:.2f}M"
    if _looks_like_meme(base.get("symbol") or "", base.get("name") or ""):
        return None, "meme name"
    links = legit.links_from_dexscreener(best)
    website = legit.pick_website(links["websites"] + [repo.get("homepage") or ""])
    return {
        "chain": chain, "token_address": base.get("address"), "pair_address": best.get("pairAddress") or "",
        "symbol": base.get("symbol") or "?", "name": base.get("name") or "", "website": website,
        "github": repo["html_url"], "github_verified": True, "socials": links["socials"],
        "market_cap": cap, "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
        "launched_at": (best.get("pairCreatedAt") or 0) / 1000 or None, "image": (best.get("info") or {}).get("imageUrl"),
        "pair_url": best.get("url"),
    }, ""


async def _gh_search(client: httpx.AsyncClient, now: float) -> None:
    if now - _state["gh_search_at"] < GH_SEARCH_S or len(_state["gh_queue"]) >= GH_QUEUE_MAX:
        return
    _state["gh_search_at"] = now
    for k in [k for k, v in _seen.items() if now - v.get("at", now) > 30 * 86400]:
        del _seen[k]
    queries = GH_QUERIES + tuple(narratives.github_queries())
    q = queries[_state["gh_query"] % len(queries)]
    _state["gh_query"] += 1
    since = time.strftime("%Y-%m-%d", time.gmtime(now - GH_FRESH_DAYS * 86400))
    r = await legit.gh_get(client, f"{legit.GITHUB_API}/search/repositories",
                         params={"q": f"{q} fork:false pushed:>{since}", "sort": "updated", "per_page": 50},
                         headers=legit._gh_headers(), timeout=10.0)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub search {r.status_code}")
    queued = {j["full_name"] for j in _state["gh_queue"]}
    _state["gh_queue"].extend(c for c in search_candidates(r.json().get("items") or [], now)
                              if c["full_name"] not in queued)


async def _gh_repo(client: httpx.AsyncClient, add: Callable[[dict, str], bool], now: float) -> None:
    if not _state["gh_queue"] or now - _state["gh_repo_at"] < GH_REPO_S:
        return
    _state["gh_repo_at"] = now
    repo = _state["gh_queue"].pop(0)
    text = ""
    for name in ("README.md", "readme.md", "README.MD", "README"):
        r = await client.get(f"https://raw.githubusercontent.com/{repo['full_name']}/{repo['default_branch']}/{name}",
                             timeout=10.0)
        if r.status_code == 200:
            text = r.text[:200_000]
            break
    found, why = None, "no contract address"
    for chain, addrs in readme_addresses(text).items():
        if not addrs:
            continue
        r = await client.get(DEX_TOKENS.format(chain=chain, addresses=",".join(addrs)), timeout=10.0)
        pairs = r.json() if r.status_code == 200 else []
        # the first address in the README with a pool is the project's token
        for a in addrs:
            own = [p for p in pairs if ((p.get("baseToken") or {}).get("address") or "").lower() == a.lower()]
            if own:
                found, why = token_from_pairs(chain, own, repo)
                break
        if found or why != "no contract address":
            break
    _note(f"gh:{repo['full_name']}", found is not None, why, now)
    if found:
        add(found, "github")
    _save()


# -------------------------------------------------------------------- giro

async def tick(client: httpx.AsyncClient, add: Callable[[dict, str], bool], now: float | None = None) -> None:
    """Chiamato dal giro della classifica. Non solleva."""
    now = now or time.time()
    if _state["running"]:
        return
    _state["running"] = True
    try:
        steps = (("coingecko", _cg_step), ("profili", _poll_profiles),
                 ("github", lambda c, a, n: _gh_search(c, n)), ("readme", _gh_repo))
        for name, step in steps:
            try:
                await step(client, add, now)
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                logger.info("scan progetti: %s non riuscito: %s", name, exc)
                if name == "coingecko":
                    _state["cg_next_at"] = now + 60
    finally:
        _state["running"] = False
    await asyncio.sleep(0)


_load()
