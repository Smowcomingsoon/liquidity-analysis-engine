"""
Il radar: token giovani on-chain che una strategia dell'archivio avrebbe
tradato bene.

Lorenzo: "voglio che nell'applicazione venga informato di quale token appena
nato, o che ha due o tre settimane di vita con tanta liquidita' e bassa
capitalizzazione [...] quale delle strategie performa bene [...] deve esserci
un minimo di backtest".

Un giro ogni `SCAN_INTERVAL_S`, per Solana e Robinhood Chain:

1. pool da GeckoTerminal (piu' scambiati, di tendenza, nuovi);
2. filtro: eta' <= 21 giorni, liquidita' alta in assoluto e rispetto alla
   capitalizzazione, capitalizzazione bassa. Un solo pool per ticker, quello
   con piu' liquidita' (le copie con lo stesso nome ne hanno meno);
3. candele del pool (5m sotto i 4 giorni di vita, 15m sopra) e backtest di
   ogni strategia del registro con i parametri predefiniti, piu' ogni
   configurazione "pronta" dell'archivio;
3b. prima del backtest, il filtro di legittimita' (legit.py): sito proprio,
   social, GitHub vero, niente rug o honeypot, nome non da meme, e con la
   chiave il parere di Claude sul sito. Lorenzo: "voglio token legittimi con
   progetti seri", cap fino a $2M ma sopra i $100k solo con una pagella eccellente;
4. si propone il token solo se la migliore supera soglie minime (trade,
   rendimento, profit factor, drawdown) su abbastanza candele.

Solo lettura: propone, non compra e non aggiunge niente alla watchlist.

Due scelte da dichiarare:
- il backtest e' SOLO LONG (on-chain spot non si va short) e con costi da DEX
  (0,3% di commissione e 0,5% di slippage per lato, riempimento all'apertura
  successiva): un rendimento da CEX su un token giovane sarebbe una promessa
  che il wallet non puo' mantenere;
- la migliore di N configurazioni e' ottimistica per costruzione. Il numero di
  configurazioni provate viaggia con la proposta, e il rendimento del semplice
  tenere il token nello stesso periodo le sta accanto.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import httpx
import pandas as pd

from app.analysis.backtest import run_backtest
from app.analysis.strategies import STRATEGY_REGISTRY, confirmed_events, spot_only
from app.providers.base import NoDataYet, ProviderError
from app.providers.dexscreener import DexScreenerProvider, gecko_urgent, make_symbol
from app.sniper.quality import _looks_like_meme
from app.trading.onchain import legit
from app.trading.onchain import listings as onchain_listings
from app.trading.onchain.storage import data_file

logger = logging.getLogger(__name__)

_STORE_FILE = data_file("onchain_radar.json")

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
CHAINS = {"solana": "solana", "robinhood": "robinhood"}

# Soglie iniziali: regolabili qui, non da un parametro di richiesta.
AGE_MAX_DAYS = 21
LIQ_MIN_USD = 8_000
MCAP_MAX_USD = 2_000_000       # sopra legit.GEM_CAP_USD solo con una pagella eccellente
LIQ_TO_MCAP_MIN = 0.10          # liquidita' almeno il 10% della capitalizzazione
MAX_VETTED_PER_CHAIN = 25       # quanti passano dal filtro di legittimita' a giro
# Le eccezioni (Lorenzo su DREGG: "va bene anche se ha 21 gg e supera i 2 mln,
# progetti in quel modo voglio che siano eccezione in quanto tecnicamente
# incredibili su github"): piu' vecchi o piu' grandi, ma con liquidita' vera,
# e poi l'asticella di legit.github_shortfall.
EXC_AGE_MAX_DAYS = 365
EXC_MCAP_MAX_USD = 20_000_000
EXC_LIQ_MIN_USD = 100_000
EXC_LIQ_TO_MCAP_MIN = 0.03
MAX_EXCEPTIONS_PER_CHAIN = 12
PUMPFUN_COINS = "https://frontend-api-v3.pump.fun/coins"
PUMPFUN_OFFSETS = (50, 100, 150, 200, 250, 300)   # per cap: da ~$20M a ~$1M
DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1"
# Di cosa si parla: profili aggiornati e token spinti su DexScreener, ognuno
# col suo X e sito. X da solo non ha un'API pubblica gratuita.
DEXSCREENER_ATTENTION = ("https://api.dexscreener.com/token-profiles/latest/v1",
                         "https://api.dexscreener.com/token-boosts/latest/v1",
                         "https://api.dexscreener.com/token-boosts/top/v1")
MAX_TESTED_PER_CHAIN = 8        # le candele costano una richiesta a token
VERDICT_TTL_S = 6 * 3600        # un verdetto "ok"/"fail" vale qualche ora; "wait" si ripete
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/pairs"
MIN_BARS = 300                  # sotto, un backtest non dice niente
MIN_TRADES = 3
MIN_PROFIT_FACTOR = 1.3
MAX_DRAWDOWN_PCT = 40.0
MAX_ARCHIVE_CONFIGS = 8
SCAN_INTERVAL_S = 15 * 60
FIRST_SCAN_DELAY_S = 45
_REQUEST_GAP_S = 2.1            # GeckoTerminal gratuito: ~30 richieste/minuto

BACKTEST_PARAMS = {
    "initial_capital": 1000.0,
    "qty_pct_of_equity": 100.0,
    "commission_pct": 0.3,
    "slippage_pct": 0.5,
    "fill_at": "next_open",
}

# Il lato "base" di questi pool non e' un token giovane: e' la moneta di
# scambio (SOL/USDC, WETH/USDG...).
_NOT_A_TOKEN = {
    "SOL", "WSOL", "USDC", "USDT", "USDG", "WETH", "ETH", "DAI", "USDS", "PYUSD",
    "USD1", "USDE", "BTC", "WBTC", "CBBTC", "JITOSOL", "MSOL", "BNSOL", "JUPSOL",
}


# Provata e mostrata, ma mai proposta: i suoi ingressi usano uno swing che si
# riconosce solo `swing_length` barre dopo (seed-0010), quindi il suo backtest
# gonfia il risultato. Via da qui quando quel fix entra.
UNRELIABLE = {
    "Accumulo / Distribuzione (long-term)": "uses future data in its entries",
}


@dataclass
class Pool:
    chain: str
    symbol: str
    name: str
    token_address: str
    pair_address: str
    dex: str
    liquidity_usd: float
    market_cap: float | None
    created_ms: int
    exception: bool = False    # oltre eta' o cap: entra solo con un GitHub eccezionale
    site: str | None = None    # sito dichiarato su DexScreener, quando lo si sa gia'

    @property
    def age_hours(self) -> float:
        return (time.time() * 1000 - self.created_ms) / 3_600_000

    @property
    def pair_url(self) -> str:
        return f"https://dexscreener.com/{self.chain}/{self.pair_address}"


def parse_pool(chain: str, pool: dict) -> Pool | None:
    """Un pool GeckoTerminal. Gli indirizzi restano come sono: in minuscolo
    un indirizzo Solana (base58) non e' piu' lo stesso indirizzo."""
    try:
        attrs = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        created = attrs.get("pool_created_at")
        pair = attrs.get("address") or ""
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        token = base_id.split("_", 1)[-1]
        if not created or not pair or not token:
            return None
        created_ms = int(datetime.fromisoformat(created.replace("Z", "+00:00"))
                         .astimezone(timezone.utc).timestamp() * 1000)
        name = attrs.get("name") or "?"
        mc = attrs.get("market_cap_usd") or attrs.get("fdv_usd")
        return Pool(
            chain=chain,
            symbol=name.split(" / ")[0].strip(),
            name=name,
            token_address=token,
            pair_address=pair,
            dex=((rel.get("dex") or {}).get("data") or {}).get("id", ""),
            liquidity_usd=float(attrs.get("reserve_in_usd") or 0),
            market_cap=float(mc) if mc else None,
            created_ms=created_ms,
        )
    except (TypeError, ValueError):
        return None


def pick_candidates(pools: list[Pool], limit: int = MAX_TESTED_PER_CHAIN) -> tuple[list[Pool], int]:
    """(da testare, quanti passano il filtro). Un pool per ticker: quello con
    piu' liquidita'. Ordine: liquidita' rispetto alla capitalizzazione."""
    best_by_ticker: dict[str, Pool] = {}
    for p in pools:
        ticker = p.symbol.upper()
        if not ticker or ticker in _NOT_A_TOKEN:
            continue
        if p.age_hours > AGE_MAX_DAYS * 24 or p.liquidity_usd < LIQ_MIN_USD:
            continue
        if not p.market_cap or p.market_cap > MCAP_MAX_USD:
            continue
        if p.liquidity_usd / p.market_cap < LIQ_TO_MCAP_MIN:
            continue
        held = best_by_ticker.get(ticker)
        if held is None or p.liquidity_usd > held.liquidity_usd:
            best_by_ticker[ticker] = p
    ranked = sorted(best_by_ticker.values(), key=lambda p: p.liquidity_usd / p.market_cap, reverse=True)
    return ranked[:limit], len(ranked)


