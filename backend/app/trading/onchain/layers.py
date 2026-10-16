"""Scoperta e punteggio dei progetti layer 0, layer 1 e layer 2.

Lorenzo (01/10): "trova i progetti layer 0, layer 1 e layer 2 con chain e
token propri, solo API gratuite, pochi al giorno; i risultati vanno nello
stesso database progetti". `discovery.py` cerca solo contratti token su
Solana/EVM, quindi una moneta nativa di un L1 (KAS, TIA...) non viene mai
trovata: qui le fonti sono pensate per le chain, non per i pool.

Le righe vanno nel database progetti; entrano nella Top 50 solo con una
coppia USDT su MEXC o Binance (`main._top_entries`), da cui vengono le candele
(`cex_candles.py`).

Fascia (Lorenzo, 01/10: "usiamo da 500k di market cap fino a 100mln"): il
tetto $10M delle gemme non vale, una L1 seria raramente sta sotto.

Fonti (tutte gratuite):
- CoinPaprika `/v1/tags` (layer-0-l0, layer-1-l1, layer-2-l2, layer-3-l3) e
  `/v1/tickers`: chi e' un layer e la sua capitalizzazione, senza chiave.
  CoinGecko non si usa: il server spende gia' tutto il budget gratuito in
  discovery.py e il 01/10 ogni chiamata dei layer prendeva 429.
- CoinPaprika `/v1/coins/{id}`: sito, GitHub, logo, descrizione, tag.
- DefiLlama `/v2/chains`: il TVL di ogni chain, per nome.
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

from app import http_client

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
DETAIL_TTL_S = 7 * 86400
CHAIN = "layer"

LLAMA_CHAINS = "https://api.llama.fi/v2/chains"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoinchains"
LLAMA_FEES = ("https://api.llama.fi/overview/fees?excludeTotalDataChart=true"
              "&excludeTotalDataChartBreakdown=true&dataType=dailyFees")
LLAMA_TVL_HISTORY = "https://api.llama.fi/v2/historicalChainTvl/{name}"
L2BEAT_SUMMARY = "https://l2beat.com/api/scaling/summary"
PAPRIKA = "https://api.coinpaprika.com/v1"
# tag -> layer type; a coin with several tags takes the lowest layer
PAPRIKA_TAGS = {"layer-0-l0": "L0", "layer-1-l1": "L1", "layer-2-l2": "L2", "layer-3-l3": "L2"}
_TYPE_ORDER = ("L0", "L1", "L2")
_BAD_TAGS = ("meme", "stablecoin", "wrapped", "bridged", "liquid staking", "liquid-staking")
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


def llama_tvl(rows: list[dict]) -> dict[str, float]:
    """Nome chain DefiLlama (minuscolo) -> TVL."""
    out: dict[str, float] = {}
    for r in rows or []:
        name = (r.get("name") or "").strip().lower()
        if name:
            out[name] = max(out.get(name, 0), r.get("tvl") or 0)
    return out


def layer_types(tags: list[dict]) -> dict[str, str]:
    """CoinPaprika id -> L0/L1/L2 dai tag layer; L0 prevale su L1 su L2."""
    out: dict[str, str] = {}
    for t in tags or []:
        kind = PAPRIKA_TAGS.get(t.get("id") or "")
        if not kind:
            continue
        for cid in t.get("coins") or []:
            old = out.get(cid)
            if old is None or _TYPE_ORDER.index(kind) < _TYPE_ORDER.index(old):
                out[cid] = kind
    return out


def paprika_candidates(tickers: list[dict], types: dict[str, str]) -> list[dict]:
    """Layer nella fascia di capitalizzazione, FDV entro LAYER_FDV_MAX_RATIO
    volte il cap, meme esclusi. Il circolante e' cap / prezzo: /v1/tickers
    non lo da'."""
    out: list[dict] = []
    for t in tickers or []:
        cid = t.get("id") or ""
        kind = types.get(cid)
        if not kind:
            continue
        usd = (t.get("quotes") or {}).get("USD") or {}
        cap = usd.get("market_cap") or 0
        price = usd.get("price") or 0
        if not (LAYER_CAP_MIN <= cap <= LAYER_CAP_MAX) or not price:
            continue
        symbol = (t.get("symbol") or "").strip().upper()
        name = t.get("name") or ""
        if _looks_like_meme(symbol, name):
            continue
        total = t.get("total_supply") or 0
        max_s = t.get("max_supply") or 0
        fdv = price * (max_s or total)
        if fdv > LAYER_FDV_MAX_RATIO * cap:
            continue
        out.append({"id": cid, "symbol": symbol, "name": name, "type": kind, "market_cap": cap,
                    "price": price, "circulating": cap / price, "total": total, "max": max_s,
                    "fdv": fdv, "rank": t.get("rank") or 0})
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


