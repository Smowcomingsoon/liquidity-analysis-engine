"""La memoria del radar: cosa e' successo davvero ai token che abbiamo visto.

Lorenzo, dopo il rug pull su VISE (-93%) proposto dal radar: "dobbiamo
implementare una specie di coscienza, imparare dai segnali, da cosa
effettivamente succede, e migliorare il sistema".

Tre passi, a ogni giro (`TICK_S`):

1. **Diario.** Ogni token che il radar o i listing hanno proposto, provato o
   scartato entra qui una volta, con i numeri e i controlli di QUEL momento
   (cap, liquidita', eta', voto di ogni controllo di legit.py) e il prezzo
   d'ingresso. Solo lettura sugli store degli altri moduli.
2. **Esito.** Per `HORIZON_S` si rilegge prezzo e liquidita' del pool su
   DexScreener (30 pool per richiesta). Rug = liquidita' scesa sotto il
   `RUG_LIQ_LEFT` di quella d'ingresso, prezzo a `RUG_DROP` o pool sparito.
   Buono = in qualche momento `GOOD_UP` sopra l'ingresso: c'era un trend da
   prendere.
3. **Lezione.** Per ogni controllo e ogni numero: separa i rug dagli altri?
   (AUC: 0.5 = moneta, 1 = perfetto, sotto 0.5 = inganna). Poi una
   regressione logistica su tutti insieme; entra nel filtro (`judge`) solo
   se prevede i rug FUORI CAMPIONE (validazione a k fold) meglio di
   `MODEL_MIN_AUC`, con almeno `MODEL_MIN_N` esiti e `MODEL_MIN_RUGS` rug.
   Prima di allora osserva e basta: una regola imparata da dieci token e'
   rumore, e qui abbiamo gia' misurato quanto il rumore sembri un segnale.

Un token visto tardi (store vecchi al primo avvio) prende il prezzo
d'ingresso dalle candele del momento della decisione, pochi per giro per
non rubare GeckoTerminal al radar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time

import httpx

from app import http_client
import numpy as np

from app.trading.onchain import meme_engine
from app.trading.onchain.storage import data_file
from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

_STORE_FILE = data_file("onchain_outcomes.json")
# Every rug, forever: the journal keeps KEEP tokens and drops the oldest, the
# archive keeps each rug once it is one (Lorenzo, 29/09: "dobbiamo
# documentare tutti i rug [...] anche quelli morti sopratutto").
_ARCHIVE_FILE = data_file("onchain_rug_archive.json")
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/pairs"

TICK_S = 10 * 60
FIRST_TICK_DELAY_S = 90
HORIZON_S = 7 * 86400          # quanto si segue un token
RUG_FINAL_S = 3 * 86400        # dopo 3 giorni senza rug, "non rug" e' un esito
RUG_LIQ_LEFT = 0.20            # liquidita' rimasta sotto il 20% d'ingresso
RUG_DROP = -0.80               # o prezzo a -80%
CRASH_DROP = -0.50
GOOD_UP = 0.50
# Una salita conta solo se si poteva vendere: qualcuno ha venduto e al
# massimo c'era liquidita' per uscire. JIRACHI "+6877%": 300 acquisti, zero
# vendite, liquidita' uguale alla cap. Quello e' un prezzo finto, non un trend.
EXIT_LIQ_MIN = 10_000
MISSING_TICKS = 3              # pool che non risponde piu' per 3 giri: tolto
LATE_S = 2 * 3600              # visto oltre 2 ore dopo la decisione: ingresso dalle candele
BACKFILL_PER_TICK = 4
KEEP = 5000

MODEL_MIN_N = 40
MODEL_MIN_RUGS = 8
MODEL_MIN_AUC = 0.65
MODEL_FOLDS = 5
BLOCK_RISK = 0.5               # sopra, il filtro scarta
SIGNAL_MIN_N = 12
WIN_MIN_N = 40
WIN_MIN_WINNERS = 8
WIN_MIN_AUC = 0.60
# Scartati per motivi "da meme" (nome, niente sito, social, GitHub, parere di
# Claude, pagella), non per sicurezza: la corsia meme parte da qui. Lorenzo:
# "dobbiamo cavalcare le meme coin [...] estremamente gambling".
SOFT_FAILS = {"name", "site", "socials", "github", "ai", "gem", "memory"}
MEME_LIQ_MIN = 8_000
MEME_MAX_AGE_S = 2 * 86400
MEME_SAFETY_PER_TICK = 20
MEME_SHOWN = 20                # the lane's length: what counts as "shown"
MEME_TOP_HOLDER_MAX = 15.0

# Quanto conta una decisione: un token proposto dopo essere stato solo
# provato prende l'ingresso del momento della proposta.
DECISIONS = {"rejected": 0, "tested": 1, "listed": 2, "proposed": 3}
PASSED = {"tested", "listed", "proposed"}
CHECK_KEYS = ("name", "security", "contract", "socials", "site", "github", "ai")
# Numeri dei controlli, non solo il voto. Dopo VISE: quanto tengono il primo
# wallet e i primi 10 (legit.holder_concentration).
FACT_KEYS = ("top_holder_pct", "top10_pct", "lp_locked_pct")
RISK_UP = {"top_holder_pct", "top10_pct", "ai_risk"}
# Measured like every check, but not in the model: DeepSeek's risk arrives
# minutes after the decision (ai_scan.py), so the model could never have it
# when it judges a new token.
SIGNAL_ONLY_KEYS = ("ai_risk",)   # qui un numero alto dovrebbe voler dire piu' rug
CHECK_LABELS = {"top_holder_pct": "Top wallet %", "top10_pct": "Top 10 wallets %",
                "vol_h1": "Volume 1h", "buys_h1": "Buys 1h", "sells_h1": "Sells 1h",
                "buy_ratio": "Buy share", "chg_h1": "Change 1h", "gem": "Gem report card",
                "memory": "Memory", "altro": "Other", "ai_risk": "DeepSeek rug risk",
                "lp_locked_pct": "Liquidity locked %",
                "name": "Name", "security": "Security (RugCheck/GoPlus)", "contract": "Contract (GoPlus)",
                "socials": "Socials", "site": "Website", "github": "GitHub", "ai": "Claude on the site",
                "report": "Overall report card", "mcap": "Market cap", "liq": "Liquidity",
                "liq_mcap": "Liquidity / cap", "age_h": "Age"}

_journal: dict[str, dict] = {}
_archive: dict[str, dict] = {}
_model: dict = {"active": False}
_wmodel: dict = {"active": False}
_state: dict = {"last_tick": 0.0, "error": ""}


# ------------------------------------------------------------------ archivio

def _load() -> None:
    if not _STORE_FILE.exists():
        return
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(raw, dict):
        _journal.update(raw.get("journal") or {})
        _state.update(raw.get("state") or {})
    try:
        arch = json.loads(_ARCHIVE_FILE.read_text(encoding="utf-8")) if _ARCHIVE_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        arch = {}
    if isinstance(arch, dict):
        _archive.update(arch)


def _prune(journal: dict, keep: int, now: float) -> None:
    """Tiene il giornale sotto `keep` senza decapitare una classe.

    Un token che fa rug e' etichettato in poche ore, uno che non lo fa solo
    dopo RUG_FINAL_S: buttando i piu' vecchi si cancellano i "non rug" prima
    che maturino e il modello resta senza esempi negativi.
    """
    if len(journal) <= keep:
        return

    def label(key: str) -> bool | None:
        return rug_label(journal[key], now)

    def drop(keys: list[str]) -> None:
        for key in keys:
            if len(journal) <= keep:
                return
            journal.pop(key, None)

    # 1. mai prezzati, o chiusi senza esito: non insegnano niente.
    drop(sorted((k for k in journal if label(k) is None and (not journal[k].get("entry") or journal[k].get("closed"))),
                key=lambda k: journal[k]["at"]))
    if len(journal) <= keep:
        return

    # 2. la classe piu' numerosa, dal piu' vecchio, una voce alla volta:
    # le due convergono invece di svuotarne una.
    rugs = sorted((k for k in journal if label(k) is True), key=lambda k: journal[k]["at"])
    non_rugs = sorted((k for k in journal if label(k) is False), key=lambda k: journal[k]["at"])
    i = j = 0
    while len(journal) > keep and (i < len(rugs) or j < len(non_rugs)):
        if j >= len(non_rugs) or (i < len(rugs) and len(rugs) - i >= len(non_rugs) - j):
            journal.pop(rugs[i], None)
            i += 1
        else:
            journal.pop(non_rugs[j], None)
            j += 1

    # 3. solo adesso gli indecisi, dal piu' vecchio.
    drop(sorted((k for k in journal if label(k) is None), key=lambda k: journal[k]["at"]))


def _archive_rugs() -> bool:
    """Copy the journal's rugs into the archive. True when it changed."""
    changed = False
    for key, e in _journal.items():
        if e.get("rug") and _archive.get(key) is not e:
            _archive[key] = e
            changed = True
    return changed