def pick_exceptions(pools: list[Pool], taken: list[Pool], limit: int | None = None) -> list[Pool]:
    """I pool fuori da eta' o cap normali ma dentro i limiti delle eccezioni.
    Solo chi dichiara un sito vero e non ha un nome da meme: i posti sono
    pochi e una memecoin liquida non deve rubarli a un progetto. Mai un
    ticker gia' scelto per la via normale."""
    seen = {p.symbol.upper() for p in taken}
    best: dict[str, Pool] = {}
    for p in pools:
        ticker = p.symbol.upper()
        if not ticker or ticker in _NOT_A_TOKEN or ticker in seen or not p.market_cap or not p.site:
            continue
        if _looks_like_meme(p.symbol, p.name):
            continue
        normal = p.age_hours <= AGE_MAX_DAYS * 24 and p.market_cap <= MCAP_MAX_USD
        if normal or p.age_hours > EXC_AGE_MAX_DAYS * 24 or p.market_cap > EXC_MCAP_MAX_USD:
            continue
        if p.liquidity_usd < EXC_LIQ_MIN_USD or p.liquidity_usd / p.market_cap < EXC_LIQ_TO_MCAP_MIN:
            continue
        if ticker not in best or p.liquidity_usd > best[ticker].liquidity_usd:
            best[ticker] = p
    ranked = sorted(best.values(), key=lambda p: p.liquidity_usd, reverse=True)[:limit]
    return [replace(p, exception=True) for p in ranked]