def classify(cand: dict, coin: dict, l2beat: dict[str, dict] | None = None) -> tuple[str | None, str]:
    """(layer_type, why_not): candidata da /v1/tickers, scheda da /v1/coins/{id}."""
    for t in coin.get("tags") or []:
        low = f"{t.get('id') or ''} {t.get('name') or ''}".lower()
        for bad in _BAD_TAGS:
            if bad in low:
                return None, f"tag {t.get('name') or t.get('id')}"
    layer_type = cand.get("type")
    if layer_type not in _TYPE_ORDER:
        return None, "not a layer"
    cap = cand.get("market_cap") or 0
    if not (LAYER_CAP_MIN <= cap <= LAYER_CAP_MAX):
        return None, (f"market cap ${cap:,.0f} outside"
                      f" ${LAYER_CAP_MIN / 1e6:g}M-${LAYER_CAP_MAX / 1e6:g}M")
    fdv = cand.get("fdv") or 0
    if fdv > LAYER_FDV_MAX_RATIO * cap:
        return None, f"FDV {fdv / cap:.1f} times the cap"
    if layer_type == "L2" and l2beat:
        entry = l2beat.get((cand.get("name") or "").lower())
        if entry and not entry.get("has_token"):
            return None, "L2 without own token"
    return layer_type, ""


def score_tokenomics(cand: dict) -> dict:
    """Pillar tokenomics: circulating/max (o total) e FDV/cap."""
    circ = cand.get("circulating") or 0
    total = cand.get("total") or 0
    max_s = cand.get("max") or 0
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
    cap = cand.get("market_cap") or 0
    fdv_ratio = (cand.get("fdv") or 0) / cap if cap else 0
    if fdv_ratio > 3:
        score -= 15
    facts = {"circulating": round(circ), "total": total, "max": max_s, "fdv_ratio": round(fdv_ratio, 2)}
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


async def _fetch_json(client: httpx.AsyncClient, url: str) -> object:
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


