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
import json
import logging
import time
from typing import Callable

import httpx

from app.sniper.quality import _looks_like_meme
from app.trading.onchain import legit, storage

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
CAP_MAX = 3_000_000
# azioni tokenizzate, stablecoin, token avvolti: non sono progetti
_NOT_PROJECT = ("meme", "stock", "stablecoin", "wrapped", "bridged", "etf")
CG_GAP_S = 12                # 5 richieste al minuto: sotto il limite gratuito
CG_SWEEP_S = 6 * 3600        # un giro completo delle categorie
CG_DETAIL_TTL_S = 7 * 86400
PROFILES_POLL_S = 120
PROFILES_PER_POLL = 12

_state = {"cg_queue": [], "cg_last_sweep": 0.0, "cg_next_at": 0.0, "profiles_at": 0.0,
          "found": 0, "skipped": 0, "running": False}
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
    _state.update(cg_queue=[], cg_last_sweep=0.0, cg_next_at=0.0, profiles_at=0.0, found=0, skipped=0,
                  running=False)


def status() -> dict:
    return {"queue": len(_state["cg_queue"]), "found": _state["found"], "skipped": _state["skipped"],
            "last_sweep": _state["cg_last_sweep"] or None, "seen": len(_seen)}


def _note(key: str, ok: bool, why: str, now: float) -> None:
    _seen[key] = {"at": now, "ok": ok, "why": why}
    _state["found" if ok else "skipped"] += 1


# ------------------------------------------------------------- CoinGecko

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
        return None, "nessun contratto sulla chain"
    cats = [c for c in coin.get("categories") or [] if c]
    bad = next((c for c in cats if any(w in c.lower() for w in _NOT_PROJECT)), None)
    if bad:
        return None, f"categoria {bad}"
    links = coin.get("links") or {}
    website = legit.pick_website(links.get("homepage") or [])
    if not website:
        return None, "nessun sito proprio"
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
        q.extend(("page", chain, cat, p) for chain, (cat, pages) in CG_CATEGORIES.items() for p in pages)
        return
    job = q.pop(0)
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


# -------------------------------------------------------------------- giro

async def tick(client: httpx.AsyncClient, add: Callable[[dict, str], bool], now: float | None = None) -> None:
    """Chiamato dal giro della classifica. Non solleva."""
    now = now or time.time()
    if _state["running"]:
        return
    _state["running"] = True
    try:
        for name, step in (("coingecko", _cg_step), ("profili", _poll_profiles)):
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