def exception_queue(cands: list[Pool], now: float | None = None, limit: int = MAX_EXCEPTIONS_PER_CHAIN) -> list[Pool]:
    """Quali eccezioni controllare in questo giro. Chi e' gia' passato
    resta in testa anche a verdetto scaduto; poi chi non e' mai stato
    visto; gli scartati tornano solo quando il loro verdetto scade. Cosi'
    in pochi giri passano tutte, senza sforare i limiti delle API."""
    now = now or time.time()

    def rank(p: Pool) -> int | None:
        cached = _verdicts.get(f"{p.chain}:{p.token_address}")
        if cached is None:
            return 1
        if cached[1].get("status") == "ok":
            return 0
        return None if now - cached[0] < VERDICT_TTL_S else 2

    queued = [(r, i, p) for i, p in enumerate(cands) if (r := rank(p)) is not None]
    return [p for _, _, p in sorted(queued)[:limit]]


def pool_from_dexscreener(pair: dict) -> Pool | None:
    base = pair.get("baseToken") or {}
    sites = [w.get("url") for w in (pair.get("info") or {}).get("websites") or [] if isinstance(w, dict)]
    try:
        return Pool(chain=pair.get("chainId") or "", symbol=(base.get("symbol") or "").strip(),
                    name=base.get("name") or "", token_address=base.get("address") or "",
                    pair_address=pair.get("pairAddress") or "", dex=pair.get("dexId") or "",
                    liquidity_usd=float((pair.get("liquidity") or {}).get("usd") or 0),
                    market_cap=float(pair.get("marketCap") or pair.get("fdv") or 0) or None,
                    created_ms=int(pair.get("pairCreatedAt") or 0), site=legit.pick_website(sites))
    except (TypeError, ValueError):
        return None


async def _pools_for_tokens(client: httpx.AsyncClient, chain: str, tokens: list[str]) -> list[Pool]:
    pools: list[Pool] = []
    tokens = list(dict.fromkeys(tokens))
    for i in range(0, len(tokens), 30):
        try:
            r = await client.get(f"{DEXSCREENER_TOKENS}/{chain}/{','.join(tokens[i:i + 30])}")
            pairs = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            continue
        pools += [p for p in (pool_from_dexscreener(x) for x in pairs if isinstance(x, dict))
                  if p and p.chain == chain]
    return pools


async def _fetch_attention_pools(client: httpx.AsyncClient, chain: str) -> list[Pool]:
    tokens: list[str] = []
    for url in DEXSCREENER_ATTENTION:
        try:
            r = await client.get(url)
            rows = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            continue
        tokens += [x["tokenAddress"] for x in rows if isinstance(rows, list)
                   if isinstance(x, dict) and x.get("chainId") == chain and x.get("tokenAddress")]
    return await _pools_for_tokens(client, chain, tokens)


async def _fetch_extra_pools(client: httpx.AsyncClient, chain: str) -> list[Pool]:
    """Oltre alle liste di GeckoTerminal: i token di cui si parla e, su
    Solana, i progetti gia' cresciuti."""
    grown = await _fetch_grown_pools(client) if chain == "solana" else []
    return grown + await _fetch_attention_pools(client, chain)