def _launched_at(started: str | None) -> float | None:
    if not started:
        return None
    try:
        return datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def run(client: httpx.AsyncClient, now: float | None = None) -> int:
    """Un giro: raccoglie le candidate, ne dettaglia fino a DETAILS_PER_RUN."""
    now = now or time.time()
    tags: list[dict] = []
    tickers: list[dict] = []
    tvl_by_name: dict[str, float] = {}
    l2beat: dict[str, dict] = {}
    extras: dict[str, dict] = {}
    try:
        tags = await _fetch_json(client, f"{PAPRIKA}/tags?additional_fields=coins") or []
        tickers = await _fetch_json(client, f"{PAPRIKA}/tickers?quotes=USD") or []
    except (httpx.HTTPError, ValueError):
        logger.exception("layers: coinpaprika lists failed")
    try:
        tvl_by_name = llama_tvl(await _fetch_json(client, LLAMA_CHAINS) or [])
    except (httpx.HTTPError, ValueError):
        logger.exception("layers: llama chains failed")
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

    cands = paprika_candidates(tickers, layer_types(tags))
    if not cands:
        # The lists failed: not a finished run, the loop tries again within
        # the hour. On 01/10 the first run ended here and was stamped done.
        logger.warning("layers: no candidates (%d tags, %d tickers), retrying later", len(tags), len(tickers))
        return 0
    for c in cands:
        c["tvl"] = tvl_by_name.get(c["name"].lower()) or 0
    found = _state.get("found") or {}
    for c in cands:     # rows studied before the alias cleanup existed
        if (found.get(c["id"]) or {}).get("type"):
            try:
                projects_db.drop_layer_alias(c["symbol"], c["id"])
            except Exception:
                logger.exception("layers: alias cleanup for %s failed", c["id"])
    todo = [c for c in cands if now - (found.get(c["id"], {}).get("at") or 0) >= DETAIL_TTL_S]
    # never seen first, then chains with DeFi usage, then by size
    todo.sort(key=lambda c: (c["id"] in found, -c["tvl"], -c["market_cap"]))
    todo = todo[:DETAILS_PER_RUN]

    done = 0
    for c in todo:
        cid = c["id"]
        try:
            coin = await _fetch_json(client, f"{PAPRIKA}/coins/{cid}") or {}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("layers: coinpaprika 429, stopping run")
                return done
            logger.exception("layers: coinpaprika detail %s failed", cid)
            continue
        except (httpx.HTTPError, ValueError):
            logger.exception("layers: coinpaprika detail %s failed", cid)
            continue
        layer_type, why = classify(c, coin, l2beat)
        if layer_type is None:
            found[cid] = {"at": now, "type": None, "score": 0, "status": "rejected", "why": why, "cex": None}
            _state["found"] = found
            _save()
            continue
        cap = c["market_cap"]
        symbol = c["symbol"]
        name = c["name"]
        links = coin.get("links") or {}
        repos = [u for u in links.get("source_code") or [] if "github.com" in (u or "")]
        launched_at = _launched_at(coin.get("started_at"))
        ex = extras.get(name.lower()) or {}
        tvl = c["tvl"]
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
            logger.exception("layers: github %s failed", cid)
            gh = depth.missing_github("github analysis failed")
        pillars.append(gh)
        item = {"chain": CHAIN, "token_address": cid, "symbol": symbol, "name": name,
                "image": coin.get("logo") or "", "description": coin.get("description") or "",
                "website": legit.pick_website(links.get("website") or []),
                "github": repos[0] if repos else "", "market_cap": cap, "launched_at": launched_at}
        try:
            site_pillar, _ = await research.analyze(item, client, launched_at)
        except Exception:
            logger.exception("layers: site %s failed", cid)
            site_pillar = depth._pending("site", "site analysis failed")
        pillars.append(site_pillar)
        pillars.append(score_tokenomics(c))
        pillars.append(score_traction(cap, tvl, tvl_30d, ex.get("stables_usd") or 0,
                                      ex.get("fees_30d") or 0, l2beat.get(name.lower())))
        fatal = next((p.get("fatal") for p in pillars if p.get("fatal")), "")
        score = final_score(pillars)
        cex = cex_symbol(symbol, mexc, binance)
        flags = []
        if cex is None:
            flags.append("no USDT pair on MEXC/Binance")
        cats = [layer_type] + [str(t.get("name")) for t in coin.get("tags") or [] if t.get("name")]
        status = "rejected" if fatal else "studied"
        try:    # one bad row must not end the run (01/10: a dict image did)
            projects_db.upsert({**item, "categories": cats}, "layers", now)
            projects_db.record({**item, "pillars": pillars,
                                "overall": {"score": score, "layer": layer_type, "cex": cex, "flags": flags}},
                               status, fatal, score=score,
                               traction={"tvl": tvl, "tvl_30d": tvl_30d,
                                         "stables_usd": ex.get("stables_usd") or 0,
                                         "fees_30d": ex.get("fees_30d") or 0,
                                         "l2beat": l2beat.get(name.lower()) or {}}, now=now)
            projects_db.drop_layer_alias(symbol, cid)
        except Exception:
            logger.exception("layers: database write for %s failed", cid)
            continue
        found[cid] = {"at": now, "type": layer_type, "score": score, "status": status,
                      "why": fatal, "cex": cex, "symbol": symbol, "name": name, "cap": cap}
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
                async with http_client.pooled(timeout=20) as client:
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
