"""Scoperta e punteggio dei progetti layer 0, layer 1 e layer 2.

Lorenzo (01/10): "trova i progetti layer 0, layer 1 e layer 2 con chain e
token propri, solo API gratuite, pochi al giorno; i risultati vanno nello
stesso database progetti". `discovery.py` cerca solo contratti token su
Solana/EVM, quindi una moneta nativa di un L1 (KAS, TIA...) non viene mai
trovata: qui le fonti sono pensate per le chain, non per i pool.

Fase 1: solo scoperta, punteggio e righe nel database. I layer NON entrano
ancora nella Top 50 (fase 2: prima servono le candele CEX): `main._top_entries`
salta `chain == "layer"`.

Fascia (Lorenzo, 01/10: "usiamo da 500k di market cap fino a 100mln"): il
tetto $10M delle gemme non vale, una L1 seria raramente sta sotto.

Fonti (tutte gratuite):
- DefiLlama `/v2/chains`: una chain per `gecko_id`, con il suo TVL.
- CoinGecko `/coins/markets` per le categorie layer-1, layer-0-l0, layer-2:
  la fascia di capitalizzazione LAYER_CAP_MIN-LAYER_CAP_MAX, meme esclusi.
- L2BEAT `/api/scaling/summary`: stage e TVS dei rollup, e se hanno un token.
- DefiLlama stablecoin e fees: stablecoin e commissioni per chain.
- MEXC e Binance `exchangeInfo`: se esiste una coppia USDT (fase 2).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import httpx

from app.atomic_write import write_text_atomic
from app.sniper.quality import _looks_like_meme
from app.trading.onchain import depth, legit, projects_db, research, storage

logger = logging.getLogger(__name__)

_STORE_FILE = storage.data_file("onchain_layers.json")

LAYER_CAP_MIN = 500_000
LAYER_CAP_MAX = 100_000_000
LAYER_FDV_MAX_RATIO = 5.0
RUN_EVERY_S = 24 * 3600
DETAILS_PER_RUN = 20
CG_GAP_S = 30
DETAIL_TTL_S = 7 * 86400
IDS_PER_CALL = 100           # 250 ids make a URL CoinGecko answers 403
CHAIN = "layer"

LLAMA_CHAINS = "https://api.llama.fi/v2/chains"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoinchains"
LLAMA_FEES = ("https://api.llama.fi/overview/fees?excludeTotalDataChart=true"
              "&excludeTotalDataChartBreakdown=true&dataType=dailyFees")
LLAMA_TVL_HISTORY = "https://api.llama.fi/v2/historicalChainTvl/{name}"
L2BEAT_SUMMARY = "https://l2beat.com/api/scaling/summary"
COINGECKO = "https://api.coingecko.com/api/v3"
CG_CATEGORIES = ("layer-1", "layer-0-l0", "layer-2")
MEXC_EXCHANGE_INFO = "https://api.mexc.com/api/v3/exchangeInfo"
BINANCE_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"

_WEIGHTS = {"github": 0.30, "site": 0.20, "tokenomics": 0.20, "traction": 0.30}

_state: dict = {"last_run": 0.0, "found": {}}


def _load() -> None:
    global _state
    try:
        data = _STORE_FILE.read_text()
    except OSError:
        return
    try:
        loaded = json.loads(data)
    except ValueError:
        return
    if isinstance(loaded, dict):
        _state = {"last_run": float(loaded.get("last_run") or 0.0),
                  "found": loaded.get("found") or {}}


def _save() -> None:
    write_text_atomic(_STORE_FILE, json.dumps(_state))


def reset_for_tests() -> None:
    global _state
    _state = {"last_run": 0.0, "found": {}}


_load()


def llama_candidates(rows: list[dict]) -> list[dict]:
    """Una candidata per ogni `gecko_id` distinto."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows or []:
        gid = (r.get("gecko_id") or "").strip()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        out.append({"id": gid, "name": r.get("name") or "", "tvl": r.get("tvl") or 0})
    return out