async def _fetch_grown_pools(client: httpx.AsyncClient) -> list[Pool]:
    """Progetti Solana gia' cresciuti, che le liste per volume o novita' di
    GeckoTerminal non vedono: pump.fun per capitalizzazione, poi il pool
    piu' liquido di ognuno da DexScreener."""
    mints: list[str] = []
    for offset in PUMPFUN_OFFSETS:
        try:
            r = await client.get(PUMPFUN_COINS, params={"offset": offset, "limit": 50, "sort": "market_cap",
                                                        "order": "DESC", "includeNsfw": "false"})
            coins = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            continue
        for c in coins if isinstance(coins, list) else []:
            cap = c.get("usd_market_cap") or 0
            if c.get("mint") and MCAP_MAX_USD / 4 <= cap <= EXC_MCAP_MAX_USD:
                mints.append(c["mint"])
    return await _pools_for_tokens(client, "solana", mints)


def watch_symbol(symbol: str, chain: str, pool: str) -> str:
    return make_symbol(symbol, chain, pool)


INTERVAL_S = {"4h": 14_400, "1h": 3_600, "15m": 900, "5m": 300, "1m": 60}
CHART_BARS = 150


def auto_interval(age_s: float, min_bars: int = CHART_BARS) -> str:
    """Il timeframe piu' largo che da' ancora `min_bars` candele di storia.
    Lorenzo: "auto aggiusta le candele col time frame corretto per la storia
    del token". Un token di 30 minuti resta a 1m (non ce n'e' di meglio)."""
    for interval, seconds in INTERVAL_S.items():
        if age_s / seconds >= min_bars:
            return interval
    return "1m"


def interval_for(age_hours: float) -> str:
    # Il backtest delle proposte vuole MIN_BARS candele.
    return auto_interval(age_hours * 3600, MIN_BARS)


def _short(strategy_name: str) -> str:
    return strategy_name.split(" (")[0]