def entries() -> dict[str, dict]:
    """The journal plus the archived rugs it has already dropped."""
    return {**_archive, **_journal}


def _save() -> None:
    archived = False
    try:
        from app.trading.onchain import quant_memory
        quant_memory.record(entries().values())
        archived = True
    except Exception:
        logger.exception("quant evidence archive unavailable")
    if _archive_rugs():
        try:
            write_text_atomic(_ARCHIVE_FILE, json.dumps(_archive, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    if archived:
        _prune(_journal, KEEP, time.time())
    try:
        write_text_atomic(_STORE_FILE, json.dumps({"journal": _journal, "state": _state}, ensure_ascii=False),
                               encoding="utf-8")
    except OSError:
        pass


def reset_for_tests() -> None:
    _journal.clear()
    _archive.clear()
    _model.clear()
    _model["active"] = False
    _wmodel.clear()
    _wmodel["active"] = False
    _state.update(last_tick=0.0, error="")


_load()


# ------------------------------------------------------------------- diario

def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def features(item: dict, now: float | None = None) -> dict:
    """I numeri di un token al momento della decisione. Stessa funzione per
    il diario e per `judge`, cosi' il modello vede sempre le stesse cose."""
    now = now or time.time()
    cap, liq = _num(item.get("market_cap")), _num(item.get("liquidity_usd"))
    age = _num(item.get("age_hours"))
    if age is None and _num(item.get("created_at")):
        age = max(0.0, (now - float(item["created_at"])) / 3600)
    checks, facts = {}, {}
    failed = next((c.get("key") for c in item.get("checks") or [] if c.get("ok") is False), None)
    for c in item.get("checks") or []:
        if c.get("key") in CHECK_KEYS:
            checks[c["key"]] = {"ok": c.get("ok"), "score": c.get("score")}
        facts.update({k: v for k, v in (c.get("facts") or {}).items() if k in FACT_KEYS})
    report = (item.get("report") or {}).get("score")
    return {"mcap": cap, "liq": liq, "liq_mcap": (liq / cap) if cap and liq else None, "age_h": age,
            "report": _num(report), "checks": checks, **{k: _num(facts.get(k)) for k in FACT_KEYS},
            "chain": item.get("chain"), "failed": failed,
            "exception": bool(item.get("exception"))}


def observe(item: dict, decision: str, at: float | None = None, reason: str = "", source: str = "",
            now: float | None = None) -> dict | None:
    """Mette un token nel diario (una volta; una decisione piu' forte lo
    aggiorna). Ritorna la voce se e' nuova o cambiata."""
    now = now or time.time()
    token = item.get("token_address") or ""
    pair = item.get("pair_address") or ""
    if not token or not pair or not item.get("chain"):
        return None
    key = f"{item['chain']}:{token}"
    old = _journal.get(key)
    if old and DECISIONS[decision] <= DECISIONS[old["decision"]]:
        return None
    at = float(at or now)
    entry = {
        "chain": item["chain"], "symbol": item.get("symbol") or "", "name": item.get("name") or "",
        "token_address": token, "pair_address": pair, "pair_url": item.get("pair_url") or "",
        "decision": decision, "reason": reason, "source": source, "at": at, "seen_at": now,
        "features": features(item, at),
        "entry": None, "last": None, "min_ret": None, "max_ret": None, "min_liq": None,
        "misses": 0, "rug": None, "rug_at": None, "rug_why": "", "closed": False,
        "late": now - at > LATE_S, "previous": old["decision"] if old else None,
    }
    _journal[key] = entry
    return entry


def collect(now: float | None = None) -> int:
    """Legge le decisioni degli altri moduli. Import qui: legit importa
    questo modulo, e radar/listings importano legit."""
    from app.trading.onchain import listings, radar
    now = now or time.time()
    added = 0
    snap = radar.snapshot()
    for p in snap.get("proposals") or []:
        added += bool(observe(p, "proposed", p.get("found_at"), p.get("strategy") or "", "radar", now))
    for t in snap.get("tested") or []:
        added += bool(observe(t, "tested", None, "no strategy", "radar", now))
    for item in listings.recent():
        added += bool(observe(item, "listed", item.get("found_at"), "", "listing", now))
    for r in listings.rejected():
        added += bool(observe(r, "rejected", r.get("at"), r.get("reason") or "", r.get("source") or "", now))
    return added


# --------------------------------------------------------------------- esito

def market_of(pair: dict | None) -> dict | None:
    """Scambi e volumi di un pair DexScreener: chi compra, chi vende."""
    if not pair:
        return None
    tx = (pair.get("txns") or {}).get("h1") or {}
    vol = pair.get("volume") or {}
    buys, sells = _num(tx.get("buys")) or 0.0, _num(tx.get("sells")) or 0.0
    day = (pair.get("txns") or {}).get("h24") or {}
    change = pair.get("priceChange") or {}
    out = {"buys_h1": buys, "sells_h1": sells, "sells_h24": _num(day.get("sells")) or 0.0,
           "buy_ratio": buys / (buys + sells) if buys + sells else None,
           "vol_h1": _num(vol.get("h1")), "chg_h1": _num(change.get("h1")), "chg_h6": _num(change.get("h6")),
           "mcap": _num(pair.get("marketCap") or pair.get("fdv")),
           "image": (pair.get("info") or {}).get("imageUrl") or None}
    # Average ticket and turnover: what separates people from bots.
    out.update(meme_engine.derived(out, _num((pair.get("liquidity") or {}).get("usd"))))
    return out


def apply_sample(e: dict, price: float | None, liq: float | None, now: float,
                 market: dict | None = None) -> None:
    """Un nuovo prezzo per una voce: aggiorna minimi, massimi ed esito."""
    if e["closed"]:
        return
    if market:
        if not e.get("market0"):
            # Com'era il mercato quando l'abbiamo visto: diventa una feature.
            e["market0"] = market
            e["features"].update({k: market.get(k) for k in MARKET_KEYS})
        e["market"] = market
        if (market.get("sells_h1") or 0) + (market.get("sells_h24") or 0) > 0:
            e["sells_seen"] = True
    if price is None:
        if e["entry"]:
            e["misses"] += 1
            if e["misses"] >= MISSING_TICKS:
                _mark_rug(e, now, "pool gone")
                meme_engine.step_trade(e.get("trade"), None, None, now, can_sell=False, rug=True)
                meme_engine.step_trade((e.get("shown") or {}).get("trade"), None, None, now, can_sell=False,
                                       rug=True)
        return
    e["misses"] = 0
    if not e["entry"]:
        e["entry"] = {"price": price, "liq": liq, "at": now, "from": "live"}
        # The trade we could have made: in at this price, paying fee and impact.
        e["trade"] = meme_engine.open_trade(liq, now)
    base = e["entry"]["price"]
    ret = price / base - 1 if base else 0.0
    e["last"] = {"price": price, "liq": liq, "at": now, "ret": round(ret, 4)}
    e["min_ret"] = ret if e["min_ret"] is None else min(e["min_ret"], ret)
    if e["max_ret"] is None or ret >= e["max_ret"]:
        e["peak_liq"] = liq
    e["max_ret"] = ret if e["max_ret"] is None else max(e["max_ret"], ret)
    if e["entry"].get("liq") is None and liq:
        e["entry"]["liq"] = liq  # ingresso dalle candele: la liquidita' si misura da qui
    entry_liq = e["entry"].get("liq")
    if liq is not None and entry_liq:
        ratio = liq / entry_liq
        e["min_liq"] = ratio if e["min_liq"] is None else min(e["min_liq"], ratio)
        if ratio <= RUG_LIQ_LEFT:
            _mark_rug(e, now, f"liquidity down to {ratio:.0%}")
    if e["rug"] is None and ret <= RUG_DROP:
        _mark_rug(e, now, f"price {ret:+.0%}")
    if now - e["entry"]["at"] >= HORIZON_S:
        e["closed"] = True
        if e["rug"] is None:
            e["rug"] = False
    can_sell = bool(e.get("sells_seen")) and (liq is None or liq >= EXIT_LIQ_MIN)
    meme_engine.step_trade(e.get("trade"), ret, liq, now, can_sell=can_sell, rug=bool(e["rug"]),
                           final=e["closed"])
    shown = e.get("shown") or {}
    if shown.get("price"):
        meme_engine.step_trade(shown.get("trade"), price / shown["price"] - 1, liq, now, can_sell=can_sell,
                               rug=bool(e["rug"]), final=e["closed"])


def _mark_rug(e: dict, now: float, why: str) -> None:
    if e["rug"]:
        return
    e.update(rug=True, rug_at=now, rug_why=why, closed=True)


def outcome(e: dict, now: float | None = None) -> str | None:
    """rug / crollo / buono / piatto, oppure None finche' non si sa."""
    now = now or time.time()
    if not e.get("entry"):
        return None
    if e["rug"]:
        return "rug"
    if (e.get("max_ret") or 0) >= GOOD_UP:
        return "buono" if tradable(e) else "finto"
    if not e["closed"]:
        return None
    return "crollo" if (e.get("min_ret") or 0) <= CRASH_DROP else "piatto"


def tradable(e: dict) -> bool:
    """Si poteva vendere? Serve almeno una vendita vista e, se nota, la
    liquidita' al massimo sopra EXIT_LIQ_MIN."""
    if not e.get("sells_seen"):
        return False
    peak = e.get("peak_liq")
    return peak is None or peak >= EXIT_LIQ_MIN


def rug_label(e: dict, now: float | None = None) -> bool | None:
    """True/False quando l'esito "rug" e' deciso, None altrimenti."""
    now = now or time.time()
    if not e.get("entry"):
        return None
    if e["rug"]:
        return True
    if e["closed"] or now - e["entry"]["at"] >= RUG_FINAL_S:
        return False
    return None


async def _sample(client: httpx.AsyncClient, now: float) -> int:
    open_by_chain: dict[str, list[dict]] = {}
    for e in _journal.values():
        if not e["closed"] and not (e["late"] and not e["entry"]):
            open_by_chain.setdefault(e["chain"], []).append(e)
    sampled = 0
    for chain, entries in open_by_chain.items():
        for i in range(0, len(entries), 30):
            batch = entries[i:i + 30]
            try:
                r = await client.get(f"{DEXSCREENER_PAIRS}/{chain}/{','.join(e['pair_address'] for e in batch)}")
                pairs = (r.json() or {}).get("pairs") or [] if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError) as exc:
                logger.info("outcomes: DexScreener %s: %s", chain, exc)
                pairs = None
            if pairs is None:
                continue  # l'API non ha risposto: non e' un pool sparito
            by_pair = {(p.get("pairAddress") or "").lower(): p for p in pairs}
            for e in batch:
                p = by_pair.get(e["pair_address"].lower())
                price = _num(p.get("priceUsd")) if p else None
                liq = _num((p.get("liquidity") or {}).get("usd")) if p else None
                apply_sample(e, price, liq, now, market_of(p))
                sampled += 1
            await asyncio.sleep(0.3)
    return sampled


async def _backfill(now: float, limit: int = BACKFILL_PER_TICK) -> int:
    """Voci viste tardi: ingresso e minimi/massimi dalle candele."""
    from app.providers.base import ProviderError
    from app.providers.dexscreener import DexScreenerProvider, make_symbol
    todo = [e for e in _journal.values() if e["late"] and not e["entry"] and not e["closed"]]
    todo.sort(key=lambda e: -e["at"])
    provider = DexScreenerProvider()
    done = 0
    for e in todo[:limit]:
        if now - e["at"] > HORIZON_S:
            e["closed"] = True  # troppo vecchio per le candele a 15m
            continue
        try:
            df = await provider.fetch_ohlcv(make_symbol(e["symbol"] or "T", e["chain"], e["pair_address"]),
                                            "15m", 1000, token_address=e["token_address"])
        except ProviderError as exc:
            logger.info("outcomes: candele %s: %s", e["symbol"], exc)
            e["misses"] += 1
            if e["misses"] >= MISSING_TICKS:
                e["closed"] = True
            continue
        replay_candles(e, df)
        done += 1
    return done


def replay_candles(e: dict, df) -> None:
    """Prezzo d'ingresso alla prima candela dopo la decisione, poi minimo e
    massimo veri. La liquidita' storica non c'e': la si legge da ora in poi."""
    idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index
    after = df[idx.map(lambda t: t.timestamp()) >= e["at"]]
    if after.empty:
        e["closed"] = True
        return
    base = float(after["open"].iloc[0])
    if base <= 0:
        e["closed"] = True
        return
    e["entry"] = {"price": base, "liq": None, "at": e["at"], "from": "candles"}
    e["min_ret"] = float(after["low"].min()) / base - 1
    e["max_ret"] = float(after["high"].max()) / base - 1
    last = float(after["close"].iloc[-1])
    e["last"] = {"price": last, "liq": None, "at": after.index[-1].timestamp(), "ret": round(last / base - 1, 4)}
    if e["min_ret"] <= RUG_DROP:
        low_at = after["low"].idxmin()
        _mark_rug(e, low_at.timestamp(), f"price {e['min_ret']:+.0%}")


# ------------------------------------------------------------------- lezione

def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Probabilita' che un rug abbia un valore piu' alto di un non-rug
    (pareggi a meta'). Mann-Whitney, senza dipendenze."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    order = sorted((s, i) for i, s in enumerate(pos + neg))
    ranks = [0.0] * len(order)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and order[j + 1][0] == order[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k][1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = sum(ranks[:len(pos)])
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _check_value(f: dict, key: str) -> float | None:
    c = (f.get("checks") or {}).get(key)
    if not c:
        return None
    if c.get("score") is not None:
        return float(c["score"])
    return {True: 100.0, False: 0.0}.get(c.get("ok"))


def _value(f: dict, key: str) -> float | None:
    if key in CHECK_KEYS:
        return _check_value(f, key)
    v = f.get(key)
    if v is None:
        return None
    return math.log10(max(v, 0) + 1) if key in LOG_KEYS else float(v)


MODEL_KEYS = ("mcap", "liq", "liq_mcap", "age_h", "report") + FACT_KEYS + CHECK_KEYS
# Il mercato al primo sguardo: per le salite conta piu' della pagella.
MARKET_KEYS = ("vol_h1", "buys_h1", "sells_h1", "buy_ratio", "chg_h1", "chg_h6", "tx_h1", "avg_trade", "turnover")
WIN_KEYS = ("mcap", "liq", "liq_mcap", "age_h") + FACT_KEYS + MARKET_KEYS + CHECK_KEYS
LOG_KEYS = {"mcap", "liq", "age_h", "vol_h1", "buys_h1", "sells_h1", "tx_h1", "avg_trade", "turnover"}


Z_CLIP = 3.0


def _matrix(rows: list[dict], means: dict | None = None,
            keys: tuple = MODEL_KEYS) -> tuple[np.ndarray, dict]:
    """Una riga per token. Un valore mancante prende la media e accende un
    indicatore: "non controllato" e' un'informazione, non uno zero."""
    cols = {k: [_value(f, k) for f in rows] for k in keys}
    if means is None:
        means = {}
        for k, vals in cols.items():
            known = [v for v in vals if v is not None]
            means[k] = (sum(known) / len(known), (np.std(known) or 1.0)) if known else (0.0, 1.0)
    out = []
    for k in keys:
        mu, sd = means[k]
        # Un valore estremo (un +5000% in un'ora) non deve decidere da solo:
        # senza tetto portava ogni probabilita' a 0 o 100%, e il controllo
        # "Memoria" scartava come rug quasi tutto.
        out.append([max(-Z_CLIP, min(Z_CLIP, ((v if v is not None else mu) - mu) / sd)) for v in cols[k]])
        out.append([float(v is None) for v in cols[k]])
    out.append([float(f.get("chain") == "solana") for f in rows])
    return np.array(out, dtype=float).T, means


def _fit(x: np.ndarray, y: np.ndarray, l2: float = 1.0, steps: int = 400, lr: float = 0.3) -> np.ndarray:
    xb = np.hstack([x, np.ones((len(x), 1))])
    w = np.zeros(xb.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-xb @ w))
        grad = xb.T @ (p - y) / len(y) + l2 * np.r_[w[:-1], 0] / len(y)
        w -= lr * grad
    return w


def _predict(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-(np.hstack([x, np.ones((len(x), 1))]) @ w)))


def labelled(now: float | None = None) -> list[tuple[dict, bool]]:
    now = now or time.time()
    out = []
    for e in _journal.values():
        y = rug_label(e, now)
        if y is not None:
            out.append((e, y))
    return out


def _cv_fit(rows: list[dict], labels: list[bool], keys: tuple) -> tuple[float | None, np.ndarray, dict]:
    """AUC fuori campione (k fold deterministici: stesso diario, stesso
    verdetto), poi il modello su tutto."""
    n = len(rows)
    y = np.array([float(v) for v in labels])
    folds = np.arange(n) % MODEL_FOLDS
    pred = np.zeros(n)
    for k in range(MODEL_FOLDS):
        tr, te = folds != k, folds == k
        if y[tr].min() == y[tr].max():
            continue
        x_tr, means = _matrix([r for r, m in zip(rows, tr) if m], keys=keys)
        x_te, _ = _matrix([r for r, m in zip(rows, te) if m], means, keys)
        pred[te] = _predict(_fit(x_tr, y[tr]), x_te)
    cv = auc(pred.tolist(), [bool(v) for v in y])
    x, means = _matrix(rows, keys=keys)
    return cv, _fit(x, y), means


def train(now: float | None = None) -> dict:
    """Allena il modello dei rug e decide se merita di entrare nel filtro."""
    data = labelled(now)
    n, rugs = len(data), sum(y for _, y in data)
    model = {"active": False, "n": n, "rugs": rugs, "cv_auc": None, "why": ""}
    if n < MODEL_MIN_N or rugs < MODEL_MIN_RUGS or n - rugs < MODEL_MIN_RUGS:
        model["why"] = (f"needs {MODEL_MIN_N} outcomes with at least {MODEL_MIN_RUGS} rugs and {MODEL_MIN_RUGS} non-rugs "
                        f"(now {n}: {rugs} rugs, {n - rugs} non-rugs)")
        _model.clear()
        _model.update(model)
        return model
    cv, w, means = _cv_fit([e["features"] for e, _ in data], [y for _, y in data], MODEL_KEYS)
    model.update(cv_auc=round(cv, 3) if cv is not None else None, weights=w.tolist(), means=means)
    if cv is None or cv < MODEL_MIN_AUC:
        model["why"] = f"out of sample it separates rugs with AUC only {cv or 0:.2f} (needs {MODEL_MIN_AUC})"
    else:
        model.update(active=True, why=f"separates rugs out of sample with AUC {cv:.2f} over {n} outcomes")
    _model.clear()
    _model.update(model)
    return model


def risk(item: dict) -> float | None:
    """Probabilita' di rug stimata dagli esiti passati, None se il modello
    non e' ancora affidabile."""
    if not _model.get("active"):
        return None
    x, _ = _matrix([features(item)], {k: tuple(v) for k, v in _model["means"].items()})
    return float(_predict(np.array(_model["weights"]), x)[0])


def judge(token: dict, checks: list[dict], report: dict | None) -> dict | None:
    """Il controllo "Memoria" per legit.assess, o None se non c'e' ancora
    abbastanza passato per dire qualcosa."""
    p = risk({**token, "checks": checks, "report": report})
    if p is None:
        return None
    ok = p < BLOCK_RISK
    detail = (f"looks like past rugs: risk {p:.0%}" if not ok
              else f"rug risk {p:.0%} from the outcomes of {_model['n']} tokens")
    return {"key": "memory", "label": "Memory", "ok": ok, "detail": detail,
            "score": int(round(100 * (1 - p)))}


def _rate(entries: list[dict], now: float) -> dict:
    outs = [outcome(e, now) for e in entries]
    done = [o for o in outs if o]
    n = len(done)
    return {"n": len(entries), "decided": n,
            "rug": done.count("rug"), "crollo": done.count("crollo"),
            "buono": done.count("buono"), "piatto": done.count("piatto"), "finto": done.count("finto"),
            "rug_rate": round(done.count("rug") / n, 3) if n else None,
            "good_rate": round(done.count("buono") / n, 3) if n else None}


def signals(now: float | None = None) -> list[dict]:
    """Per ogni controllo e numero: quanto separa i rug e quanto le salite.
    Ogni numero nel verso in cui dovrebbe proteggere (voto alto = meno rug,
    concentrazione alta = piu' rug): 1 = li prevede, 0.5 = inutile, <0.5 =
    inganna."""
    now = now or time.time()
    data = labelled(now)
    good = [(e, outcome(e, now) == "buono") for e in _journal.values() if outcome(e, now)]
    out = []
    for key in MODEL_KEYS + SIGNAL_ONLY_KEYS:
        pairs = [(v, y) for e, y in data if (v := _value(e["features"], key)) is not None]
        gpairs = [(v, y) for e, y in good if (v := _value(e["features"], key)) is not None]
        if len(pairs) < SIGNAL_MIN_N:
            continue
        sign = 1 if key in RISK_UP else -1
        a = auc([sign * v for v, _ in pairs], [y for _, y in pairs])
        g = auc([-sign * v for v, _ in gpairs], [y for _, y in gpairs]) if len(gpairs) >= SIGNAL_MIN_N else None
        out.append({"key": key, "label": CHECK_LABELS.get(key, key), "n": len(pairs),
                    "rugs": sum(y for _, y in pairs),
                    "rug_auc": round(a, 3) if a is not None else None,
                    "good_auc": round(g, 3) if g is not None else None,
                    "verdict": _verdict(a)})
    out.sort(key=lambda s: -abs((s["rug_auc"] or 0.5) - 0.5))
    return out


def _verdict(a: float | None) -> str:
    if a is None:
        return "no rugs or no non-rugs"
    if a >= 0.7:
        return "predicts rugs"
    if a >= 0.6:
        return "helps a little"
    if a > 0.4:
        return "says nothing"
    return "misleading: high scores, more rugs"


def _brief(e: dict, now: float) -> dict:
    return {"chain": e["chain"], "symbol": e["symbol"], "pair_url": e["pair_url"], "decision": e["decision"],
            "reason": e["reason"], "at": e["at"], "outcome": outcome(e, now), "rug_why": e["rug_why"],
            "max_ret": e["max_ret"], "min_ret": e["min_ret"], "report": e["features"].get("report")}


def lessons(now: float | None = None) -> dict:
    """Tutto quello che il diario ha imparato, per l'API e la pagina."""
    now = now or time.time()
    entries = list(_journal.values())
    passed = [e for e in entries if e["decision"] in PASSED]
    rejected = [e for e in entries if e["decision"] == "rejected"]
    slipped = sorted((e for e in passed if e["rug"]), key=lambda e: -(e["rug_at"] or 0))
    missed = sorted((e for e in rejected if outcome(e, now) == "buono"), key=lambda e: -(e["max_ret"] or 0))
    sig = signals(now)
    notes = []
    for s in sig[:3]:
        if s["rug_auc"] is not None and s["verdict"] != "says nothing":
            notes.append(f"{s['label']}: {s['verdict']} (AUC {s['rug_auc']:.2f} over {s['n']})")
    p, r = _rate(passed, now), _rate(rejected, now)
    if p["rug_rate"] is not None and r["rug_rate"] is not None:
        notes.insert(0, f"Passed the filter: {p['rug_rate']:.0%} rugs; rejected: {r['rug_rate']:.0%} rugs")
    return {
        "tracked": len(entries), "open": sum(not e["closed"] for e in entries),
        "passed": p, "rejected": r,
        "by_decision": {d: _rate([e for e in entries if e["decision"] == d], now) for d in DECISIONS},
        "signals": sig, "model": {k: v for k, v in _model.items() if k not in ("weights", "means")},
        "slipped": [_brief(e, now) for e in slipped[:10]],
        "missed": [_brief(e, now) for e in missed[:10]],
        "study": study(now),
        "notes": notes, "last_tick": _state["last_tick"], "error": _state["error"],
        "rules": {"horizon_days": HORIZON_S / 86400, "rug_liq_left": RUG_LIQ_LEFT, "rug_drop": RUG_DROP,
                  "good_up": GOOD_UP, "model_min_n": MODEL_MIN_N, "model_min_auc": MODEL_MIN_AUC},
    }


# ------------------------------------------------ le salite che abbiamo perso

def win_labelled(now: float | None = None) -> list[tuple[dict, bool]]:
    """Did it pay? Once enough simulated trades have closed, the label is the
    trade netting WIN_RET after costs (meme_engine); before that, the old
    one: a sellable +50% at some point."""
    now = now or time.time()
    trades = [(e, meme_engine.won(e.get("trade"))) for e in _journal.values()]
    trades = [(e, y) for e, y in trades if y is not None]
    if len(trades) >= WIN_MIN_N:
        return trades
    out = []
    for e in _journal.values():
        o = outcome(e, now)
        if o:
            out.append((e, o == "buono"))
    return out


def train_winners(now: float | None = None) -> dict:
    """Il modello delle salite vere: su tutto il diario, non solo sugli
    scartati, perche' le salite sono poche."""
    data = win_labelled(now)
    n, wins = len(data), sum(y for _, y in data)
    model = {"active": False, "n": n, "winners": wins, "cv_auc": None, "why": ""}
    if n < WIN_MIN_N or wins < WIN_MIN_WINNERS or n - wins < WIN_MIN_WINNERS:
        model["why"] = (f"needs {WIN_MIN_N} outcomes with at least {WIN_MIN_WINNERS} real pumps and {WIN_MIN_WINNERS} "
                        f"without (now {n}: {wins} pumps, {n - wins} without)")
    else:
        cv, w, means = _cv_fit([e["features"] for e, _ in data], [y for _, y in data], WIN_KEYS)
        model.update(cv_auc=round(cv, 3) if cv is not None else None, weights=w.tolist(), means=means)
        if cv is None or cv < WIN_MIN_AUC:
            model["why"] = f"out of sample it spots pumps with AUC only {cv or 0:.2f} (needs {WIN_MIN_AUC})"
        else:
            model.update(active=True, why=f"spots pumps out of sample with AUC {cv:.2f} over {n} outcomes")
    _wmodel.clear()
    _wmodel.update(model)
    return model


def win_chance(f: dict) -> float | None:
    if not _wmodel.get("active"):
        return None
    x, _ = _matrix([f], {k: tuple(v) for k, v in _wmodel["means"].items()}, WIN_KEYS)
    return float(_predict(np.array(_wmodel["weights"]), x)[0])


def _median(vals: list) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    return vals[len(vals) // 2] if vals else None


def study(now: float | None = None) -> dict:
    """Lorenzo: "ho visto che hai scartato dei token che hanno fatto
    grandissime percentuali, devi andare a studiare tutti quei casi". Per
    ogni motivo di scarto: quanti sono esplosi davvero, quanti erano salite
    finte, quanti rug. Poi come erano, al primo sguardo, le salite vere
    rispetto alle altre."""
    now = now or time.time()
    rejected = [e for e in _journal.values() if e["decision"] == "rejected" and outcome(e, now)]
    by: dict[str, dict] = {}
    for e in rejected:
        key = failed_key(e)
        row = by.setdefault(key, {"key": key, "label": CHECK_LABELS.get(key, key), "n": 0, "buono": 0,
                                  "finto": 0, "rug": 0})
        row["n"] += 1
        o = outcome(e, now)
        if o in ("buono", "finto", "rug"):
            row[o] += 1
    for row in by.values():
        row["win_rate"] = round(row["buono"] / row["n"], 3)
        row["rug_rate"] = round(row["rug"] / row["n"], 3)
    wins = [e for e in rejected if outcome(e, now) == "buono"]
    rest = [e for e in rejected if outcome(e, now) != "buono"]
    traits = []
    for k in ("mcap", "liq", "liq_mcap", "age_h", "vol_h1", "buys_h1", "sells_h1", "buy_ratio", "chg_h1",
              "top_holder_pct"):
        w, r = _median([e["features"].get(k) for e in wins]), _median([e["features"].get(k) for e in rest])
        if w is not None and r is not None:
            traits.append({"key": k, "label": CHECK_LABELS.get(k, k), "winners": w, "others": r})
    fakes = sorted((e for e in rejected if outcome(e, now) == "finto"), key=lambda e: -(e["max_ret"] or 0))
    return {"by_reason": sorted(by.values(), key=lambda r: -r["buono"]), "traits": traits,
            "winners": [_brief(e, now) for e in sorted(wins, key=lambda e: -(e["max_ret"] or 0))[:12]],
            "fakes": [_brief(e, now) for e in fakes[:8]],
            "model": {k: v for k, v in _wmodel.items() if k not in ("weights", "means")}}


_REASON_KEYS = (("Memoria", "memory"), ("Memory", "memory"), ("meme", "name"), ("RugCheck", "security"),
                ("GoPlus", "contract"), ("Sicurezza", "security"), ("Security", "security"), ("GitHub", "github"),
                ("Claude", "ai"), ("sito", "site"), ("website", "site"), ("social", "socials"),
                ("pagella", "gem"), ("eccellente", "gem"), ("report card", "gem"))


def failed_key(e: dict) -> str:
    """Quale controllo l'ha scartato. Le voci vecchie non lo hanno salvato:
    lo si ricava dal motivo scritto."""
    key = e["features"].get("failed")
    if key:
        return key
    reason = e.get("reason") or ""
    return next((k for word, k in _REASON_KEYS if word in reason), "altro")


def _meme_candidates(now: float) -> list[dict]:
    out = []
    for e in _journal.values():
        if e["decision"] != "rejected" or e["closed"] or e["rug"] or not e.get("last"):
            continue
        if failed_key(e) not in SOFT_FAILS or now - e["at"] > MEME_MAX_AGE_S:
            continue
        if (e["last"].get("liq") or 0) < MEME_LIQ_MIN or not e.get("sells_seen"):
            continue  # senza vendite non si sa se si puo' uscire
        if (e.get("safety") or {}).get("ok") is False:
            continue
        out.append(e)
    return out


def meme_read(e: dict, now: float | None = None) -> dict:
    """The engine's reading of a meme right now (meme_engine.score)."""
    now = now or time.time()
    m = e.get("market") or {}
    created = (e.get("features") or {}).get("age_h")
    age = None if created is None else created + (now - e["at"]) / 3600
    return meme_engine.score(m, (e.get("last") or {}).get("liq"), m.get("mcap"), e.get("safety"), age)


def _meme_score(e: dict) -> float:
    """The order of the lane: the memory's odds once it predicts out of
    sample, the engine's score (0-1) until then."""
    p = win_chance(e["features"])
    if p is not None:
        return p
    return round(meme_read(e)["score"] / 100, 4)


def meme_board(now: float | None = None, limit: int = 20) -> dict:
    """La corsia meme: scartati per i motivi da meme, non per sicurezza,
    ancora vivi, vendibili, ordinati per somiglianza alle salite vere."""
    now = now or time.time()
    # Un ticker una volta sola: le copie con lo stesso nome restano fuori.
    best: dict[str, dict] = {}
    for e in sorted(_meme_candidates(now), key=lambda e: -(e["last"].get("liq") or 0)):
        best.setdefault(f"{e['chain']}:{e['symbol'].upper()}", e)
    cands = sorted(best.values(), key=lambda e: -_meme_score(e))[:limit]
    from app.trading.onchain import narratives
    items = []
    for e in cands:
        m = e.get("market") or {}
        read = meme_read(e, now)
        items.append({
            "chain": e["chain"], "symbol": e["symbol"], "name": e["name"], "token_address": e["token_address"],
            "pair_address": e["pair_address"], "pair_url": e["pair_url"], "seen_at": e["at"],
            "reason": e["reason"], "failed": failed_key(e), "image": m.get("image"),
            "ret_since_seen": e["last"].get("ret"), "max_ret": e["max_ret"], "liquidity_usd": e["last"].get("liq"),
            "market_cap": m.get("mcap"), "buys_h1": m.get("buys_h1"), "sells_h1": m.get("sells_h1"),
            "vol_h1": m.get("vol_h1"), "score": read["score"], "win_chance": win_chance(e["features"]),
            "reasons": read["reasons"], "risks": read["risks"], "parts": read["parts"],
            "band": meme_engine.band(read["score"]), "safety": e.get("safety"),
            "narratives": narratives.labels(narratives.match(e["name"], e["symbol"], kind="meme")),
        })
    return {"items": items, "model": {k: v for k, v in _wmodel.items() if k not in ("weights", "means")},
            "warning": "Pure gambling: meme coins with no project behind them. Only put in what you can afford to lose.",
            "track": meme_track(now),
            "rules": {"liq_min_usd": MEME_LIQ_MIN, "max_age_h": MEME_MAX_AGE_S / 3600,
                      "top_holder_max_pct": MEME_TOP_HOLDER_MAX, "exit_liq_min_usd": EXIT_LIQ_MIN,
                      "parts": meme_engine.PARTS}}


def mark_shown(now: float | None = None, limit: int = MEME_SHOWN) -> int:
    """Remember what the lane showed, with which score, and open a trade at
    that moment's price: the track record judges the lane on what it put in
    front of people, from when it did."""
    now = now or time.time()
    marked = 0
    for item in meme_board(now, limit)["items"]:
        e = _journal.get(f"{item['chain']}:{item['token_address']}")
        last = (e or {}).get("last") or {}
        if e is not None and not e.get("shown") and last.get("price"):
            e["shown"] = {"at": now, "score": item["score"], "band": item["band"], "price": last["price"],
                          "trade": meme_engine.open_trade(last.get("liq"), now)}
            marked += 1
    return marked


def meme_track(now: float | None = None) -> dict:
    """How the lane's picks really went, as closed simulated trades, next to
    every meme we watched (the baseline a ranking has to beat)."""
    shown = [e["shown"] for e in _journal.values()
             if meme_engine.won((e.get("shown") or {}).get("trade")) is not None]
    base = [e for e in _journal.values() if e["decision"] == "rejected" and failed_key(e) in SOFT_FAILS
            and meme_engine.won(e.get("trade")) is not None]
    bands = []
    for _floor, label in meme_engine.BANDS:
        rets = [s["trade"]["ret"] for s in shown if s.get("band") == label]
        bands.append({"band": label, **meme_engine.summarize(rets)})
    exits: dict[str, int] = {}
    for s in shown:
        exits[s["trade"]["exit"]] = exits.get(s["trade"]["exit"], 0) + 1
    return {"shown": meme_engine.summarize([s["trade"]["ret"] for s in shown]),
            "baseline": meme_engine.summarize([e["trade"]["ret"] for e in base]),
            "bands": bands, "exits": exits, "rules": meme_engine.rules()}


async def _meme_safety(client: httpx.AsyncClient, now: float, limit: int = MEME_SAFETY_PER_TICK) -> int:
    """Il minimo anti-rug per un meme scartato per il nome (il filtro si
    ferma li' e non guarda la sicurezza): RugCheck o GoPlus, e nessun wallet
    sopra MEME_TOP_HOLDER_MAX."""
    from app.trading.onchain import legit
    todo = [e for e in sorted(_meme_candidates(now), key=lambda e: -_meme_score(e)) if not e.get("safety")]
    done = 0
    for e in todo[:limit]:
        extra: dict = {}
        try:
            if e["chain"] == "solana":
                report = await legit.rugcheck(e["token_address"], client)
                check = legit.judge_rugcheck(report)
                if report:
                    top, _top10 = legit.holder_concentration(report)
                    extra = {"top_holder_pct": round(top, 2) if top is not None else None,
                             "warnings": [r.get("name") for r in report.get("risks") or [] if r.get("name")][:8]}
                if report and check["ok"] is not False and top is not None and top > MEME_TOP_HOLDER_MAX:
                    check = {"ok": False, "detail": f"one wallet holds {top:.0f}%"}
            elif e["chain"] in legit.GOPLUS_CHAIN:
                check = legit.judge_goplus(await legit.goplus(e["chain"], e["token_address"], client))
            else:
                continue
        except Exception:
            logger.exception("outcomes: sicurezza meme %s", e["symbol"])
            continue
        if check.get("ok") is None:
            continue  # report non ancora pronto: si riprova al giro dopo
        e["safety"] = {"ok": bool(check["ok"]), "detail": check.get("detail") or "", "at": now, **extra}
        done += 1
    return done


# -------------------------------------------------------- il muro della vergogna

# Lorenzo: "fammi sparire VISE che e' un RUGPULL, crea sezione ironica dove ci
# andranno a finire tutte le inculate subite". Un rug che il filtro aveva
# lasciato passare sparisce da proposte e listing (e dai candidati del
# pilota) e finisce qui. I rug che il filtro aveva scartato no: quelli li
# abbiamo evitati.
SHAME_SEED = [
    {"chain": "solana", "symbol": "VISE", "name": "Vise",
     "token_address": "4vXJMBFT6rcxNHohy2e4UPXGSv8eYpWRZmEQTqi6uRdg",
     "pair_address": "5GUGd9GgUefptTKwe6aAythc6LJNCyBMSxwzFTysqZuc",
     "pair_url": "https://dexscreener.com/solana/5gugd9gguefpttkwe6aaythc6ljncybmsxwzftysqzuc",
     "rug_at": 1790247060, "rug_why": "-95% in 8 minutes, coordinated selling",
     "fooled_by": "a pump.fun GitHub passed off as its own"},
]


def shame() -> list[dict]:
    """Le fregature subite, la piu' recente prima."""
    out = {f"{e['chain']}:{e['token_address']}": dict(e) for e in SHAME_SEED}
    for key, e in _journal.items():
        if e.get("rug") and e["decision"] in PASSED and key not in out:
            out[key] = {k: e.get(k) for k in ("chain", "symbol", "name", "token_address", "pair_address",
                                               "pair_url", "rug_at", "rug_why")}
            out[key].update(decision=e["decision"], min_ret=e.get("min_ret"))
    return sorted(out.values(), key=lambda e: -(e.get("rug_at") or 0))


def shamed() -> set[str]:
    """chain:token dei rug subiti, per toglierli dalle altre viste."""
    return {f"{e['chain']}:{e['token_address']}" for e in shame()}


def without_shamed(items: list[dict], gone: set[str] | None = None) -> list[dict]:
    gone = shamed() if gone is None else gone
    return [i for i in items if f"{i.get('chain')}:{i.get('token_address')}" not in gone]


# --------------------------------------------------------------------- giro

async def tick(now: float | None = None) -> dict:
    now = now or time.time()
    try:
        collect(now)
        async with http_client.pooled(timeout=20.0) as client:
            await _sample(client, now)
            await _meme_safety(client, now)
        await _backfill(now)
        train(now)
        train_winners(now)
        mark_shown(now)
        _state["error"] = ""
    except Exception as exc:
        logger.exception("outcomes: giro fallito")
        _state["error"] = str(exc)[:200]
    _state["last_tick"] = now
    _save()
    return _state


async def outcomes_loop() -> None:
    await asyncio.sleep(FIRST_TICK_DELAY_S)
    train()
    train_winners()
    while True:
        await tick()
        await asyncio.sleep(TICK_S)