def cg_candidates(rows: list[dict]) -> list[dict]:
    """Monete CoinGecko nella fascia di capitalizzazione, meme esclusi."""
    out: list[dict] = []
    for r in rows or []:
        cap = r.get("market_cap") or 0
        if not (LAYER_CAP_MIN <= cap <= LAYER_CAP_MAX):
            continue
        symbol = r.get("symbol") or ""
        name = r.get("name") or ""
        if _looks_like_meme(symbol, name):
            continue
        out.append({"id": r.get("id") or "", "symbol": symbol.upper(), "name": name,
                    "market_cap": cap, "image": r.get("image") or ""})
    return out


def l2beat_index(summary: dict) -> dict[str, dict]:
    """Indice per nome e slug (minuscolo) -> stage, tvs, has_token."""
    out: dict[str, dict] = {}
    projects = (summary or {}).get("projects") or {}
    for p in projects.values():
        if p.get("isArchived"):
            continue
        tvs = ((p.get("tvs") or {}).get("breakdown") or {}).get("total") or 0
        entry = {"stage": p.get("stage") or "", "tvs": tvs,
                 "has_token": bool((p.get("tvs") or {}).get("associatedTokens"))}
        for key in (p.get("name"), p.get("slug")):
            if key:
                out[str(key).lower()] = entry
    return out