def strategy_configs(entries: list[dict] | None = None) -> list[dict]:
    """Ogni strategia del registro con i suoi predefiniti. Le configurazioni
    dell'archivio entrano solo se passate esplicitamente: sono tarate su altri
    ticker, e Lorenzo non le vuole vedere ("mi basta vedere le strategie
    Lorenzo, Accumulo, Gabriele [...] non voglio vedere strategie applicate
    ad altri ticker")."""
    configs = [{"strategy_name": name, "params": None, "label": _short(name), "source": "predefinita"}
               for name in STRATEGY_REGISTRY]
    seen: set[str] = set()
    for e in entries or []:
        name = e.get("strategy_name")
        if name not in STRATEGY_REGISTRY or not e.get("params"):
            continue
        key = name + json.dumps(e["params"], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        configs.append({
            "strategy_name": name,
            "params": dict(e["params"]),
            "label": f"{_short(name)} · archive {e.get('symbol', '')} {e.get('interval', '')}".strip(),
            "source": "archivio",
        })
        if len(configs) >= len(STRATEGY_REGISTRY) + MAX_ARCHIVE_CONFIGS:
            break
    return configs


def spot_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Solo spot: si compra e si vende, mai short (Lorenzo: "non voglio
    short, solo acquisti spot e vendite"). Un segnale short della strategia
    diventa una vendita di quello che si ha, se lo si ha."""
    return spot_only(STRATEGY_REGISTRY[cfg["strategy_name"]](df, cfg["params"]))


def run_spot(df: pd.DataFrame, cfg: dict):
    """Backtest ed eventi dagli stessi segnali spot: il grafico e i numeri
    raccontano gli stessi acquisti e le stesse vendite."""
    signals = spot_signals(df, cfg)
    result = run_backtest(df, cfg["params"], BACKTEST_PARAMS, signals=signals, strategy_name=cfg["strategy_name"])
    events, position = confirmed_events(df, cfg["params"], signals=signals, strategy_name=cfg["strategy_name"])
    return result, events, position


def _long_only(trades, commission_pct: float) -> dict:
    """Rendimento composto dei soli long, con i costi del DEX."""
    equity = peak = 1.0
    max_dd = gross_win = gross_loss = 0.0
    wins = n = 0
    for t in trades:
        if t.direction != "long":
            continue
        n += 1
        r = t.pnl_pct / 100 - 2 * commission_pct / 100
        equity *= 1 + r
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        if r > 0:
            wins += 1
            gross_win += r
        else:
            gross_loss -= r
    return {
        "return_pct": (equity - 1) * 100,
        "trades": n,
        "win_rate": wins / n * 100 if n else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "max_dd": max_dd,
    }


def _qualifies(m: dict) -> bool:
    if m["trades"] < MIN_TRADES or m["return_pct"] <= 0 or m["max_dd"] > MAX_DRAWDOWN_PCT:
        return False
    pf = m["profit_factor"]
    return pf is None or pf >= MIN_PROFIT_FACTOR


def evaluate(df: pd.DataFrame, configs: list[dict]) -> dict:
    """Backtest di ogni configurazione su `df`. Ritorna la migliore che passa
    le soglie (o None) e il risultato di tutte, per trasparenza."""
    tried: list[dict] = []
    best: dict | None = None
    for cfg in configs:
        try:
            result, _events, position = run_spot(df, cfg)
            m = _long_only(result.trades, BACKTEST_PARAMS["commission_pct"])
        except Exception as exc:  # una strategia che non gira su questa serie non ferma le altre
            logger.debug("radar: %s fallita: %s", cfg["label"], exc)
            continue
        row = {**m, "label": cfg["label"], "strategy_name": cfg["strategy_name"],
               "source": cfg["source"], "params": cfg["params"], "in_position": position > 0,
               "excluded": UNRELIABLE.get(cfg["strategy_name"])}
        tried.append(row)
        if not row["excluded"] and _qualifies(m) and (best is None or m["return_pct"] > best["return_pct"]):
            best = row
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    return {
        "best": best,
        "tried": sorted(tried, key=lambda r: r["return_pct"], reverse=True),
        "hold_pct": (last / first - 1) * 100 if first > 0 else None,
    }


# ------------------------------------------------------------------- stato

_state: dict = {
    "last_scan": 0.0,
    "next_scan": 0.0,
    "running": False,
    "chains": {},
    "proposals": [],
    "tested": [],
    "configs": 0,
    "error": "",
}
_first_seen: dict[str, float] = {}  # chain:token -> quando e' diventato proposta
_verdicts: dict[str, tuple[float, dict]] = {}  # chain:token -> (quando, esito di legit.assess)


def _load() -> None:
    if not _STORE_FILE.exists():
        return
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(raw, dict):
        _state.update({k: v for k, v in raw.get("state", {}).items() if k in _state and k != "running"})
        _first_seen.update(raw.get("first_seen") or {})


def _save() -> None:
    try:
        _STORE_FILE.write_text(json.dumps({"state": _state, "first_seen": _first_seen}, ensure_ascii=False),
                               encoding="utf-8")
    except OSError:
        pass


_load()


def snapshot() -> dict:
    return {
        **{k: v for k, v in _state.items()},
        "filters": {
            "age_max_days": AGE_MAX_DAYS,
            "liq_min_usd": LIQ_MIN_USD,
            "mcap_max_usd": MCAP_MAX_USD,
            "liq_to_mcap_min": LIQ_TO_MCAP_MIN,
            "min_bars": MIN_BARS,
            "min_trades": MIN_TRADES,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
        },
        "costs": {"commission_pct": BACKTEST_PARAMS["commission_pct"],
                  "slippage_pct": BACKTEST_PARAMS["slippage_pct"]},
        "interval_s": SCAN_INTERVAL_S,
    }


def reset_for_tests() -> None:
    _state.update(last_scan=0.0, next_scan=0.0, running=False, chains={}, proposals=[], tested=[],
                  configs=0, error="")
    _first_seen.clear()
    _verdicts.clear()


# ---------------------------------------------------------------------- giro

async def _dex_links(client: httpx.AsyncClient, chain: str, pools: list[Pool]) -> dict[str, dict]:
    """pair -> {websites, socials} da DexScreener, 30 pool per richiesta."""
    out: dict[str, dict] = {}
    for i in range(0, len(pools), 30):
        batch = ",".join(p.pair_address for p in pools[i:i + 30])
        try:
            r = await client.get(f"{DEXSCREENER_PAIRS}/{chain}/{batch}")
            for pair in (r.json() or {}).get("pairs") or [] if r.status_code == 200 else []:
                out[pair.get("pairAddress") or ""] = legit.links_from_dexscreener(pair)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("radar: DexScreener %s: %s", chain, exc)
    return out


async def vet(client: httpx.AsyncClient, pool: Pool, links: dict) -> dict:
    key = f"{pool.chain}:{pool.token_address}"
    cached = _verdicts.get(key)
    if cached and time.time() - cached[0] < VERDICT_TTL_S:
        return cached[1]
    token = {"chain": pool.chain, "symbol": pool.symbol, "name": pool.name, "token_address": pool.token_address,
             "market_cap": pool.market_cap, "liquidity_usd": pool.liquidity_usd, "exception": pool.exception}
    try:
        verdict = await legit.assess(token, links, client)
    except Exception:
        logger.exception("radar: filtro fallito su %s", pool.symbol)
        return {"status": "wait", "reason": "check failed", "checks": []}
    if verdict["status"] != "wait":
        _verdicts[key] = (time.time(), verdict)
    return verdict


async def _fetch_pools(client: httpx.AsyncClient, chain: str) -> list[Pool]:
    network = CHAINS[chain]
    requests = [("pools", {"page": p, "sort": "h24_volume_usd_desc"}) for p in (1, 2, 3)]
    requests += [("trending_pools", {"page": 1}), ("new_pools", {"page": 1})]
    pools: list[Pool] = []
    for endpoint, params in requests:
        try:
            resp = await client.get(f"{GECKO_BASE}/networks/{network}/{endpoint}", params=params,
                                    headers={"Accept": "application/json"})
            if resp.status_code == 200:
                pools += [p for p in (parse_pool(chain, x) for x in resp.json().get("data") or []) if p]
            else:
                logger.info("radar: GeckoTerminal %s su %s/%s", resp.status_code, chain, endpoint)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("radar: %s/%s non raggiungibile: %s", chain, endpoint, exc)
        await asyncio.sleep(_REQUEST_GAP_S)
    return pools


def _as_item(pool: Pool) -> dict:
    return {"chain": pool.chain, "symbol": pool.symbol, "name": pool.name,
            "token_address": pool.token_address, "pair_address": pool.pair_address,
            "pair_url": pool.pair_url, "market_cap": pool.market_cap, "liquidity_usd": pool.liquidity_usd,
            "created_at": time.time() - pool.age_hours * 3600}


def _proposal_rank(p: dict) -> tuple:
    """Prima le eccezioni GitHub, per voto; poi le altre, per rendimento."""
    if p.get("exception"):
        return (0, -((p.get("report") or {}).get("score") or 0))
    return (1, -(p.get("return_pct") or 0))


async def scan_once() -> dict:
    """Un giro completo. Non solleva: un errore finisce in `error`."""
    if _state["running"]:
        return snapshot()
    _state["running"] = True
    provider = DexScreenerProvider()
    configs = strategy_configs()
    proposals: list[dict] = []
    tested: list[dict] = []
    chains: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for chain in CHAINS:
                pools = await _fetch_pools(client, chain)
                pools += await _fetch_extra_pools(client, chain)
                picked, passing = pick_candidates(pools, MAX_VETTED_PER_CHAIN)
                picked += exception_queue(pick_exceptions(pools, picked))
                info = {"seen": len({p.token_address for p in pools}), "passing": passing,
                        "legit": 0, "rejected": 0, "waiting": 0, "tested": 0, "too_young": 0}
                links = await _dex_links(client, chain, picked)
                cleared: list[tuple[Pool, dict]] = []
                for pool in picked:
                    verdict = await vet(client, pool, links.get(pool.pair_address) or {})
                    if verdict["status"] == "ok":
                        info["legit"] += 1
                        cleared.append((pool, verdict))
                    else:
                        info["rejected" if verdict["status"] == "fail" else "waiting"] += 1
                    if verdict["status"] == "fail":
                        onchain_listings.note_rejected(_as_item(pool), verdict["reason"], time.time(),
                                                       verdict, source="radar")
                # Le eccezioni si provano sempre: sono poche e sono il motivo del giro.
                normal = [c for c in cleared if not c[0].exception]
                for pool, verdict in normal[:MAX_TESTED_PER_CHAIN] + [c for c in cleared if c[0].exception]:
                    interval = interval_for(pool.age_hours)
                    try:
                        df = await provider.fetch_ohlcv(make_symbol(pool.symbol, chain, pool.pair_address),
                                                        interval, 1000)
                    except ProviderError as exc:
                        logger.info("radar: candele %s: %s", pool.symbol, exc)
                        await asyncio.sleep(_REQUEST_GAP_S)
                        continue
                    await asyncio.sleep(_REQUEST_GAP_S)
                    if len(df) < MIN_BARS:
                        info["too_young"] += 1
                        continue
                    info["tested"] += 1
                    outcome = await asyncio.to_thread(evaluate, df, configs)
                    base = {
                        "chain": chain, "symbol": pool.symbol, "name": pool.name,
                        "token_address": pool.token_address, "pair_address": pool.pair_address,
                        "pair_url": pool.pair_url, "dex": pool.dex,
                        "age_hours": round(pool.age_hours, 1), "liquidity_usd": pool.liquidity_usd,
                        "market_cap": pool.market_cap, "interval": interval, "bars": len(df),
                        "days": round((df.index[-1] - df.index[0]).total_seconds() / 86400, 1),
                        "hold_pct": outcome["hold_pct"], "exception": pool.exception,
                        "website": verdict.get("website"), "socials": verdict.get("socials") or [],
                        "github": verdict.get("github"), "checks": verdict.get("checks") or [],
                        "scan_url": verdict.get("scan_url"), "report": verdict.get("report"),
                    }
                    best = outcome["best"]
                    if best is None and pool.exception:
                        # Un progetto eccezionale si mostra per quello che e', anche
                        # se nessuna strategia l'avrebbe tradato bene.
                        best = next((r for r in outcome["tried"] if not r["excluded"]), None)
                    if best is None:
                        top = next((r for r in outcome["tried"] if not r["excluded"]), None)
                        tested.append({**base, "best_label": top["label"] if top else None,
                                       "best_return_pct": top["return_pct"] if top else None})
                        continue
                    key = f"{chain}:{pool.token_address}"
                    found_at = _first_seen.setdefault(key, time.time())
                    proposals.append({
                        **base,
                        "found_at": found_at, "strategy_ok": outcome["best"] is not None,
                        "strategy": best["label"],
                        "strategy_name": best["strategy_name"],
                        "source": best["source"],
                        "params": best["params"],
                        "return_pct": best["return_pct"],
                        "trades": best["trades"],
                        "win_rate": best["win_rate"],
                        "profit_factor": best["profit_factor"],
                        "max_dd": best["max_dd"],
                        "in_position": best["in_position"],
                        "configs_tried": len(outcome["tried"]),
                        "tried": [{k: r[k] for k in ("label", "return_pct", "trades", "max_dd", "excluded")}
                                  for r in outcome["tried"]],
                    })
                chains[chain] = info
        live = {f"{p['chain']}:{p['token_address']}" for p in proposals}
        for key in list(_first_seen):
            if key not in live:
                del _first_seen[key]
        proposals.sort(key=_proposal_rank)
        _state.update(proposals=proposals, tested=tested, chains=chains, configs=len(configs), error="")
    except Exception as exc:
        logger.exception("radar: giro fallito")
        _state["error"] = str(exc)[:200]
    finally:
        now = time.time()
        _state.update(running=False, last_scan=now, next_scan=now + SCAN_INTERVAL_S)
        _save()
    return snapshot()


async def radar_loop() -> None:
    _state["next_scan"] = time.time() + FIRST_SCAN_DELAY_S
    await asyncio.sleep(FIRST_SCAN_DELAY_S)
    while True:
        await scan_once()
        await asyncio.sleep(SCAN_INTERVAL_S)


# ------------------------------------------------------------------ grafico

CHART_TTL_S = 30
_chart_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

_SPOT_VIEW = {"entry_long": "in", "exit_long": "out"}


def long_events(events, df: pd.DataFrame | None = None) -> list[dict]:
    """Acquisti e vendite dove avvengono davvero. Il segnale nasce alla
    chiusura di una candela, ma il backtest (fill_at "next_open") compra e
    vende all'apertura di quella dopo: la freccia va li', a quel prezzo, non
    una candela prima (Lorenzo: "non appare nel grafico gli ingressi in modo
    preciso")."""
    fill: dict[int, tuple[int, float]] = {}
    if df is not None and len(df) > 1:
        times = [int(ts.timestamp()) for ts in df.index]
        opens = df["open"].tolist()
        fill = {times[i]: (times[i + 1], float(opens[i + 1])) for i in range(len(times) - 1)}
    out: list[dict] = []
    holding = False
    for e in events:
        kind = _SPOT_VIEW.get(e.type)
        if kind is None or (kind == "in") == holding:
            continue
        holding = kind == "in"
        at, price = fill.get(e.time, (e.time, e.price))
        out.append({"time": at, "kind": kind, "price": price, "signal_time": e.time})
    return out


def chart_strategies(df: pd.DataFrame, configs: list[dict]) -> list[dict]:
    """Ingressi/uscite di ogni strategia e il suo backtest sulle stesse
    candele, cosi' la lista accanto al grafico dice anche quanto rende."""
    rows = []
    for cfg in configs:
        try:
            result, events, position = run_spot(df, cfg)
            m = _long_only(result.trades, BACKTEST_PARAMS["commission_pct"])
        except Exception as exc:
            logger.debug("radar chart: %s fallita: %s", cfg["label"], exc)
            continue
        rows.append({
            "label": cfg["label"], "strategy_name": cfg["strategy_name"], "params": cfg["params"],
            "source": cfg["source"], "excluded": UNRELIABLE.get(cfg["strategy_name"]),
            "events": long_events(events, df), "in_position": position > 0,
            "return_pct": m["return_pct"], "trades": m["trades"], "win_rate": m["win_rate"],
            "max_dd": m["max_dd"], "qualifies": _qualifies(m),
        })
    return rows


def best_row(rows: list[dict]) -> dict | None:
    """La strategia da mostrare per prima: la migliore che supera le soglie,
    se no la migliore con almeno un trade. Mai quella che usa dati futuri."""
    usable = [r for r in rows if not r.get("excluded") and r.get("trades")]
    ranked = sorted(usable, key=lambda r: (bool(r.get("qualifies")), r["return_pct"]), reverse=True)
    return ranked[0] if ranked else None


async def chart_payload(chain: str, pool: str, symbol: str, interval: str) -> dict:
    """Candele del pool e ingressi/uscite di ogni strategia, sulle stesse
    candele del backtest. Tenute 30 s: chi guarda aggiorna ogni 30 s."""
    key = (chain, pool, interval)
    cached = _chart_cache.get(key)
    if cached and time.time() - cached[0] < CHART_TTL_S:
        df = cached[1]
    else:
        # Chi guarda il grafico passa davanti ai giri in background, e se
        # GeckoTerminal rifiuta si tiene l'ultimo grafico buono.
        gecko_urgent.set(True)
        try:
            df = await DexScreenerProvider().fetch_ohlcv(make_symbol(symbol, chain, pool), interval, 1000)
        except NoDataYet:
            if not cached:
                raise
            df = cached[1]
        _chart_cache[key] = (time.time(), df)
        if len(_chart_cache) > 64:
            _chart_cache.pop(next(iter(_chart_cache)))
    strategies = await asyncio.to_thread(chart_strategies, df, strategy_configs())
    return {
        "chain": chain, "pool": pool, "symbol": symbol, "interval": interval,
        "candles": [
            {"time": int(ts.timestamp()), "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close)}
            for ts, r in df.iterrows()
        ],
        "strategies": strategies,
        "best": (best_row(strategies) or {}).get("label"),
        "updated_at": time.time(),
    }


# ------------------------------------------------- backtest dei nuovi listing

LISTING_MIN_BARS = 45           # tre quarti d'ora di candele da 1 minuto
LISTING_TESTED = 20             # i piu' recenti
LISTINGS_TICK_S = 30


def listing_interval(age_s: float) -> str:
    return auto_interval(age_s)


def listing_refresh_s(age_s: float) -> float:
    return 120 if age_s < 2 * 3600 else 600


def summarize_listing_backtest(df: pd.DataFrame, interval: str, configs: list[dict]) -> dict:
    rows = chart_strategies(df, configs)
    best = best_row(rows)
    out = {"at": time.time(), "interval": interval, "bars": len(df), "configs": len(rows)}
    if best is None:
        return {**out, "state": "none"}
    return {**out, "state": "ok" if best["qualifies"] else "weak", "label": best["label"],
            "strategy_name": best["strategy_name"], "return_pct": best["return_pct"],
            "trades": best["trades"], "win_rate": best["win_rate"], "max_dd": best["max_dd"],
            "in_position": best["in_position"],
            "last_event": best["events"][-1] if best["events"] else None}


async def backtest_listing(item: dict, provider: DexScreenerProvider | None = None) -> dict:
    """Tutte le strategie sulle candele del listing: quale avrebbe reso di
    piu' finora. Lorenzo: "di tutti quelli rilevati, quale strategia
    performerebbe meglio facendo un backtest"."""
    age = time.time() - (item.get("created_at") or item.get("found_at") or time.time())
    interval = listing_interval(age)
    df = await (provider or DexScreenerProvider()).fetch_ohlcv(
        make_symbol(item["symbol"], item["chain"], item["pair_address"]), interval, 1000)
    if len(df) < LISTING_MIN_BARS:
        return {"at": time.time(), "interval": interval, "bars": len(df), "state": "young",
                "need": LISTING_MIN_BARS}
    return await asyncio.to_thread(summarize_listing_backtest, df, interval, strategy_configs())


async def listings_backtest_loop() -> None:
    await asyncio.sleep(FIRST_SCAN_DELAY_S)
    provider = DexScreenerProvider()
    while True:
        now = time.time()
        for item in onchain_listings.recent()[:LISTING_TESTED]:
            age = now - (item.get("created_at") or item.get("found_at") or now)
            last = (item.get("backtest") or {}).get("at", 0)
            if now - last < listing_refresh_s(age):
                continue
            try:
                result = await backtest_listing(item, provider)
            except ProviderError as exc:
                logger.info("radar: candele del listing %s: %s", item.get("symbol"), exc)
                result = {**(item.get("backtest") or {}), "at": now}
            except Exception:
                logger.exception("radar: backtest del listing %s", item.get("symbol"))
                result = {**(item.get("backtest") or {}), "at": now}
            onchain_listings.update_listing(item["chain"], item["pair_address"], backtest=result)
            await asyncio.sleep(_REQUEST_GAP_S)
        await asyncio.sleep(LISTINGS_TICK_S)