def llama_extras(stables: list[dict], fees: dict) -> dict[str, dict]:
    """Indice per nome chain (minuscolo) -> stables_usd, fees_30d."""
    out: dict[str, dict] = {}
    for r in stables or []:
        name = (r.get("name") or "").lower()
        if not name:
            continue
        usd = (r.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0
        out.setdefault(name, {})["stables_usd"] = usd
    for p in (fees or {}).get("protocols") or []:
        if p.get("category") != "Chain":
            continue
        name = (p.get("name") or "").lower()
        if not name:
            continue
        out.setdefault(name, {})["fees_30d"] = p.get("total30d") or 0
    return out


def cex_symbol(symbol: str, mexc: set[str], binance: set[str]) -> dict | None:
    """Coppia USDT su MEXC (prima) o Binance, o None."""
    sym = (symbol or "").upper()
    if not sym:
        return None
    pair = f"{sym}USDT"
    if pair in mexc:
        return {"exchange": "mexc", "pair": pair}
    if pair in binance:
        return {"exchange": "binance", "pair": pair}
    return None


def classify(coin: dict, l2beat: dict[str, dict] | None = None) -> tuple[str | None, str]:
    """(layer_type, why_not) da una scheda CoinGecko /coins/{id}."""
    cats = [str(c) for c in (coin.get("categories") or [])]
    low = [c.lower() for c in cats]
    for bad in ("meme", "stablecoin", "wrapped", "bridged", "liquid staking"):
        for c in low:
            if bad in c:
                return None, f"category {c}"
    layer_type = None
    for c in cats:
        if "Layer 0 (L0)" in c:
            layer_type = "L0"
            break
        if "Layer 1 (L1)" in c:
            layer_type = "L1"
            break
        if "Layer 2 (L2)" in c or "Layer 3 (L3)" in c or "Rollup" in c:
            layer_type = "L2"
            break
    if layer_type is None:
        return None, "not a layer"
    md = coin.get("market_data") or {}
    cap = (md.get("market_cap") or {}).get("usd") or 0
    if not (LAYER_CAP_MIN <= cap <= LAYER_CAP_MAX):
        return None, (f"market cap ${cap:,.0f} outside"
                      f" ${LAYER_CAP_MIN / 1e6:g}M-${LAYER_CAP_MAX / 1e6:g}M")
    fdv = (md.get("fully_diluted_valuation") or {}).get("usd") or 0
    if cap and fdv > LAYER_FDV_MAX_RATIO * cap:
        return None, f"FDV {fdv / cap:.1f} times the cap"
    if layer_type == "L2" and l2beat:
        name = (coin.get("name") or "").lower()
        entry = l2beat.get(name)
        if entry and not entry.get("has_token"):
            return None, "L2 without own token"
    return layer_type, ""


def score_tokenomics(coin: dict) -> dict:
    """Pillar tokenomics: circulating/max (o total) e FDV/cap."""
    md = coin.get("market_data") or {}
    circ = (md.get("circulating_supply") or 0) or 0
    total = (md.get("total_supply") or 0) or 0
    max_s = (md.get("max_supply") or 0) or 0
    denom = max_s or total
    if not circ or not denom:
        return depth._pillar("tokenomics", None, {"circulating": circ, "total": total, "max": max_s},
                             ["no supply data"])
    ratio = min(1.0, circ / denom)
    if ratio >= 0.8:
        score = 90
    elif ratio >= 0.6:
        score = 75
    elif ratio >= 0.4:
        score = 60
    elif ratio >= 0.25:
        score = 45
    else:
        score = 30
    cap = (md.get("market_cap") or {}).get("usd") or 0
    fdv = (md.get("fully_diluted_valuation") or {}).get("usd") or 0
    fdv_ratio = (fdv / cap) if cap else 0
    if fdv_ratio > 3:
        score -= 15
    facts = {"circulating": circ, "total": total, "max": max_s, "fdv_ratio": round(fdv_ratio, 2)}
    return depth._pillar("tokenomics", score, facts, [])


def score_traction(cap: float, tvl: float, tvl_30d_ago: float, stables_usd: float,
                   fees_30d: float, l2beat: dict | None) -> dict:
    """Pillar traction: cap/TVL, crescita TVL, stablecoin, fees, stage L2BEAT."""
    if not tvl:
        return depth._pillar("traction", 35, {"tvl": 0}, ["no DeFi usage data"])
    score = 30.0
    ratio = cap / tvl if tvl else 0
    if ratio < 2:
        score += 25
    elif ratio < 5:
        score += 15
    elif ratio < 15:
        score += 5
    if tvl_30d_ago:
        growth = (tvl - tvl_30d_ago) / tvl_30d_ago
        if growth > 0.20:
            score += 10
        elif growth < -0.30:
            score -= 10
    if stables_usd > 10_000_000:
        score += 10
    annual_fees = fees_30d * 12
    if annual_fees and cap:
        fr = cap / annual_fees
        if fr < 100:
            score += 15
        elif fr < 500:
            score += 5
    stage = (l2beat or {}).get("stage") or ""
    if stage in ("Stage 1", "Stage 2"):
        score += 10
    facts = {"tvl": tvl, "tvl_30d": tvl_30d_ago, "stables_usd": stables_usd,
             "fees_30d": fees_30d, "l2beat": l2beat or {}}
    return depth._pillar("traction", score, facts, [])


def final_score(pillars: list[dict]) -> float:
    """Media pesata dei pillar con un punteggio, pesi rinormalizzati."""
    num = 0.0
    den = 0.0
    for p in pillars:
        w = _WEIGHTS.get(p.get("key") or "", 0)
        s = p.get("score")
        if s is None or not w:
            continue
        num += w * s
        den += w
    return round(num / den, 2) if den else 0.0


RETRY_429_S = 65            # CoinGecko's free minute, shared with discovery.py


async def _cg_slot() -> None:
    """Wait for discovery.py's next CoinGecko slot and take it. On 01/10 the
    two plus the board's cap check asked ~9 times a minute and every layer
    call got 429: now layer calls replace discovery calls, they never add."""
    from app.trading.onchain import discovery
    while True:
        now = time.time()
        nxt = discovery._state.get("cg_next_at") or 0
        if now >= nxt:
            discovery._state["cg_next_at"] = now + max(CG_GAP_S, discovery.CG_GAP_S)
            return
        await asyncio.sleep(nxt - now + 0.1)


async def _fetch_json(client: httpx.AsyncClient, url: str) -> object:
    """GET as JSON. A CoinGecko 429 waits out the minute and tries once more:
    discovery.py spends the same free budget, so a first 429 is normal."""
    r = await client.get(url)
    if r.status_code == 429 and url.startswith(COINGECKO):
        from app.trading.onchain import discovery
        discovery._state["cg_next_at"] = time.time() + RETRY_429_S     # discovery backs off too
        await asyncio.sleep(RETRY_429_S)
        r = await client.get(url)
    r.raise_for_status()
    return r.json()


async def _fetch_cex(client: httpx.AsyncClient, url: str, statuses: tuple[str, ...]) -> set[str]:
    try:
        data = await _fetch_json(client, url)
    except (httpx.HTTPError, ValueError):
        return set()
    out: set[str] = set()
    for s in (data or {}).get("symbols") or []:
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") in statuses:
            out.add(s.get("symbol") or "")
    return out


async def _caps_by_ids(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    """Market rows for DefiLlama's gecko ids, 250 per call: the band is checked
    before spending a detail call on Ethereum or Solana."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), IDS_PER_CALL):
        await _cg_slot()
        try:
            rows = await _fetch_json(client, f"{COINGECKO}/coins/markets?vs_currency=usd&per_page=250"
                                             f"&ids={','.join(ids[i:i + IDS_PER_CALL])}") or []
        except (httpx.HTTPError, ValueError):
            logger.exception("layers: coingecko markets by id failed")
            continue
        out.update({r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")})
    return out


async def run(client: httpx.AsyncClient, now: float | None = None) -> int:
    """Un giro: raccoglie le candidate, ne dettaglia fino a DETAILS_PER_RUN."""
    now = now or time.time()
    llama_rows: list[dict] = []
    cg_rows: list[dict] = []
    l2beat: dict[str, dict] = {}
    extras: dict[str, dict] = {}
    try:
        llama_rows = await _fetch_json(client, LLAMA_CHAINS) or []
    except (httpx.HTTPError, ValueError):
        logger.exception("layers: llama chains failed")
    for cat in CG_CATEGORIES:
        await _cg_slot()
        try:
            rows = await _fetch_json(
                client, f"{COINGECKO}/coins/markets?vs_currency=usd&category={cat}"
                        "&order=market_cap_desc&per_page=250&page=1") or []
            cg_rows += rows
        except (httpx.HTTPError, ValueError):
            logger.exception("layers: coingecko category %s failed", cat)
    try:
        l2beat = l2beat_index(await _fetch_json(client, L2BEAT_SUMMARY) or {})
    except (httpx.HTTPError, ValueError):
        logger.exception("layers: l2beat failed")
    try:
        stables = await _fetch_json(client, LLAMA_STABLES) or []
        fees = await _fetch_json(client, LLAMA_FEES) or {}
        extras = llama_extras(stables, fees)
    except (httpx.HTTPError, ValueError):
        logger.exception("layers: llama extras failed")
    mexc = await _fetch_cex(client, MEXC_EXCHANGE_INFO, ("1", "ENABLED"))
    binance = await _fetch_cex(client, BINANCE_EXCHANGE_INFO, ("TRADING",))

    merged: dict[str, dict] = {}
    llama = llama_candidates(llama_rows)
    caps = await _caps_by_ids(client, [c["id"] for c in llama]) if llama else {}
    in_band = {r["id"] for r in cg_candidates(list(caps.values()))}
    for c in llama:
        if c["id"] in in_band:
            merged[c["id"]] = c
    for c in cg_candidates(cg_rows):
        merged.setdefault(c["id"], {}).update(c)

    if not merged:
        # Every list failed (CoinGecko busy): not a finished run, the loop
        # tries again within the hour. On 01/10 the first run ended here and
        # was stamped done for a day.
        logger.warning("layers: no candidates (llama %d, coingecko %d rows), retrying later",
                       len(llama_rows), len(cg_rows))
        return 0
    found = _state.get("found") or {}
    todo = [c for c in merged.values()
            if now - (found.get(c["id"], {}).get("at") or 0) >= DETAIL_TTL_S]
    todo.sort(key=lambda c: (c["id"] in found, -(c.get("tvl") or 0)))
    todo = todo[:DETAILS_PER_RUN]

    done = 0
    for c in todo:
        gid = c["id"]
        await _cg_slot()
        try:
            coin = await _fetch_json(
                client, f"{COINGECKO}/coins/{gid}?localization=false&tickers=false"
                        "&market_data=true&community_data=false&developer_data=true")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Not a finished run: the loop tries again within the hour.
                logger.warning("layers: coingecko 429, stopping run")
                return done
            logger.exception("layers: coingecko detail %s failed", gid)
            continue
        except (httpx.HTTPError, ValueError):
            logger.exception("layers: coingecko detail %s failed", gid)
            continue
        layer_type, why = classify(coin, l2beat)
        if layer_type is None:
            found[gid] = {"at": now, "type": None, "score": 0, "status": "rejected", "why": why, "cex": None}
            _state["found"] = found
            _save()
            continue
        md = coin.get("market_data") or {}
        cap = (md.get("market_cap") or {}).get("usd") or 0
        symbol = (coin.get("symbol") or "").upper()
        name = coin.get("name") or ""
        links = coin.get("links") or {}
        repos = (links.get("repos_url") or {}).get("github") or []
        launched_at = None
        gd = coin.get("genesis_date")
        if gd:
            try:
                launched_at = datetime.fromisoformat(gd).timestamp()
            except ValueError:
                launched_at = None
        ex = extras.get(name.lower()) or {}
        tvl = c.get("tvl") or 0
        tvl_30d = 0.0
        if tvl:
            try:
                hist = await _fetch_json(client, LLAMA_TVL_HISTORY.format(name=name)) or []
                if len(hist) >= 30:
                    tvl_30d = hist[-30].get("tvl") or 0
            except (httpx.HTTPError, ValueError):
                logger.exception("layers: tvl history %s failed", name)
        pillars: list[dict] = []
        try:
            gh = await depth.analyze_github(repos, client, launched_at)
        except Exception:
            logger.exception("layers: github %s failed", gid)
            gh = depth.missing_github("github analysis failed")
        if gh.get("facts") is not None:
            gh["facts"]["commit_count_4_weeks"] = (coin.get("developer_data") or {}).get("commit_count_4_weeks")
        pillars.append(gh)
        item = {"chain": CHAIN, "token_address": gid, "symbol": symbol, "name": name,
                "image": coin.get("image") or "", "description": (coin.get("description") or {}).get("en") or "",
                "website": legit.pick_website(links.get("homepage") or []),
                "github": repos[0] if repos else "", "market_cap": cap, "launched_at": launched_at}
        try:
            site_pillar, _ = await research.analyze(item, client, launched_at)
        except Exception:
            logger.exception("layers: site %s failed", gid)
            site_pillar = depth._pending("site", "site analysis failed")
        pillars.append(site_pillar)
        pillars.append(score_tokenomics(coin))
        pillars.append(score_traction(cap, tvl, tvl_30d, ex.get("stables_usd") or 0,
                                      ex.get("fees_30d") or 0, l2beat.get(name.lower())))
        fatal = next((p.get("fatal") for p in pillars if p.get("fatal")), "")
        score = final_score(pillars)
        cex = cex_symbol(symbol, mexc, binance)
        flags = []
        if cex is None:
            flags.append("no USDT pair on MEXC/Binance")
        cats = [layer_type] + [str(x) for x in (coin.get("categories") or [])]
        projects_db.upsert({**item, "cg_id": gid, "categories": cats}, "layers", now)
        status = "rejected" if fatal else "studied"
        projects_db.record({**item, "cg_id": gid, "pillars": pillars,
                            "overall": {"score": score, "layer": layer_type, "cex": cex, "flags": flags}},
                           status, fatal, score=score,
                           traction={"tvl": tvl, "tvl_30d": tvl_30d,
                                     "stables_usd": ex.get("stables_usd") or 0,
                                     "fees_30d": ex.get("fees_30d") or 0,
                                     "l2beat": l2beat.get(name.lower()) or {}}, now=now)
        found[gid] = {"at": now, "type": layer_type, "score": score, "status": status,
                      "why": fatal, "cex": cex}
        _state["found"] = found
        _save()
        done += 1
    _state["last_run"] = now
    _save()
    return done


async def layers_loop() -> None:
    """Aspetta 120 s, poi un giro ogni RUN_EVERY_S, controllando ogni ora."""
    await asyncio.sleep(120)
    while True:
        try:
            if not _state.get("found") or time.time() - (_state.get("last_run") or 0) >= RUN_EVERY_S:
                async with httpx.AsyncClient(timeout=20) as client:
                    await run(client)
        except Exception:
            logger.exception("layers loop failed")
        await asyncio.sleep(3600)


def snapshot() -> dict:
    """Stato del layer engine: ultimo giro e progetti trovati."""
    found = _state.get("found") or {}
    items = [{"id": gid, **v} for gid, v in found.items()]
    items.sort(key=lambda x: -(x.get("score") or 0))
    return {"last_run": _state.get("last_run") or 0, "found": items}
