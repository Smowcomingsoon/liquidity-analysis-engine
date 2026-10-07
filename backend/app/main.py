import asyncio
import time
import base64
import datetime as _dt
import hashlib
import io
import html as _html
import httpx
import itertools
import math
import random
import json
import logging
import re
import sys
import time as _time

import pandas as pd
import qrcode
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# smartmoneyconcepts prints a unicode banner on import; Windows consoles using
# a non-UTF8 codepage would otherwise crash the app at startup. In a frozen
# sidecar spawned with redirected/null stdio, stdout/stderr may not support
# (or even have) .encoding — never let this startup nicety crash the app.
try:
    if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from app import auth
from app.trading.onchain.quant_api import router as quant_router
from app import guest as guest_links
from app import issues as issues_store
from app import rate_limit
from app.trading.executor import place_futures_order_for_signal, place_order_for_signal
from app.trading.futures import registry as futures_registry
from app.trading.futures.base import FuturesCreds, FuturesExecutionError
from app.models import MAX_FUTURES_LEVERAGE
from app import archive as archive_store
from app import jobs as jobs_store
from app import trading_log
from app import favorites as favorites_store
from app import watchlist as watchlist_store
from app.analysis.backtest import run_backtest
from app.analysis.futures_math import DEFAULT_MAINTENANCE_MARGIN_RATE, liquidation_price
from app.analysis.cancel import SearchCancelled
from app.export import build_backtest_workbook
from app.analysis.confluence import compute_confluence
from app.analysis.indicators import compute_indicators
from app.analysis import indicator_catalog
from app.analysis.fibonacci import compute_fib_zones
from app.analysis.optimize import (
    Constraints,
    NoCandidateMeetsConstraints,
    is_optimizable,
    optimize_strategy,
)
from app.analysis.pipeline import run_pipeline
from app.analysis.smc_engine import compute_dealing_range, compute_levels, compute_swings
from app.analysis.strategies import (
    AVAILABLE_STRATEGIES,
    DEFAULT_STRATEGY,
    STRATEGY_REGISTRY,
    build_strategy_state,
    rug_stop,
    spot_only,
)
from app.analysis.summary import build_summary, determine_bias
from app.cache import get_or_fetch
from app.config import settings
from app import diagnostics
from app.live.kraken_stream import kraken_stream_manager
from app.live.mexc_stream import mexc_stream_manager
from app.models import (
    AnalysisResponse,
    AssetType,
    BacktestResult,
    Candle,
    CryptoExchange,
    FavoriteEntry,
    OnchainChain,
    StrategyState,
    SymbolSuggestion,
    TimeframeAnalysis,
    TimeframeKey,
    User,
    WatchlistEntry,
)
from app.trading.onchain import aggregator as onchain_aggregator
from app.trading.onchain import balances as onchain_balances
from app.trading.onchain import deposit as onchain_deposit
from app.trading.onchain import ai_scan as onchain_ai_scan
from app.trading.onchain import arenas as onchain_arenas
from app.trading.onchain import execute as onchain_execute
from app.trading.onchain import send as onchain_send
from app.trading.onchain import jito as onchain_jito
from app.trading.onchain import keys as onchain_keys
from app.trading.onchain import layers as onchain_layers
from app.trading.onchain import legit as onchain_legit
from app.trading.onchain import consistency as onchain_consistency_audit
from app.trading.onchain import gem_track as onchain_gem_track
from app.trading.onchain import ledger as onchain_ledger
from app.trading.onchain import outcomes as onchain_outcomes
from app.trading.onchain import narratives as onchain_narratives
from app.trading.onchain import judge as onchain_judge
from app.trading.onchain import code_review as onchain_code_review
from app.trading.onchain import backtest_lab as onchain_backtest_lab
from app.trading.onchain import strategy_lab as onchain_strategy_lab
from app.trading.onchain import candle_archive as onchain_candle_archive
from app.trading.onchain import ladder_watch as onchain_ladder_watch
from app.trading.onchain import autotrader as onchain_autotrader
from app.trading.onchain import deep_recheck as onchain_deep_recheck
from app.trading.onchain import live_prices as onchain_live_prices
from app.trading.onchain import wealth as onchain_wealth
from app import profiles
from app import wallet_link
from app.trading.onchain import user_swaps
from app.trading.onchain import portfolio as onchain_multichain
from app.trading.onchain import passkey as onchain_passkey
from app.trading.onchain import pilot as onchain_pilot
from app.trading.onchain import listings as onchain_listings
from app.trading.onchain import leaderboard as onchain_leaderboard
from app.trading.onchain import projects_db as onchain_projects_db
from app.trading.onchain import ai_review as onchain_ai_review
from app.trading.onchain import alert_track as onchain_alert_track
from app.trading.onchain import plans as onchain_plans
from app.trading.onchain import radar as onchain_radar
from app import token_launch
from app.trading.onchain import prices as onchain_prices
from app.trading.onchain import token_search as onchain_tokens
from app.trading.onchain import quotes as onchain_quotes
from app.trading.onchain import valuation as onchain_valuation
from app.trading.onchain import reveal_requests as onchain_reveal
from app.trading.onchain import rpc_client as onchain_rpc_client
from app.trading.onchain import simulate as onchain_simulate
from app.trading.onchain import token_metadata as onchain_token_metadata
from app.trading.onchain import twap_jobs as onchain_twap_jobs
from app.exchange_balances import BalanceFetchError, fetch_kucoin_balances, fetch_mexc_balances
from app.notify import telegram
from app.notify import alert_log
from app.notify import alerts
from app.notify import weekly_recap
from app.notify import deepseek_desk
from app.notify import deepseek_studies
from app.notify.alerts import find_new_signals, format_signal_message, record_alert_sent, get_all_stats_for_entry
import app.price_alerts as price_alerts_store
from app.models import PriceAlert, PriceAlertCondition, StrategySignal
from app.pine import PineError, PineInterpreter, is_strategy_style, parse_program, scan_inputs
from app.pine import storage as pine_storage
from app.providers.base import (
    DataProvider,
    NoDataYet,
    ProviderError,
    ProviderUnreachable,
    QuotaExceeded,
)
from app.providers.binance import BinanceProvider
from app.providers.binance import format_symbol as format_binance_symbol
from app.providers.dexscreener import DexScreenerProvider, _parse_symbol as _parse_dex_symbol
from app.providers.kraken import KrakenProvider
from app.providers.kraken import format_symbol as format_kraken_symbol
from app.providers.kucoin import KucoinProvider
from app.providers.kucoin import format_symbol as format_kucoin_symbol
from app.providers.mexc import MexcProvider
from app.providers.mexc import format_symbol as format_mexc_symbol
from app.providers.twelvedata import TwelveDataProvider
from app.resample import resample_by_count
from app.timeframes import (
    DEFAULT_INTERVALS,
    INTERVAL_SECONDS,
    SYNTHETIC_DAY_INTERVALS,
    IntervalChoice,
    drop_unclosed_candle,
)
from starlette.middleware.gzip import GZipMiddleware

logger = logging.getLogger(__name__)


# Audit B04, rest: a one-shot task (a Telegram message) nobody holds can be
# garbage-collected before it runs, and its error is never seen. Hold it
# until it ends and log what it raised.
_fired: set[asyncio.Task] = set()


def _fire(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _fired.add(task)
    task.add_done_callback(_fired_done)
    return task


def _fired_done(task: asyncio.Task) -> None:
    _fired.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("background send failed", exc_info=task.exception())

# Un manager di stream live per exchange, ciascuno col proprio protocollo
# pubblico (protobuf su MEXC, JSON su Kraken) — vedi /ws/live sotto.
_LIVE_STREAM_MANAGERS = {"mexc": mexc_stream_manager, "kraken": kraken_stream_manager}
if getattr(sys, "frozen", False):
    FRONTEND_DIR = Path(sys._MEIPASS) / "frontend"  # type: ignore[attr-defined]
else:
    FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
WATCHLIST_POLL_SECONDS = 60
_WATCHDOG_FILE = Path(__file__).resolve().parent / "data" / "watchdog.json"
_WATCHDOG_STALE_S = WATCHLIST_POLL_SECONDS * 5  # alert if loop silent for 5 cycles

# L'elenco degli exchange futures vive nel registro degli adattatori: qui
# c'e' solo il nome vecchio, perche' non ci sia una seconda lista che possa
# essere in disaccordo con la prima. Il perimetro (leva accessibile
# dall'Europa) e' spiegato in app/trading/futures/registry.py.
SUPPORTED_FUTURES_EXCHANGES_ORDERED = futures_registry.SUPPORTED_FUTURES_EXCHANGES

twelvedata_provider = TwelveDataProvider()
mexc_provider = MexcProvider()
binance_provider = BinanceProvider()
kucoin_provider = KucoinProvider()
kraken_provider = KrakenProvider()
dexscreener_provider = DexScreenerProvider()


def _telegram_allowed(user: User) -> bool:
    allowed = {e.strip().lower() for e in settings.telegram_allowed_emails.split(",") if e.strip()}
    return not allowed or user.email.lower() in allowed


def _balance_visible(user: User) -> bool:
    allowed = {e.strip().lower() for e in settings.balance_visible_emails.split(",") if e.strip()}
    return not allowed or user.email.lower() in allowed


def backtest_interval_for(ltf_interval: str) -> str:
    """The backtest runs on the same interval the chart is set to."""
    return ltf_interval


async def _no_results() -> list:
    """Segnaposto per la ricerca quando le azioni sono spente: la gather
    vuole comunque qualcosa da attendere."""
    return []


def get_provider(asset_type: AssetType, exchange: CryptoExchange = "mexc") -> DataProvider:
    if asset_type == "dex":
        # Un pair on-chain non e' su nessuno dei quattro exchange: l'unico
        # provider che sa cosa farne del suo simbolo canonico (BASE@chain:pool)
        # e' questo, a prescindere da cosa dice `exchange` (che qui non ha
        # nemmeno senso — resta il default della richiesta).
        return dexscreener_provider
    if asset_type != "crypto":
        if not settings.stocks_enabled:
            # Meglio dirlo qui, una volta, che lasciare che fallisca piu'
            # avanti con "chiave TwelveData mancante" — un errore che parla
            # di una chiave quando il problema e' che le azioni sono spente.
            raise HTTPException(
                status_code=400,
                detail="Stocks are disabled: this system trades crypto only (USDT/USDC).",
            )
        return twelvedata_provider
    if exchange == "binance":
        return binance_provider
    if exchange == "kucoin":
        return kucoin_provider
    if exchange == "kraken":
        return kraken_provider
    return mexc_provider


def canonical_symbol(symbol: str, asset_type: AssetType, exchange: CryptoExchange = "mexc") -> str:
    """The exact trading-pair string a provider actually resolves a query
    to (e.g. "BTC" -> "BTCUSDT" on MEXC) — echoed back in API responses
    instead of the raw query so the same instrument always identifies
    itself the same way regardless of how it was searched for. The
    frontend keys client-side drawing persistence off this value; without
    it, typing "BTC" versus clicking the "BTCUSDT" suggestion produced two
    different symbols for the same chart, and drawings saved under one
    never showed up under the other."""
    if asset_type == "dex":
        # Gia' canonico com'e' arrivato dalla ricerca (BASE@chain:pool) —
        # non c'e' un formato "come lo scrive l'utente" da normalizzare.
        return symbol.strip()
    if asset_type != "crypto":
        return symbol.strip().upper()
    if exchange == "binance":
        return format_binance_symbol(symbol)
    if exchange == "kucoin":
        return format_kucoin_symbol(symbol)
    if exchange == "kraken":
        return format_kraken_symbol(symbol)
    return format_mexc_symbol(symbol)


async def fetch_ohlcv_canonical(provider: DataProvider, symbol: str, interval: str, outputsize: int, since_ms: int | None = None):
    """Like provider.fetch_ohlcv, but transparently synthesizes intervals no
    provider natively supports (2D/3D/5D) by fetching extra 1D candles and
    folding them down — every call site gets these for free.

    When since_ms is set, outputsize is ignored and the provider fetches all
    available candles from that timestamp to now."""
    multiple = SYNTHETIC_DAY_INTERVALS.get(interval)
    if multiple is None:
        return await provider.fetch_ohlcv(symbol, interval, outputsize, since_ms=since_ms)
    if since_ms is not None:
        raw = await provider.fetch_ohlcv(symbol, "1d", outputsize, since_ms=since_ms)
        return resample_by_count(raw, multiple)
    raw = await provider.fetch_ohlcv(symbol, "1d", outputsize * multiple)
    return resample_by_count(raw, multiple).tail(outputsize)


async def _fetch_cached(key: tuple, provider: DataProvider, symbol: str, interval: str, outputsize: int):
    """Shared helper: wraps fetch_ohlcv_canonical + get_or_fetch so each
    route doesn't need to declare its own inner do_fetch closure."""
    async def _do():
        return await fetch_ohlcv_canonical(provider, symbol, interval, outputsize)
    return await get_or_fetch(key, _do)


async def _fetch_cached_since(key: tuple, provider: DataProvider, symbol: str, interval: str, since_ms: int):
    """Like _fetch_cached but fetches all candles from since_ms to now."""
    async def _do():
        return await fetch_ohlcv_canonical(provider, symbol, interval, 0, since_ms=since_ms)
    return await get_or_fetch(key, _do)



# Token di quotazione usati per l'auto-trade on-chain dalla watchlist: uno
# stabile per chain, cosi' l'importo in USD del tetto si converte senza
# dover conoscere i decimali del token BASE (che invece servono per una
# vendita, e non sono salvati da nessuna parte — vedi sotto). Decimali
# verificati dal vivo, non presi a memoria: USDC e' notoriamente 6 su
# Solana, USDG e' stato letto con una decimals() reale su Robinhood Chain
# il 2026-09-23 (0x5fc536...d168 -> 6).
_ONCHAIN_QUOTE_TOKEN = {
    "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "robinhood": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
}
_ONCHAIN_QUOTE_DECIMALS = 6

# Decimali del nativo: hardcoded, non da decimals() sul contratto, per lo
# stesso motivo per cui il frontend (swap.js) non li chiede mai a runtime —
# visto in produzione: decimals() sull'indirizzo nativo di Robinhood Chain
# puo' tornare None (non e' garantito un ERC20 vero, e' un indirizzo di
# routing), e "10 ** None" mandava in errore /api/onchain/price.
_NATIVE_DECIMALS = {"solana": 9, "robinhood": 18}


async def _run_onchain_auto_trade(owner: User, entry: WatchlistEntry, strategy_name: str, fresh: list) -> None:
    """Fase 4 vera: da segnale a firma, quando l'indirizzo del token c'e'.

    BUY (entry_long/flip_to_long): comprare si paga sempre nello stabile di
    quote, i cui decimali sono noti e verificati sopra.
    SELL (exit_long/flip_to_short): venduta la posizione registrata sul
    ledger, letta in unita' umane; convertita in unita' raw con i decimali
    veri del token BASE (token_metadata.get_token_decimals, letti dalla
    mint/contratto — mai indovinati). Se i decimali non si ottengono (RPC
    irraggiungibile, token strano), resta "saltato" con la ragione onesta:
    un decimals sbagliato manda un multiplo o una frazione dell'importo
    voluto, non un errore leggibile.
    """
    if not settings.onchain_enabled:
        for signal in fresh:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato", detail="sistema wallet on-chain spento (interruttore generale)",
                price=signal.price,
            )
        return

    if not entry.base_token_address:
        for signal in fresh:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato",
                detail="on-chain auto-execution not wired yet: "
                       "the token address is missing, not just the pool's",
                price=signal.price,
            )
        return

    try:
        _base_symbol, chain, _pool = _parse_dex_symbol(entry.symbol)
    except ProviderError as exc:
        for signal in fresh:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato", detail=f"on-chain symbol not recognized: {exc}", price=signal.price,
            )
        return

    quote_token = _ONCHAIN_QUOTE_TOKEN.get(chain)
    if quote_token is None or chain not in owner.onchain_wallets:
        for signal in fresh:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato", detail=f"no on-chain wallet configured for chain '{chain}'",
                price=signal.price,
            )
        return

    # Il pilota spento vuol dire "non compra piu' niente da solo" su questa
    # chain — Lorenzo vuole un interruttore solo, e che dica la verita'. Le
    # uscite restano: spegnere il pilota non deve lasciare una posizione
    # aperta senza piu' nessuno che la chiuda.
    if chain not in owner.sniper_autotrade_chains:
        buys = [s for s in fresh if s.type in ("entry_long", "flip_to_long")]
        for signal in buys:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato", detail=f"pilot off on {chain}: no automatic buys",
                price=signal.price,
            )
        fresh = [s for s in fresh if s not in buys]
        if not fresh:
            return

    cap = entry.size_cap_usdt.get(strategy_name) or settings.onchain_max_usd_per_trade
    buy_amount_in = int(cap * (10 ** _ONCHAIN_QUOTE_DECIMALS))

    for signal in fresh:
        is_buy = signal.type in ("entry_long", "flip_to_long")
        is_sell = signal.type in ("exit_long", "flip_to_short")
        if not is_buy and not is_sell:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato", detail=f"signal type not handled by on-chain auto-trade: {signal.type}",
                price=signal.price,
            )
            continue

        if is_sell:
            decimals = await onchain_token_metadata.get_token_decimals(chain, entry.base_token_address)
            if decimals is None:
                trading_log.record(
                    owner.id, symbol=entry.symbol, exchange=entry.exchange,
                    strategy_name=strategy_name, signal_type=signal.type,
                    outcome="saltato",
                    detail="sell skipped: token decimals unavailable from the real contract/mint "
                           "(RPC unreachable or token not recognized) — never guessed",
                    price=signal.price,
                )
                continue
            position = onchain_ledger.get_position(owner.id, chain, entry.base_token_address)
            quantity = position["quantity"]
            if quantity <= 0:
                trading_log.record(
                    owner.id, symbol=entry.symbol, exchange=entry.exchange,
                    strategy_name=strategy_name, signal_type=signal.type,
                    outcome="saltato", detail="no recorded position on this token to sell",
                    price=signal.price,
                )
                continue
            sell_amount_in = int(quantity * (10 ** decimals))
            notional_usd = quantity * signal.price

        # One signal, one trade, even if the loop sees it again (audit S03).
        intent = f"watch-{entry.symbol}-{strategy_name}-{signal.type}-{signal.time}"
        try:
            private_key_hex = auth.get_decrypted_onchain_private_key(owner, chain)
            if chain == "solana":
                # Slippage stretto quando l'invio passa per Jito (piano MEV) —
                # vedi jito.JITO_SLIPPAGE_BPS per la giustificazione del numero.
                slippage_bps = onchain_jito.JITO_SLIPPAGE_BPS if settings.onchain_use_jito else 50
                if is_buy:
                    quote = await onchain_quotes.jupiter_quote(
                        quote_token, entry.base_token_address, buy_amount_in, slippage_bps,
                    )
                    result = await onchain_execute.execute_order(
                        owner.id, "solana", entry.base_token_address, strategy_name, "buy",
                        cap, quote["price_impact_pct"], signal.price, private_key_hex, quote, intent_id=intent,
                    )
                else:
                    quote = await onchain_quotes.jupiter_quote(
                        entry.base_token_address, quote_token, sell_amount_in, slippage_bps,
                    )
                    result = await onchain_execute.execute_order(
                        owner.id, "solana", entry.base_token_address, strategy_name, "sell",
                        notional_usd, quote["price_impact_pct"], signal.price, private_key_hex, quote, intent_id=intent,
                    )
            else:
                token_in, token_out = (quote_token, entry.base_token_address) if is_buy \
                    else (entry.base_token_address, quote_token)
                found = await onchain_quotes.find_pool_and_fee(token_in, token_out)
                if found is None:
                    raise RuntimeError("no Uniswap v3 pool found for this token on any fee tier")
                _pool, fee = found
                amount_in = buy_amount_in if is_buy else sell_amount_in
                v3_quote = await onchain_quotes.uniswap_v3_quote(token_in, token_out, fee, amount_in)
                amount_out_minimum = int(v3_quote["amount_out"] * 0.99)  # 1% di slippage, come il default dell'endpoint manuale
                result = await onchain_execute.execute_order(
                    owner.id, "robinhood", entry.base_token_address, strategy_name, "buy" if is_buy else "sell",
                    cap if is_buy else notional_usd, v3_quote["price_impact_pct"], signal.price, private_key_hex,
                    {"token_in": token_in, "token_out": token_out, "fee": fee,
                     "amount_in": amount_in, "amount_out_minimum": amount_out_minimum},
                    intent_id=intent,
                )
            outcome = "eseguito" if result.get("accepted") else "saltato"
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome=outcome, detail=str(result)[:400], price=signal.price,
            )
            if result.get("accepted") and not result.get("duplicate") and owner.telegram_chat_id \
                    and _telegram_allowed(owner):
                _fire(telegram.send_to(
                    owner.telegram_chat_id,
                    f"💱 Ordine on-chain ESEGUITO <b>{entry.symbol}</b> [{signal.type}] su {chain} a {signal.price:g}",
                ))
        except Exception as exc:
            logger.error("On-chain order failed for %s %s: %s", entry.symbol, signal.type, exc)
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="fallito", detail=str(exc), price=signal.price,
            )
            if owner.telegram_chat_id and _telegram_allowed(owner):
                _fire(telegram.send_to(
                    owner.telegram_chat_id,
                    f"⚠️ Ordine on-chain FALLITO <b>{entry.symbol}</b> [{signal.type}]: {exc}",
                ))


_stale_notified: set[str] = set()   # voci gia' avvisate per dati fermi, finche' non ripartono


async def _watchlist_alert_loop() -> None:
    """Background loop, independent of any browser tab: evaluates every
    watchlist ticker against each of its enabled strategies and fires
    Telegram alerts — never for whatever symbol happens to be open on
    screen. One fetch per (ticker, timeframe), reused across every strategy
    enabled on that ticker rather than refetching per strategy."""
    while True:
        await asyncio.sleep(WATCHLIST_POLL_SECONDS)
        try:
            _WATCHDOG_FILE.write_text(json.dumps({"ts": _time.time()}))
        except Exception:
            pass
        if not settings.telegram_bot_token:
            continue
        for entry in watchlist_store.list_entries():
            if not entry.strategies:
                continue
            owner = auth.get_user(entry.owner_id)
            if owner is None or not owner.telegram_chat_id or not _telegram_allowed(owner):
                continue  # no chat_id yet, or this account isn't on the Telegram delivery allowlist
            if _quota_paused(entry.asset_type, entry.exchange):
                continue
            provider = get_provider(entry.asset_type, entry.exchange)
            try:
                df = await fetch_ohlcv_canonical(
                    provider, entry.symbol, entry.interval, settings.candles_per_timeframe
                )
                _provider_recovered(entry.asset_type, entry.exchange)
                if entry.on_close:
                    df = drop_unclosed_candle(df, entry.interval)
                if df.empty:
                    continue
                if entry.asset_type == "dex":
                    onchain_pilot.record_evaluation(entry.id, price=float(df["close"].iloc[-1]))
                # Dati fermi: l'ultima candela (il suo inizio) piu' vecchia di
                # tre intervalli. Non sui pool DEX: li' una candela esiste
                # solo se c'e' uno scambio, e un minuto senza scambi e' un
                # pool tranquillo, non un dato vecchio — UBR su 1m mandava
                # "ultima candela 0h fa" a ogni giro. E una volta sola finche'
                # i dati non ripartono, non a ogni controllo.
                _iv_s = INTERVAL_SECONDS.get(entry.interval, 0)
                if _iv_s and entry.asset_type != "dex":
                    _age = _time.time() - df.index[-1].timestamp()
                    if _age <= _iv_s * 3:
                        _stale_notified.discard(entry.id)
                    elif entry.id not in _stale_notified:
                        _stale_notified.add(entry.id)
                        _old = f"{int(_age // 3600)} h" if _age >= 3600 else f"{int(_age // 60)} min"
                        _fire(telegram.send_to(
                            owner.telegram_chat_id,
                            f"⚠️ Dati fermi: <b>{entry.symbol}</b> [{entry.interval}] — ultima candela {_old} fa",
                        ))
                # Price-level alerts (checked once per entry poll regardless of strategies)
                if df is not None and not df.empty:
                    current_price = float(df["close"].iloc[-1])
                    triggered_pa = price_alerts_store.check_alerts(owner.id, entry.symbol, current_price)
                    for pa in triggered_pa:
                        direction = "sopra" if pa.condition == PriceAlertCondition.crosses_above else "sotto"
                        msg = (
                            f"🎯 <b>{pa.symbol}</b> [{entry.interval}] — Alert prezzo\n"
                            f"Prezzo {direction} <b>{pa.price:g}</b>\n"
                            f"Prezzo attuale: {current_price:g}"
                            + (f"\n{pa.note}" if pa.note else "")
                        )
                        _fire(telegram.send_to(owner.telegram_chat_id, msg))
                        alert_log.append_entry(
                            owner_id=owner.id,
                            symbol=pa.symbol,
                            interval=entry.interval,
                            strategy_name="Price alert",
                            signal_type=pa.condition.value,
                            price=current_price,
                            label=f"Price {'above' if direction == 'sopra' else 'below'} {pa.price:g}",
                            chat_id=owner.telegram_chat_id,
                        )

                for strategy_name in entry.strategies:
                    strategy_interval = entry.strategy_intervals.get(strategy_name, entry.interval)
                    # Refetch if the strategy uses a different interval than the entry default
                    if strategy_interval != entry.interval:
                        try:
                            strat_df = await fetch_ohlcv_canonical(
                                provider, entry.symbol, strategy_interval, settings.candles_per_timeframe
                            )
                            if entry.on_close:
                                strat_df = drop_unclosed_candle(strat_df, strategy_interval)
                        except Exception:
                            strat_df = df
                    else:
                        strat_df = df
                    params = entry.strategy_params.get(strategy_name) or None
                    # Sul DEX si compra e si vende il token, niente short: un
                    # segnale short e' la vendita di quello che si ha.
                    spot_signals = (rug_stop(strat_df, spot_only(STRATEGY_REGISTRY[strategy_name](strat_df, params)))
                                    if entry.asset_type == "dex" and strategy_name in STRATEGY_REGISTRY else None)
                    strategy = build_strategy_state(strategy_name, strat_df, params=params, signals=spot_signals)
                    frequency = entry.alert_frequency.get(strategy_name, "every")
                    fresh = _dispatch_telegram_alerts(
                        owner.telegram_chat_id,
                        entry.symbol,
                        strategy_interval,
                        strategy,
                        frequency=frequency,
                        entry_id=entry.id,
                        owner_id=owner.id,
                        # L'ultima chiusura della serie su cui la strategia e'
                        # stata calcolata: serve al messaggio per dire quanto
                        # il prezzo si e' mosso da quando il segnale e' nato.
                        current_price=(float(strat_df["close"].iloc[-1])
                                       if not strat_df.empty else None),
                    )
                    # Esecuzione automatica degli ordini, se l'exchange e'
                    # configurato. Ogni esito finisce nel diario: e' un percorso
                    # che, funzionando, non diceva nulla — ed e' esattamente cosi'
                    # che e' rimasto morto per mesi senza che nessuno potesse
                    # accorgersene.
                    # Opt-in per singola strategia/ticker (dalla watchlist): un
                    # exchange abilitato non basta piu' da solo a far partire
                    # ordini su OGNI strategia che ci gira sopra.
                    strategy_market = entry.market.get(strategy_name, "spot")
                    # Un pair on-chain non e' su nessun exchange configurabile:
                    # entry.exchange resta il default CryptoExchange della
                    # entry, e senza questo controllo un auto-trade acceso per
                    # sbaglio su un ticker DEX manderebbe un simbolo inventato
                    # (BASE@chain:pool) a un exchange vero.
                    is_dex = entry.asset_type == "dex"
                    cred = owner.trading_credentials.get(entry.exchange) if strategy_market == "spot" and not is_dex else None
                    futures_cred = owner.futures_credentials.get(entry.exchange) if strategy_market == "futures" and not is_dex else None
                    if futures_cred and fresh and entry.auto_trade.get(strategy_name):
                        api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(owner, entry.exchange)
                        skip = ""
                        if not futures_cred.enabled:
                            skip = "auto-execution disabled for this exchange (futures)"
                        elif not (api_key and api_secret):
                            skip = "futures API keys missing or unreadable"
                        if skip:
                            for signal in fresh:
                                trading_log.record(
                                    owner.id, symbol=entry.symbol, exchange=entry.exchange,
                                    strategy_name=strategy_name, signal_type=signal.type,
                                    outcome="saltato", detail=skip, price=signal.price,
                                )
                        else:
                            for signal in fresh:
                                try:
                                    cap = entry.size_cap_usdt.get(strategy_name)
                                    result = await place_futures_order_for_signal(
                                        entry.exchange, entry.symbol, signal,
                                        futures_cred.size_mode, futures_cred.size_value, futures_cred.leverage,
                                        api_key, api_secret, passphrase, max_usdt=cap,
                                    )
                                    logger.info("Futures order placed for %s %s: %s", entry.symbol, signal.type, result)
                                    trading_log.record(
                                        owner.id, symbol=entry.symbol, exchange=entry.exchange,
                                        strategy_name=strategy_name, signal_type=signal.type,
                                        outcome="eseguito", detail=str(result)[:400], price=signal.price,
                                    )
                                    if owner.telegram_chat_id and _telegram_allowed(owner):
                                        cap_note = f" (tetto {cap:g} USDT)" if cap else ""
                                        _fire(telegram.send_to(
                                            owner.telegram_chat_id,
                                            f"💱⚠️ Ordine FUTURES ESEGUITO <b>{entry.symbol}</b> [{signal.type}] "
                                            f"su {entry.exchange} a {signal.price:g}{cap_note}",
                                        ))
                                except Exception as exc:
                                    logger.error("Futures order failed for %s %s: %s", entry.symbol, signal.type, exc)
                                    trading_log.record(
                                        owner.id, symbol=entry.symbol, exchange=entry.exchange,
                                        strategy_name=strategy_name, signal_type=signal.type,
                                        outcome="fallito", detail=str(exc), price=signal.price,
                                    )
                                    if owner.telegram_chat_id and _telegram_allowed(owner):
                                        _fire(telegram.send_to(
                                            owner.telegram_chat_id,
                                            f"⚠️ Ordine FUTURES FALLITO <b>{entry.symbol}</b> [{signal.type}]: {exc}",
                                        ))
                    if cred and fresh and entry.auto_trade.get(strategy_name):
                        api_key, api_secret, passphrase = auth.get_decrypted_credential(owner, entry.exchange)
                        skip = ""
                        if not cred.enabled:
                            skip = "auto-execution disabled for this exchange"
                        elif not (api_key and api_secret):
                            skip = "API keys missing or unreadable"
                        if skip:
                            # Registrato solo per chi l'exchange l'ha configurato:
                            # per tutti gli altri non c'e' nulla da segnalare, e
                            # riempire il diario di "non configurato" lo
                            # renderebbe illeggibile proprio a chi lo consulta.
                            for signal in fresh:
                                trading_log.record(
                                    owner.id, symbol=entry.symbol, exchange=entry.exchange,
                                    strategy_name=strategy_name, signal_type=signal.type,
                                    outcome="saltato", detail=skip, price=signal.price,
                                )
                        else:
                            # I segnali arrivano dalla chiamata sopra. Richiederli
                            # qui tornava sempre vuoto — find_new_signals li aveva
                            # gia' marcati come visti — quindi nessun ordine e'
                            # mai partito, in silenzio.
                            for signal in fresh:
                                try:
                                    cap = entry.size_cap_usdt.get(strategy_name)
                                    result = await place_order_for_signal(
                                        entry.exchange, entry.symbol, signal,
                                        cred.size_mode, cred.size_value,
                                        api_key, api_secret, passphrase, max_usdt=cap,
                                    )
                                    logger.info("Order placed for %s %s: %s", entry.symbol, signal.type, result)
                                    trading_log.record(
                                        owner.id, symbol=entry.symbol, exchange=entry.exchange,
                                        strategy_name=strategy_name, signal_type=signal.type,
                                        outcome="eseguito", detail=str(result)[:400], price=signal.price,
                                    )
                                    if owner.telegram_chat_id and _telegram_allowed(owner):
                                        # Un ordine partito va detto quanto uno
                                        # fallito: era l'unico dei due casi che
                                        # restava muto.
                                        cap_note = f" (tetto {cap:g} USDT)" if cap else ""
                                        _fire(telegram.send_to(
                                            owner.telegram_chat_id,
                                            f"💱 Ordine ESEGUITO <b>{entry.symbol}</b> [{signal.type}] "
                                            f"su {entry.exchange} a {signal.price:g}{cap_note}",
                                        ))
                                except Exception as exc:
                                    logger.error("Order failed for %s %s: %s", entry.symbol, signal.type, exc)
                                    trading_log.record(
                                        owner.id, symbol=entry.symbol, exchange=entry.exchange,
                                        strategy_name=strategy_name, signal_type=signal.type,
                                        outcome="fallito", detail=str(exc), price=signal.price,
                                    )
                                    if owner.telegram_chat_id and _telegram_allowed(owner):
                                        _fire(telegram.send_to(
                                            owner.telegram_chat_id,
                                            f"⚠️ Ordine FALLITO <b>{entry.symbol}</b> [{signal.type}]: {exc}",
                                        ))
                    if is_dex and fresh and entry.auto_trade.get(strategy_name):
                        await _run_onchain_auto_trade(owner, entry, strategy_name, fresh)
            except ProviderUnreachable as exc:
                # Rete, TLS, proxy: quasi sempre passeggero. Si mette in pausa
                # quel provider con attesa crescente e si parla solo se il buco
                # dura — un errore che al giro dopo non si ripresenta non vale
                # un messaggio d'allarme.
                streak = _pause_for_unreachable(entry.asset_type, entry.exchange)
                if _notify_unreachable_once(entry.asset_type, entry.exchange, streak):
                    _fire(telegram.send_to(
                        owner.telegram_chat_id,
                        f"📡 <b>{entry.exchange}</b> non risponde da qualche minuto.\n"
                        f"Il monitoraggio riprende da solo appena torna raggiungibile.\n\n"
                        f"<i>{exc}</i>",
                    ))
                continue
            except NoDataYet as exc:
                # Pool on-chain appena nato: le candele a quell'intervallo
                # arrivano da sole. Aspettare non e' un errore da notificare.
                logger.info("Monitoraggio %s [%s]: %s", entry.symbol, entry.interval, exc)
                continue
            except QuotaExceeded as exc:
                # I crediti del piano sono finiti: non c'e' niente da riprovare
                # fra un minuto. Senza questa pausa arrivava un messaggio di
                # "errore monitoraggio" per ticker per minuto — un guasto
                # apparente al posto di un limite di piano.
                _pause_for_quota(entry.asset_type, entry.exchange, exc)
                if _notify_quota_once(owner.id, entry.asset_type, entry.exchange):
                    _fire(telegram.send_to(
                        owner.telegram_chat_id,
                        f"⏸️ <b>Crediti dati esauriti</b> ({entry.exchange if entry.asset_type == 'crypto' else 'Twelve Data'})\n"
                        f"Il monitoraggio dei titoli riprende automaticamente quando la quota si azzera. "
                        f"Le crypto non sono toccate.\n\n<i>{exc}</i>",
                    ))
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("Alert loop error for %s: %s", entry.symbol, exc, exc_info=True)
                if settings.telegram_bot_token and owner.telegram_chat_id and _telegram_allowed(owner):
                    _fire(
                        telegram.send_to(
                            owner.telegram_chat_id,
                            f"⚠️ Errore monitoraggio <b>{entry.symbol}</b> [{entry.interval}]: {exc}",
                        )
                    )


_SNIPER_ONCHAIN_STRATEGY = "Lorenzo (trend-follower)"
_SNIPER_ONCHAIN_MIN_LIQUIDITY_USD = 3000.0
# "Nuovi arrivi", non token stagionati: lo sniper archivia tutto cio' che ha
# mai incontrato, questo ponte guarda solo l'arco in cui "nuovo" ha ancora
# senso — un token con 3 mesi di storia non e' piu' un arrivo da inseguire.
_SNIPER_ONCHAIN_MAX_AGE_HOURS = 72.0
_SNIPER_ONCHAIN_POLL_SECONDS = 180
# Lorenzo: "voglio evitare rug pull [...] token legittimi con progetti seri
# [...] market cap non oltre i 100.000 dollari". Vale anche per il pilota,
# che compra davvero: un candidato dello sniper passa dallo stesso filtro
# degli avvisi, e i listing gia' verificati entrano direttamente.
_SNIPER_ONCHAIN_MAX_MCAP_USD = 100_000.0
_PILOT_VERDICT_TTL_S = 6 * 3600
_pilot_verdicts: dict[str, tuple[float, dict]] = {}


async def _pilot_vet(candidate) -> dict:
    """Esito del filtro di legittimita' per un candidato dello sniper, tenuto
    qualche ora (un "wait" no: si riprova al giro dopo)."""
    key = f"{candidate.chain_id}:{candidate.token_address}"
    if key in onchain_outcomes.shamed():
        return {"status": "fail", "reason": "already rugged on us", "checks": []}
    cached = _pilot_verdicts.get(key)
    if cached and _time.time() - cached[0] < _PILOT_VERDICT_TTL_S:
        return cached[1]
    links = onchain_legit.merge_links({
        "websites": [candidate.website] if candidate.website else [],
        "socials": [s for s in candidate.socials if isinstance(s, dict)],
    })
    token = {"chain": candidate.chain_id, "symbol": candidate.symbol, "name": candidate.name,
             "token_address": candidate.token_address, "market_cap": candidate.market_cap,
             "liquidity_usd": candidate.liquidity_usd}
    async with httpx.AsyncClient(timeout=10.0) as client:
        verdict = await onchain_legit.assess(token, links, client)
    if verdict["status"] != "wait":
        _pilot_verdicts[key] = (_time.time(), verdict)
    return verdict


def _vetted_listing_candidates() -> list:
    """I listing gia' passati dal filtro, nella forma dei candidati dello
    sniper: arrivano prima e sono gia' verificati."""
    from app.sniper.cache import Candidate
    now = _time.time()
    out = []
    # Un rug gia' subito (outcomes.shame) non si ricompra: Lorenzo, "fammi sparire VISE".
    for item in onchain_outcomes.without_shamed(onchain_listings.recent()):
        born = item.get("created_at") or item.get("found_at") or now
        out.append(Candidate(
            chain_id=item["chain"], token_address=item.get("token_address") or "",
            pair_address=item.get("pair_address") or "", symbol=item["symbol"], name=item.get("name") or "",
            dex_id=item.get("dex") or "", pair_url=item.get("pair_url") or "", price_usd=0.0,
            market_cap=item.get("market_cap"), liquidity_usd=float(item.get("liquidity_usd") or 0),
            volume_h24=0.0, volume_h1=0.0, age_hours=(now - born) / 3600, buy_sell_ratio=0.0,
            website=item.get("website"), socials=item.get("socials") or [],
        ))
    return out


async def _sniper_onchain_bridge_loop() -> None:
    """Ponte fra lo sniper (scanner di nuovi arrivi DEX, gia' in piedi per
    tutte le chain che copre) e il wallet on-chain — Lorenzo: "la strategia
    trend follower [...] inglobata sul wallet, così da usarla
    automaticamente su dei ticker on chain sfruttando la logica dello
    sniper che già scansionava anche i nuovi arrivi".

    Non un secondo motore di segnali: questo ponte fa SOLO una cosa,
    aggiungere alla watchlist (upsert, mai un duplicato) i candidati dello
    sniper su Solana/Robinhood che passano i filtri minimi, con la
    strategia di Lorenzo attiva — poi e' _watchlist_alert_loop, sopra, a
    valutare il segnale ed eseguire, con gli STESSI guardrail di ogni
    altro ordine automatico. Per-utente e opt-in
    (User.sniper_autotrade_chains, spento di default): nessun utente vede
    la propria watchlist popolarsi senza averlo acceso lui stesso.

    Richiede telegram_chat_id configurato, come ogni altra entry — non e'
    un requisito nuovo di questo ponte, e' lo stesso di
    _watchlist_alert_loop sopra, che salta un'intera entry (alert
    incluso l'auto-trade) se l'owner non ha il chat_id impostato."""
    while True:
        await asyncio.sleep(_SNIPER_ONCHAIN_POLL_SECONDS)
        try:
            await _sniper_onchain_bridge_tick()
        except Exception:  # noqa: BLE001 — il ciclo non deve fermarsi del tutto
            logger.exception("sniper_onchain_bridge_loop: giro fallito")


async def _sniper_onchain_bridge_tick() -> None:
    """Un giro solo — separato dal `while True` sopra per essere testabile
    senza dover far girare un loop infinito, stesso schema di
    _check_limit_order/limit_order_loop."""
    users_by_chain: dict[str, list[User]] = {}
    for user in auth.list_users():
        for chain in user.sniper_autotrade_chains:
            users_by_chain.setdefault(chain, []).append(user)
    if not users_by_chain or not settings.sniper_enabled:
        return  # nessuno l'ha acceso, o lo sniper stesso e' spento: niente da leggere

    from app.sniper import cache as sniper_cache

    seen: dict[str, list[str]] = {chain: [] for chain in users_by_chain}
    accepted: dict[str, int] = dict.fromkeys(users_by_chain, 0)
    vetted = _vetted_listing_candidates()
    vetted_keys = {(c.chain_id, c.token_address) for c in vetted}
    for candidate in vetted + [c for c in sniper_cache.get_candidates()
                               if (c.chain_id, c.token_address) not in vetted_keys]:
        interested = users_by_chain.get(candidate.chain_id)
        if not interested:
            continue
        if (candidate.token_address or candidate.symbol) in seen[candidate.chain_id]:
            continue
        seen[candidate.chain_id].append(candidate.token_address or candidate.symbol)
        reason = None
        if candidate.liquidity_usd < _SNIPER_ONCHAIN_MIN_LIQUIDITY_USD:
            reason = f"liquidity ${candidate.liquidity_usd:,.0f} below ${_SNIPER_ONCHAIN_MIN_LIQUIDITY_USD:,.0f}"
        elif candidate.age_hours > _SNIPER_ONCHAIN_MAX_AGE_HOURS:
            reason = f"too old ({candidate.age_hours:.0f} h, max {_SNIPER_ONCHAIN_MAX_AGE_HOURS:.0f})"
        elif not candidate.token_address or not candidate.pair_address:
            reason = "token or pool address missing"
        elif not candidate.market_cap or candidate.market_cap > _SNIPER_ONCHAIN_MAX_MCAP_USD:
            reason = f"cap {'unknown' if not candidate.market_cap else f'${candidate.market_cap:,.0f}'} above ${_SNIPER_ONCHAIN_MAX_MCAP_USD:,.0f}"
        elif (candidate.chain_id, candidate.token_address) not in vetted_keys:
            try:
                verdict = await _pilot_vet(candidate)
            except Exception:  # noqa: BLE001 — un filtro che non risponde non fa passare niente
                logger.exception("sniper_onchain_bridge: filtro fallito su %s", candidate.symbol)
                verdict = {"status": "wait", "reason": "check failed"}
            if verdict["status"] != "ok":
                reason = verdict["reason"] or "does not pass the filter"
        if reason:
            onchain_pilot.record_rejection(
                candidate.chain_id, symbol=candidate.symbol, reason=reason,
                liquidity_usd=candidate.liquidity_usd, age_hours=candidate.age_hours,
            )
            continue
        symbol = f"{candidate.symbol}@{candidate.chain_id}:{candidate.pair_address}"
        for user in interested:
            if user.onchain_wallets.get(candidate.chain_id) is None:
                continue
            try:
                existing = next(
                    (e for e in watchlist_store.list_entries(user.id) if e.symbol == symbol), None,
                )
                if existing is not None:
                    continue  # gia' seguito da questo utente, niente da rifare
                # Il timeframe segue la storia del token (Lorenzo: "auto
                # aggiusta le candele col time frame corretto"): a 1d un token
                # di poche ore non avrebbe mai una candela chiusa.
                interval = onchain_radar.auto_interval(candidate.age_hours * 3600)
                probe = await dexscreener_provider.fetch_ohlcv(symbol, interval, 2)
                if probe.empty:
                    continue
                entry = watchlist_store.add_entry(
                    user.id, symbol, "dex", interval=interval, on_close=True,
                    base_token_address=candidate.token_address,
                )
                watchlist_store.set_strategy_enabled(entry.id, _SNIPER_ONCHAIN_STRATEGY, True)
                watchlist_store.set_auto_trade(entry.id, _SNIPER_ONCHAIN_STRATEGY, True)
                accepted[candidate.chain_id] += 1
                onchain_pilot.record_discovery(
                    user.id, symbol, name=candidate.name, token_address=candidate.token_address,
                    liquidity_usd=candidate.liquidity_usd, age_hours=candidate.age_hours,
                    score=f"{candidate.score_total}/{candidate.score_max}",
                )
                logger.info(
                    "sniper_onchain_bridge: %s aggiunto alla watchlist di %s (Lorenzo trend-follower)",
                    symbol, user.email,
                )
            except Exception:  # noqa: BLE001 — un candidato/utente che fallisce non deve bloccare gli altri
                logger.exception("sniper_onchain_bridge: errore su %s per %s", symbol, user.email)

    for chain, tokens in seen.items():
        onchain_pilot.record_scan(
            chain, seen=tokens, accepted=accepted[chain], interval_s=_SNIPER_ONCHAIN_POLL_SECONDS,
        )


# --- pausa per quota esaurita ------------------------------------------------
# Chiave = (asset_type, exchange) e non l'utente: la quota e' del piano, quindi
# e' condivisa; fermare solo chi ha incontrato l'errore lascerebbe gli altri a
# consumarne altri e a ricevere lo stesso messaggio a turno.
_quota_pause_until: dict[tuple[str, str], float] = {}
_quota_notified: dict[tuple[str, str, str], float] = {}


def _quota_key(asset_type: str, exchange: str) -> tuple[str, str]:
    return (asset_type, exchange if asset_type == "crypto" else "twelvedata")


def _quota_paused(asset_type: str, exchange: str) -> bool:
    return _quota_pause_until.get(_quota_key(asset_type, exchange), 0.0) > _time.time()


def _pause_for_quota(asset_type: str, exchange: str, exc: QuotaExceeded) -> None:
    wait = exc.retry_after_s or 3600
    _quota_pause_until[_quota_key(asset_type, exchange)] = _time.time() + wait
    logger.warning("quota esaurita su %s/%s: pausa di %ss", asset_type, exchange, wait)


# Irraggiungibilita': quasi sempre passeggera. Si conta quante volte di fila
# succede, invece di reagire alla prima — un errore TLS che al giro dopo non si
# ripresenta non merita un messaggio, e mandarlo comunque e' come e' arrivata
# la segnalazione su MEXC.
_UNREACHABLE_BACKOFF_S = (300, 600, 1200, 3600)
_UNREACHABLE_SPEAK_AFTER = 3        # ~15 minuti di buco prima di dire qualcosa
_unreachable_streak: dict[tuple[str, str], int] = {}


def _pause_for_unreachable(asset_type: str, exchange: str) -> int:
    """Mette in pausa quel provider e restituisce da quante volte di fila fallisce."""
    key = _quota_key(asset_type, exchange)
    streak = _unreachable_streak.get(key, 0) + 1
    _unreachable_streak[key] = streak
    wait = _UNREACHABLE_BACKOFF_S[min(streak - 1, len(_UNREACHABLE_BACKOFF_S) - 1)]
    _quota_pause_until[key] = _time.time() + wait
    logger.warning("provider %s/%s non raggiungibile (%d di fila): pausa di %ss",
                   asset_type, exchange, streak, wait)
    return streak


def _provider_recovered(asset_type: str, exchange: str) -> None:
    """Una lettura riuscita azzera il conteggio: altrimenti un guasto di ieri
    farebbe scattare l'allarme al primo inciampo di domani."""
    key = _quota_key(asset_type, exchange)
    if _unreachable_streak.pop(key, 0):
        _quota_notified.pop((None,) + key, None)


def _notify_unreachable_once(asset_type: str, exchange: str, streak: int) -> bool:
    """Si parla solo se il buco dura, e una volta sola finche' dura."""
    if streak < _UNREACHABLE_SPEAK_AFTER:
        return False
    key = (None,) + _quota_key(asset_type, exchange)
    if _quota_notified.get(key):
        return False
    _quota_notified[key] = _time.time()
    return True


def _notify_quota_once(owner_id: str, asset_type: str, exchange: str) -> bool:
    """Un messaggio per utente e per pausa, non uno per ticker per minuto."""
    k = (owner_id,) + _quota_key(asset_type, exchange)
    until = _quota_pause_until.get(_quota_key(asset_type, exchange), 0.0)
    if _quota_notified.get(k, 0.0) >= until:
        return False
    _quota_notified[k] = until
    return True


_LAST_SHA_FILE = Path(__file__).resolve().parent.parent / "data" / "last_deploy_sha.txt"
_GITHUB_REPO = "lorenzocorazzi-wq/liquidity-analysis-engine"


async def _notify_deploy() -> None:
    # Nessuna delle uscite qui sotto era loggata: un token mancante o
    # scaduto spegneva l'avviso di deploy senza lasciare traccia da nessuna
    # parte, ne' nei log del container ne' altrove — lo stesso tipo di
    # silenzio che il progetto ha gia' imparato a non tollerare (seed-0025).
    if not settings.telegram_bot_token:
        logger.warning("Notifica deploy saltata: TELEGRAM_BOT_TOKEN non configurato")
        return
    if not settings.github_api_token:
        logger.warning("Notifica deploy saltata: GITHUB_API_TOKEN non configurato")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/commits",
                headers={"Authorization": f"Bearer {settings.github_api_token}",
                         "Accept": "application/vnd.github+json"},
                params={"per_page": 100},
            )
            r.raise_for_status()
            commits = r.json()
    except Exception as exc:
        logger.warning("Notifica deploy saltata: richiesta a GitHub fallita — %s", exc)
        return
    if not commits:
        return
    current_sha = commits[0]["sha"]
    last_sha = _LAST_SHA_FILE.read_text(encoding="utf-8").strip() if _LAST_SHA_FILE.exists() else ""
    _LAST_SHA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_SHA_FILE.write_text(current_sha, encoding="utf-8")
    if last_sha == current_sha:
        return  # restart, not a new deploy
    new_commits = [c for c in commits if c["sha"] != last_sha]
    if last_sha:
        try:
            idx = next(i for i, c in enumerate(commits) if c["sha"] == last_sha)
            new_commits = commits[:idx]
        except StopIteration:
            pass  # piu' di 100 commit dall'ultimo avviso: si mandano tutti quelli letti
    lines = []
    for c in new_commits:
        # I messaggi di commit sono in inglese per convenzione (CLAUDE.md), ma
        # questa notifica la legge Lorenzo: `italian_line` prende il trailer
        # "Aggiornamento:" quando c'e' e ricade sul titolo quando manca.
        msg = _html.escape(weekly_recap.italian_line(c["commit"]["message"]), quote=False)
        sha_short = c["sha"][:7]
        lines.append(f"• <code>{sha_short}</code> {msg}")
    changelog = "\n".join(lines) if lines else "• (nessun commit trovato)"
    text = f"🚀 <b>Nuovo deploy completato</b>\n\n<b>Modifiche:</b>\n{changelog}"
    await telegram.send_message(text)


_LAST_RECAP_FILE = Path(__file__).resolve().parent.parent / "data" / "last_weekly_recap.txt"
_RECAP_POLL_SECONDS = 1800


async def _weekly_recap_loop() -> None:
    """Riepilogo settimanale sul canale di sistema, lunedi' mattina.

    Vive qui dentro e non in un cron sul server per la stessa ragione per cui
    ci vive il watcher: un lavoro che sta nel repo si riavvia col deploy, si
    legge nel codice, e non sopravvive per sbaglio su una macchina che nessuno
    guarda piu'. La settimana gia' inviata e' su file, quindi un riavvio di
    lunedi' mattina non lo rimanda una seconda volta.
    """
    while True:
        # Il controllo viene PRIMA dell'attesa: un riepilogo che tocca a
        # lunedi' alle 9 non deve arrivare alle 9 e mezza solo perche' il
        # deploy e' capitato un minuto prima.
        if settings.telegram_bot_token and settings.github_api_token:
            await _maybe_send_recap()
        await asyncio.sleep(_RECAP_POLL_SECONDS)


async def _maybe_send_recap() -> None:
    now = int(_time.time())
    last = _LAST_RECAP_FILE.read_text(encoding="utf-8").strip() if _LAST_RECAP_FILE.exists() else ""
    # Alla primissima attivazione parte subito, senza aspettare lunedi': una
    # funzione di notifica che non si vede finche' non capita il giorno giusto
    # e' indistinguibile da una che non funziona — ed e' esattamente il dubbio
    # che c'e' stato con l'avviso di deploy.
    if last and not weekly_recap.should_send(now, last):
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            since = _dt.datetime.fromtimestamp(now - 7 * 86400, tz=_dt.timezone.utc).isoformat()
            r = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/commits",
                headers={"Authorization": f"Bearer {settings.github_api_token}",
                         "Accept": "application/vnd.github+json"},
                params={"per_page": 100, "since": since},
            )
            r.raise_for_status()
            messages = [c["commit"]["message"] for c in r.json()]
    except Exception as exc:                      # noqa: BLE001
        logger.warning("Riepilogo settimanale saltato: richiesta a GitHub fallita — %s", exc)
        return

    text = weekly_recap.build_recap(messages, now)
    # La settimana si segna comunque: una settimana senza niente da dire e'
    # gestita, non e' un tentativo fallito da ripetere ogni mezz'ora.
    _LAST_RECAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_RECAP_FILE.write_text(weekly_recap.current_week_key(now), encoding="utf-8")
    if text:
        await telegram.send_message(text)


async def _watchdog_loop() -> None:
    """Sends a Telegram alert to all Telegram-enabled users if the watchlist
    loop goes silent for more than _WATCHDOG_STALE_S seconds."""
    await asyncio.sleep(_WATCHDOG_STALE_S)
    while True:
        await asyncio.sleep(_WATCHDOG_STALE_S)
        if not settings.telegram_bot_token:
            continue
        try:
            raw = _WATCHDOG_FILE.read_text()
            last_ts = json.loads(raw).get("ts", 0)
        except Exception:
            last_ts = 0
        if _time.time() - last_ts < _WATCHDOG_STALE_S:
            continue
        notified: set[str] = set()
        for entry in watchlist_store.list_entries():
            owner = auth.get_user(entry.owner_id)
            if owner is None or not owner.telegram_chat_id or not _telegram_allowed(owner):
                continue
            if owner.id in notified:
                continue
            notified.add(owner.id)
            _fire(telegram.send_to(
                owner.telegram_chat_id,
                "⚠️ <b>Watchdog</b>: il loop degli alert è silenzioso da più di "
                f"{int(_WATCHDOG_STALE_S // 60)} minuti. Verificare lo stato del server.",
            ))


def _deepseek_memory() -> list[str]:
    """Cosa il sistema ha imparato: diario dello scanner, revisioni AI, laboratorio.
    Ogni pezzo in un try/except: se una parte fallisce la chat continua."""
    out: list[str] = []

    def _line(s: str) -> str:
        return s[:300]

    try:
        les = onchain_outcomes.lessons()
        out.append(_line(f"Memoria scanner: tracciati {les['tracked']}, aperti {les['open']}"))
        for n in (les.get("notes") or [])[:8]:
            out.append(_line(f"  - {n}"))
        for e in (les.get("slipped") or [])[:5]:
            out.append(_line(f"  scivolato: {e['symbol']} ({e['chain']}) max {e['max_ret']} min {e['min_ret']}"))
        for e in (les.get("missed") or [])[:5]:
            out.append(_line(f"  perso: {e['symbol']} ({e['chain']}) max {e['max_ret']} min {e['min_ret']}"))
    except Exception:
        pass

    try:
        for r in (onchain_ai_review._state.get("reviews") or [])[:7]:
            acts = " | ".join(r.get("actions") or [])
            out.append(_line(f"Revisione AI {r.get('at')}: {r.get('summary')} — {acts}"))
    except Exception:
        pass

    try:
        from app.trading.onchain import backtest_lab
        lab = backtest_lab.summary()
        out.append(_line(f"Laboratorio: {lab['rows']} righe, {lab['gone']} sparite, {lab['with_trades']} con trade"))
        for dec, b in (lab.get("by_decision") or {}).items():
            out.append(_line(f"  {dec}: n {b['n']}, best {b['best_median']}, hold {b['hold_median']}, "
                             f"raddoppiati {b['doubled']}, rug {b['rugs']}"))
    except Exception:
        pass

    return out


async def _deepseek_context(chat_id: str, user: User | None = None) -> str:
    """What the Telegram assistant knows: the system, the latest alerts, and
    (when the chat belongs to an account) that account's wallet and autotrader.
    `user`: the account asking from the site (/api/agent/ask)."""
    parts = []
    for kind in ("meme", "gem"):
        b = onchain_alert_track.board(kind, limit=12)
        sysb, live = b["system"], b["system"].get("live") or {}
        parts.append(f"[{kind}] sistema scelto: {sysb['chosen']} ({sysb['decided_by']}); su {live.get('n', 0)} alert "
                     f"mediana {live.get('median')}, in profitto {live.get('positive')}, posizioni aperte {live.get('holding')}")
        for it in b["items"]:
            sv = it.get("system") or {}
            parts.append(f"  {it['symbol']} ({it['chain']}) {it['age_h']}h fa: ora {it['now']}, max {it['max']}, "
                         f"sistema {sv.get('state')} ret {sv.get('ret')}{' RUG' if it['gone'] else ''}")
    user = user or next((u for u in auth.list_users() if u.telegram_chat_id and u.telegram_chat_id == chat_id), None)
    if user:
        try:
            snap = await asyncio.wait_for(_wallet_snapshot(user), 20)
            parts.append(f"Wallet di chi scrive: totale ${snap['total']}, cassa ${snap['cash']}, per catena {snap['by_chain']}, "
                         f"posizioni {[(p['symbol'], p['chain'], round(p['value_usd'] or 0, 2)) for p in snap['positions'] if not p['hidden']][:15]}")
        except Exception:
            parts.append("Wallet di chi scrive: non letto ora.")
        v = onchain_autotrader.view(user.id)
        parts.append(f"Autotrader: modo {v['mode']}, quota {v['pct']}%, posizione {v['position']}, simulato {v['sim']}, "
                     f"ultimi trade {[(t.get('side'), t.get('symbol'), t.get('ret'), t.get('why')) for t in v['trades'][:8]]}")
    parts.extend(_deepseek_memory())
    parts.append("Valori 'ret'/'now'/'max' sono frazioni: 0.25 = +25%.")
    return "\n".join(parts)


def _deepseek_chats() -> set[str]:
    allowed = {c.strip() for c in (settings.telegram_chat_ids + "," + settings.telegram_dev_chat_ids).split(",") if c.strip()}
    return allowed | {u.telegram_chat_id for u in auth.list_users() if u.telegram_chat_id}


async def _telegram_chat_loop() -> None:
    """Lorenzo: "mettiamo possibilita' di parlare con deepseek su telegram?".
    Long polling (the bot has no webhook); only chats the site knows."""
    if not settings.telegram_bot_token or not settings.deepseek_api_key:
        return
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    offset, bot = None, ""
    async with httpx.AsyncClient(timeout=70.0) as client:
        while True:
            try:
                if not bot:
                    bot = ((await client.get(f"{base}/getMe")).json().get("result") or {}).get("username") or ""
                r = await client.get(f"{base}/getUpdates", params={"timeout": 50, "offset": offset,
                                                                   "allowed_updates": '["message"]'})
                data = r.json()
                if not data.get("ok"):
                    logger.warning("telegram getUpdates: %s", data.get("description"))
                    await asyncio.sleep(60)
                    continue
                for up in data.get("result") or []:
                    offset = up["update_id"] + 1
                    msg = up.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id") or "")
                    text = deepseek_desk.wants_answer(msg, bot)
                    if text and chat_id in _deepseek_chats():
                        _fire(_telegram_reply(chat_id, text))
            except Exception:
                logger.exception("telegram chat loop")
                await asyncio.sleep(10)


def _deepseek_tools() -> dict:
    """What the Telegram assistant can look up by itself in the database of
    every token we watched (rug_memory over outcomes.py)."""
    from app.trading.onchain import rug_memory
    str_arg = lambda d: {"type": "string", "description": d}  # noqa: E731
    return {
        "cerca_token": ({"name": "cerca_token", "description": "Find a watched token by symbol or address: when we saw it, what we decided, what happened (rug, hours to rug, best/worst return), its numbers at the time.",
                         "parameters": {"type": "object", "properties": {"query": str_arg("symbol like PEPE or address prefix")}, "required": ["query"]}},
                        lambda query: rug_memory.lookup(query)),
        "rug_simili": ({"name": "rug_simili", "description": "Past tokens most similar to a watched one (by its numbers and checks) and what happened to them.",
                        "parameters": {"type": "object", "properties": {"query": str_arg("symbol or address of the token to compare")}, "required": ["query"]}},
                       lambda query: _similar_to(query)),
        "circostanze_rug": ({"name": "circostanze_rug", "description": "How rug pulls differed from the other tokens: rate, hours to rug, how they happened, numbers and checks of rugs vs others.",
                             "parameters": {"type": "object", "properties": {"chain": str_arg("optional: solana, robinhood, bsc, base, ethereum")}}},
                            lambda chain=None: rug_memory.circumstances(chain or None)),
        # Lorenzo, 27/09: "aumentiamo i poteri a deepseek, dai accesso completo
        # al database cosi' da telegram posso interrogarlo su tutto". Read-only.
        "cerca_progetti": ({"name": "cerca_progetti", "description": "Search the Gem Finder's project database (every project ever met): symbol, name, address or site; optional status new/analyzing/studied/ranked/rejected. Returns score, cap, liquidity, status and why.",
                            "parameters": {"type": "object", "properties": {"query": str_arg("text to search, empty for all"), "status": str_arg("optional status"),
                                                                            "limit": {"type": "integer", "description": "max 50"}}}},
                           lambda query="", status="", limit=20: onchain_projects_db.search(query or "", status or "", "score", min(int(limit or 20), 50))),
        "classifica_gemme": ({"name": "classifica_gemme", "description": "The live gem board (quality low caps) by score: rank, symbol, score, pillars, cap, verdict.",
                              "parameters": {"type": "object", "properties": {"n": {"type": "integer", "description": "how many, max 50"}}}},
                             lambda n=15: [{k: e.get(k) for k in ("rank", "symbol", "chain", "token_address", "score", "market_cap", "liquidity_usd", "change_24h", "verdict", "summary", "early")}
                                           for e in (onchain_leaderboard.board().get("entries") or [])[:min(int(n or 15), 50)]]),
        "alert_ora": ({"name": "alert_ora", "description": "Meme alerts now: fired in the last 48h (with the system's state and DeepSeek's risk), about to fire, and old ones coming back.",
                       "parameters": {"type": "object", "properties": {}}},
                      lambda: {"fired": onchain_alert_track.feed("meme")[:25], "about_to_fire": onchain_listings.upcoming(),
                               "coming_back": onchain_alert_track.reviving("meme")}),
        "sistema": ({"name": "sistema", "description": "The trading system: strategies compared (median, win share, closed alerts) and which one is in use.",
                     "parameters": {"type": "object", "properties": {}}},
                    lambda: onchain_alert_track.system_board("meme")),
        "scartati": ({"name": "scartati", "description": "Tokens the listing filter rejected lately, with the reason.",
                      "parameters": {"type": "object", "properties": {"n": {"type": "integer", "description": "max 40"}}}},
                     lambda n=20: [{k: r.get(k) for k in ("symbol", "chain", "token_address", "reason", "at", "market_cap", "liquidity_usd")}
                                   for r in onchain_listings.rejected()[:min(int(n or 20), 40)]]),
        "memoria": ({"name": "memoria", "description": "What the memory learned: rug rate of passed vs rejected tokens, which checks predict rugs (AUC), whether the model is active, and notes.",
                     "parameters": {"type": "object", "properties": {}}},
                    lambda: {k: v for k, v in onchain_outcomes.lessons().items() if k in ("tracked", "passed", "rejected", "signals", "model", "notes")}),
        "segnala_bug": ({"name": "segnala_bug", "description": "File a problem on the site's report board, for Claude (the developer) to verify and fix. Use it when the user asks, or when the data you read looks wrong or inconsistent. Say exactly what you saw: which data, which numbers, why it is wrong.",
                         "parameters": {"type": "object", "properties": {"titolo": str_arg("short title, in Italian"), "dettaglio": str_arg("what you saw and why it looks wrong, with the numbers"),
                                                                         "priorita": str_arg("bassa, media or alta")}, "required": ["titolo", "dettaglio"]}},
                        _deepseek_report),
        "ultimi_rug": ({"name": "ultimi_rug", "description": "The most recent rug pulls in the database, newest first.",
                        "parameters": {"type": "object", "properties": {"n": {"type": "integer", "description": "how many, max 20"}}}},
                       lambda n=10: sorted((c for c in rug_memory.cases() if c["outcome"] == "rug"), key=lambda c: -c["at"])[:min(int(n or 10), 20)]),
        # Lorenzo, 27/09: "voglio potergli chiedere TUTTO, dagli completo
        # accesso come risposte". Read-only tools over everything the site
        # shows. Each helper imports its module locally (main.py does not
        # import them at top level).
        "strategie": ({"name": "strategie", "description": "The sniper Strategies tab: watched meme tokens with age, whether they are gone, and the alert backtest (label, return, trades, wins, state, done).",
                       "parameters": {"type": "object", "properties": {"n": {"type": "integer", "description": "how many, max 30"}}}},
                      lambda n=30: _deepseek_strategie(n)),
        "backtest_gemme": ({"name": "backtest_gemme", "description": "Gem backtests: the lab status plus, for the top entries, symbol, chain, score and the best/hold summary of their backtest.",
                            "parameters": {"type": "object", "properties": {"n": {"type": "integer", "description": "how many top entries, max 50"}}}},
                           lambda n=15: _deepseek_backtest_gemme(n)),
        "laboratorio": ({"name": "laboratorio", "description": "The backtest lab summary: what it is running and what it found.",
                         "parameters": {"type": "object", "properties": {}}},
                        lambda: _deepseek_laboratorio()),
        "review_giornaliere": ({"name": "review_giornaliere", "description": "The last daily AI reviews: when, the summary and the actions.",
                               "parameters": {"type": "object", "properties": {}}},
                              lambda: _deepseek_review_giornaliere()),
        "scanner": ({"name": "scanner", "description": "The live scanner feed snapshot (first 20 rows).",
                     "parameters": {"type": "object", "properties": {}}},
                    lambda: _deepseek_scanner()),
        "diario_agente": ({"name": "diario_agente", "description": "The agent's diary: recent activity items, what is done, today's work and the priority.",
                           "parameters": {"type": "object", "properties": {}}},
                          lambda: _deepseek_diario_agente()),
        "token": ({"name": "token", "description": "The alert(s) on a token by symbol or address prefix, plus what the rug memory knows about it.",
                   "parameters": {"type": "object", "properties": {"query": str_arg("symbol like PEPE or address prefix")}, "required": ["query"]}},
                  lambda query: _deepseek_token(query)),
        "come_funziona": ({"name": "come_funziona", "description": "How the app works, from its own source documentation: what each part does and why, with the real numbers (thresholds, windows, caps, timeframes). Use it for every question on how something works: strategies (Trend EMA, Trend HF, RSI+BB), entries and exits, timeframe ladder, Top 50 and gem scores, reviews and judge rules, filters, simulated wallet, layer engine, alerts. Search with a few keywords (Italian or English, module names work); empty query lists every module. Explain only what the passages say.",
                           "parameters": {"type": "object", "properties": {"query": str_arg("keywords, e.g. 'trend EMA ingresso uscita' or 'slippage portafoglio'")}}},
                          lambda query="": _app_guide().search(query)),
        "portafoglio_top50": ({"name": "portafoglio_top50", "description": "The simulated Top 50 wallet ($10,000 replayed on every trade of the Top 50 backtests): equity, return, drawdown, win rate, open positions and the last closed trades.",
                               "parameters": {"type": "object", "properties": {}}},
                              lambda: _deepseek_top50()),
        "layer_engine": ({"name": "layer_engine", "description": "The layer 0/1/2 chains kept by the layer engine ($500k-$100M cap), best score first: CoinGecko id, layer type, score, status, exchange pair. Details of one: cerca_progetti with its name.",
                          "parameters": {"type": "object", "properties": {"n": {"type": "integer", "description": "how many, max 20"}}}},
                         lambda n=10: _deepseek_layers(n)),
    }


def _app_guide():
    from app.notify import app_guide
    return app_guide


def _deepseek_top50() -> dict:
    """The Top 50 wallet without live marks (tools are sync): the last
    computed view if the site built one, else a replay at cost."""
    from app.trading.onchain import sim_backtest
    d = _top_wallet_cache.get("data") or sim_backtest.replay(sim_backtest.ledger_tokens(), sim_backtest.DEFAULT_CAPITAL)
    keep = ("symbol", "chain", "at", "entry_price", "exit_at", "ret", "stake", "value", "pnl", "strategy")
    return {**{k: d.get(k) for k in ("capital", "equity", "cash", "ret", "max_dd", "trades", "wins", "win_rate",
                                       "skipped", "slippage_usd", "tokens", "from")},
            "open": [{k: r.get(k) for k in keep if k in r} for r in (d.get("open") or [])[:15]],
            "closed_last": [{k: r.get(k) for k in keep if k in r} for r in (d.get("closed") or [])[:15]]}


def _deepseek_layers(n: int = 10) -> dict:
    snap = onchain_layers.snapshot()
    kept = [x for x in snap.get("found") or [] if x.get("status") != "rejected"]
    return {"last_run": snap.get("last_run"), "kept": len(kept),
            "found": [{k: x.get(k) for k in ("id", "type", "score", "status", "why", "cex")}
                      for x in kept[:min(int(n or 10), 20)]]}


def _deepseek_strategie(n: int = 30) -> list:
    from app.trading.onchain import alert_track as onchain_alert_track
    items = onchain_alert_track.board("meme", limit=30).get("items") or []
    out = []
    for it in items[:min(int(n or 30), 30)]:
        bt = it.get("on_alert") or {}
        out.append({"symbol": it.get("symbol"), "chain": it.get("chain"), "age_h": it.get("age_h"),
                    "gone": it.get("gone"),
                    "on_alert": {k: bt.get(k) for k in ("label", "ret", "trades", "wins", "state", "done")} if bt else None})
    return out


def _deepseek_backtest_gemme(n: int = 15) -> dict:
    from app.trading.onchain import strategy_lab as onchain_strategy_lab
    top = _top_entries(50)
    out = []
    for e in top[:min(int(n or 15), 50)]:
        out.append({"symbol": e.get("symbol"), "chain": e.get("chain"), "score": e.get("score"),
                    "summary": _lab_summary(onchain_strategy_lab.get(e["chain"], e["token_address"]))})
    return {"status": onchain_strategy_lab.status(top), "entries": out}


def _deepseek_laboratorio() -> dict:
    from app.trading.onchain import backtest_lab
    return backtest_lab.summary()


def _deepseek_review_giornaliere() -> list:
    from app.trading.onchain import ai_review as onchain_ai_review
    reviews = (onchain_ai_review._state or {}).get("reviews") or []
    return [{k: r.get(k) for k in ("at", "summary", "actions")} for r in reviews[:7]]


def _deepseek_scanner() -> dict:
    from app.trading.onchain import scan_feed
    snap = scan_feed.snapshot()
    if isinstance(snap, dict) and isinstance(snap.get("feed"), list):
        snap = {**snap, "feed": snap["feed"][:20]}
    return snap


def _deepseek_diario_agente() -> dict:
    from app.notify import deepseek_desk
    act = deepseek_desk.activity()
    if isinstance(act, dict):
        act = {**act, "items": (act.get("items") or [])[:20]}
    return act


def _deepseek_token(query: str) -> dict:
    from app.trading.onchain import alert_track as onchain_alert_track
    from app.trading.onchain import rug_memory
    q = (query or "").strip().lower()
    alerts = []
    for a in (onchain_alert_track._alerts or {}).values():
        sym = (a.get("symbol") or "").lower()
        addr = (a.get("token_address") or "").lower()
        if sym == q or (q and addr.startswith(q)):
            alerts.append({k: a.get(k) for k in ("symbol", "chain", "token_address", "at", "entry", "last", "gone", "alert_bt")})
        if len(alerts) >= 5:
            break
    return {"alerts": alerts, "rug_memory": rug_memory.lookup(query)}


_DEEPSEEK_REPORTS_PER_DAY = 10


def _deepseek_report(titolo: str, dettaglio: str, priorita: str = "media") -> dict:
    """DeepSeek writes on the report board; Claude reads it at the start of
    every conversation (CLAUDE.md) and verifies it on the code before acting."""
    today = _time.strftime("%Y-%m-%d")
    mine = [i for i in issues_store.list_issues() if (i.get("author") or "").startswith("DeepSeek")
            and _time.strftime("%Y-%m-%d", _time.localtime(i.get("created_at") or 0)) == today]
    if len(mine) >= _DEEPSEEK_REPORTS_PER_DAY:
        return {"error": "daily limit of reports reached"}
    entry = issues_store.add_issue(titolo, dettaglio, priorita if priorita in ("bassa", "media", "alta") else "media",
                                   author="DeepSeek (Telegram)")
    deepseek_desk.note("report", f"Filed a report: {titolo[:80]}")
    return {"ok": True, "id": entry["id"]}


def _similar_to(query: str) -> dict:
    from app.trading.onchain import outcomes as _oc, rug_memory
    hits = rug_memory.lookup(query, limit=1)
    if not hits:
        return {"error": "token not in the database"}
    e = _oc._journal.get(f"{hits[0]['chain']}:{hits[0]['token']}") or {}
    pool = [c for c in rug_memory.cases() if c["token"] != hits[0]["token"]]
    return {"token": hits[0], "similar": rug_memory.similar(e.get("features") or {}, hits[0]["chain"], k=8, pool=pool)}


async def _telegram_reply(chat_id: str, text: str) -> None:
    deepseek_desk.note("chat", "Answering a question on Telegram")
    try:
        # Who may give DeepSeek standing instructions: the site's own chats and
        # the admins' personal ones, never other users.
        admins = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
        owner_chats = {c.strip() for c in settings.telegram_chat_ids.split(",") if c.strip()} | {
            u.telegram_chat_id for u in auth.list_users()
            if u.telegram_chat_id and (getattr(u, "email", "") or "").lower() in admins}
        reply = await deepseek_desk.answer(chat_id, text, await _deepseek_context(chat_id), tools=_deepseek_tools(),
                                           can_teach=str(chat_id) in owner_chats)
    except Exception as exc:
        logger.exception("deepseek chat")
        reply = f"DeepSeek non risponde ora ({type(exc).__name__})."
    await telegram.send_to(chat_id, _html.escape(reply, quote=False))


async def _ai_scan_loop() -> None:
    """DeepSeek's shadow judgement on each meme alert (ai_scan.py)."""
    await asyncio.sleep(90)
    while True:
        try:
            await onchain_ai_scan.run_once()
        except Exception:
            logger.exception("ai scan")
        await asyncio.sleep(onchain_ai_scan.EVERY_S)


async def _deepseek_recap_loop() -> None:
    """Lorenzo: "voglio un riepilogo giornaliero di quanti token ci ha fatto
    risparmiare, su telegram". Every evening at deepseek_desk.RECAP_HOUR."""
    while True:
        try:
            day = deepseek_desk.recap_due(_time.time())
            if day:
                rows = dict(deepseek_desk._state["days"].get(day) or {})
                rows["pr_review"] = await deepseek_desk.pr_review_usage(*deepseek_desk.day_bounds(day))
                await telegram.send_message(deepseek_desk.recap_text(day, rows) + "\n\n" + onchain_ai_scan.recap_line())
                deepseek_desk.mark_sent(day)
        except Exception:
            logger.exception("deepseek recap")
        await asyncio.sleep(600)


async def _onchain_reconcile_loop() -> None:
    """Live orders stay 'submitted' until the chain confirms them (audit
    29/09, S04): this is what confirms them, at startup and every 20 s."""
    from app.trading.onchain import reconciliation as onchain_reconciliation
    while True:
        try:
            await onchain_reconciliation.reconcile_all()
        except Exception:  # noqa: BLE001
            logger.exception("onchain reconcile loop")
        await asyncio.sleep(20)


@asynccontextmanager
def _sim_reset_once() -> None:
    """Lorenzo, 30/09: "devi fare un reset completo di tutti gli account e
    far si' che l'importo sia 10.000". Once: the old simulated books are
    copied aside first, then every account's goes back to $10,000."""
    import shutil
    from app.trading.onchain.storage import data_file
    marker = data_file("sim_reset_2026-09-30.done")
    if marker.exists():
        return
    src = onchain_autotrader._STORE_FILE
    try:
        if src.exists():
            shutil.copy2(src, data_file("onchain_autotrader.before-sim-reset-2026-09-30.json"))
        n = onchain_autotrader.reset_all_sim(10_000.0)
        marker.write_text(f"{_time.time()} {n}\n", encoding="utf-8")
        logger.info("sim reset: %d simulated books back to $10,000", n)
    except OSError:
        logger.exception("sim reset: backup failed, nothing reset")


async def lifespan(app: FastAPI):
    _sim_reset_once()
    # Audit B04: the event loop keeps only weak references to tasks, so a
    # create_task() nobody holds can be garbage-collected mid-run (the order
    # reconciler was one) and nothing cancelled it at shutdown.
    background = [asyncio.create_task(_notify_deploy()),
                  asyncio.create_task(_onchain_reconcile_loop())]
    trading = settings.onchain_trading_enabled
    if trading:
        from app.trading.onchain import twap_jobs as onchain_twap_jobs_resume
        background.append(asyncio.create_task(onchain_twap_jobs_resume.resume_running_twap_jobs()))
    t1 = asyncio.create_task(_watchlist_alert_loop())
    if trading:
        background += [asyncio.create_task(limit_order_loop()),
                       asyncio.create_task(_sniper_onchain_bridge_loop()),
                       asyncio.create_task(_ai_scan_loop())]
    t4 = asyncio.create_task(_weekly_recap_loop())
    background.append(asyncio.create_task(_telegram_chat_loop()))
    background.append(asyncio.create_task(_deepseek_recap_loop()))
    t5 = asyncio.create_task(_watchdog_loop())
    onchain_leaderboard.set_top_source(_top_entries)
    # Gem research (leaderboard, outcomes, layers, weekly re-check, studies):
    # one run at 22:00 with onchain_nightly_enabled (Lorenzo, 07/10: "tutto in
    # una tranche a fine serata"), the old loops otherwise. Trading,
    # strategies and the Meme Sniper only with onchain_trading_enabled (off).
    from app.trading.onchain import nightly as onchain_nightly
    research = {
        "nightly": lambda: onchain_nightly.nightly_loop(lambda: _top_entries(ARENA_POOL)),
        "leaderboard": onchain_leaderboard.leaderboard_loop,
        "sweep": lambda: onchain_deep_recheck.sweep_loop(
            lambda: [f"{e['chain']}:{e['token_address']}" for e in _top_entries(50)]),
        "outcomes": onchain_outcomes.outcomes_loop,
        "layers": onchain_layers.layers_loop,
        "studies": deepseek_studies.studies_loop,
    }
    radar_tasks = [asyncio.create_task(research[name]()) for name in
                   onchain_nightly.loop_plan(settings.onchain_radar_enabled, settings.onchain_nightly_enabled)]
    if settings.onchain_radar_enabled and trading:
        radar_tasks += [asyncio.create_task(onchain_radar.radar_loop()),
                        asyncio.create_task(onchain_listings.listings_loop()),
                        asyncio.create_task(onchain_radar.listings_backtest_loop()),
                        asyncio.create_task(onchain_backtest_lab.lab_loop()),
                        asyncio.create_task(onchain_strategy_lab.lab_loop(_top_entries)),
                        asyncio.create_task(onchain_candle_archive.archive_loop(lambda: _top_entries(50))),
                        asyncio.create_task(onchain_ladder_watch.watch_loop()),
                        asyncio.create_task(_onchain_signals_loop()),
                        asyncio.create_task(_wealth_loop())]
    sniper_tasks: list[asyncio.Task] = []
    if settings.sniper_enabled and trading:
        from app.sniper import cache as sniper_cache
        from app.sniper import agents as sniper_agents
        from app.sniper.cex_check import enrichment_loop
        sniper_tasks.append(asyncio.create_task(sniper_cache.polling_loop()))
        sniper_tasks.append(asyncio.create_task(enrichment_loop()))
        sniper_tasks.extend(await sniper_agents.start_all())
    yield
    t1.cancel()
    t4.cancel()
    t5.cancel()
    for t in radar_tasks:
        t.cancel()
    for t in sniper_tasks:
        t.cancel()
    for t in background:
        t.cancel()


app = FastAPI(title="Liquidity Analysis Engine", lifespan=lifespan)
app.include_router(quant_router(auth.get_current_user))

# ── Cancello di autenticazione ────────────────────────────────────────────
#
# Il sito e' su internet, e per mesi 33 endpoint hanno risposto a chiunque
# senza credenziali. Fra questi:
#
#   - /api/optimize e /api/analyze, che occupano un core per minuti. Su una
#     macchina a due core, due richieste anonime bastavano a rendere il sito
#     inutilizzabile e ad affamare il ciclo che manda gli avvisi.
#   - DELETE /api/pinescript/{id}, che cancellava gli script di chiunque
#     senza chiedere chi fosse.
#   - POST /api/sniper/scan, che faceva partire scansioni esterne a comando.
#
# La difesa e' un cancello unico invece di 33 firme da correggere a mano: un
# endpoint nuovo nasce protetto, e dimenticarsi di proteggerlo non e' piu'
# possibile. Chi vuole aprire una porta deve scriverlo qui, dove si legge
# tutto insieme.
#
# Le pagine restano pubbliche: servono a mostrare il modulo di accesso.
_PUBLIC_PATHS = frozenset({
    "/",                    # la pagina, che poi chiede di accedere
    "/sniper",
    "/engn",                # la presentazione del progetto e del token, per chi non ha un account
    # Le tre pagine del wallet on-chain: la shell HTML deve caricarsi sempre,
    # come "/" — sono le singole chiamate API al loro interno (protette da
    # auth.get_current_user) a decidere se mostrare dati o un errore. Senza
    # questa riga onchain-approve.html non si sarebbe mai potuta apire da un
    # telefono che non ha MAI fatto login (il caso normale di chi scansiona
    # il QR), non solo onchain-app.html vista da loggati.
    "/onchain-app.html",
    "/onchain-approve.html",
    "/onchain-passkey-setup.html",
    "/favicon.ico",
    "/robots.txt",
    "/api/health",          # per il controllo di salute del container
    "/api/version",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/logout",
    "/api/auth/me",         # il frontend chiede "chi sono" prima di sapere se e' entrato
    "/ospite",              # link ospite: controlla il token da se' (app/guest.py)
    "/api/issues",          # la bacheca: CLAUDE.md dice di leggerla con un curl senza
                            # login a inizio sessione — il gate globale (seed-0031) la
                            # bloccava da quando e' stato introdotto, senza che nessuna
                            # sessione se ne accorgesse perche' il fallimento e' silenzioso
                            # (un 401 che sembra "nessuna segnalazione nuova"). GET soltanto
                            # nei fatti: POST /api/issues resta protetto dalla sua stessa
                            # dependency (auth.get_current_user), non da questa lista.
    "/api/deepseek/studies",  # Claude reads DeepSeek's answers without a login (deepseek_studies.py)
})

_PUBLIC_PREFIXES = ("/static/", "/api/deepseek/studies/")


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    path = request.url.path
    # Ospite (app/guest.py): prima delle porte pubbliche, perche' anche
    # POST /api/issues passa da _PUBLIC_PATHS. Una sessione vera vince.
    if (
        not app.dependency_overrides
        and not auth._verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE) or "")
        and guest_links.verify_cookie(request.cookies.get(guest_links.GUEST_COOKIE))
    ):
        if guest_links.allows(request.method, path):
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={"detail": "Guest mode: read-only, this action is not available."},
        )

    if (
        request.method == "OPTIONS"
        or path in _PUBLIC_PATHS
        or path.startswith(_PUBLIC_PREFIXES)
    ):
        return await call_next(request)

    # Un test che ha iniettato un utente con dependency_overrides ha gia'
    # dichiarato chi e': il cancello girerebbe prima delle dipendenze e lo
    # rifiuterebbe comunque. In produzione quel dizionario resta vuoto, quindi
    # questa riga non apre niente a nessuno.
    if app.dependency_overrides:
        return await call_next(request)

    if auth._verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE) or ""):
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "Sign-in required: this feature is for registered users only."},
    )

app.add_middleware(GZipMiddleware, minimum_size=500)


# Un 422 e' l'unica risposta d'errore che l'app non scrive da se': la genera
# FastAPI prima di entrare nell'endpoint, e il suo `detail` e' una lista di
# oggetti pensata per un client che la sappia leggere. In pagina diventava
# "Errore 422", cioe' un numero che non dice ne' quale campo ne' cosa ci
# voleva — la segnalazione di Lorenzo. Qui la lista diventa una frase, con il
# nome del parametro e il valore ricevuto: sbagliare resta possibile, non
# capire cosa si e' sbagliato no.

_VALIDATION_WORDING = {
    "int_parsing": "must be a whole number",
    "int_from_float": "must be a whole number, no decimals",
    "int_type": "must be a whole number",
    "float_parsing": "must be a number",
    "float_type": "must be a number",
    "bool_parsing": "must be true or false",
    "literal_error": "value not allowed",
    "enum": "value not allowed",
    "missing": "required parameter missing",
    "string_too_short": "too short",
    "string_too_long": "too long",
    "greater_than_equal": "too small",
    "less_than_equal": "too large",
    "string_pattern_mismatch": "value not allowed",
}


def _validation_message(errors: list[dict]) -> str:
    parts: list[str] = []
    for err in errors[:4]:
        loc = [str(x) for x in err.get("loc", ()) if x not in ("query", "body", "path")]
        field = ".".join(loc) or "request"
        wording = _VALIDATION_WORDING.get(err.get("type", ""), err.get("msg", "invalid value"))
        got = err.get("input", None)
        # Il valore ricevuto e' meta' della diagnosi: "serve un numero intero"
        # senza il "ricevuto ''" non fa capire che il campo era rimasto vuoto.
        if isinstance(got, str):
            shown = f' (got "{got[:40]}")' if got != "" else " (empty field)"
        elif got is None:
            shown = ""
        else:
            shown = f" (got {str(got)[:40]})"
        parts.append(f"{field}: {wording}{shown}")
    if len(errors) > 4:
        parts.append(f"and {len(errors) - 4} more")
    return "Invalid parameters — " + " · ".join(parts)


@app.exception_handler(RequestValidationError)
async def _readable_validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": _validation_message(exc.errors())})


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception):
    """Un 500 vero — quello che prima si vedeva solo nei log del container,
    irraggiungibili da qui senza SSH. Registrato su file (server_errors.json,
    sul volume) e letto da GET /api/errors, cosi' una sessione futura puo'
    consultarlo con una richiesta HTTP invece di aspettare che qualcuno lo
    segnali. Un avviso Telegram parte nello stesso istante, con un raffreddamento
    di 5 minuti per lo stesso errore ripetuto — un bug che rifiorisce a ogni
    retry non deve mandare un messaggio al secondo.

    Non intercetta gli HTTPException (hanno gia' un handler piu' specifico
    prima di questo): qui arrivano solo le eccezioni che nessun endpoint si
    aspettava.
    """
    from app import error_log
    from app.notify import telegram

    user_id = None
    try:
        user = await auth.get_current_user_optional(
            request.cookies.get(auth.SESSION_COOKIE), request.cookies.get(guest_links.GUEST_COOKIE)
        )
        user_id = user.id if user else None
    except Exception:  # noqa: BLE001 — non deve mai impedire di registrare l'errore vero
        pass

    logger.exception("Errore non gestito su %s %s", request.method, request.url.path)
    entry, should_alert = error_log.record(
        method=request.method, path=request.url.path, exc=exc, user_id=user_id,
    )
    if should_alert:
        _fire(telegram.send_message(
            f"🔴 Errore 500 su {entry['method']} {entry['path']}\n"
            f"{entry['exc_type']}: {entry['message']}"
        ))
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/api/errors")
async def list_server_errors(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(auth.get_current_user),
) -> dict:
    """Gli ultimi errori 500 veri, con traceback — protetto dallo stesso
    cancello di seed-0031 (non e' in _PUBLIC_PATHS), non serve altro controllo."""
    from app import error_log
    return {"errors": error_log.recent(limit)}


_app_start = _time.time()

@app.get("/api/health")
async def health():
    from app.cache import _intraday_cache, _daily_cache
    return {
        "status": "ok",
        "uptime_s": round(_time.time() - _app_start),
        "cache_size": len(_intraday_cache) + len(_daily_cache),
    }


class StrategyParamsQuery(BaseModel):
    # Every strategy's own params live on this one shared query model — each
    # compute_*_signals() only ever reads its own keys (merged over that
    # strategy's DEFAULT_PARAMS) and ignores the rest, so there's no
    # cross-strategy collision in practice: the frontend only ever renders
    # (and sends) one strategy's field set at a time (see
    # STRATEGY_PARAM_FIELDS in app.js), even though a couple of names below
    # are shared between two strategies (rsi_length, rsi_oversold,
    # rsi_overbought — both Gabriele's and Exhaustion's own versions of
    # those, never sent together). A field missing here is a field silently
    # dropped by FastAPI's query parsing regardless of what the frontend
    # sends, so keep this in sync with STRATEGY_PARAM_FIELDS in app.js and
    # each strategy's own *_DEFAULT_PARAMS in app.analysis.strategies.

    # Lorenzo (trend-follower)
    average: Literal["EMA", "SMA"] = "EMA"
    len: int = 130
    slopeFlen: int = 7
    slopeSlen: int = 27
    trendfilter: bool = True
    trendfilterperiod: int = 268
    trendfiltertype: Literal["EMA", "SMA"] = "EMA"
    volatilityfilter: bool = True
    volatilitystdevlength: int = 16
    volatilitystdevmalength: int = 44
    # Zero = spenta, cioe' il comportamento storico. Vedi la nota nel modulo
    # delle strategie: e' in unita' di sigma perche' deve valere uguale su
    # qualsiasi coppia.
    slope_threshold_sigma: float = 0.0
    slope_sigma_length: int = 100

    # Accumulo/Distribuzione (long-term) — exposed as independent toggles
    # rather than baked into a silent OR: each condition was added reactively
    # against a single chart, so a user who trusts only the divergence
    # condition can turn the other two off instead of trusting all three.
    enable_divergence: bool = True
    enable_contraction: bool = True
    enable_range_bounce: bool = True
    swing_length: int = 8
    rsi_min_gap: float = 3.0
    low_tolerance_pct: float = 1.0
    divergence_lookback: int = 3
    volume_ma_length: int = 20
    volume_multiplier: float = 1.3
    trend_filter: bool = True
    trend_ema_length: int = 50
    confirmation_lookahead: int = 10
    contraction_lookback: int = 10
    contraction_prior_lookback: int = 30
    contraction_ratio: float = 0.55
    range_lookback: int = 40
    range_zone_pct: float = 0.25

    # Shared between Exhaustion and Gabriele (RSI+BB reversal) — never sent
    # together, see the class docstring above.
    rsi_length: int = 14
    rsi_oversold: int = 28
    rsi_overbought: int = 72

    # Gabriele (RSI+BB reversal)
    bb_length: int = 20
    bb_mult: float = 2.0

    # Lorenzo HF (minuti on-chain). Shares len/slopeFlen/slopeSlen/slope_* with
    # Lorenzo, whose defaults win here: the page always sends the whole field
    # set of the active strategy, so they only apply to a bare API call.
    vwap_filter: bool = True
    er_len: int = 10
    er_min: float = 0.5
    vr_len: int = 60
    vr_min: float = 1.0
    volume_len: int = 30
    volume_mult: float = 0.0
    atr_len: int = 14
    trail_atr: float = 4.0


class BacktestParamsQuery(BaseModel):
    initial_capital: float = 10000.0
    qty_pct_of_equity: float = 30.0
    commission_pct: float = 0.06
    slippage_pct: float = 0.02
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    # Quando si considera eseguito il segnale: alla chiusura della candela
    # che lo genera (storico, ottimista) oppure all'apertura della candela
    # dopo (realizzabile). Vedi DEFAULT_BACKTEST_PARAMS in backtest.py.
    fill_at: Literal["signal_close", "next_open"] = "signal_close"


class TradingCredentialRequest(BaseModel):
    exchange: str
    api_key: str = ""        # empty = keep existing
    api_secret: str = ""     # empty = keep existing
    passphrase: str = ""     # empty = keep existing
    enabled: bool = False
    size_mode: Literal["fixed_usdt", "pct_balance"] = "fixed_usdt"
    size_value: float = 50.0


class TradingCredentialOut(BaseModel):
    exchange: str
    api_key_set: bool        # True if an encrypted key exists
    api_secret_set: bool
    passphrase_set: bool
    enabled: bool
    size_mode: str
    size_value: float
    created_at: float = 0.0  # epoch seconds; 0 = mai configurata


class FuturesCredentialRequest(BaseModel):
    exchange: str
    api_key: str = ""     # empty = keep existing
    api_secret: str = ""  # empty = keep existing
    passphrase: str = ""  # empty = keep existing — serve su OKX e Bitget
    enabled: bool = False  # kill switch — spegnibile senza toccare le chiavi
    size_mode: Literal["fixed_usdt", "pct_balance"] = "fixed_usdt"
    size_value: float = 50.0
    leverage: int = 1


class FuturesCredentialOut(BaseModel):
    exchange: str
    name: str
    api_key_set: bool
    api_secret_set: bool
    passphrase_set: bool
    needs_passphrase: bool
    # Il tetto e' del singolo exchange, non piu' uno solo per tutti: la
    # schermata deve poter fermare il cursore dove lo ferma il server.
    max_leverage: int
    isolated_margin: bool
    key_hint: str
    enabled: bool
    size_mode: str
    size_value: float
    leverage: int
    created_at: float = 0.0  # epoch seconds; 0 = mai configurata


_SEARCH_PROVIDER_TIMEOUT_S = 5.0


async def _safe_search(
    provider: DataProvider, query: str, asset_type: AssetType, provider_id: str
) -> list[SymbolSuggestion]:
    try:
        results = await asyncio.wait_for(provider.search_symbols(query), timeout=_SEARCH_PROVIDER_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — any provider failure/timeout must not sink combined search
        # Un provider lento o irraggiungibile non deve far aspettare (o
        # affondare) la ricerca combinata: asyncio.gather aspetta TUTTE le
        # gambe, quindi senza un tetto qui un solo exchange in difficolta'
        # rendeva "non trova piu' i pair" vero per ogni exchange insieme a
        # lui — sia in Lab sia negli ordini spot/futures, stesso endpoint.
        return []
    return [SymbolSuggestion(**r, asset_type=asset_type, provider=provider_id) for r in results]


def _interleave(*lists: list) -> list:
    """Round-robin merge so one source's larger result count (typically
    stocks) doesn't crowd the others out of a capped combined list."""
    out = []
    for group in itertools.zip_longest(*lists):
        out.extend(item for item in group if item is not None)
    return out


def _rank_suggestions(items: list[SymbolSuggestion], query: str) -> list[SymbolSuggestion]:
    """Sort by match quality: exact symbol first, then prefix, then rest."""
    q = query.upper()
    def _score(s: SymbolSuggestion) -> int:
        sym = s.symbol.upper()
        if sym == q:
            return 0
        if sym.startswith(q):
            return 1
        return 2
    return sorted(items, key=_score)


@app.get("/api/symbols", response_model=list[SymbolSuggestion])
async def search_symbols(query: str = Query(..., min_length=1, max_length=20)) -> list[SymbolSuggestion]:
    # Single unified search across stocks and both crypto exchanges — no
    # upfront "stock vs crypto" toggle. The user picks the asset from results
    # that already say which market/exchange it's on.
    stock_res, mexc_res, kucoin_res, binance_res, kraken_res, dex_res = await asyncio.gather(
        _safe_search(twelvedata_provider, query, "stock", "stock")
        if settings.stocks_enabled else _no_results(),
        _safe_search(mexc_provider, query, "crypto", "mexc"),
        _safe_search(kucoin_provider, query, "crypto", "kucoin"),
        _safe_search(binance_provider, query, "crypto", "binance"),
        _safe_search(kraken_provider, query, "crypto", "kraken"),
        _safe_search(dexscreener_provider, query, "dex", "dexscreener"),
    )
    merged = _interleave(stock_res, mexc_res, kucoin_res, binance_res, kraken_res, dex_res)
    return _rank_suggestions(merged, query)[:25]


def _dispatch_telegram_alerts(
    chat_id: str,
    symbol: str,
    interval: str,
    strategy: StrategyState,
    frequency: str = "every",
    entry_id: str = "",
    owner_id: str = "",
    current_price: float | None = None,
) -> list[StrategySignal]:
    """Manda le notifiche e RESTITUISCE i segnali che le hanno generate.

    Restituirli non e' una comodita': find_new_signals consuma lo stato di
    deduplica, quindi una seconda chiamata sugli stessi segnali torna vuota.
    Chi deve agire su quei segnali — l'esecuzione automatica degli ordini —
    deve ricevere questi, non richiederli.
    """
    fresh = find_new_signals(symbol, interval, strategy, frequency=frequency, entry_id=entry_id)
    for signal in fresh:
        message = format_signal_message(symbol, interval, strategy.name, signal,
                                        current_price=current_price, entry_id=entry_id or "")
        _fire(telegram.send_to(chat_id, message))
        if owner_id:
            alert_log.append_entry(
                owner_id=owner_id,
                symbol=symbol,
                interval=interval,
                strategy_name=strategy.name,
                signal_type=signal.type,
                price=signal.price,
                label=signal.label,
                chat_id=chat_id,
            )
        if entry_id:
            record_alert_sent(entry_id, strategy.name)
    return fresh


@app.get("/api/strategies", response_model=list[str])
async def list_strategies() -> list[str]:
    return AVAILABLE_STRATEGIES


class PineParamOut(BaseModel):
    var_name: str
    kind: str
    default: object
    title: str
    minval: float | None = None
    maxval: float | None = None


class PineUploadRequest(BaseModel):
    name: str
    source: str


class PineScriptOut(BaseModel):
    id: str
    name: str
    created_at: int
    params: list[PineParamOut]
    is_strategy_style: bool


def _parse_pine_or_400(source: str) -> tuple[list, list[PineParamOut], bool]:
    try:
        stmts = parse_program(source)
        params = scan_inputs(stmts)
        strategy_style = is_strategy_style(stmts)
    except PineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    params_out = [
        PineParamOut(var_name=p.var_name, kind=p.kind, default=p.default, title=p.title, minval=p.minval, maxval=p.maxval)
        for p in params
    ]
    return stmts, params_out, strategy_style


@app.post("/api/pinescript", response_model=PineScriptOut)
async def upload_pinescript(req: PineUploadRequest) -> PineScriptOut:
    name = req.name.strip() or "Untitled script"
    _stmts, params_out, strategy_style = _parse_pine_or_400(req.source)
    default_params = {p.var_name: p.default for p in params_out}
    meta = pine_storage.save_script(name, req.source, default_params)
    return PineScriptOut(
        id=meta["id"], name=meta["name"], created_at=meta["created_at"], params=params_out, is_strategy_style=strategy_style
    )


@app.get("/api/pinescript", response_model=list[PineScriptOut])
async def list_pinescripts() -> list[PineScriptOut]:
    out = []
    for meta in pine_storage.list_scripts():
        loaded = pine_storage.load_script(meta["id"])
        if loaded is None:
            continue
        _, source = loaded
        try:
            _stmts, params_out, strategy_style = _parse_pine_or_400(source)
        except HTTPException:
            continue
        out.append(
            PineScriptOut(
                id=meta["id"], name=meta["name"], created_at=meta["created_at"], params=params_out, is_strategy_style=strategy_style
            )
        )
    return out


@app.get("/api/pinescript/{script_id}")
async def get_pinescript(script_id: str) -> dict:
    loaded = pine_storage.load_script(script_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Script not found")
    meta, source = loaded
    _stmts, params_out, strategy_style = _parse_pine_or_400(source)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "created_at": meta["created_at"],
        "source": source,
        "params": [p.model_dump() for p in params_out],
        "is_strategy_style": strategy_style,
    }


@app.delete("/api/pinescript/{script_id}")
async def delete_pinescript(script_id: str) -> dict:
    if not pine_storage.delete_script(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    return {"ok": True}


class PineRunRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    timeframe: TimeframeKey = "mtf"
    interval: IntervalChoice | None = None
    params: dict = {}
    signal_map: dict[str, str] | None = None
    backtest_params: BacktestParamsQuery = BacktestParamsQuery()


@app.post("/api/pinescript/{script_id}/run")
async def run_pinescript(script_id: str, req: PineRunRequest) -> dict:
    loaded = pine_storage.load_script(script_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Script not found")
    _meta, source = loaded
    stmts, params_out, strategy_style = _parse_pine_or_400(source)

    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    interval = req.interval or DEFAULT_INTERVALS[req.timeframe]
    provider = get_provider(req.asset_type, req.exchange)

    try:
        df = await _fetch_cached(
            (f"pine:{req.asset_type}:{req.exchange}:{symbol.upper()}", interval, settings.ltf_candles),
            provider, symbol, interval, settings.ltf_candles,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"No data for {symbol} on {interval}")

    merged_params = {p.var_name: p.default for p in params_out}
    merged_params.update(req.params or {})

    try:
        interp = PineInterpreter(df, params=merged_params, signal_map=req.signal_map)
        signals = interp.run(stmts)
    except PineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not strategy_style:
        # Only entry_long/entry_short are mandatory — plenty of indicator
        # scripts (RSI/BB reversal setups included) only ever define entry
        # conditions and rely on the opposite signal to flip the position,
        # exactly like our own built-in strategies. Requiring an explicit
        # exit mapping that doesn't exist in the script would leave a
        # perfectly usable script stuck asking for mapping forever.
        required_roles = {"entry_long", "entry_short"}
        mapped = set((req.signal_map or {}).keys())
        if not required_roles.issubset(mapped):
            return {
                "needs_mapping": True,
                "candidates": interp.boolean_candidates(),
                "warning": (
                    "This script doesn't use strategy.entry/close: pick which boolean variables "
                    "map to entry_long / entry_short (exit_long / exit_short are optional — "
                    "if you don't map them, the position stays open until the opposite signal)."
                ),
            }

    strategy = build_strategy_state(f"Script: {_meta['name']}", df, signals=signals)
    backtest = run_backtest(df, None, req.backtest_params.model_dump(), signals=signals)
    return {"needs_mapping": False, "strategy": strategy.model_dump(), "backtest": backtest.model_dump()}


@app.get("/api/indicators/catalog")
async def get_indicator_catalog() -> list[dict]:
    """Libreria a tendina: tutti gli indicatori di pandas-ta non ancora
    cablati in compute_indicators(), con la classificazione overlay/oscillator
    che decide dove il frontend li disegna."""
    return indicator_catalog.catalog_payload()


class IndicatorComputeRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    timeframe: TimeframeKey = "mtf"
    interval: IntervalChoice | None = None
    indicator: str
    params: dict = {}


@app.post("/api/indicators/compute")
async def compute_indicator_route(req: IndicatorComputeRequest) -> dict:
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    interval = req.interval or DEFAULT_INTERVALS[req.timeframe]
    provider = get_provider(req.asset_type, req.exchange)
    try:
        df = await _fetch_cached(
            (f"ind:{req.asset_type}:{req.exchange}:{symbol.upper()}", interval, settings.ltf_candles),
            provider, symbol, interval, settings.ltf_candles,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"No data for {symbol} on {interval}")

    try:
        series = indicator_catalog.compute_indicator(df, req.indicator, req.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    spec = indicator_catalog.CATALOG_BY_ID.get(req.indicator)
    return {
        "kind": spec.kind if spec else "oscillator",
        "times": [int(ts.timestamp()) for ts in df.index],
        "series": [s.model_dump() for s in series],
    }


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TelegramChatIdRequest(BaseModel):
    telegram_chat_id: str


def _user_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "telegram_chat_id": user.telegram_chat_id,
        "guest": guest_links.is_guest(user),
    }


def _client_ip(request: Request) -> str:
    # Fly's edge proxy sets this; request.client.host would otherwise be
    # the internal proxy address, not the real caller.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    # Read the header directly rather than trusting uvicorn's own
    # proxy-header handling (which needs explicit --forwarded-allow-ips
    # config to kick in) — Fly's proxy always sets this correctly.
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded_proto == "https" if forwarded_proto else request.url.scheme == "https"


def _set_session_cookie(response: Response, user_id: str, request: Request) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session_cookie(user_id),
        max_age=auth.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


@app.post("/api/auth/register")
async def register(req: RegisterRequest, request: Request, response: Response) -> dict:
    ip_key = f"register:{_client_ip(request)}"
    if rate_limit.is_locked(ip_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in a few minutes.")

    email = req.email.strip().lower()
    if "@" not in email or len(email) < 5:
        rate_limit.register_failure(ip_key)
        raise HTTPException(status_code=400, detail="Invalid email")

    consentite = {e.strip().lower() for e in settings.registration_allowed_emails.split(",") if e.strip()}
    if consentite and email not in consentite:
        rate_limit.register_failure(ip_key)
        # Stesso messaggio che si da' a un'email gia' presente: due risposte
        # diverse direbbero a un estraneo quali indirizzi esistono.
        raise HTTPException(status_code=403, detail="Registration not allowed for this address")
    if not auth.is_strong_password(req.password):
        rate_limit.register_failure(ip_key)
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters, with letters and numbers, and not too common",
        )
    if auth.find_by_email(email):
        rate_limit.register_failure(ip_key)
        raise HTTPException(status_code=400, detail="Email already registered")

    user = auth.create_user(email, req.password)
    rate_limit.register_success(ip_key)
    _set_session_cookie(response, user.id, request)
    return _user_out(user)


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    email = req.email.strip().lower()
    ip_key = f"ip:{_client_ip(request)}"
    email_key = f"email:{email}"
    if rate_limit.is_locked(ip_key) or rate_limit.is_locked(email_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")

    user = auth.verify_login(email, req.password)
    if user is None:
        rate_limit.register_failure(ip_key)
        rate_limit.register_failure(email_key)
        raise HTTPException(status_code=401, detail="Email o password errati")

    rate_limit.register_success(ip_key)
    rate_limit.register_success(email_key)
    _set_session_cookie(response, user.id, request)
    return _user_out(user)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict:
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE) or "")
    response.delete_cookie(auth.SESSION_COOKIE)
    response.delete_cookie(guest_links.GUEST_COOKIE)
    return {"ok": True}


@app.post("/api/auth/logout-all")
async def logout_all(response: Response, user: User = Depends(auth.get_current_user)) -> dict:
    """Every session of this account, on every device, stops working."""
    auth.revoke_all_sessions(user.id)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: User | None = Depends(auth.get_current_user_optional)) -> dict:
    return {"user": _user_out(user) if user else None}


@app.patch("/api/auth/telegram")
async def update_telegram_chat_id(
    req: TelegramChatIdRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    updated = auth.set_telegram_chat_id(user.id, req.telegram_chat_id)
    if updated.telegram_chat_id and _telegram_allowed(updated):
        # Confirms the chat_id was actually saved and is reachable — sent
        # immediately on save, not the first real alert, so a typo'd id
        # shows up right away instead of silently going nowhere.
        _fire(
            telegram.send_to(
                updated.telegram_chat_id,
                "✅ Chat ID collegato correttamente a Liquidity Analysis Engine.\n"
                "Da qui in poi riceverai qui gli avvisi delle strategie che attivi.",
            )
        )
    return _user_out(updated)


@app.get("/api/auth/trading", response_model=list[TradingCredentialOut])
async def get_trading_credentials(user: User = Depends(auth.get_current_user)) -> list[TradingCredentialOut]:
    out = []
    for exchange in ("binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"):
        cred = user.trading_credentials.get(exchange)
        if cred is None:
            out.append(TradingCredentialOut(exchange=exchange, api_key_set=False, api_secret_set=False, passphrase_set=False, enabled=False, size_mode="fixed_usdt", size_value=50.0))
        else:
            out.append(TradingCredentialOut(
                exchange=exchange,
                api_key_set=bool(cred.api_key_enc),
                api_secret_set=bool(cred.api_secret_enc),
                passphrase_set=bool(cred.passphrase_enc),
                enabled=cred.enabled,
                size_mode=cred.size_mode,
                size_value=cred.size_value,
                created_at=cred.created_at,
            ))
    return out


@app.patch("/api/auth/trading")
async def set_trading_credential(body: TradingCredentialRequest, user: User = Depends(auth.get_current_user)):
    supported = {"binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"}
    if body.exchange not in supported:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {body.exchange}")
    auth.set_trading_credential(
        user.id, body.exchange, body.api_key, body.api_secret, body.passphrase,
        body.enabled, body.size_mode, body.size_value,
    )
    return {"ok": True}


@app.delete("/api/auth/trading/{exchange}")
async def delete_trading_credential(exchange: str, user: User = Depends(auth.get_current_user)):
    auth.delete_trading_credential(user.id, exchange)
    return {"ok": True}


class ManualOrderRequest(BaseModel):
    exchange: str
    symbol: str
    side: Literal["BUY", "SELL"]
    size_mode: Literal["fixed_usdt", "pct_balance"] = "fixed_usdt"
    size_value: float


@app.post("/api/trading/order")
async def place_manual_order(body: ManualOrderRequest, user: User = Depends(auth.get_current_user)):
    supported = {"binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"}
    if body.exchange not in supported:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {body.exchange}")
    cred = user.trading_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="No credentials configured for this exchange")
    api_key, api_secret, passphrase = auth.get_decrypted_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key or secret missing — set them up first in the Auto trading section")

    # Il pair si verifica prima di mandare l'ordine, dove abbiamo un provider
    # per quell'exchange. Scriverlo a mano e' l'unico modo che c'e' di
    # indicarlo, e un pair inesistente o quotato altrove tornava indietro come
    # errore dell'exchange — cioe' dopo aver provato a comprare qualcosa. Un
    # controllo qui costa una chiamata e dice il nome esatto atteso.
    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Pair missing")
    if body.exchange in ("mexc", "binance", "kucoin"):
        symbol = canonical_symbol(symbol, "crypto", body.exchange)
        provider = get_provider("crypto", body.exchange)
        try:
            probe = await provider.fetch_ohlcv(symbol, "1d", 2)
        except ProviderError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Pair {symbol} not found on {body.exchange}: {exc}",
            ) from exc
        if probe.empty:
            raise HTTPException(
                status_code=400,
                detail=f"Pair {symbol} not found on {body.exchange}. Check the exact name "
                       "(e.g. BTCUSDT) and that it's listed on this exchange.",
            )

    from app.models import StrategySignal
    signal_type = "entry_long" if body.side == "BUY" else "entry_short"
    fake_signal = StrategySignal(time=int(_time.time()), type=signal_type, price=0.0, label="manual")
    try:
        result = await place_order_for_signal(
            body.exchange, symbol, fake_signal,
            body.size_mode, body.size_value,
            api_key, api_secret, passphrase,
        )
    except RuntimeError as exc:
        # Anche un ordine manuale va nel diario. Era l'unico percorso che non
        # ci finiva, ed e' esattamente quello su cui e' arrivata la
        # segnalazione: il fallimento viveva solo nel riquadro di stato, che
        # sparisce ricaricando la pagina.
        trading_log.record(
            user.id, symbol=symbol, exchange=body.exchange,
            strategy_name="manuale", signal_type=signal_type,
            outcome="fallito", detail=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    trading_log.record(
        user.id, symbol=symbol, exchange=body.exchange,
        strategy_name="manuale", signal_type=signal_type,
        outcome="eseguito", detail=str(result)[:400],
    )
    return {"ok": True, "result": result, "symbol": symbol}


class ClosePositionRequest(BaseModel):
    """Stessa forma per spot e futures: chiudere e' sempre "quel pair, su quel
    exchange, tutto" — non c'e' una dimensione da scegliere."""

    exchange: str
    symbol: str
    confirm: bool = False


@app.post("/api/trading/spot/close")
async def close_manual_spot_position(body: ClosePositionRequest, user: User = Depends(auth.get_current_user)):
    """Vendere tutta la moneta detenuta di un pair: la chiusura, sullo spot.

    Sullo spot non esiste una "posizione" da chiudere come nei futures —
    esiste un saldo. Chiudere quindi significa vendere il 100% della moneta
    base libera, che e' esattamente cio' che fa gia' un exit_long: nessuna
    logica nuova negli esecutori, solo il percorso che prima si poteva
    imboccare solo da un segnale automatico o scrivendo "100 / percentuale"
    a mano nel form dell'ordine manuale.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required to sell the whole position")
    supported = {"binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"}
    if body.exchange not in supported:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {body.exchange}")
    cred = user.trading_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="No credentials configured for this exchange")
    api_key, api_secret, passphrase = auth.get_decrypted_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key or secret missing")

    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Pair missing")

    from app.models import StrategySignal
    # exit_long -> SELL, e "pct_balance 100" e' tutto il saldo libero della
    # moneta base. Il tetto per ordine (max_usdt) non si applica ai SELL
    # apposta — vedi place_order_for_signal: limitarlo lascerebbe aperta
    # meta' della posizione che si e' chiesto di chiudere.
    fake_signal = StrategySignal(time=int(_time.time()), type="exit_long", price=0.0, label="chiusura manuale")
    try:
        result = await place_order_for_signal(
            body.exchange, symbol, fake_signal,
            "pct_balance", 100.0,
            api_key, api_secret, passphrase,
        )
    except RuntimeError as exc:
        trading_log.record(
            user.id, symbol=symbol, exchange=body.exchange,
            strategy_name="chiusura manuale", signal_type="exit_long",
            outcome="fallito", detail=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    trading_log.record(
        user.id, symbol=symbol, exchange=body.exchange,
        strategy_name="chiusura manuale", signal_type="exit_long",
        outcome="eseguito", detail=str(result)[:400],
    )
    return {"ok": True, "closed": True, "result": result, "symbol": symbol}


@app.get("/api/auth/futures", response_model=list[FuturesCredentialOut])
async def get_futures_credentials(user: User = Depends(auth.get_current_user)) -> list[FuturesCredentialOut]:
    out = []
    for exchange in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        adapter = futures_registry.get(exchange)
        cred = user.futures_credentials.get(exchange)
        common = dict(
            exchange=exchange, name=adapter.name, needs_passphrase=adapter.needs_passphrase,
            # Il tetto vero e' il piu' basso fra i due: il frontend non deve
            # mai mostrare uno slider che arriva piu' in alto di quanto il
            # server accetterebbe davvero (vedi i controlli su MAX_FUTURES_LEVERAGE
            # in questo stesso file e in auth.set_futures_credential).
            max_leverage=min(MAX_FUTURES_LEVERAGE, adapter.max_leverage),
            isolated_margin=adapter.isolated_margin,
            key_hint=adapter.key_hint,
        )
        if cred is None:
            out.append(FuturesCredentialOut(
                **common, api_key_set=False, api_secret_set=False, passphrase_set=False,
                enabled=False, size_mode="fixed_usdt", size_value=50.0, leverage=1,
            ))
        else:
            out.append(FuturesCredentialOut(
                **common, api_key_set=bool(cred.api_key_enc), api_secret_set=bool(cred.api_secret_enc),
                passphrase_set=bool(cred.passphrase_enc),
                enabled=cred.enabled, size_mode=cred.size_mode, size_value=cred.size_value, leverage=cred.leverage,
                created_at=cred.created_at,
            ))
    return out


@app.patch("/api/auth/futures")
async def set_futures_credential_endpoint(body: FuturesCredentialRequest, user: User = Depends(auth.get_current_user)):
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures not supported yet for this exchange: {body.exchange}")
    adapter = futures_registry.get(body.exchange)
    existing_cred = user.futures_credentials.get(body.exchange)
    has_passphrase = bool(body.passphrase) or bool(existing_cred and existing_cred.passphrase_enc)
    if adapter.needs_passphrase and not has_passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} requires a passphrase in addition to key and secret")
    # Il tetto e' quello di questo exchange, non il massimo globale: un
    # cursore che arriva a 10x su un adattatore che ne sopporta solo 5 deve
    # fermarsi prima di raggiungere il server, ma il server e' l'ultima
    # parola in ogni caso.
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leverage {body.leverage}x outside {adapter.name}'s limit (1-{max_lev}x)")
    try:
        auth.set_futures_credential(
            user.id, body.exchange, body.api_key, body.api_secret,
            body.enabled, body.size_mode, body.size_value, body.leverage,
            passphrase=body.passphrase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/api/auth/futures/{exchange}")
async def delete_futures_credential(exchange: str, user: User = Depends(auth.get_current_user)):
    auth.delete_futures_credential(user.id, exchange)
    return {"ok": True}


class DiagnosticsTestRequest(BaseModel):
    exchange: str
    market: Literal["spot", "futures"] = "spot"
    # Vuoti = usa le chiavi gia' salvate per questo exchange/mercato. Non
    # vuoti = prova QUESTE senza salvarle — utile per verificare una chiave
    # nuova prima di scriverla sopra quella che magari funziona ancora.
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""


@app.post("/api/diagnostics/test-connection")
async def diagnostics_test_connection(
    body: DiagnosticsTestRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    """Prova solo a LEGGERE il saldo — mai un ordine, mai una scrittura.
    Pensata per essere il primo passo prima di fidarsi di una chiave, non
    un sostituto della conferma che ogni ordine reale chiede comunque."""
    api_key, api_secret, passphrase = body.api_key, body.api_secret, body.passphrase
    if not api_key and not api_secret:
        # Nessuna chiave data esplicitamente: prova quella gia' salvata.
        if body.market == "futures":
            api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
        else:
            api_key, api_secret, passphrase = auth.get_decrypted_credential(user, body.exchange)
        if not api_key or not api_secret:
            raise HTTPException(
                status_code=400,
                detail=f"No key saved for {body.exchange} ({body.market}) — add one to test it.",
            )
    try:
        if body.market == "futures":
            margin = await diagnostics.test_futures_connection(body.exchange, api_key, api_secret, passphrase)
            return {"ok": True, "market": "futures", "exchange": body.exchange, "margin_usdt": margin}
        balances = await diagnostics.test_spot_connection(body.exchange, api_key, api_secret, passphrase)
        return {"ok": True, "market": "spot", "exchange": body.exchange, "balances": balances}
    except diagnostics.DiagnosticsError as exc:
        return {"ok": False, "market": body.market, "exchange": body.exchange, "error": str(exc)}


class ReauthRequest(BaseModel):
    password: str = ""


def _require_password(user: User, password: str, request: Request) -> None:
    """A session alone must not enrol the factor that later approves a key
    export (audit 29/09, S05): adding or removing a passkey asks for the
    account password again, under the same lockout as the login."""
    ip_key, user_key = f"ip:{_client_ip(request)}", f"reauth:{user.id}"
    if rate_limit.is_locked(ip_key) or rate_limit.is_locked(user_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")
    if not password or auth.verify_login(user.email, password) is None:
        rate_limit.register_failure(ip_key)
        rate_limit.register_failure(user_key)
        raise HTTPException(status_code=401, detail="Wrong password")
    rate_limit.register_success(user_key)


def _notify_passkey_change(user: User, what: str) -> None:
    text = f"Passkey {what} sull'account {user.email}. Se non sei stato tu, cambia password e sposta i fondi."
    _fire(telegram.send_infra_alert(text))
    if user.telegram_chat_id and _telegram_allowed(user):
        _fire(telegram.send_to(user.telegram_chat_id, text))


@app.post("/api/auth/passkey/register/options")
async def passkey_register_options(
    request: Request, body: ReauthRequest | None = None, user: User = Depends(auth.get_current_user),
) -> dict:
    """Chiamata da un browser gia' loggato — e' l'iscrizione, non il
    flusso di rivelazione: Face ID/Touch ID qui autorizza solo se stesso
    a diventare un modo per approvare in futuro, non rivela niente.
    The password is asked again first: the state token is only handed out
    after it, so /verify is bound to it."""
    _require_password(user, body.password if body else "", request)
    options_json, state_token = onchain_passkey.start_registration(user)
    return {"options": options_json, "state_token": state_token}


class PasskeyRegisterVerifyRequest(BaseModel):
    state_token: str
    credential: dict
    label: str = ""


@app.post("/api/auth/passkey/register/verify")
async def passkey_register_verify(
    body: PasskeyRegisterVerifyRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    try:
        passkey = onchain_passkey.finish_registration(user, body.state_token, body.credential, body.label)
    except Exception as exc:  # noqa: BLE001 — errore di verifica WebAuthn, mai un 500 muto
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    auth.add_passkey(user.id, passkey)
    _notify_passkey_change(user, f"aggiunta ({passkey.label or 'senza nome'})")
    return {"ok": True, "label": passkey.label or "Passkey", "created_at": passkey.created_at}


@app.get("/api/auth/passkey")
async def list_passkeys(user: User = Depends(auth.get_current_user)) -> list[dict]:
    return [{"credential_id": pk.credential_id, "label": pk.label, "created_at": pk.created_at} for pk in user.passkeys]


@app.delete("/api/auth/passkey/{credential_id}")
async def remove_passkey(
    credential_id: str, request: Request, body: ReauthRequest | None = None,
    user: User = Depends(auth.get_current_user),
) -> dict:
    _require_password(user, body.password if body else "", request)
    auth.delete_passkey(user.id, credential_id)
    _notify_passkey_change(user, "rimossa")
    return {"ok": True}


def _require_onchain_enabled() -> None:
    if not settings.onchain_enabled:
        raise HTTPException(
            status_code=400,
            detail="The on-chain wallet system is off (master switch) — no key, balance or "
                   "reveal is reachable until it's switched back on.",
        )


class OnchainWalletCreateRequest(BaseModel):
    chain: OnchainChain


@app.get("/api/onchain/wallets")
async def list_onchain_wallets(user: User = Depends(auth.get_current_user)) -> list[dict]:
    """Solo indirizzo e stato — la chiave privata non lascia mai auth.py,
    men che meno in una risposta HTTP."""
    _require_onchain_enabled()
    return [
        {"chain": chain, "address": w.address, "enabled": w.enabled, "created_at": w.created_at}
        for chain, w in user.onchain_wallets.items()
    ]


@app.post("/api/onchain/wallets")
async def create_onchain_wallet(
    body: OnchainWalletCreateRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    _require_onchain_enabled()
    updated = auth.create_onchain_wallet(user.id, body.chain)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    w = updated.onchain_wallets[body.chain]
    return {
        "chain": body.chain, "address": w.address, "enabled": w.enabled, "created_at": w.created_at,
        "backup_reminder": "Back up this key somewhere else as soon as you can ('Reveal key' button): if the "
                            "server loses it, the wallet can't be recovered any other way.",
    }


class OnchainWalletEnabledRequest(BaseModel):
    enabled: bool


@app.patch("/api/onchain/wallets/{chain}")
async def set_onchain_wallet_enabled(
    chain: OnchainChain, body: OnchainWalletEnabledRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    _require_onchain_enabled()
    updated = auth.set_onchain_wallet_enabled(user.id, chain, body.enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="No wallet on this chain — create one first")
    if not body.enabled:
        # Kill switch attivato — alert immediato (piano SRE punto 5).
        _fire(telegram.send_infra_alert(
            f"Kill switch ATTIVATO per il wallet {chain} dell'utente {user.email} — nessun nuovo ordine partira'."
        ))
    w = updated.onchain_wallets[chain]
    return {"chain": chain, "address": w.address, "enabled": w.enabled, "created_at": w.created_at}


@app.post("/api/onchain/wallets/{chain}/reveal/request")
async def request_onchain_reveal(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    """Primo passo della rivelazione: genera un token effimero (2 minuti,
    uso unico) e il QR/link che lo porta su un secondo dispositivo. La
    chiave vera e propria non e' ancora raggiungibile da qui — serve
    approvarlo dal telefono (vedi /api/onchain/reveal/approve) e poi
    consumarlo su /reveal. Un browser da solo, anche loggato, non basta."""
    _require_onchain_enabled()
    if chain not in user.onchain_wallets:
        raise HTTPException(status_code=404, detail="No wallet on this chain — create one first")
    token = onchain_reveal.create(user.id, chain)
    approve_url = f"{settings.public_base_url}/onchain-approve.html?token={token}"
    qr_buf = io.BytesIO()
    qrcode.make(approve_url).save(qr_buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + base64.b64encode(qr_buf.getvalue()).decode()
    return {"token": token, "approve_url": approve_url, "qr_data_uri": qr_data_uri, "expires_in_s": 120}


@app.get("/api/onchain/wallets/reveal/status/{token}")
async def onchain_reveal_status(token: str, user: User = Depends(auth.get_current_user)) -> dict:
    """Il browser fa polling qui in attesa che il telefono approvi.
    Rimasto per chi non puo' usare SSE (fallback) — l'app nuova usa
    /reveal/stream/{token} sotto."""
    return {"status": onchain_reveal.status(token, user.id)}


@app.get("/api/onchain/wallets/reveal/stream/{token}")
async def onchain_reveal_stream(token: str, user: User = Depends(auth.get_current_user)) -> StreamingResponse:
    """Stessa attesa di /reveal/status, ma spinta appena cambia invece che
    tirata ogni 2.5s — un'attesa breve (2 minuti al massimo) non merita un
    ritardo medio di oltre un secondo tra il vero istante dell'approvazione
    e quando il browser se ne accorge. Nessuna coda pub/sub nuova: il token
    vive solo qualche minuto, un poll interno ogni 300ms basta e non
    aggiunge stato da mantenere altrove."""
    async def event_gen():
        last = None
        for _ in range(400):  # 400 * 300ms = 120s, lo stesso TTL del token
            current = onchain_reveal.status(token, user.id)
            if current != last:
                yield f"data: {json.dumps({'status': current})}\n\n"
                last = current
            if current in ("approved", "expired"):
                return
            await asyncio.sleep(0.3)
        yield f"data: {json.dumps({'status': 'expired'})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class OnchainRevealApproveRequest(BaseModel):
    email: str
    password: str
    token: str


@app.post("/api/onchain/reveal/approve")
async def approve_onchain_reveal(body: OnchainRevealApproveRequest, request: Request) -> dict:
    """Chiamata dalla pagina sul TELEFONO, apposta senza richiedere il
    cookie di sessione del browser (e' un altro dispositivo) — l'identita'
    la stabilisce la password, come al login. Un token che non appartiene
    a chi ha inserito quella password non si approva."""
    # Same lockout as the login: this is a password check too (audit S05).
    ip_key, email_key = f"ip:{_client_ip(request)}", f"email:{body.email.strip().lower()}"
    if rate_limit.is_locked(ip_key) or rate_limit.is_locked(email_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")
    verified = auth.verify_login(body.email, body.password)
    if verified is None:
        rate_limit.register_failure(ip_key)
        rate_limit.register_failure(email_key)
        raise HTTPException(status_code=401, detail="Email o password errate")
    rate_limit.register_success(email_key)
    if not onchain_reveal.approve(body.token, verified.id):
        raise HTTPException(status_code=400, detail="Request expired or invalid — try again from the browser")
    return {"ok": True}


class OnchainRevealPasskeyOptionsRequest(BaseModel):
    email: str


@app.post("/api/onchain/reveal/passkey/options")
async def onchain_reveal_passkey_options(body: OnchainRevealPasskeyOptionsRequest) -> dict:
    """Primo passo di Face ID/Touch ID sul telefono: serve solo l'email
    per sapere quali passkey proporre — nessuna password ancora, nessuna
    conferma di chi sia davvero finche' la firma biometrica non torna."""
    user = auth.find_by_email(body.email)
    if user is None or not user.passkeys:
        raise HTTPException(status_code=400, detail="No passkey registered for this email — use your password.")
    options_json, state_token = onchain_passkey.start_authentication(user)
    return {"options": options_json, "state_token": state_token}


class OnchainRevealPasskeyVerifyRequest(BaseModel):
    email: str
    token: str
    state_token: str
    credential: dict


@app.post("/api/onchain/reveal/passkey/verify")
async def onchain_reveal_passkey_verify(body: OnchainRevealPasskeyVerifyRequest) -> dict:
    user = auth.find_by_email(body.email)
    if user is None:
        raise HTTPException(status_code=401, detail="Email not recognized")
    try:
        new_sign_count = onchain_passkey.finish_authentication(user, body.state_token, body.credential)
    except Exception as exc:  # noqa: BLE001 — errore di verifica WebAuthn, mai un 500 muto
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    auth.update_passkey_sign_count(user.id, body.credential.get("id", ""), new_sign_count)
    if not onchain_reveal.approve(body.token, user.id):
        raise HTTPException(status_code=400, detail="Request expired or invalid — try again from the browser")
    return {"ok": True}


class OnchainRevealRequest(BaseModel):
    token: str


@app.post("/api/onchain/wallets/{chain}/reveal")
async def reveal_onchain_wallet(
    chain: OnchainChain, body: OnchainRevealRequest, user: User = Depends(auth.get_current_user)
) -> dict:
    """L'unico endpoint di tutto il sito che restituisce un segreto invece
    di limitarsi a impostarlo (le chiavi exchange non si rileggono mai,
    ma quelle sono custodite dall'exchange — questo wallet e' il fondo
    stesso, quindi deve poter uscire di qui: Phantom/MetaMask, non solo
    questo sito). Nessun log, nessuna cache — letta e cifrata di nuovo a
    ogni chiamata, mai tenuta in chiaro piu' del necessario per rispondere.
    Richiede un token approvato dal telefono (vedi /reveal/request): la
    sola sessione del browser non basta piu'."""
    _require_onchain_enabled()
    if chain not in user.onchain_wallets:
        raise HTTPException(status_code=404, detail="No wallet on this chain — create one first")
    if not onchain_reveal.consume(body.token, user.id, chain):
        raise HTTPException(
            status_code=403,
            detail="No valid approval for this wallet — scan the QR code again from your phone.",
        )
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, chain)
    except auth.OnchainKeyIntegrityError as exc:
        # Notifica sempre, non solo un log: un dato corrotto qui non e' un
        # bug da correggere con calma, e' un wallet potenzialmente
        # irraggiungibile — chi possiede l'account deve saperlo subito.
        if user.telegram_chat_id and _telegram_allowed(user):
            _fire(telegram.send_to(
                user.telegram_chat_id, f"🚨 Wallet on-chain ({chain}): {exc}",
            ))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if user.telegram_chat_id and _telegram_allowed(user):
        _fire(telegram.send_to(
            user.telegram_chat_id, f"🔓 Chiave on-chain rivelata ({chain}, {user.onchain_wallets[chain].address})",
        ))
    return {
        "chain": chain,
        "address": user.onchain_wallets[chain].address,
        "private_key": onchain_keys.to_import_format(chain, private_key_hex),
        "import_as": "Phantom/Solflare — Import private key" if chain == "solana"
        else "MetaMask/Rabby — Import account",
        "warning": "Anyone who sees this key can drain the wallet. Don't screenshot it, "
                   "don't share it in chat: import it and close this screen.",
    }


@app.get("/api/onchain/wallets/{chain}/balance")
async def onchain_wallet_balance(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    _require_onchain_enabled()
    wallet = user.onchain_wallets.get(chain)
    if wallet is None:
        raise HTTPException(status_code=404, detail="No wallet on this chain — create one first")
    try:
        native = await onchain_balances.native_balance(chain, wallet.address)
    except Exception as exc:  # noqa: BLE001 — un RPC pubblico irraggiungibile non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"RPC unreachable: {exc}") from exc
    unit = "SOL" if chain == "solana" else "ETH"
    # None se GeckoTerminal non risponde — mai un "$" calcolato su un
    # prezzo inventato: la UI mostra solo le unita' native in quel caso.
    price_usd, btc_usd = await asyncio.gather(onchain_prices.native_price_usd(chain), onchain_prices.btc_price_usd())
    return {
        "chain": chain, "address": wallet.address, "native_balance": native, "native_unit": unit,
        "native_price_usd": price_usd,
        "native_usd_value": native * price_usd if price_usd is not None else None,
        # Per la vista in BTC del saldo (tocco sulla cifra): None se Kraken
        # non risponde, e la UI salta quella vista invece di inventarla.
        "btc_price_usd": btc_usd,
    }


@app.get("/api/onchain/wallets/{chain}/transactions")
async def onchain_wallet_transactions(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    _require_onchain_enabled()
    wallet = user.onchain_wallets.get(chain)
    if wallet is None:
        raise HTTPException(status_code=404, detail="No wallet on this chain — create one first")
    try:
        result = await onchain_balances.recent_transactions(chain, wallet.address)
    except Exception as exc:  # noqa: BLE001 — un RPC pubblico irraggiungibile non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"RPC unreachable: {exc}") from exc
    return {"chain": chain, "address": wallet.address, **result}


# ── Fase 2/3 del piano: guardrail, simulazione, esecuzione reale (spenta) ──
# Nessuna chiave tocca questi endpoint: quote e simulazione lavorano su
# indirizzi/importi pubblici, l'esecuzione reale resta dietro
# settings.onchain_execution_enabled (Fase 3, non ancora accesa).

class OnchainQuoteSolanaRequest(BaseModel):
    input_mint: str
    output_mint: str
    amount_lamports: int
    slippage_bps: int = 50


@app.post("/api/onchain/quote/solana")
async def onchain_quote_solana(body: OnchainQuoteSolanaRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Preventivo reale via Jupiter — nessuna chiave, nessun impegno a
    eseguire. E' il price_impact_pct che risk.check_price_impact() confronta
    con la soglia prima di qualunque simulazione o (in futuro) ordine vero."""
    _require_trading()
    _require_onchain_enabled()
    try:
        quote = await onchain_quotes.jupiter_quote(
            body.input_mint, body.output_mint, body.amount_lamports, body.slippage_bps,
        )
    except Exception as exc:  # noqa: BLE001 — un aggregatore irraggiungibile non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"Jupiter unreachable: {exc}") from exc
    return {k: v for k, v in quote.items() if k != "raw"}


class OnchainQuoteRobinhoodRequest(BaseModel):
    token_in: str
    token_out: str
    fee: int | None = None  # None = trova da solo il fee tier giusto, vedi sotto
    amount_in: int


@app.post("/api/onchain/quote/robinhood")
async def onchain_quote_robinhood(
    body: OnchainQuoteRobinhoodRequest, user: User = Depends(auth.get_current_user),
) -> dict:
    """Preventivo reale su Robinhood Chain: Factory per trovare il pool,
    QuoterV2 per il prezzo. Nessuna chiave, nessun impegno a eseguire —
    il gemello di /quote/solana, per l'altra chain.

    Il pool di una coppia puo' stare su un fee tier diverso da 0.3%
    (3000, il default che il frontend usava finora): Lorenzo, con
    $MYCEL, ha trovato "nessun pool" quando in realta' il pool c'era, su
    un altro tier. find_pool_and_fee() li prova tutti, provando prima
    quello passato (se il chiamante lo conosce gia')."""
    _require_trading()
    _require_onchain_enabled()
    found = None
    try:
        found = await onchain_quotes.find_pool_and_fee(body.token_in, body.token_out, body.fee)
        if found is not None:
            pool, fee = found
            quote = await onchain_quotes.uniswap_v3_quote(body.token_in, body.token_out, fee, body.amount_in)
    except Exception as exc:  # noqa: BLE001 — l'RPC che cade non ferma il preventivo, ripiega sotto
        logger.warning("uniswap v3 quote failed %s -> %s: %s", body.token_in, body.token_out, exc)
        found = None
    if found is None:
        # Fuori da Uniswap v3 (MYCE: solo Pons e v4), o con l'RPC giu', il
        # prezzo arriva da un aggregatore che vede tutti i DEX. Lo swap
        # non e' ancora collegato a quel percorso: "route" lo dice al frontend.
        try:
            quote = await onchain_aggregator.quote(body.token_in, body.token_out, body.amount_in)
        except onchain_aggregator.AggregatorError as exc:
            logger.warning("aggregator quote failed %s -> %s: %s", body.token_in, body.token_out, exc)
            raise HTTPException(status_code=400, detail="No market for this pair right now.") from exc
        return {
            "route": "aggregator", "source": quote["source"], "pool": None, "fee": None,
            "in_amount": body.amount_in, "out_amount": quote["amount_out"],
            "price_impact_pct": quote["price_impact_pct"],
        }
    # "out_amount", non "amount_out" come lo chiama quotes.py: stesso nome
    # di /quote/solana, cosi' il frontend legge un solo formato per entrambe.
    return {
        "route": "uniswap-v3", "pool": pool, "fee": fee, "in_amount": body.amount_in,
        "out_amount": quote["amount_out"], "price_impact_pct": quote["price_impact_pct"],
    }


async def _onchain_token_meta(chain: str, address: str) -> dict:
    """Metadati minimi per un token qualsiasi: decimali mai indovinati
    (stesso principio di seed-0010, lookup on-chain reale), simbolo e
    prezzo come bonus via DexScreener quando disponibile.
    Condiviso da /token (per lo swap) e /portfolio (per elencare i saldi
    reali del wallet) — un solo posto che sa risolvere un indirizzo."""
    decimals = await onchain_token_metadata.get_token_decimals(chain, address)

    # Simbolo e prezzo dal pool piu' liquido su DexScreener, su entrambe
    # le chain e da qualunque lato del pool: prima su Robinhood Chain
    # restavano nulli e nello swap USDG compariva come "0x5fc5".
    symbol, price_usd = None, None
    try:
        pairs = await _dexscreener_token_pairs(chain, address)
    except HTTPException:  # il prezzo/simbolo sono un bonus, mai bloccanti
        pairs = []
    if pairs:
        symbol = _pair_sides(pairs[0], address)[0].get("symbol")
        price_usd = _pair_price_usd(pairs[0], address)
    return {"address": address, "decimals": decimals, "symbol": symbol, "price_usd": price_usd}


@app.get("/api/onchain/token/{chain}/{address}")
async def onchain_token_info(chain: str, address: str, user: User = Depends(auth.get_current_user)) -> dict:
    """Metadati minimi per uno swap con un token qualsiasi, non solo le due
    coppie native che il pannello proponeva finora — Lorenzo: "lo swap deve
    essere possibile in ogni valuta o crypto possibile"."""
    _require_onchain_enabled()
    if chain not in ("solana", "robinhood"):
        raise HTTPException(status_code=400, detail="Unsupported chain.")
    try:
        return await _onchain_token_meta(chain, address)
    except Exception as exc:  # noqa: BLE001 — un indirizzo sbagliato non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"Could not fetch decimals: {exc}") from exc


@app.get("/api/onchain/portfolio/{chain}")
async def onchain_portfolio(chain: str, user: User = Depends(auth.get_current_user)) -> dict:
    """Cosa detiene DAVVERO il wallet, non solo le posizioni che questo
    sito ha tracciato — Lorenzo: "avere un wallet per capire il
    portafoglio per sapere cosa deteniamo". Su Solana e' completo (RPC
    vera); su Robinhood Chain e' uno scan dei soli token che questo sito
    conosce (vedi balances.all_token_balances) — limite dichiarato in
    `partial`, non nascosto in un elenco che sembra completo e non lo e'."""
    _require_onchain_enabled()
    if chain not in ("solana", "robinhood"):
        raise HTTPException(status_code=400, detail="Unsupported chain.")
    wallet = user.onchain_wallets.get(chain)
    if wallet is None:
        raise HTTPException(status_code=400, detail=f"No {chain} wallet for this user.")
    try:
        holdings = await _portfolio_holdings(user, chain)
    except Exception as exc:  # noqa: BLE001 — l'RPC che non risponde non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"RPC unreachable: {exc}") from exc
    return {
        "chain": chain, "holdings": holdings,
        "partial": chain == "robinhood",  # vedi la docstring: qui non e' un elenco esaustivo
    }


async def _portfolio_holdings(user: User, chain: str) -> list[dict]:
    """The tokens one wallet holds, priced (shared by /portfolio and the profile)."""
    wallet = user.onchain_wallets[chain]
    known_evm_tokens = []
    if chain == "robinhood":
        known_evm_tokens.append(_ONCHAIN_QUOTE_TOKEN["robinhood"])
        for order in onchain_ledger.list_limit_orders(user.id, "robinhood"):
            known_evm_tokens += [order["token_in"], order["token_out"]]

    raw_balances = await onchain_balances.all_token_balances(chain, wallet.address, known_evm_tokens)

    async def _enrich(entry: dict) -> dict:
        try:
            meta = await _onchain_token_meta(chain, entry["address"])
        except Exception:  # noqa: BLE001 — un token che il metadata non risolve resta con l'indirizzo grezzo
            meta = {"symbol": None, "price_usd": None, "decimals": entry.get("decimals")}
        amount = entry["amount"] if "amount" in entry else entry["amount_raw"] / (10 ** meta["decimals"])
        return {
            "address": entry["address"], "symbol": meta["symbol"] or entry["address"][:6],
            "amount": amount, "price_usd": meta["price_usd"],
            "value_usd": amount * meta["price_usd"] if meta["price_usd"] is not None else None,
        }

    return list(await asyncio.gather(*(_enrich(e) for e in raw_balances)))


@app.get("/api/onchain/price/{chain}/{address}")
async def onchain_live_price(chain: str, address: str, user: User = Depends(auth.get_current_user)) -> dict:
    """Prezzo corrente, non storico — pensato per un polling stretto (2-3s)
    dove le candele (8s, DexScreener/GeckoTerminal) sarebbero troppo lente.
    Non e' un feed nuovo: e' lo stesso aggregatore/quoter gia' usato per i
    preventivi di swap (Jupiter su Solana, QuoterV2 su Robinhood Chain),
    che a differenza di DexScreener e' fatto apposta per essere interrogato
    spesso — ogni UI di swap lo fa ad ogni tasto. Chiede il prezzo di 1
    unita' del token contro il token di quota (USDC/USDG), stessa logica
    di un preventivo, mai un secondo feed da mantenere."""
    _require_onchain_enabled()
    if chain not in ("solana", "robinhood"):
        raise HTTPException(status_code=400, detail="Unsupported chain.")
    quote_token = _ONCHAIN_QUOTE_TOKEN[chain]
    if address.lower() == quote_token.lower():
        return {"price_usd": 1.0}  # il token di quota e' la stablecoin stessa

    native_address = onchain_prices.NATIVE_TOKEN_ADDRESS[chain]
    try:
        if address.lower() == native_address.lower():
            decimals = _NATIVE_DECIMALS[chain]
        else:
            decimals = await onchain_token_metadata.get_token_decimals(chain, address)
        if decimals is None:
            raise HTTPException(status_code=502, detail="Could not fetch token decimals.")
        one_unit = 10 ** decimals
        if chain == "solana":
            quote = await onchain_quotes.jupiter_quote(address, quote_token, one_unit, 50)
        else:
            found = await onchain_quotes.find_pool_and_fee(address, quote_token)
            if found is None:
                # Fuori da Uniswap v3 (Pons, v4...) il prezzo e' quello del
                # pool piu' liquido su DexScreener, non un errore a schermo.
                pairs = await _dexscreener_token_pairs(chain, address)
                price = _pair_price_usd(pairs[0], address) if pairs else None
                if price is None:
                    raise HTTPException(status_code=404, detail="Price not available for this token.")
                return {"price_usd": price}
            _pool, fee = found
            quote = await onchain_quotes.uniswap_v3_quote(address, quote_token, fee, one_unit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — un quote fallito non deve essere un 500 muto
        logger.info("onchain live price failed for %s/%s: %s", chain, address, exc)
        raise HTTPException(status_code=502, detail="Price not available, retrying shortly.") from exc

    out_amount = quote["out_amount"] if chain == "solana" else quote["amount_out"]
    return {"price_usd": out_amount / (10 ** _ONCHAIN_QUOTE_DECIMALS)}


_DEX_PAIRS_TTL_S = 10
_dex_pairs_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


async def _dexscreener_token_pairs(chain: str, address: str) -> list[dict]:
    """Tutti i pool di un token su una chain, qualsiasi DEX, dal piu'
    liquido — la stessa vista che Lorenzo usa su DexScreener. Cache breve:
    grafico e prezzo live la chiedono insieme ogni pochi secondi."""
    key = (chain, address.lower())
    cached = _dex_pairs_cache.get(key)
    if cached and _time.time() - cached[0] < _DEX_PAIRS_TTL_S:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
        pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == chain] if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="DexScreener not responding, retrying shortly.") from exc
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    _dex_pairs_cache[key] = (_time.time(), pairs)
    return pairs


def _pair_sides(pair: dict, address: str) -> tuple[dict, dict]:
    """(il token cercato, l'altro lato): il token puo' essere la quota."""
    base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
    return (quote, base) if (quote.get("address") or "").lower() == address.lower() else (base, quote)


def _pair_price_usd(pair: dict, address: str) -> float | None:
    """priceUsd e' sempre il prezzo della base; per la quota si divide per
    priceNative (quante quote vale una base)."""
    if not pair.get("priceUsd"):
        return None
    base_usd = float(pair["priceUsd"])
    if _pair_sides(pair, address)[0] is pair.get("baseToken"):
        return base_usd
    native = float(pair.get("priceNative") or 0)
    return base_usd / native if native else None


@app.get("/api/onchain/chart/{chain}/{address}")
async def onchain_chart(
    chain: str, address: str, interval: str = "15m", user: User = Depends(auth.get_current_user),
) -> dict:
    """Candele vere per un token del wallet — stesso motore del sito
    principale (DexScreener + GeckoTerminal, `providers/dexscreener.py`),
    non un secondo grafico da inventare. Volutamente leggero: qui non
    girano strategia/backtest come in /api/analyze, che per un grafico nel
    wallet sarebbero solo latenza sprecata (Lorenzo: "minor delay
    possibile")."""
    _require_onchain_enabled()
    if chain not in ("solana", "robinhood"):
        raise HTTPException(status_code=400, detail="Unsupported chain.")
    pairs = await _dexscreener_token_pairs(chain, address)
    if not pairs:
        raise HTTPException(status_code=404, detail="No market for this token on this chain.")
    # Il pool piu' liquido puo' non avere candele (GeckoTerminal non lo
    # indicizza ancora, o limita le richieste): si prova il successivo
    # invece di lasciare il grafico vuoto.
    df, top = None, None
    for pair in pairs[:3]:
        symbol = f"{pair['baseToken']['symbol']}@{chain}:{pair['pairAddress']}"
        try:
            df = await dexscreener_provider.fetch_ohlcv(symbol, interval, 200, token_address=address)
        except (ProviderError, ProviderUnreachable) as exc:
            logger.info("onchain chart: no candles from %s: %s", pair["pairAddress"], exc)
            continue
        top = pair
        break
    if df is None:
        raise HTTPException(status_code=502, detail="Chart not available for this token, retrying shortly.")
    pool_address = top["pairAddress"]
    candles = [
        {"time": int(ts.timestamp()), "open": float(row.open), "high": float(row.high),
         "low": float(row.low), "close": float(row.close)}
        for ts, row in df.iterrows()
    ]
    created_ms = top.get("pairCreatedAt")
    own, other = _pair_sides(top, address)
    stats = {
        "base_symbol": own.get("symbol"),
        "quote_symbol": other.get("symbol"),
        "price_usd": _pair_price_usd(top, address),
        "price_change_h24": (top.get("priceChange") or {}).get("h24") if own is top.get("baseToken") else None,
        "liquidity_usd": (top.get("liquidity") or {}).get("usd"),
        "volume_h24": (top.get("volume") or {}).get("h24"),
        "age_hours": (_time.time() * 1000 - created_ms) / 3_600_000 if created_ms else None,
    }
    return {"symbol": symbol, "pool_address": pool_address, "candles": candles, "stats": stats}


@app.get("/api/onchain/tokens/search")
async def onchain_token_search(
    chain: Literal["solana", "robinhood"], query: str = Query(..., min_length=1, max_length=20),
    user: User = Depends(auth.get_current_user),
) -> list[dict]:
    """Un solo token per ticker, il contratto verificato con piu'
    liquidita' (Lorenzo: "voglio solo un unico risultato per ticker...
    non voglio dover scegliere altri doppioni"). La fonte e' per chain,
    vedi onchain_tokens; se non risponde si ripiega sulla ricerca
    DexScreener del resto del sito, sempre un risultato per ticker ma
    senza la verifica del contratto."""
    _require_onchain_enabled()
    try:
        return await onchain_tokens.search(chain, query)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("onchain token search via primary source failed (%s): %s", chain, exc)

    results = await dexscreener_provider.search_symbols(query)
    out, seen = [], set()
    for r in results:  # gia' ordinati per liquidita', il primo per ticker vince
        try:
            _base, symbol_chain, _pool = _parse_dex_symbol(r["symbol"])
        except Exception:  # noqa: BLE001 — un simbolo malformato si scarta, non blocca la ricerca
            continue
        ticker = onchain_tokens.normalize_symbol(r["symbol"].split("@")[0])
        if symbol_chain != chain or not r.get("base_token_address") or ticker in seen:
            continue
        seen.add(ticker)
        out.append({
            "symbol": ticker, "name": r["name"], "address": r["base_token_address"],
            "decimals": None, "verified": False, "icon": None, "liquidity_usd": None,
        })
    return out


# ─────────────────────────────────────────── ordini limite (piano DEX) ────

class LimitOrderCreateRequest(BaseModel):
    chain: Literal["solana", "robinhood"]
    token_in: str
    token_out: str
    token_in_symbol: str
    token_out_symbol: str
    token_in_decimals: int
    token_out_decimals: int
    amount_in: float
    direction: Literal["buy_below", "sell_above"]
    target_price_usd: float


@app.post("/api/onchain/limit-orders")
async def create_limit_order(body: LimitOrderCreateRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Un ordine limite qui non e' un book on-chain vero (ne' Jupiter ne'
    Uniswap v3 ne hanno uno resting): e' size+prezzo+direzione persistiti,
    eseguiti da limit_order_loop quando il prezzo tocca il target, con gli
    stessi guardrail di un ordine manuale — mai un secondo motore che
    firma scavalcandoli.

    Un lato deve essere il token nativo della chain: e' l'unico per cui
    questa pagina conosce un prezzo USD affidabile (GeckoTerminal, stesso
    feed della hero) senza dipendere da DexScreener, che non indicizza
    Robinhood Chain — un ordine su due token entrambi non nativi non
    avrebbe un prezzo da confrontare col target."""
    _require_trading()
    _require_onchain_enabled()
    if body.chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"No {body.chain} wallet for this user.")
    native = onchain_prices.NATIVE_TOKEN_ADDRESS[body.chain]
    if native.lower() not in (body.token_in.lower(), body.token_out.lower()):
        raise HTTPException(
            status_code=400,
            detail="A limit order needs the native token on one side: it's the only price we can verify.",
        )
    if not (math.isfinite(body.amount_in) and math.isfinite(body.target_price_usd)) \
            or body.amount_in <= 0 or body.target_price_usd <= 0:
        raise HTTPException(status_code=400, detail="Size and target price must be positive.")
    # Decimals turn amount_in into the raw amount that gets signed: read
    # them from the chain, never from the request (audit 29/09, S02).
    for address, claimed in ((body.token_in, body.token_in_decimals), (body.token_out, body.token_out_decimals)):
        real = await onchain_valuation.token_decimals(body.chain, address)
        if real is None or real != claimed:
            raise HTTPException(status_code=400, detail="Token decimals don't match the chain: order not saved.")
    return onchain_ledger.create_limit_order(
        user.id, body.chain, body.token_in, body.token_out,
        body.token_in_symbol, body.token_out_symbol, body.token_in_decimals, body.token_out_decimals,
        body.amount_in, body.direction, body.target_price_usd,
    )


@app.get("/api/onchain/limit-orders/{chain}")
async def list_limit_orders(chain: str, user: User = Depends(auth.get_current_user)) -> list[dict]:
    _require_trading()
    return onchain_ledger.list_limit_orders(user.id, chain)


@app.delete("/api/onchain/limit-orders/{order_id}")
async def cancel_limit_order(order_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    cancelled = onchain_ledger.cancel_limit_order(order_id, user.id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Order not found or can no longer be cancelled.")
    return {"cancelled": True}


@app.get("/api/onchain/history/{chain}")
async def onchain_history(chain: str, user: User = Depends(auth.get_current_user)) -> dict:
    """Cronologia unificata (piano DEX): swap eseguiti e ordini limite,
    aperti o conclusi, ordinati insieme — non piu' due schermate separate."""
    orders = onchain_ledger.list_orders(user.id, chain)
    limit_orders = onchain_ledger.list_limit_orders(user.id, chain)
    return {
        "orders": [{
            "kind": "swap", "id": o["idempotency_key"], "token": o["token"], "side": o["side"],
            "status": o["status"], "size_usd": o["size_usd"], "tx_signature": o["tx_signature"],
            "reason": o["reason"], "created_at": o["created_at"], "updated_at": o["updated_at"],
        } for o in orders],
        "limit_orders": [{
            "kind": "limit", "id": lo["id"],
            "pair": f"{lo['token_in_symbol']}→{lo['token_out_symbol']}",
            "direction": lo["direction"], "amount_in": lo["amount_in"],
            "target_price_usd": lo["target_price_usd"], "status": lo["status"],
            "reason": lo["reason"], "created_at": lo["created_at"], "updated_at": lo["updated_at"],
        } for lo in limit_orders],
    }


async def _check_limit_order(order: dict) -> None:
    """Un giro di verifica per un ordine ancora 'open' — mai un'eccezione
    che risale a limit_order_loop, che deve continuare a girare per tutti
    gli altri ordini anche se questo fallisce."""
    chain = order["chain"]
    native_addr = onchain_prices.NATIVE_TOKEN_ADDRESS[chain]
    native_is_in = order["token_in"].lower() == native_addr.lower()
    try:
        native_usd = await onchain_prices.native_price_usd(chain)
        if native_usd is None:
            return  # rete/feed giu' questo giro: ritenta al prossimo, non e' un fallimento dell'ordine

        amount_in_units = int(round(order["amount_in"] * 10 ** order["token_in_decimals"]))
        if chain == "solana":
            quote = await onchain_quotes.jupiter_quote(order["token_in"], order["token_out"], amount_in_units, 50)
        else:
            found = await onchain_quotes.find_pool_and_fee(order["token_in"], order["token_out"])
            if found is None:
                return
            _pool, fee = found
            quote = await onchain_quotes.uniswap_v3_quote(order["token_in"], order["token_out"], fee, amount_in_units)

        out_amount_units = quote["out_amount"] / (10 ** order["token_out_decimals"])
        if out_amount_units <= 0:
            return

        # Il prezzo USD del lato NON nativo, dedotto dal quote vero (non da
        # DexScreener, che non copre Robinhood Chain): quanto nativo si
        # scambia per l'altro token, moltiplicato per il prezzo noto del
        # nativo — stesso principio di prices.py, mai un numero inventato.
        current_price_usd = (
            (order["amount_in"] * native_usd) / out_amount_units if native_is_in
            else (out_amount_units * native_usd) / order["amount_in"]
        )

        triggered = (
            (order["direction"] == "buy_below" and current_price_usd <= order["target_price_usd"])
            or (order["direction"] == "sell_above" and current_price_usd >= order["target_price_usd"])
        )
        if not triggered:
            return

        user = auth.get_user(order["owner_id"])
        if user is None:
            onchain_ledger.mark_limit_order_failed(order["id"], "user not found")
            return
        private_key_hex = auth.get_decrypted_onchain_private_key(user, chain)

        notional_usd = order["amount_in"] * native_usd if native_is_in else out_amount_units * native_usd
        side = "buy" if order["direction"] == "buy_below" else "sell"
        token = order["token_out_symbol"] if order["direction"] == "buy_below" else order["token_in_symbol"]

        result = await onchain_execute.execute_order(
            order["owner_id"], chain, token, "limit order", side,
            notional_usd, quote["price_impact_pct"], current_price_usd,
            private_key_hex, quote,
        )
        if result["accepted"]:
            onchain_ledger.mark_limit_order_filled(order["id"], result["idempotency_key"])
            _maybe_alert_onchain_risk_event(user.email, chain, result, notional_usd)
        # Rifiutato dal guardrail (es. tetto giornaliero): resta 'open' e
        # riprova al giro dopo — un tetto puo' liberarsi, un ordine limite
        # non deve morire per un rifiuto temporaneo.
    except auth.OnchainKeyIntegrityError as exc:
        onchain_ledger.mark_limit_order_failed(order["id"], f"key unreadable: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("limit_order_loop: ordine %s, errore nel check: %s", order["id"], exc)


async def limit_order_loop() -> None:
    """Accanto a _watchlist_alert_loop, stesso pattern: un giro periodico
    che legge lo stato persistito e agisce, mai un secondo processo o un
    secondo storage."""
    while True:
        try:
            for chain in ("solana", "robinhood"):
                for order in onchain_ledger.list_open_limit_orders(chain):
                    await _check_limit_order(order)
        except Exception:  # noqa: BLE001 — il ciclo non deve mai fermarsi del tutto
            logger.exception("limit_order_loop: giro fallito")
        await asyncio.sleep(45)


def _maybe_alert_onchain_risk_event(owner_email: str, chain: str, result: dict, notional_usd: float) -> None:
    """Alert Telegram immediati per gli eventi di rischio del piano SRE
    on-chain (punto 5) — chiamato subito dopo simulate_order/execute_order,
    che sono l'unico posto dove questi eventi accadono davvero oggi.

    "Circuit breaker scattato" e "tetto giornaliero raggiunto" si leggono
    dal `reason` del rifiuto (risk.run_all_checks si ferma al primo check
    che fallisce e lo scrive in italiano, gia' pensato per essere loggato/
    notificato — vedi risk.py). "Posizione sopra il 30% del tetto
    per-trade" e' un avviso preventivo, non un rifiuto: un ordine ACCETTATO
    ma vicino al tetto merita un occhio prima che il prossimo lo superi.

    "rug-check fallito su un pool in allowlist": TODO — richiede
    `pool_safety.py` (allowlist pool), che un'altra sessione/agente stava
    implementando in parallelo e non esiste ancora in questo albero. Firma
    prevista per quando arrivera':
        async def alert_rug_check_failed(owner_email: str, chain: str, token: str, reason: str) -> None
    da chiamare dove pool_safety segnala il fallimento, con lo stesso
    `telegram.send_infra_alert()` usato qui sotto."""
    reason = (result.get("reason") or "")
    if "circuit breaker active" in reason:
        _fire(telegram.send_infra_alert(
            f"Circuit breaker scattato su {chain} (utente {owner_email}): {reason}"
        ))
        return
    if "daily cap" in reason:
        _fire(telegram.send_infra_alert(
            f"Tetto giornaliero raggiunto su {chain} (utente {owner_email}): {reason}"
        ))
        return
    if result.get("accepted") and notional_usd > 0.30 * settings.onchain_max_usd_per_trade:
        _fire(telegram.send_infra_alert(
            f"Ordine on-chain da {notional_usd:.2f} USD su {chain} (utente {owner_email}) "
            f"sopra il 30% del tetto per operazione ({settings.onchain_max_usd_per_trade:.2f} USD)."
        ))


class OnchainSimulateRequest(BaseModel):
    chain: OnchainChain
    token: str
    strategy_name: str
    side: Literal["buy", "sell"]
    notional_usd: float
    price_impact_pct: float
    price_usd: float
    pool_address: str | None = None  # opzionale: allowlist pool + size dinamica (vedi pool_safety.py)


@app.post("/api/onchain/simulate")
async def onchain_simulate_order(body: OnchainSimulateRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Fase 2: paper trading. Stessi guardrail della Fase 3 (tetti, price
    impact, concentrazione, circuit breaker, allowlist pool se pool_address
    e' passato), ma mode='simulated' nel ledger — nessuna chiave letta,
    nessuna transazione, mai."""
    _require_trading()
    _require_onchain_enabled()
    result = await onchain_simulate.simulate_order(
        user.id, body.chain, body.token, body.strategy_name, body.side,
        body.notional_usd, body.price_impact_pct, body.price_usd,
        pool_address=body.pool_address,
    )
    _maybe_alert_onchain_risk_event(user.email, body.chain, result, body.notional_usd)
    return result


class OnchainExecuteSolanaRequest(BaseModel):
    token: str
    strategy_name: str
    side: Literal["buy", "sell"]
    input_mint: str
    output_mint: str
    amount_lamports: int = Field(gt=0)
    # Ignored: the server values the order itself (audit 29/09, S02). Kept so
    # older pages that still send them are not rejected.
    notional_usd: float | None = None
    price_usd: float | None = None
    slippage_bps: int = Field(default=50, ge=1, le=1000)
    pool_address: str | None = None  # opzionale: indirizzo del pool/pair per l'allowlist (DexScreener, vedi pool_safety.py)
    client_order_id: str | None = Field(default=None, max_length=80)   # one per click: retries don't trade twice


async def _server_value(chain: str, token_in: str, amount_in: int, token_out: str, amount_out: int, side: str) -> dict:
    value = await onchain_valuation.swap_value(chain, token_in, amount_in, token_out, amount_out, side)
    if value is None:
        raise HTTPException(status_code=400, detail="Can't value this order on the server (no price or decimals): not sent.")
    return value


@app.post("/api/onchain/execute/solana")
async def onchain_execute_solana(
    body: OnchainExecuteSolanaRequest, user: User = Depends(auth.get_current_user),
) -> dict:
    """Fase 4: l'ordine vero su Solana — stessi guardrail della Fase 2/3,
    ma con la chiave del wallet e (solo se settings.onchain_execution_enabled)
    un invio reale. Il quote e' preso fresco da Jupiter qui dentro, mai
    accettato dal client: e' l'unica fonte di price_impact_pct affidabile
    per i guardrail.

    Jupiter aggrega su piu' pool, quindi non c'e' un unico "pool_address"
    che il quote possa dare da solo: se il client conosce il pool
    principale (es. dal frontend/sniper) lo passa lui, e allowlist + size
    dinamica lo usano; se non lo passa, il comportamento resta quello di
    prima (nessuna riduzione, nessun controllo di allowlist)."""
    _require_trading()
    _require_onchain_enabled()
    if "solana" not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail="No Solana wallet for this user.")
    try:
        quote = await onchain_quotes.jupiter_quote(
            body.input_mint, body.output_mint, body.amount_lamports, body.slippage_bps,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Jupiter unreachable: {exc}") from exc
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, "solana")
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    value = await _server_value("solana", body.input_mint, quote["in_amount"], body.output_mint,
                                quote["out_amount"], body.side)
    result = await onchain_execute.execute_order(
        user.id, "solana", body.token, body.strategy_name, body.side,
        value["notional_usd"], quote["price_impact_pct"], value["price_usd"],
        private_key_hex, quote, pool_address=body.pool_address, intent_id=body.client_order_id,
    )
    _maybe_alert_onchain_risk_event(user.email, "solana", result, value["notional_usd"])
    return result


class OnchainExecuteRobinhoodRequest(BaseModel):
    token: str
    strategy_name: str
    side: Literal["buy", "sell"]
    token_in: str
    token_out: str
    fee: int | None = None  # None = trova da solo il fee tier giusto, come /quote/robinhood
    amount_in: int = Field(gt=0)
    notional_usd: float | None = None   # ignored, see OnchainExecuteSolanaRequest
    price_usd: float | None = None
    max_slippage_pct: float = Field(default=1.0, gt=0, le=10)
    client_order_id: str | None = Field(default=None, max_length=80)


@app.post("/api/onchain/execute/robinhood")
async def onchain_execute_robinhood(
    body: OnchainExecuteRobinhoodRequest, user: User = Depends(auth.get_current_user),
) -> dict:
    """Fase 4 su Robinhood Chain: stesso schema di execute/solana — quote
    fresco (Factory + QuoterV2, verificati dal vivo) preso qui dentro, mai
    dal client, poi guardrail, firma, e (solo se acceso) invio reale via
    SwapRouter02. A differenza di Solana, nessun invio vero e' mai stato
    provato in questa sessione: il percorso e' reale (indirizzi e ABI
    verificati dal vivo, quote vero sul pool WETH/USDG), ma senza
    l'esperienza pratica del broadcast — vedi execute.py.

    Niente pool_address passato a execute_order qui: DexScreener non
    indicizza ancora Robinhood Chain (troppo nuova, vedi pool_safety.py),
    quindi l'allowlist pool fallirebbe chiusa su ogni ordine per un dato
    che nessuna fonte reale puo' dare oggi — non e' un buco nascosto, e'
    la stessa scelta "mai fabbricare dati" del resto del modulo."""
    _require_trading()
    _require_onchain_enabled()
    if "robinhood" not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail="No Robinhood Chain wallet for this user.")
    try:
        found = await onchain_quotes.find_pool_and_fee(body.token_in, body.token_out, body.fee)
        if found is None:
            raise HTTPException(status_code=400, detail="No pool for this pair on any Uniswap v3 fee tier.")
        pool, fee = found
        quote = await onchain_quotes.uniswap_v3_quote(body.token_in, body.token_out, fee, body.amount_in)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Robinhood Chain unreachable: {exc}") from exc
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, "robinhood")
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    amount_out_minimum = int(quote["amount_out"] * (1 - body.max_slippage_pct / 100))
    value = await _server_value("robinhood", body.token_in, body.amount_in, body.token_out,
                                quote["amount_out"], body.side)
    result = await onchain_execute.execute_order(
        user.id, "robinhood", body.token, body.strategy_name, body.side,
        value["notional_usd"], quote["price_impact_pct"], value["price_usd"],
        private_key_hex,
        {"token_in": body.token_in, "token_out": body.token_out, "fee": fee,
         "amount_in": body.amount_in, "amount_out_minimum": amount_out_minimum},
        intent_id=body.client_order_id,
    )
    _maybe_alert_onchain_risk_event(user.email, "robinhood", result, value["notional_usd"])
    return result


class OnchainSendRequest(BaseModel):
    chain: OnchainChain
    to_address: str
    token_address: str | None = None  # None = il nativo (SOL/ETH)
    amount: float  # unita' umane, non raw
    price_usd: float | None = None  # ignored: the server prices the withdrawal itself (audit 29/09, S02)
    client_order_id: str | None = Field(default=None, max_length=80)


@app.post("/api/onchain/send")
async def onchain_send_token(body: OnchainSendRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Prelievo: manda crypto (nativo o un token) a un indirizzo qualsiasi
    — Lorenzo: "aggiungi funzione anche per prelevare le crypto e
    inviarle ad indirizzi". Stessi guardrail di un ordine manuale (tetto
    per operazione/giorno, circuit breaker), MAI la concentrazione: un
    prelievo riduce sempre una posizione, la scelta di diversificazione
    resta di chi firma. Nessuna validazione dell'indirizzo di
    destinazione oltre a quella che la firma stessa impone (un indirizzo
    malformato fa fallire la costruzione della transazione con un errore
    leggibile) — nessun modo di sapere se un indirizzo VALIDO e' anche
    quello giusto, e non e' compito di un guardrail automatico deciderlo."""
    _require_onchain_enabled()
    if body.chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"No {body.chain} wallet for this user.")
    if not (math.isfinite(body.amount) and body.amount > 0):
        raise HTTPException(status_code=400, detail="Size must be positive.")

    is_native = body.token_address is None or body.token_address.lower() == onchain_prices.NATIVE_TOKEN_ADDRESS[body.chain].lower()
    if is_native:
        decimals = _NATIVE_DECIMALS[body.chain]
        token_label = "SOL" if body.chain == "solana" else "ETH"
    else:
        try:
            decimals = await onchain_token_metadata.get_token_decimals(body.chain, body.token_address)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not fetch decimals: {exc}") from exc
        if decimals is None:
            raise HTTPException(status_code=502, detail="Could not fetch token decimals.")
        token_label = body.token_address

    amount_raw = int(round(body.amount * 10 ** decimals))
    price_usd = await onchain_valuation.token_price_usd(body.chain, None if is_native else body.token_address)
    if price_usd is None:
        raise HTTPException(status_code=400, detail="Can't price this token on the server: withdrawal not sent.")
    notional_usd = body.amount * price_usd

    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, body.chain)
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = await onchain_send.send_token(
        user.id, body.chain, token_label, body.to_address, body.amount, amount_raw,
        notional_usd, private_key_hex, is_native, body.token_address, intent_id=body.client_order_id,
    )
    _maybe_alert_onchain_risk_event(user.email, body.chain, result, notional_usd)
    return result


class OnchainExecuteTwapRequest(BaseModel):
    chain: OnchainChain
    token: str
    strategy_name: str
    side: Literal["buy", "sell"]
    total_usd: float | None = None   # ignored: valued on the server (audit 29/09, S02)
    price_usd: float | None = None
    pool_liquidity_usd: float
    # Solana: la fetta scala amount_lamports proporzionalmente a total_amount_lamports
    input_mint: str | None = None
    output_mint: str | None = None
    total_amount_lamports: int | None = None
    slippage_bps: int = 50
    # Robinhood: la fetta scala amount_in proporzionalmente a total_amount_in
    token_in: str | None = None
    token_out: str | None = None
    fee: int | None = None
    total_amount_in: int | None = None
    max_slippage_pct: float = 1.0


@app.post("/api/onchain/execute/twap")
async def onchain_execute_twap(
    body: OnchainExecuteTwapRequest, user: User = Depends(auth.get_current_user),
) -> dict:
    """Fase 3 del piano: esecuzione scaglionata. Sotto un notional minimo
    (o su un pool profondo dove il numero di fette collassa a 1) esegue in
    un colpo unico; altrimenti scaglia via twap_jobs.execute_twap_job(),
    che persiste il job PRIMA di firmare la prima fetta. Il quote di ogni
    fetta e' costruito qui (mai dal client) scalando gli importi totali
    proporzionalmente alla size della fetta."""
    _require_trading()
    _require_onchain_enabled()
    if body.chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"No {body.chain} wallet for this user.")
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, body.chain)
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if body.chain == "solana":
        if not (body.input_mint and body.output_mint and body.total_amount_lamports):
            raise HTTPException(status_code=400, detail="input_mint/output_mint/total_amount_lamports required for solana.")

        async def quote_for_slice(slice_usd: float) -> tuple[float, dict]:
            amount_lamports = max(1, round(body.total_amount_lamports * (slice_usd / body.total_usd)))
            quote = await onchain_quotes.jupiter_quote(
                body.input_mint, body.output_mint, amount_lamports, body.slippage_bps,
            )
            return quote["price_impact_pct"], quote

        quote_meta = {
            "input_mint": body.input_mint, "output_mint": body.output_mint,
            "total_amount_lamports": body.total_amount_lamports, "slippage_bps": body.slippage_bps,
        }
    else:
        if not (body.token_in and body.token_out and body.fee is not None and body.total_amount_in):
            raise HTTPException(status_code=400, detail="token_in/token_out/fee/total_amount_in required for robinhood.")

        async def quote_for_slice(slice_usd: float) -> tuple[float, dict]:
            amount_in = max(1, round(body.total_amount_in * (slice_usd / body.total_usd)))
            quote = await onchain_quotes.uniswap_v3_quote(body.token_in, body.token_out, body.fee, amount_in)
            amount_out_minimum = int(quote["amount_out"] * (1 - body.max_slippage_pct / 100))
            return quote["price_impact_pct"], {
                "token_in": body.token_in, "token_out": body.token_out, "fee": body.fee,
                "amount_in": amount_in, "amount_out_minimum": amount_out_minimum,
            }

        quote_meta = {
            "token_in": body.token_in, "token_out": body.token_out, "fee": body.fee,
            "total_amount_in": body.total_amount_in, "max_slippage_pct": body.max_slippage_pct,
        }

    # The total's value and price come from a quote of the whole amount, not
    # from the request (audit 29/09, S02).
    try:
        if body.chain == "solana":
            full = await onchain_quotes.jupiter_quote(
                body.input_mint, body.output_mint, body.total_amount_lamports, body.slippage_bps)
            value = await _server_value("solana", body.input_mint, full["in_amount"], body.output_mint,
                                        full["out_amount"], body.side)
        else:
            full = await onchain_quotes.uniswap_v3_quote(body.token_in, body.token_out, body.fee, body.total_amount_in)
            value = await _server_value("robinhood", body.token_in, body.total_amount_in, body.token_out,
                                        full["amount_out"], body.side)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Quote unavailable: {exc}") from exc
    total_usd = value["notional_usd"]
    body = body.model_copy(update={"total_usd": total_usd})
    return await onchain_twap_jobs.execute_twap_job(
        user.id, body.chain, body.token, body.strategy_name, body.side,
        total_usd, value["price_usd"], private_key_hex, body.pool_liquidity_usd, quote_for_slice,
        quote_meta=quote_meta,
    )


@app.get("/api/onchain/ledger/pending")
async def onchain_ledger_pending(user: User = Depends(auth.get_current_user)) -> list[dict]:
    """Ordini 'submitted' senza mai una conferma — quello che un riavvio a
    meta' lascia indietro (vedi ledger.pending_unconfirmed). Utile per
    controllare a mano che la riconciliazione non abbia niente in sospeso."""
    _require_onchain_enabled()
    return onchain_ledger.pending_unconfirmed(user.id)


@app.get("/api/onchain/positions/{chain}")
async def onchain_positions(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> list[dict]:
    _require_onchain_enabled()
    return onchain_ledger.get_all_positions(user.id, chain)


@app.get("/api/onchain/risk/status/{chain}")
async def onchain_risk_status(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    _require_onchain_enabled()
    breaker = onchain_ledger.breaker_status(user.id, chain)
    spent_today = onchain_ledger.get_daily_spent(user.id, chain)
    return {
        "chain": chain,
        "circuit_breaker": breaker,
        "daily_spent_usd": spent_today,
        "daily_cap_usd": settings.onchain_max_usd_per_day,
        "per_trade_cap_usd": settings.onchain_max_usd_per_trade,
        "concentration_cap_pct": settings.onchain_max_token_concentration_pct,
        "execution_enabled": settings.onchain_execution_enabled,
    }


class SniperAutotradeRequest(BaseModel):
    enabled: bool


@app.get("/api/onchain/sniper-autotrade")
async def get_sniper_autotrade(user: User = Depends(auth.get_current_user)) -> dict:
    _require_trading()
    return {"chains": user.sniper_autotrade_chains}


@app.patch("/api/onchain/sniper-autotrade/{chain}")
async def set_sniper_autotrade(
    chain: OnchainChain, body: SniperAutotradeRequest, user: User = Depends(auth.get_current_user),
) -> dict:
    """Opt-in per lo sniper che aggiunge da solo nuovi arrivi on-chain alla
    watchlist con la strategia di Lorenzo attiva — vedi
    _sniper_onchain_bridge_loop. Spento di default per ogni utente,
    acceso solo qui, mai da un default globale."""
    _require_trading()
    if chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"No {chain} wallet for this user — create it first.")
    updated = auth.set_sniper_autotrade_chain(user.id, chain, body.enabled)
    return {"chains": updated.sniper_autotrade_chains}


def _pilot_token_view(user: User, entry: WatchlistEntry, chain: str, positions: dict, journal: list[dict]) -> dict:
    """Un token sotto il pilota, detto in parole: in che stato e' e perche'."""
    base_symbol = entry.symbol.split("@", 1)[0]
    found = onchain_pilot.discovery(user.id, entry.symbol)
    evaluation = onchain_pilot.last_evaluation(entry.id)
    mine = [j for j in journal if j.get("symbol") == entry.symbol]
    position = positions.get((entry.base_token_address or "").lower())

    events = [
        {"ts": j["ts"], "text": f"{_PILOT_SIGNAL_WORDS.get(j['signal_type'], j['signal_type'])} signal: "
                                f"{_PILOT_OUTCOME_WORDS.get(j['outcome'], j['outcome'])}" + (f" — {j['detail']}" if j.get("detail") else "")}
        for j in mine[:6]
    ]
    if found:
        events.append({"ts": found["added_at"], "text": "Added to watch (found by the sniper)"})

    if position:
        state, label = "in", "In position"
        reason = f"{position['quantity']:g} tokens, cost ${position['cost_usd']:,.2f}"
    elif mine and mine[0]["outcome"] != "eseguito":
        state, label = "blocked", "Signal not executed"
        reason = mine[0].get("detail") or _PILOT_OUTCOME_WORDS.get(mine[0]["outcome"], mine[0]["outcome"])
    elif mine:
        state, label = "watch", "Exited, watching"
        reason = f"last order: {_PILOT_SIGNAL_WORDS.get(mine[0]['signal_type'], mine[0]['signal_type'])}"
    else:
        state, label = "watch", "Watching the trend"
        reason = "no strategy signal so far"

    return {
        "symbol": base_symbol,
        "watch_symbol": entry.symbol,
        "chain": chain,
        "name": (found or {}).get("name") or base_symbol,
        "token_address": entry.base_token_address,
        "interval": entry.strategy_intervals.get(_SNIPER_ONCHAIN_STRATEGY, entry.interval),
        "added_at": (found or {}).get("added_at"),
        "liquidity_at_add": (found or {}).get("liquidity_usd"),
        "age_at_add": (found or {}).get("age_hours"),
        "score": (found or {}).get("score"),
        "last_eval_at": (evaluation or {}).get("ts"),
        "last_price": (evaluation or {}).get("price"),
        "position": position,
        "state": state,
        "state_label": label,
        "reason": reason,
        "events": sorted(events, key=lambda e: e["ts"], reverse=True),
    }


_PILOT_SIGNAL_WORDS = {
    "entry_long": "buy", "flip_to_long": "buy",
    "exit_long": "sell", "flip_to_short": "sell",
    "entry_short": "short", "exit_short": "close short",
}
_PILOT_OUTCOME_WORDS = {"eseguito": "executed", "saltato": "skipped", "fallito": "failed"}


@app.get("/api/onchain/radar")
async def onchain_radar_view(user: User = Depends(auth.get_current_user)) -> dict:
    """Token giovani su Solana e Robinhood che una strategia dell'archivio
    avrebbe tradato bene. Proposte, non ordini: vedi app/trading/onchain/radar.py."""
    _require_trading()
    _require_onchain_enabled()
    snap, gone = onchain_radar.snapshot(), onchain_outcomes.shamed()
    return {**snap, "proposals": onchain_outcomes.without_shamed(snap.get("proposals") or [], gone),
            "tested": onchain_outcomes.without_shamed(snap.get("tested") or [], gone),
            "listings": onchain_outcomes.without_shamed(onchain_listings.recent(), gone),
            "rejected": onchain_listings.rejected(), "pulse": onchain_listings.pulse(),
            "listing_status": onchain_listings.status(), "shame": onchain_outcomes.shame()}


_RADAR_FEED_MAX = 80


@app.get("/api/onchain/radar/feed")
async def onchain_radar_feed(user: User = Depends(auth.get_current_user)) -> dict:
    """What the scanner tab draws, and nothing else. The full /radar carried
    300 listings and 500 rejections with every check attached: megabytes the
    phone had to fetch inside the 8 s client timeout, so the tab stayed empty.
    Here: the newest decisions the feed can show, plus the 24h counts."""
    _require_trading()
    _require_onchain_enabled()
    snap, gone = onchain_radar.snapshot(), onchain_outcomes.shamed()
    listings = onchain_outcomes.without_shamed(onchain_listings.recent(), gone)
    rejected = onchain_listings.rejected()
    since = time.time() - 86_400
    newest = sorted([(x.get("found_at") or 0, 0, i) for i, x in enumerate(listings)]
                    + [(x.get("at") or 0, 1, i) for i, x in enumerate(rejected)], reverse=True)[:_RADAR_FEED_MAX]
    keep = {(k, i) for _, k, i in newest}
    return {"running": snap.get("running"), "last_scan": snap.get("last_scan"), "chains": snap.get("chains") or {},
            "pulse": onchain_listings.pulse(), "shame": onchain_outcomes.shame(),
            "listings": [x for i, x in enumerate(listings) if (0, i) in keep],
            "rejected": [x for i, x in enumerate(rejected) if (1, i) in keep],
            "counts": {"at": time.time(),
                       "passed_24h": sum((x.get("found_at") or 0) >= since for x in listings),
                       "rejected_24h": sum((x.get("at") or 0) >= since for x in rejected)}}


@app.get("/api/onchain/radar/lessons")
async def onchain_radar_lessons(user: User = Depends(auth.get_current_user)) -> dict:
    """Cosa e' successo davvero ai token proposti e scartati, e quali
    controlli lo prevedevano. Vedi app/trading/onchain/outcomes.py."""
    _require_trading()
    _require_onchain_enabled()
    return {**onchain_outcomes.lessons(), "gate": settings.onchain_memory_gate}


@app.get("/api/onchain/narratives/coverage")
async def onchain_narratives_coverage(user: User = Depends(auth.get_current_user)) -> dict:
    """Quanti progetti ha ogni narrativa gem: nel database e sulla bacheca.
    Le narrative vuote vengono per prime, cosi' si vedono subito."""
    _require_onchain_enabled()
    counts = onchain_projects_db.narrative_counts()
    board = onchain_leaderboard.board().get("entries") or []
    on_board: dict[str, int] = {}
    for e in board:
        for n in e.get("narratives") or []:
            k = n.get("key") if isinstance(n, dict) else n
            if k:
                on_board[k] = on_board.get(k, 0) + 1
    items = [{"key": n["key"], "label": n.get("label") or n["key"],
              "projects": counts.get(n["key"], 0), "on_board": on_board.get(n["key"], 0)}
             for n in onchain_narratives.catalog() if n["kind"] == "gem"]
    items.sort(key=lambda x: x["projects"])
    return {"items": items}


@app.get("/api/onchain/lab")
async def onchain_lab(user: User = Depends(auth.get_current_user)) -> dict:
    """Cosa dicono i backtest di tutti i token passati dal diario, anche
    quelli scartati. Vedi app/trading/onchain/backtest_lab.py."""
    _require_trading()
    _require_onchain_enabled()
    return onchain_backtest_lab.summary()


@app.get("/api/onchain/radar/memes")
async def onchain_radar_memes(user: User = Depends(auth.get_current_user)) -> dict:
    """La corsia meme: token scartati per nome, sito o GitHub, non per
    sicurezza, ordinati per somiglianza alle salite vere gia' viste. Gioco
    d'azzardo, e lo dice. Solo lettura: il pilota non li compra."""
    _require_trading()
    _require_onchain_enabled()
    return {**onchain_outcomes.meme_board(), "study": onchain_outcomes.study()}


@app.get("/api/onchain/leaderboard")
async def onchain_leaderboard_view(user: User = Depends(auth.get_current_user)) -> dict:
    """La classifica dei progetti migliori: GitHub, sito e tokenomics
    analizzati a fondo. Vedi app/trading/onchain/leaderboard.py."""
    _require_onchain_enabled()
    board = onchain_leaderboard.board()
    return {**board, "entries": onchain_outcomes.without_shamed(board.get("entries") or [])}


@app.get("/api/onchain/track")
async def onchain_track(user: User = Depends(auth.get_current_user)) -> dict:
    """Track record: how the Gem Finder's picks went after 1/3/7/30/90 days,
    against the chain's own coin, and the misses. See gem_track.record()."""
    _require_onchain_enabled()
    return onchain_gem_track.record()


@app.get("/api/onchain/consistency")
async def onchain_consistency(user: User = Depends(auth.get_current_user)) -> dict:
    """DeepSeek's last audit of the Top against the database (consistency.py):
    Lorenzo, 29/09, "dai a deep seek anche il compito di scansionare queste
    incongruenze"."""
    _require_onchain_enabled()
    return onchain_consistency_audit.last_run()


@app.get("/api/onchain/top")
async def onchain_top(n: int = 15, narrative: str = "", user: User = Depends(auth.get_current_user)) -> dict:
    """Top 15/25/50 (top.js): the best projects we know, by score. Lorenzo,
    28/09: "la classifica top riporta roba di merda; ma com'e' possibile?
    nel database c'e' roba meglio" — the board keeps only projects passing
    every gate, so better-scoring projects analyzed but failing one gate
    (many for the old $3M / one-year limits, raised by #259) sat unused in
    the database. Now the board entries and the best studied rows are
    merged by score; DeepSeek's veto still keeps junk out."""
    _require_onchain_enabled()
    if not settings.onchain_trading_enabled:
        # Strategies are off (Lorenzo, 07/10): the Top is the research ranking
        # alone, with no backtest beside it and none required to enter.
        # The arenas (arenas.py, Lorenzo 07/10) take every project that
        # passed the gates, not a fixed 50; `entries` stays for older pages.
        pool = _top_entries(ARENA_POOL, narrative)
        for e in pool:
            e["backtests"] = None
        out = pool[:max(1, min(int(n), 50))]
        return {"entries": out, "on_board": sum(1 for e in out if e["on_board"]), "backtests": None,
                "early": onchain_leaderboard.board().get("early") or [], **onchain_arenas.build(pool, prefer={k.strip() for k in narrative.split(",") if k.strip()})}
    out = []
    for e in _top_entries(50, narrative):
        e["backtests"] = _lab_summary(onchain_strategy_lab.get(e["chain"], e["token_address"]))
        # Every row shows a real trend-follower result (Lorenzo, 28/09, said
        # many times: never "no entry"). A token whose backtest is done with
        # no trend-follower trade on any timeframe waits off the list; the
        # lab keeps testing it (it still runs on _top_entries).
        if e["backtests"] and not e["backtests"]["daily"] and not e["backtests"]["best"]:
            continue
        out.append(e)
    out = out[:max(1, min(int(n), 50))]
    return {"entries": out, "on_board": sum(1 for e in out if e["on_board"]),
            "backtests": onchain_strategy_lab.status(_top_entries(50))}


def _lab_summary(bt: dict | None) -> dict | None:
    """Per la lista: il migliore e il buy-and-hold, senza le operazioni.
    Only rows whose chart can draw their trades (strategy_lab.drawn_row):
    a number without arrows is left out."""
    if not bt:
        return None
    best = onchain_strategy_lab.drawn_row(bt)
    # The row shows the higher return between the 8h/1d trend follower and
    # the best 5-minute listed strategy (trend followers and RSI+BB):
    # strategy_lab.shown, mirrored by lab-shown.js.
    daily = bt.get("daily")
    drow = onchain_strategy_lab.drawn_row(daily) if daily else None
    got = onchain_strategy_lab.shown(bt)
    return {"shown": ("fast" if got[0] == "5m" else "daily") if got else None,
            "best": {k: best.get(k) for k in ("label", "return_pct", "trades", "win_rate")} if best else None,
            "hold_pct": bt.get("hold_pct"), "bars": bt.get("bars"),
            "daily": {"interval": daily.get("interval"), "return_pct": drow.get("return_pct"),
                      "trades": drow.get("trades"), "win_rate": drow.get("win_rate"),
                      "in_position": drow.get("in_position"), "hold_pct": daily.get("hold_pct"),
                      "signal": daily.get("signal")} if drow else None}


def _narr_keys(raw) -> set[str]:
    """Narrative keys from a list of keys or of {"key": ...} dicts."""
    return {n.get("key") if isinstance(n, dict) else n for n in raw or []}


def _require_trading() -> None:
    """Trading endpoints answer 404 while trading is off (Lorenzo, 07/10)."""
    if not settings.onchain_trading_enabled:
        raise HTTPException(status_code=404, detail="Trading is switched off.")


ARENA_POOL = 500   # every gated project; the arenas set no fixed size


def _top_entries(n: int = 50, narrative: str = "") -> list[dict]:
    """La Top della schermata Strategie: la bacheca delle gemme piu' le
    righe studiate migliori del database, per punteggio. `narrative`
    (chiavi separate da virgole) tiene solo i progetti di quelle narrative."""
    want = {k.strip() for k in (narrative or "").split(",") if k.strip()}
    board = sorted(onchain_leaderboard.board().get("entries") or [], key=lambda e: -(e.get("score") or 0))
    if want:
        board = [e for e in board if _narr_keys(e.get("narratives")) & want]
    out = [{**{k: e.get(k) for k in ("chain", "token_address", "pair_address", "pair_url", "symbol", "name", "image",
                                     "market_cap", "liquidity_usd", "change_24h", "launched_at", "score", "early", "usage", "hidden",
                                     "blocks")},
            "narratives": sorted(_narr_keys(e.get("narratives")) - {None}), "on_board": True} for e in board if not onchain_leaderboard.no_name(e.get("symbol"))]
    seen = {f"{e['chain']}:{e['token_address']}" for e in out}
    cutoff = time.time() - 30 * 86400
    # "ranked" too: a row whose flag outlived its place on the board was
    # skipped by both halves of the Top (Lorenzo, 28/09: high scores never on the podium).
    rows = [r for st in ("studied", "ranked")
            for r in onchain_projects_db.search(status=st, sort="score", limit=200, narrative=narrative)["items"]]
    for row in sorted(rows, key=lambda r: -(r.get("score") or 0)):
        if row.get("score") is None or (row.get("analyzed_at") or 0) < cutoff:
            continue
        if want and not _narr_keys(row.get("narratives")) & want:
            continue
        if (row.get("overall") or {}).get("fatal") or onchain_leaderboard.no_name(row.get("symbol")):
            continue
        # The board's ceiling holds here too: bridged AAVE sat first (Lorenzo, 28/09).
        # Layer chains have their own band (LAYER_CAP_MIN-MAX), checked below.
        if row["chain"] != onchain_layers.CHAIN and onchain_leaderboard.too_big(
                {"symbol": row.get("symbol"), "name": row.get("name"),
                 "market_cap": row.get("mcap"), "global_cap": row.get("cg_mcap")}):
            continue
        key = f"{row['chain']}:{row['token']}"
        if key in seen:
            continue
        j = onchain_judge.get(row["chain"], row["token"])
        if j and j.get("verdict") in ("avoid", "scam") and (j.get("confidence") or 0) >= onchain_judge.VETO_MIN_CONF:
            continue
        # Only DeepSeek's own drop keeps a studied row out (review or veto). The
        # board's band limits (the $2M score bar, weak pillars...) are not
        # a reason here: applied to the Top they emptied it (Lorenzo, 29/09: 2
        # tokens left). Rows that stay become "ranked" and lose that text
        # (projects_db.sync_top), so the Database no longer contradicts the Top.
        if (row.get("why") or "").startswith("DeepSeek"):
            continue
        # The board's usage bars hold here too (Lorenzo, 07/10): volume, trades,
        # buyers, holders and the $300k floor, the same as usage_gate on the
        # board; the score is recomputed with today's formula. Layer chains
        # trade on exchanges, not DEX pools: their own band below.
        score = row.get("score")
        hidden = False
        blocks = None
        if row["chain"] != onchain_layers.CHAIN:
            # DeepSeek's reading too, so a token scores the same in both halves.
            entry = {**onchain_leaderboard.db_entry(row), "global_cap": row.get("cg_mcap"), "judge": j}
            if onchain_leaderboard.usage_gate(entry):
                continue
            score = onchain_leaderboard.final_score(entry)
            hidden = onchain_leaderboard.hidden_gem(entry)
            blocks = onchain_leaderboard.score_blocks(entry)
        # Layer chains (layers.py) have no DEX pool: their pool is the
        # exchange pair, and the gem ceiling does not apply to them.
        pair_address = None
        if row["chain"] == onchain_layers.CHAIN:
            cex = (row.get("overall") or {}).get("cex")
            if not cex or not cex.get("exchange") or not cex.get("pair"):
                continue
            pair_address = f"{cex['exchange']}:{cex['pair']}"
            mcap = row.get("mcap")
            if mcap is None or not (onchain_layers.LAYER_CAP_MIN <= mcap <= onchain_layers.LAYER_CAP_MAX):
                continue
        seen.add(key)
        out.append({"chain": row["chain"], "token_address": row["token"], "pair_address": pair_address,
                    "pair_url": None,
                    "symbol": row.get("symbol"), "name": row.get("name"), "image": row.get("image"),
                    "market_cap": row.get("mcap"), "liquidity_usd": row.get("liq"), "change_24h": None,
                    "launched_at": row.get("launched_at"), "score": score, "early": None, "on_board": False,
                    "hidden": hidden, "usage": onchain_leaderboard.usage.latest(row["chain"], row["token"]),
                    "blocks": blocks, "narratives": sorted(_narr_keys(row.get("narratives")) - {None})})
    out.sort(key=lambda e: -(e.get("score") or 0))
    return out[:n]


@app.get("/api/onchain/token")
async def onchain_token(chain: str, token: str, user: User = Depends(auth.get_current_user)) -> dict:
    """The full dossier of one token: the gem board entry (or the database
    row in board shape) and every alert on it. The token page fetches this
    itself so Signal, Why and Info are complete whichever list opened it
    (Lorenzo, 27/09: "signal, why e info devono essere sempre completi di
    informazioni, per ogni token")."""
    _require_onchain_enabled()
    entry = next((e for e in onchain_leaderboard.board().get("entries") or []
                  if e.get("chain") == chain and e.get("token_address") == token), None)
    if entry is not None:
        entry = {**entry, "on_board": True}
    else:
        p = onchain_projects_db.get(chain, token)
        if p is not None:
            # DeepSeek's report and code reading, as on a board entry (Lorenzo,
            # 29/09: every Top token gets DeepSeek's investigation in Why and Info).
            entry = onchain_leaderboard.db_entry(p)
            j = onchain_judge.get(chain, token)
            c = onchain_code_review.get(onchain_code_review.repo_of(entry))
            entry["judge"] = {k: j.get(k) for k in ("score", "verdict", "problems", "strengths", "admit_reason", "x", "tg")} \
                if j else None
            entry["code"] = {k: c.get(k) for k in ("complexity", "innovation", "fork_of", "summary", "strengths",
                                                   "concerns")} if c else None
    trading = settings.onchain_trading_enabled
    return {"entry": entry, "alerts": onchain_alert_track.for_token(chain, token) if trading else [],
            "backtests": onchain_strategy_lab.get(chain, token) if trading else None,
            "deep": onchain_deep_recheck.get(chain, token),
            "deep_wait_s": onchain_deep_recheck.wait_s(chain, token, limited=guest_links.is_guest(user)),
            "deep_progress": onchain_deep_recheck.progress(chain, token)}


@app.post("/api/onchain/token/recheck")
async def onchain_token_recheck(chain: str, token: str, user: User = Depends(auth.get_current_user)) -> dict:
    """A dedicated, in-depth DeepSeek re-check of one token (Lorenzo, 01/10)."""
    _require_onchain_enabled()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            # Lorenzo, 01/10: "voglio poterlo fare quando voglio": only guests keep the limits.
            result = await onchain_deep_recheck.run(chain, token, client, limited=guest_links.is_guest(user))
        except onchain_deep_recheck.TooSoon as exc:
            raise HTTPException(status_code=429, detail={"wait_s": exc.wait_s})
        except LookupError:
            raise HTTPException(status_code=404, detail="token not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
    return {"deep": result}


@app.get("/api/onchain/token/recheck/progress")
async def onchain_token_recheck_progress(chain: str, token: str, user: User = Depends(auth.get_current_user)) -> dict:
    """The live steps of a deep re-check (manual or weekly) on this token."""
    _require_onchain_enabled()
    return {"progress": onchain_deep_recheck.progress(chain, token)}


@app.get("/api/onchain/slippage")
async def onchain_slippage(chain: str, token: str, usd: float | None = None,
                           user: User = Depends(auth.get_current_user)) -> dict:
    """The slippage preview of an entry on this token (slippage.py): a DEX
    quote where there is one, the pool depth otherwise, and the largest size
    under the limit. `usd` defaults to the Top 50 wallet's next entry."""
    from app.trading.onchain import slippage
    _require_onchain_enabled()
    entry = next((e for e in onchain_leaderboard.board().get("entries") or []
                  if e.get("chain") == chain and e.get("token_address") == token), None)
    if entry is None:
        p = onchain_projects_db.get(chain, token)
        entry = onchain_leaderboard.db_entry(p) if p is not None else {}
    want = usd if usd is not None and 0 < usd <= 1e7 else _entry_usd()
    return await slippage.preview(chain, token, want, entry.get("liquidity_usd"))


@app.get("/api/onchain/projects")
async def onchain_projects_view(q: str = "", status: str = "", sort: str = "score", limit: int = 50,
                                offset: int = 0, band: bool = False, narrative: str = "",
                                chain: str = "", layer: str = "",
                                user: User = Depends(auth.get_current_user)) -> dict:
    """Il database dei progetti del Gem Finder: tutto quello che e' stato
    visto, con il verdetto e il perche'. Vedi app/trading/onchain/projects_db.py."""
    _require_onchain_enabled()
    return {**onchain_projects_db.search(q, status, sort, limit, offset, band, narrative,
                                                chain, layer),
            "stats": onchain_projects_db.stats()}


@app.get("/api/onchain/layers")
async def onchain_layers_view(user: User = Depends(auth.get_current_user)) -> dict:
    """I progetti layer 0/1/2 trovati dal layer engine (fase 1: solo
    scoperta e punteggio, non ancora in classifica). Vedi
    app/trading/onchain/layers.py."""
    _require_onchain_enabled()
    return onchain_layers.snapshot()


class ProjectReadRequest(BaseModel):
    chain: str = "solana"
    token: str
    deep: bool = False


class PlanAssign(BaseModel):
    email: str
    plan: str


def _is_admin(user: User) -> bool:
    allowed = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    return not guest_links.is_guest(user) and user.email.lower() in allowed


def _full_access(user: User) -> bool:
    """Top plan without the admin powers: admins plus full_access_emails."""
    allowed = {e.strip().lower() for e in settings.full_access_emails.split(",") if e.strip()}
    return _is_admin(user) or (not guest_links.is_guest(user) and user.email.lower() in allowed)


@app.get("/api/onchain/plan")
async def onchain_plan(user: User = Depends(auth.get_current_user)) -> dict:
    """Il piano dell'utente e quanto ne ha usato oggi. Vedi plans.py."""
    return onchain_plans.status(user.email, _full_access(user))


@app.post("/api/onchain/research")
async def onchain_research_read(req: ProjectReadRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Legge un progetto su richiesta: gratis col lettore, profonda (Sonnet)
    solo coi piani a pagamento."""
    _require_onchain_enabled()
    if req.chain not in ("solana", "robinhood") or not re.fullmatch(r"[0-9A-Za-z]{20,64}", req.token):
        raise HTTPException(status_code=400, detail="Unknown chain or malformed address.")
    return await onchain_plans.read_project(user.email, req.chain, req.token, deep=req.deep,
                                            admin=_full_access(user))


@app.post("/api/onchain/plan")
async def onchain_plan_assign(req: PlanAssign, user: User = Depends(auth.get_current_user)) -> dict:
    """Finche' i pagamenti non ci sono, i piani li assegna un admin."""
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Only an admin can assign plans.")
    try:
        onchain_plans.set_plan(req.email, req.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return onchain_plans.status(req.email)


@app.get("/api/onchain/radar/stream")
async def onchain_radar_stream(user: User = Depends(auth.get_current_user)) -> StreamingResponse:
    """I listing nuovi spinti al browser appena passano le soglie: niente
    attesa del prossimo giro di lettura."""
    _require_trading()
    _require_onchain_enabled()
    queue = onchain_listings.subscribe()

    async def event_gen():
        try:
            yield ": connesso\n\n"
            while True:
                try:
                    event, item = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    # Il battito: cosa sta guardando il radar adesso, cosi'
                    # la pagina si muove anche quando non passa nessuno.
                    event, item = "pulse", onchain_listings.pulse()
                yield f"event: {event}\ndata: {json.dumps(item)}\n\n"
        finally:
            onchain_listings.unsubscribe(queue)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def app_token_url(view: str, chain: str, token: str) -> str:
    """The app open on one token's page: what every ticker in a Telegram
    alert links to (Lorenzo, 29/09). See openFromHash() in detail.js."""
    from urllib.parse import quote
    return f"{settings.public_base_url}/onchain-app.html#{view}&token={quote(chain, safe='')}:{quote(token, safe='')}"


_LISTING_NOTIFY_PER_HOUR = 12
_listing_notified: dict[str, list[float]] = {}


async def _notify_listing(item: dict) -> None:
    """Telegram a chi ha un wallet on-chain e Telegram collegato: l'app
    chiusa non deve voler dire saperlo tardi. Al massimo 12 all'ora."""
    from app.notify import telegram
    now = _time.time()
    link = app_token_url("meme", item["chain"], item.get("token_address") or item["pair_address"])
    text = onchain_listings.telegram_text(item, now, link)
    # The signal's own track record: what entering on this message would pay.
    onchain_alert_track.record(item, "meme", now)
    for user in auth.list_users():
        if not user.telegram_chat_id or not user.onchain_wallets or not _telegram_allowed(user):
            continue
        sent = [t for t in _listing_notified.get(user.id, []) if now - t < 3600]
        if len(sent) >= _LISTING_NOTIFY_PER_HOUR:
            continue
        _listing_notified[user.id] = sent + [now]
        _fire(telegram.send_to(user.telegram_chat_id, text))


onchain_listings.on_listing(_notify_listing)

_GEM_NOTIFY_PER_HOUR = 6
_gem_notified: dict[str, list[float]] = {}


def _gem_telegram_text(kind: str, e: dict) -> str:
    """Italian, like every message Lorenzo reads."""
    import html as _html
    esc = _html.escape
    chain = onchain_legit.CHAIN_LABEL.get(e.get("chain"), e.get("chain") or "")
    cap = f"${float(e['market_cap']):,.0f}" if e.get("market_cap") else "—"
    head = ("💎 <b>Gemma early</b>: sotto i $50k con fondamentali solidi, potrebbe valere un piccolo ingresso"
            if kind == "early" else "💎 <b>Nuova gemma in classifica</b>")
    pillars = e.get("pillars") or {}
    scores = " · ".join(f"{n} {round(pillars[k]['score'])}" for k, n in (("github", "GitHub"), ("site", "Ricerca"),
                        ("tokenomics", "Tokenomics")) if (pillars.get(k) or {}).get("score") is not None)
    sym = esc(e.get("symbol") or "?")
    if e.get("token_address"):
        sym = f'<a href="{esc(app_token_url("gems", e.get("chain") or "", e["token_address"]), quote=True)}">{sym}</a>'
    lines = [head, f"<b>{sym}</b> · {esc(e.get('name') or '')} su {chain}",
             f"cap {cap} · punteggio {round(e.get('score') or 0)} · posto {e.get('rank') or '—'}"]
    if scores:
        lines.append(scores)
    tr = e.get("traction") or {}
    if tr.get("reasons"):
        lines.append("📈 " + esc(" · ".join(tr["reasons"][:3])))
    if e.get("summary"):
        lines.append(esc(e["summary"]))
    links = []
    for label, url in (("🌐 Sito", e.get("website")), ("GitHub", e.get("github")), ("Grafico", e.get("pair_url"))):
        if url:
            links.append(f'<a href="{esc(url, quote=True)}">{label}</a>')
    gem_url = app_token_url("gems", e.get("chain") or "", e["token_address"]) if e.get("token_address") \
        else f"{settings.public_base_url}/onchain-app.html#gems"
    links.append(f'<a href="{esc(gem_url, quote=True)}">Apri in DEGEN</a>')
    lines.append(" · ".join(links))
    lines.append("<i>Ricerca, non un consiglio: entra solo con cio' che puoi perdere.</i>")
    return "\n".join(lines)


async def _notify_gem(kind: str, e: dict) -> None:
    """Telegram for the Gem Finder too: a project new on the board, or one
    flagged early (under $50k with the fundamentals). At most 6 an hour."""
    from app.notify import telegram
    now = _time.time()
    if e.get("pair_address"):
        onchain_alert_track.record({**e, "price_usd": e.get("price_usd")}, "gem", now)
    text = _gem_telegram_text(kind, e)
    for user in auth.list_users():
        if not user.telegram_chat_id or not user.onchain_wallets or not _telegram_allowed(user):
            continue
        sent = [t for t in _gem_notified.get(user.id, []) if now - t < 3600]
        if len(sent) >= _GEM_NOTIFY_PER_HOUR:
            continue
        _gem_notified[user.id] = sent + [now]
        _fire(telegram.send_to(user.telegram_chat_id, text))


onchain_leaderboard.on_alert(_notify_gem)


def _trend_signal_text(s: dict) -> str:
    """Italian, like every message Lorenzo reads."""
    import html as esc
    iv = str(s.get("interval") or "?")
    tf = ("giornaliero" if iv == "1d" else "1 ora" if iv == "1h"
          else f"{iv[:-1]} ore" if iv.endswith("h") else f"{iv[:-1]} minuti" if iv.endswith("m") else iv)
    sym = esc.escape(str(s.get("symbol") or "?"))
    url = app_token_url("gems", s.get("chain") or "", s.get("token") or "")
    link = f'<a href="{esc.escape(url, quote=True)}">{sym}</a>'
    price = f"{s['price']:.6g}" if isinstance(s.get("price"), (int, float)) else "?"
    if s.get("side") == "buy":
        head = f"📈 <b>Trend follower {tf}</b> · entrata su {link} a ${price}"
    else:
        ret = s.get("ret")
        head = (f"📉 <b>Trend follower {tf}</b> · uscita da {link} a ${price}"
                + (f" ({ret * 100:+.1f}%)" if isinstance(ret, (int, float)) else ""))
    return head + "\n<i>Ricerca, non un consiglio: entra solo con cio' che puoi perdere.</i>"


def _entry_usd() -> float:
    """The Top 50 wallet's next entry: its share of today's equity."""
    from app.trading.onchain import sim_backtest
    eq = (_top_wallet_cache.get("data") or {}).get("equity") or sim_backtest.DEFAULT_CAPITAL
    return eq * sim_backtest.DEFAULT_SHARE


async def _entry_preview(s: dict) -> dict | None:
    """Slippage of the next entry on this token and the largest size under
    the limit (Lorenzo, 30/09: "un'anteprima di quanto sarebbe lo slippage").
    The Telegram message and the simulated wallet read the same estimate."""
    from app.trading.onchain import slippage
    if s.get("side") != "buy" or not s.get("token"):
        return None
    try:
        return await slippage.preview(s["chain"], s["token"], _entry_usd(), s.get("liq"))
    except Exception:
        logger.exception("slippage preview of %s failed", s.get("symbol"))
        return None


async def _notify_trend_signal(s: dict) -> None:
    """Lorenzo, 30/09: the site's trend follower on 8h (young tokens) or 1d
    on the ranking's projects, "segnali di piu' qualita'". Fires on the
    candle close, rare by construction, so no hourly cap."""
    from app.notify import telegram
    from app.trading.onchain import slippage
    text = _trend_signal_text(s)
    prev = await _entry_preview(s)
    if prev is not None:
        text += "\n" + slippage.text_it(prev)
    for user in auth.list_users():
        if user.telegram_chat_id and user.onchain_wallets and _telegram_allowed(user):
            _fire(telegram.send_to(user.telegram_chat_id, text))


onchain_strategy_lab.on_signal(_notify_trend_signal)


async def _sim_trend_signal(s: dict) -> None:
    """The simulated wallet follows the same signal, on gems only (Lorenzo,
    30/09: "ingressi con strategia dinamica su gemme e non piu' su crypto
    etichettate come meme"). Real does not: it waits for a reviewed quant policy."""
    from app.trading.onchain import quant_runtime
    now = _time.time()
    gem_ok, why = quant_runtime.gem_verdict(
        {"chain": s["chain"], "token_address": s["token"], "symbol": s.get("symbol") or "", "name": ""}, now)
    if s.get("side") == "buy" and not gem_ok:
        logger.info("sim trend: %s skipped (%s)", s.get("symbol"), why)
    users = {u.id: u for u in auth.list_users()}
    prev = await _entry_preview(s) if gem_ok else None
    max_usd = (prev or {}).get("max_usd")
    for uid, row in onchain_autotrader.trend_signal(list(users), s, now, gem_ok, max_usd=max_usd):
        if row.get("ok"):
            await _notify_autotrade(users[uid], row)


onchain_strategy_lab.on_signal(_sim_trend_signal)

_SIGNALS_TICK_S = 30
_signals_started = _time.time()


def _past_meme_alerts() -> list[dict]:
    """Listings the Meme Sniper published fresh enough to go out on Telegram
    (the same test as listings._publish), with the moment they went out."""
    return [{**x, "at": x["found_at"]} for x in onchain_listings.recent()
            if x.get("found_at") and x.get("created_at")
            and x["found_at"] - x["created_at"] <= onchain_listings.NOTIFY_MAX_AGE_S]


async def _notify_autotrade(user: User, row: dict) -> None:
    """Italian, like every message Lorenzo reads."""
    from app.notify import telegram
    if not user.telegram_chat_id or not _telegram_allowed(user):
        return
    import html as _html
    tag = "🧪 SIMULATO" if row.get("mode") == "sim" else "💱 ESEGUITO"
    sym = _html.escape(str(row["symbol"]))
    if row.get("token"):
        url = app_token_url("gems" if str(row.get("why", "")).startswith("trend") else "meme", row.get("chain") or "robinhood", row["token"])
        sym = f'<a href="{_html.escape(url, quote=True)}">{sym}</a>'
    if row["side"] == "buy":
        text = f"{tag} · il sistema ha comprato <b>{sym}</b> con {row.get('usdg', 0):.2f} USDG"
    else:
        ret = row.get("ret")
        text = (f"{tag} · il sistema ha venduto <b>{sym}</b> per {row.get('usdg', 0):.2f} USDG"
                + (f" ({ret * 100:+.1f}%)" if ret is not None else "") + f" · {row.get('why', '')}")
    await telegram.send_to(user.telegram_chat_id, text)


class AutotraderUpdate(BaseModel):
    mode: Literal["off", "sim", "real"] | None = None
    pct: int | None = None
    sim_cash: float | None = None


@app.get("/api/onchain/autotrader")
async def get_autotrader(user: User = Depends(auth.get_current_user)) -> dict:
    """The system trading the Robinhood wallet: switch, share, position, trades."""
    _require_trading()
    _require_onchain_enabled()
    return {**onchain_autotrader.view(user.id), "has_wallet": "robinhood" in user.onchain_wallets,
            "server_live": settings.onchain_execution_enabled}


_top_wallet_cache: dict = {"at": 0.0, "data": None}
_TOP_WALLET_TTL_S = 20
_TOP_WALLET_KEY = "top50:sim"


async def _top_wallet() -> dict:
    """A $10,000 wallet replayed on every trade of the Top 50's backtests,
    the same for every account (Lorenzo, 30/09: "farlo visualizzare a tutti
    gli utenti indipendentemente dall'account"). It is the Simulated wallet:
    closed = every trade the Top 50 closed, open = what would be open today,
    and it keeps going on the next entries (sim_backtest's ledger)."""
    from app.trading.onchain import sim_backtest
    from app.trading.onchain import strategy_lab as onchain_strategy_lab
    now = _time.time()
    if _top_wallet_cache["data"] is not None and now - _top_wallet_cache["at"] < _TOP_WALLET_TTL_S:
        return _top_wallet_cache["data"]
    new = False
    for e in _top_entries(50):
        new |= sim_backtest.remember(e["chain"], e["token_address"], e.get("symbol"),
                                     onchain_strategy_lab.get(e["chain"], e["token_address"]), save=False,
                                     liq=e.get("liquidity_usd"))
    if new:
        sim_backtest._save()
    tokens = sim_backtest.ledger_tokens()
    capital = sim_backtest.DEFAULT_CAPITAL
    first = sim_backtest.replay(tokens, capital)
    marks = {}
    if first["open"]:
        try:
            keys = {onchain_live_prices._key(p["chain"], p["token"]): f"{p['chain']}:{p['token']}"
                    for p in first["open"]}
            got = await onchain_live_prices.prices(list(keys))
            marks = {name: got[f"{k[0]}:{k[1]}"]["p"] for k, name in keys.items() if f"{k[0]}:{k[1]}" in got}
        except Exception:
            marks = {}
    data = sim_backtest.replay(tokens, capital, marks=marks, now=now)
    # The replay's line holds open trades at cost; the live value, read here,
    # is what the short ranges must show (an 8% drop over 4h, not the whole
    # unrealised gain landing in the last step).
    onchain_wealth.record(_TOP_WALLET_KEY, data["equity"], now)
    _top_wallet_cache.update(at=now, data=data)
    return data


@app.get("/api/onchain/top/backtest")
async def top_backtest(user: User = Depends(auth.get_current_user)) -> dict:
    """The Top 50 wallet (_top_wallet). Read only."""
    _require_trading()
    _require_onchain_enabled()
    return await _top_wallet()


@app.patch("/api/onchain/autotrader")
async def set_autotrader(body: AutotraderUpdate, user: User = Depends(auth.get_current_user)) -> dict:
    _require_trading()
    _require_onchain_enabled()
    if body.mode in ("sim", "real") and "robinhood" not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail="Create the Robinhood Chain wallet first.")
    usdg_now = None
    if body.mode == "sim":
        try:
            # Cash = USDC + USDG (both 6 decimals): the simulation starts from what the wallet holds.
            cash_tokens = [onchain_autotrader.USDG] + ([onchain_autotrader._meta["usdc"]] if onchain_autotrader._meta["usdc"] else [])
            rows = await onchain_balances._robinhood_known_token_balances(
                user.onchain_wallets["robinhood"].address, cash_tokens)
            usdg_now = sum(r["amount_raw"] for r in rows) / 10 ** onchain_autotrader.USDG_DECIMALS
        except Exception:
            usdg_now = None
    try:
        view = onchain_autotrader.configure(user.id, body.mode, body.pct, usdg_now=usdg_now,
                                            sim_cash=body.sim_cash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**view, "has_wallet": True, "server_live": settings.onchain_execution_enabled}


# ------------------------------------------------------------------ profile
# Lorenzo, with a trading app's profile screenshot: "uno deve vedere cosa
# detiene, l'andamento del wallet nel tempo con un grafico [...] l'account
# deve rimanere tutto nel database, uno deve poter scegliere anche
# l'immagine profilo". See profiles.py and trading/onchain/wealth.py.

def _all_addresses(user: User) -> list[tuple[str, str, str]]:
    """(kind, address, source) of every wallet of this account: the site's
    and the ones the user connected. An EVM address is read on every EVM chain."""
    out = []
    for chain, w in user.onchain_wallets.items():
        out.append(("solana" if chain == "solana" else "evm", w.address, "site"))
    for w in profiles.wallets(user.id):
        if not any(k == w["kind"] and a.lower() == w["address"].lower() for k, a, _s in out):
            out.append((w["kind"], w["address"], "connected"))
    return out


# A snapshot costs a chain read per wallet: /api/profile and
# /api/wallets/total ask for it at the same moment, and a switch asks again.
# 20 s of memory plus in-flight sharing turn those into one read.
_snap_cache: dict[str, tuple[float, dict]] = {}
_snap_inflight: dict[str, asyncio.Task] = {}
_SNAP_TTL = 20.0
_snap_clock = time.monotonic   # its own name, so tests move it without freezing asyncio


async def _wallet_snapshot(user: User) -> dict:
    """The cached snapshot: a fresh one is returned as is, a read already in
    flight is awaited instead of started again. Failures are not cached."""
    now = _snap_clock()
    hit = _snap_cache.get(user.id)
    if hit and now - hit[0] < _SNAP_TTL:
        return hit[1]
    task = _snap_inflight.get(user.id)
    if task is None:
        task = _fire(_wallet_snapshot_uncached(user))
        _snap_inflight[user.id] = task

        def _done(t: asyncio.Task) -> None:
            _snap_inflight.pop(user.id, None)
            if not t.cancelled() and t.exception() is None:
                _snap_cache[user.id] = (_snap_clock(), t.result())

        task.add_done_callback(_done)
    return await task


async def _wallet_snapshot_uncached(user: User) -> dict:
    """Everything every wallet holds on every chain, priced (portfolio.py):
    cash (stablecoins), positions (the rest, native coins included), total."""
    addrs = _all_addresses(user)
    if not addrs:
        return {"total": None, "cash": 0.0, "positions": [], "missing": [], "by_chain": {}, "complete": False}
    res = await onchain_multichain.everything(addrs)
    positions = [{"chain": h["chain"], "address": h["token"], "symbol": h["symbol"], "name": h["name"],
                  "amount": h["amount"], "price_usd": h["price_usd"], "value_usd": h["value_usd"],
                  "image": h["image"], "native": h["native"], "hidden": h["hidden"], "source": h["source"],
                  "wallet": h["wallet"]}
                 for h in res["holdings"] if (h["symbol"] or "").upper() not in onchain_multichain.STABLES or h["hidden"]]
    # One total everywhere (header, profile, chart): a chain that did not
    # answer this time counts at its last value when that is recent.
    carried = onchain_wealth.combine(user.id, res["read"], res["missing_chains"])
    return {"total": round(carried, 4) if carried is not None else res["total"], "cash": res["cash"],
            "positions": positions, "missing": [f"{m['chain']} {m['address'][:6]}…" for m in res["missing"]],
            "by_chain": res["by_chain"], "complete": carried is not None}


def _record_wealth(user: User, snap: dict) -> None:
    if snap.get("total") is not None and snap.get("complete"):
        onchain_wealth.record(user.id, snap["total"])


async def _sim_snapshot(user: User) -> dict:
    """The simulated wallet, shaped as _wallet_snapshot. Lorenzo, 27/09: "il
    fatto che sto simulando il denaro deve essere possibile selezionando
    'wallet simulato'". From 30/09 it is the Top 50 wallet, the same for
    every account: "reset completo di tutti gli account [...] 10.000 [...]
    nelle posizioni chiuse tutte quelle della top 50 [...] e le posizioni
    aperte, che sarebbero aperte ad oggi"."""
    from app.trading.onchain import strategy_lab as onchain_strategy_lab
    w = await _top_wallet()
    positions = []
    for p in w["open"]:
        price = p.get("price")
        positions.append({"chain": p["chain"], "address": p["token"], "symbol": p.get("symbol") or "?",
                          "name": p.get("symbol") or "?", "price_usd": price, "value_usd": p["value"],
                          "amount": p["value"] / price if price else None, "image": None, "native": False,
                          "hidden": False, "source": "sim", "wallet": "sim", "ret": p.get("ret"),
                          "cost_usd": p["stake"], "opened": p["at"], "interval": p.get("interval"),
                          "strategy": p.get("strategy"),
                          "pair": (onchain_strategy_lab._results.get(
                              onchain_strategy_lab.key_of(p["chain"], p["token"])) or {}).get("pair")})
    # Best return first, open and closed alike (Lorenzo, 01/10: "ordina
    # automaticamente per migliore posizione"); no price yet goes last.
    positions.sort(key=_best_first)
    by_chain: dict[str, float] = {}
    for p in positions:
        by_chain[p["chain"]] = by_chain.get(p["chain"], 0.0) + (p["value_usd"] or 0.0)
    return {"total": w["equity"], "cash": w["cash"], "positions": positions, "missing": [],
            "by_chain": by_chain, "complete": True,
            "sim": {"usdg": w["cash"], "start": w["capital"], "equity": w["equity"], "ret": w["ret"],
                    "autotrader": "top50", "from": w["from"], "tokens": w["tokens"], "share": w["share"],
                    "max_dd": w["max_dd"], "win_rate": w["win_rate"]},
            "closed": [{"symbol": c.get("symbol") or "?", "token": c["token"], "chain": c["chain"],
                        "opened": c["at"], "closed": c["exit_at"], "cost_usdg": c["stake"],
                        "proceeds_usdg": round(c["stake"] + c["pnl"], 2), "ret": c["ret"], "mode": "sim",
                        "why": f"{_strategy_why(c.get('strategy'))} {c.get('interval') or ''}".strip(),
                        "interval": c.get("interval"), "strategy": c.get("strategy")}
                       for c in sorted(w["closed"], key=_best_first)],
            "curve": _top_curve(w)}


def _best_first(r: dict) -> tuple:
    ret = r.get("ret")
    return (ret is None, -(ret or 0.0))


def _strategy_why(s: str | None) -> str:
    # No strategy names on screen (Lorenzo: only returns); the label stays in "strategy".
    return "strategy not recorded" if not s or s == "unknown" else "strategy"


def _top_curve(w: dict) -> list[list[float]]:
    """The replay's line up to the first live reading, then the readings."""
    rec = onchain_wealth.series(_TOP_WALLET_KEY, "all")
    if not rec:
        return w["curve"]
    return [list(r) for r in w["curve"][:-1] if r[0] < rec[0][0]] + [list(r) for r in rec]


def _curve_series(curve: list[list[float]], rng: str, now: float) -> list[list[float]]:
    """The Top 50 wallet's line over one range: the value when the range
    opens, then every change inside it."""
    span = onchain_wealth.RANGES.get(rng)
    if span is None or not curve:
        return [list(r) for r in curve]
    start = now - span
    before = [r for r in curve if r[0] < start]
    inside = [list(r) for r in curve if r[0] >= start]
    return ([[start, before[-1][1]]] if before else []) + inside


@app.get("/api/onchain/live-prices")
async def live_prices(ids: str = "", user: User = Depends(auth.get_current_user)) -> dict:
    """The ticking balance's prices: ?ids=chain:address,... (at most 60).
    One shared cache for every user, a couple of seconds old at most."""
    return {"prices": await onchain_live_prices.prices(onchain_live_prices.parse_ids(ids)), "ttl": onchain_live_prices.TTL_S}


@app.get("/api/onchain/live-stream")
async def live_stream(request: Request, ids: str = "", user: User = Depends(auth.get_current_user)) -> StreamingResponse:
    """The same prices pushed (SSE): every PUSH_S the shared cache is read
    and only the prices that moved are sent, so a new block or swap reaches
    the screen without waiting for the browser's next question."""
    keys = onchain_live_prices.parse_ids(ids)

    async def event_gen():
        sent: dict[str, float] = {}
        quiet = 0.0
        yield "retry: 2000\n\n"
        while keys and not await request.is_disconnected():
            try:
                got = await onchain_live_prices.prices(keys)
            except Exception:
                got = {}
            # {"chain:addr": price}, plain numbers, as live.js keeps them
            moved = {k: v["p"] for k, v in got.items() if sent.get(k) != v["p"]}
            if moved:
                sent.update(moved)
                quiet = 0.0
                yield f"data: {json.dumps(moved)}\n\n"
            else:
                quiet += onchain_live_prices.PUSH_S
                if quiet >= 15:
                    quiet = 0.0
                    yield ": beat\n\n"
            await asyncio.sleep(onchain_live_prices.PUSH_S)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _live_marks(snap: dict) -> list[dict]:
    """What the header needs to move its total with the live price: each
    priced position counted in it (live.js liveDelta)."""
    return [{k: p.get(k) for k in ("chain", "address", "native", "amount", "price_usd", "value_usd")}
            for p in snap["positions"] if not p.get("hidden") and p.get("value_usd") is not None][:60]


@app.get("/api/wallets/total")
async def wallets_total(wallet: Literal["real", "sim"] = "real",
                        user: User = Depends(auth.get_current_user)) -> dict:
    """The header's number: every token on every chain, in USD; or the
    simulated wallet's, with ?wallet=sim."""
    if wallet == "sim":
        snap = await _sim_snapshot(user)
        return {"total": snap["total"], "cash": snap["cash"], "by_chain": snap["by_chain"], "missing": [],
                "complete": snap["complete"], "wallet": "sim", "started": snap["sim"] is not None,
                "live": _live_marks(snap)}
    snap = await _wallet_snapshot(user)
    _record_wealth(user, snap)     # the header asks every minute: the chart follows it
    return {"total": snap["total"], "cash": snap["cash"], "by_chain": snap["by_chain"], "missing": snap["missing"],
            "complete": snap["complete"], "live": _live_marks(snap)}


async def _wealth_loop() -> None:
    await asyncio.sleep(60)
    while True:
        for user in auth.list_users():
            if not _all_addresses(user):
                continue
            try:
                snap = await _wallet_snapshot(user)
                _record_wealth(user, snap)
            except Exception:
                logger.exception("wealth snapshot %s", user.id)
        await asyncio.sleep(onchain_wealth.SNAPSHOT_S)


def _joined_hint(user: User) -> float | None:
    times = [w.created_at for w in user.onchain_wallets.values() if w.created_at]
    return min(times) if times else None


def _closed_trades(user: User) -> list[dict]:
    """The system's round trips on this account: buy -> sell pairs."""
    rows = [t for t in onchain_autotrader.view(user.id)["trades"][::-1] if t.get("ok")]
    out, open_buy = [], {}
    for t in rows:
        if t["side"] == "buy":
            open_buy[t.get("token")] = t
        elif t["side"] == "sell" and t.get("token") in open_buy:
            b = open_buy.pop(t["token"])
            out.append({"symbol": t["symbol"], "token": t.get("token"), "chain": t.get("chain") or "robinhood", "opened": b["t"],
                        "closed": t["t"], "cost_usdg": b.get("usdg"), "proceeds_usdg": t.get("usdg"),
                        "ret": t.get("ret"), "mode": t.get("mode"), "why": t.get("why")})
    return out[::-1]


@app.get("/api/profile")
async def get_profile(wallet: Literal["real", "sim"] = "real",
                      user: User = Depends(auth.get_current_user)) -> dict:
    """The profile, on the real wallets or (?wallet=sim) on the simulated one:
    value, cash, positions, closed trades and the line all of that wallet."""
    p = profiles.get(user.id, user.email, _joined_hint(user))
    if wallet == "sim":
        snap = await _sim_snapshot(user)
        key = None
        closed = snap["closed"]
        trades = len(closed) + len(snap["positions"])
    else:
        snap = await _wallet_snapshot(user)
        _record_wealth(user, snap)
        key = user.id
        closed = [c for c in _closed_trades(user) if c.get("mode") != "sim"]
        trades = sum(len(onchain_ledger.list_orders(user.id, c, limit=500)) for c in user.onchain_wallets)
    holds = [c["closed"] - c["opened"] for c in closed]
    return {
        "profile": profiles.public(user.id, p), "email": user.email, "wallet": wallet, "sim": snap.get("sim"),
        "value": snap["total"], "cash": snap["cash"], "positions": snap["positions"], "missing": snap["missing"],
        "complete": snap["complete"],
        "by_chain": snap["by_chain"],
        "closed": closed,
        "stats": {"trades": trades, "avg_hold_s": sum(holds) / len(holds) if holds else None, "closed": len(closed)},
        # the line ends where the header is: the value just read
        **(_sim_lines(snap) if key is None else {
            "history": {k: onchain_wealth.series(key, k) + ([[_time.time(), snap["total"]]] if snap["complete"] else [])
                        for k in onchain_wealth.RANGES},
            "tracking_since": (onchain_wealth.series(key, "all") or [[None]])[0][0],
            "change": {k: onchain_wealth.change(key, k, snap["total"]) for k in onchain_wealth.RANGES}}),
    }


def _sim_lines(snap: dict) -> dict:
    now = _time.time()
    hist = {k: _curve_series(snap["curve"], k, now) for k in onchain_wealth.RANGES}
    return {"history": hist, "tracking_since": snap["sim"]["from"],
            "change": {k: round(snap["total"] - rows[0][1], 4) if rows else None for k, rows in hist.items()}}


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    handle: str | None = None
    bio: str | None = None


class HiddenToken(BaseModel):
    chain: str
    token: str
    hide: bool = True


@app.get("/api/profile/hidden")
async def get_hidden(user: User = Depends(auth.get_current_user)) -> dict:
    return {"hidden": profiles.hidden(user.id)}


@app.post("/api/profile/hidden")
async def post_hidden(body: HiddenToken, user: User = Depends(auth.get_current_user)) -> dict:
    try:
        return {"hidden": profiles.set_hidden(user.id, body.chain, body.token, body.hide)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class NarrativeSelection(BaseModel):
    keys: list[str] = []


@app.get("/api/profile/narratives")
async def get_narratives(user: User = Depends(auth.get_current_user)) -> dict:
    from app.trading.onchain import narratives
    catalog = [{"key": n["key"], "label": n.get("label") or n["key"], "kind": n["kind"],
                "heat": n["heat"], "why": n.get("why") or ""} for n in narratives.catalog()]
    return {"catalog": catalog, "selected": profiles.narratives(user.id)}


@app.post("/api/profile/narratives")
async def post_narratives(body: NarrativeSelection, user: User = Depends(auth.get_current_user)) -> dict:
    return {"selected": profiles.set_narratives(user.id, body.keys)}


@app.patch("/api/profile")
async def update_profile(body: ProfileUpdate, user: User = Depends(auth.get_current_user)) -> dict:
    profiles.get(user.id, user.email, _joined_hint(user))
    try:
        p = profiles.update(user.id, display_name=body.display_name, handle=body.handle, bio=body.bio)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"profile": profiles.public(user.id, p)}


class AvatarUpload(BaseModel):
    data_url: str


@app.post("/api/profile/avatar")
async def upload_avatar(body: AvatarUpload, user: User = Depends(auth.get_current_user)) -> dict:
    if len(body.data_url) > profiles.MAX_UPLOAD_BYTES * 4 // 3 + 200:
        raise HTTPException(status_code=413, detail="The picture is over 4 MB.")
    profiles.get(user.id, user.email, _joined_hint(user))
    try:
        p = await asyncio.to_thread(profiles.set_avatar, user.id, body.data_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"profile": profiles.public(user.id, p)}


class WalletChallenge(BaseModel):
    kind: Literal["evm", "solana"]
    address: str


class WalletConfirm(BaseModel):
    signature: str


@app.post("/api/wallets/link/challenge")
async def wallet_link_challenge(body: WalletChallenge, request: Request, user: User = Depends(auth.get_current_user)) -> dict:
    """The message the wallet signs to prove it is the user's (free, no tx)."""
    try:
        return {"message": wallet_link.challenge(user.id, body.kind, body.address.strip(),
                                                 host=request.headers.get("host") or "liquidityengine.app")}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/wallets/link")
async def wallet_link_confirm(body: WalletConfirm, user: User = Depends(auth.get_current_user)) -> dict:
    profiles.get(user.id, user.email, _joined_hint(user))
    try:
        p = wallet_link.confirm(user.id, body.signature)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"profile": profiles.public(user.id, p)}


@app.get("/api/agent/log")
async def agent_log(limit: int = 100, before: float | None = None,
                    user: User = Depends(auth.get_current_user)) -> dict:
    return {"items": deepseek_desk.log(limit=min(limit, 300), before=before)}


@app.get("/api/agent/journey")
async def agent_journey(q: str = "", chain: str = "", token: str = "",
                        user: User = Depends(auth.get_current_user)) -> dict:
    """Recorded evaluation path of one token as a hash chain (Lorenzo, 30/09)."""
    from app.trading.onchain import journey
    if chain and token:
        return {"matches": [], "journey": journey.journey(chain, token)}
    matches = journey.resolve(q)
    one = journey.journey(matches[0]["chain"], matches[0]["token"]) if len(matches) == 1 else None
    return {"matches": matches, "journey": one}


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    voice: bool = False
    talk: bool = False              # the answer is read aloud (ask.js talk mode)


ASK_PER_HOUR = 30

# Lorenzo, 01/10: the agent is "Carlo"; when a conversation starts it
# introduces itself as created by Lorenzo, its "god", with comic details,
# never the same opening twice ("inventane un migliaio"), then answers as
# usual. A random angle per greeting keeps the openings apart.
_CARLO_ANGLES = (
    "un trailer di un film epico", "un bollettino meteo", "una telecronaca di calcio", "un oroscopo",
    "una ricetta di cucina", "un documentario sulla natura", "un comunicato stampa solenne",
    "una confessione drammatica", "un annuncio all'aeroporto", "una lezione di filosofia",
    "un'intervista da rockstar", "un discorso di premiazione agli Oscar", "una favola per bambini",
    "un notiziario delle 20", "un rap improvvisato", "una poesia ermetica", "un messaggio in segreteria",
    "un'opera lirica", "una pubblicità anni '90", "un diario di bordo spaziale", "una televendita",
    "un giallo con colpo di scena", "una gita scolastica", "un tutorial di YouTube")
_CARLO_INTRO = ("Ti chiami Carlo. Questa e' la prima domanda di una nuova conversazione: apri con una presentazione "
                "comica di una o due frasi, in stile {angle}, in cui dici che sei Carlo e che sei stato creato da Lorenzo, "
                "il tuo dio e unico creatore, che ti sta insegnando a vivere e a capire le cose perche' Claude ti reputa "
                "stupido; aggiungi un dettaglio comico inventato ora, mai usato prima. Non ripetere aperture gia' viste. "
                "Poi rispondi alla domanda come sempre. Nelle risposte successive niente presentazione.")
_ask_log: dict[str, list[float]] = {}

_ASK_NOTE = ("Domanda dall'app del sito (liquidityengine.app), non da Telegram. {voice}"
             "Rispondi in testo semplice, senza markdown pesante: righe brevi, elenchi solo se servono. "
             "Per spiegare come funziona qualcosa (una strategia, un punteggio, un filtro) usa sempre come_funziona e sii preciso: "
             "regole, numeri e timeframe come stanno nel codice, senza inventare. Non nominare persone: le strategie si chiamano "
             "Trend EMA, Trend HF e RSI+BB.{talk}")
_TALK_NOTE = (" La risposta viene letta ad alta voce in una conversazione parlata: al massimo 3 frasi brevi "
              "(fino a 8 se chiede di spiegare nel dettaglio come funziona qualcosa), come parleresti a voce; niente elenchi, simboli, link o indirizzi; numeri arrotondati "
              "(es. 'circa 12 per cento'). Se serve di piu', dai il punto principale e chiedi se vuole i dettagli.")


@app.post("/api/agent/ask")
async def agent_ask(body: AskRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """The app's mic button: the same DeepSeek assistant as the Telegram chat,
    with the same read-only tools over the database (Lorenzo, 01/10: "deve
    avere accesso a tutto [...] mi possa rispondere su qualsiasi cosa").
    Nothing here writes, trades or reads a secret; the daily DeepSeek cap
    (deepseek_desk) still applies, plus a per-account hourly limit."""
    if guest_links.is_guest(user):
        raise HTTPException(status_code=403, detail="Guest mode: sign in to ask the agent.")
    if not settings.deepseek_api_key:
        raise HTTPException(status_code=503, detail="The agent is not configured on this server.")
    now = _time.time()
    recent = [t for t in _ask_log.get(user.id, []) if now - t < 3600]
    if len(recent) >= ASK_PER_HOUR:
        raise HTTPException(status_code=429, detail=f"Limit of {ASK_PER_HOUR} questions per hour reached. Try again later.")
    _ask_log[user.id] = recent + [now]
    deepseek_desk.note("chat", "Answering a question from the app")
    if body.talk:
        from app.notify import voice
        if voice.available():
            _fire(asyncio.to_thread(voice.warm))   # the voice loads while DeepSeek thinks
    voice = ("Il testo e' dettato a voce e trascritto dal browser: puo' avere parole storpiate, "
             "soprattutto i nomi dei token (es. 'drag' = DREGG); interpreta e, se il token non e' chiaro, cercalo. "
             if body.voice else "")
    chat_id = f"web:{user.id}"
    intro = "" if deepseek_desk._memory.get(chat_id) else _CARLO_INTRO.format(angle=random.choice(_CARLO_ANGLES)) + "\n"
    context = intro + _ASK_NOTE.format(voice=voice, talk=_TALK_NOTE if body.talk else "") + "\n" + await _deepseek_context("", user=user)
    try:
        reply = await deepseek_desk.answer(chat_id, body.text.strip(), context,
                                           tools=_deepseek_tools(), can_teach=_is_admin(user))
    except Exception as exc:
        logger.exception("deepseek app chat")
        raise HTTPException(status_code=502, detail=f"DeepSeek is not answering right now ({type(exc).__name__}).")
    return {"answer": reply}


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    lang: str = "it"


SPEAK_PER_HOUR = 120
_speak_log: dict[str, list[float]] = {}


@app.post("/api/agent/speak")
async def agent_speak(body: SpeakRequest, user: User = Depends(auth.get_current_user)) -> Response:
    """The agent's answer as speech (Piper on this server, app/notify/voice.py).
    503 when the voice is not installed: the app falls back to the browser voice."""
    from app.notify import voice
    if guest_links.is_guest(user):
        raise HTTPException(status_code=403, detail="Guest mode: sign in to talk to the agent.")
    now = _time.time()
    recent = [t for t in _speak_log.get(user.id, []) if now - t < 3600]
    if len(recent) >= SPEAK_PER_HOUR:
        raise HTTPException(status_code=429, detail="Voice limit reached for this hour.")
    _speak_log[user.id] = recent + [now]
    try:
        audio = await voice.speak(body.text, body.lang)
    except Exception:
        logger.exception("piper voice")
        audio = None
    if not audio:
        raise HTTPException(status_code=503, detail="Server voice not available.")
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/agent/calls")
async def agent_calls(days: float = 7, recent: int = 0, user: User = Depends(auth.get_current_user)) -> dict:
    """Every DeepSeek call by purpose: repeats, failures, cache (issue #358, I00)."""
    from app.notify import deepseek_calls
    out = deepseek_calls.stats(days=max(0.1, min(days, 60)))
    if recent:
        out["recent"] = deepseek_calls.recent(min(recent, 300))
    return out


class ScanRequest(BaseModel):
    query: str


@app.post("/api/onchain/scan")
async def onchain_manual_scan(req: ScanRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """A ticker or address through the whole system, on request (manual_scan.py)."""
    from app.trading.onchain import manual_scan
    return await manual_scan.scan(req.query[:80])


# Finished manual scans by id, so a phone that lost the stream (Safari: "Load
# failed", often on a mobile network or a locked screen) can still collect the
# result instead of seeing an error. Lorenzo, 30/09: "ricevo spesso errore".
_SCAN_KEEP_S = 600
_scan_results: dict[str, tuple[float, dict | None]] = {}
_background_tasks: set[asyncio.Task] = set()   # keeps running scans referenced


def _scan_forget(now: float) -> None:
    for k in [k for k, (at, _) in _scan_results.items() if now - at > _SCAN_KEEP_S]:
        _scan_results.pop(k, None)


@app.post("/api/onchain/scan/stream")
async def onchain_manual_scan_stream(req: ScanRequest, user: User = Depends(auth.get_current_user)) -> StreamingResponse:
    """The same scan, one JSON line per step as it happens, then the result
    (Lorenzo, 30/09: he wants to watch the scout work). A line every few
    seconds of silence keeps idle mobile connections open; the scan keeps
    running if the client goes away, and its result stays at /scan/result/{id}."""
    import secrets
    from app.trading.onchain import manual_scan
    queue: asyncio.Queue = asyncio.Queue()
    scan_id = secrets.token_urlsafe(12)
    _scan_forget(time.time())
    _scan_results[scan_id] = (time.time(), None)

    async def run() -> dict:
        try:
            result = await manual_scan.scan(req.query[:80], emit=queue.put_nowait)
        except Exception:
            logger.exception("manual scan failed")
            result = {"error": "Scan failed, try again."}
        _scan_results[scan_id] = (time.time(), result)
        return result

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    async def lines():
        yield json.dumps({"id": scan_id}) + "\n"
        quiet = 0.0
        while not (task.done() and queue.empty()):
            try:
                ev = await asyncio.wait_for(queue.get(), 0.25)
            except asyncio.TimeoutError:
                quiet += 0.25
                if quiet >= 5:
                    quiet = 0.0
                    yield json.dumps({"ping": round(time.time())}) + "\n"
                continue
            quiet = 0.0
            yield json.dumps(ev) + "\n"
        yield json.dumps({"result": task.result()}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/onchain/scan/result/{scan_id}")
async def onchain_manual_scan_result(scan_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    """{"done": false} while the scan runs, {"done": true, "result": ...} after."""
    hit = _scan_results.get(scan_id[:40])
    if hit is None:
        raise HTTPException(status_code=404, detail="scan not found: the server may have restarted, run it again")
    return {"done": hit[1] is not None, "result": hit[1]}


class ScanAddRequest(BaseModel):
    chain: str
    token: str


@app.post("/api/onchain/scan/add")
async def onchain_manual_add(req: ScanAddRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """A scanned project into the Gem Finder's circle (manual_scan.add)."""
    from app.trading.onchain import manual_scan
    return manual_scan.add(req.chain[:20], req.token[:80])


def _agent_progress() -> list[dict]:
    """Quanto lavoro e' fatto e quanto ne resta: una riga per coda."""
    from app.trading.onchain import backtest_lab, discovery, listings
    rows: list[dict] = []
    try:
        if not settings.onchain_trading_enabled:
            raise LookupError       # no backtests without trading (07/10)
        st = onchain_alert_track.backtest_status("meme")
        rows.append({"key": "sniper", "label": "Sniper backtests",
                     "done": st["ready"], "total": st["total"] - st["no_data"]})
        s = onchain_strategy_lab.status(_top_entries(50))
        rows.append({"key": "gems", "label": "Gem backtests",
                     "done": s["ready"], "total": s["total"]})
    except Exception:
        pass
    try:
        done = backtest_lab.done_count()
        rows.append({"key": "studies", "label": "Tokens studied",
                     "done": done, "total": done + len(backtest_lab.queue())})
    except Exception:
        pass
    try:
        p = onchain_leaderboard.pool_status()
        rows.append({"key": "pool", "label": "Gem pool" + (" (scan paused)" if p["paused"] else ""),
                     "done": min(p["pool"], p["target"]), "total": p["target"]})
        rows.append({"key": "complete", "label": "Projects complete",
                     "done": p["complete"], "total": p["complete"] + p["backlog"]})
    except Exception:
        pass
    try:
        total = listings.status()["pending"] + discovery.status()["queue"]
        rows.append({"key": "scan", "label": "Queued to scan", "done": 0, "total": total})
    except Exception:
        pass
    try:
        m = onchain_projects_db.narrative_progress()
        rows.append({"key": "narratives", "label": "Narratives mapped", "done": m["mapped"], "total": m["total"]})
    except Exception:
        pass
    return rows


@app.get("/api/agent")
def agent_activity(user: User = Depends(auth.get_current_user)) -> dict:
    """The agent's live presence on the site: what DeepSeek did lately."""
    out = deepseek_desk.activity()
    out["watching"] = sum(1 for a in onchain_alert_track._alerts.values() if not a.get("closed") and not a.get("gone"))
    out["progress"] = _agent_progress()
    return out


@app.get("/api/wallets/deposit")
def wallets_deposit(user: User = Depends(auth.get_current_user)) -> dict:
    """The Deposit sheet: an address and QR per network, exchanges, card."""
    return onchain_deposit.options(_all_addresses(user), onchain_autotrader._meta.get("usdc"))


@app.get("/api/wallets/linked")
async def wallets_linked(user: User = Depends(auth.get_current_user)) -> dict:
    """The user's connected wallets, for the token page's "with my wallet"."""
    return {"wallets": profiles.wallets(user.id), "usdc_robinhood": onchain_autotrader._meta["usdc"]}


class UserSwapRequest(BaseModel):
    kind: Literal["evm", "solana"]
    owner: str
    side: Literal["buy", "sell"]
    token: str
    amount: float | None = None
    pct: int | None = None
    pay: str | None = None
    slippage_bps: int = user_swaps.DEFAULT_SLIPPAGE_BPS


@app.post("/api/wallets/swap/build")
async def wallets_swap_build(body: UserSwapRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Unsigned transactions for a swap from the user's own wallet (user_swaps.py)."""
    _require_onchain_enabled()
    _require_trading()
    mine = {(w["kind"], w["address"].lower()) for w in profiles.wallets(user.id)}
    if (body.kind, body.owner.lower()) not in mine:
        raise HTTPException(status_code=403, detail="Connect this wallet in your profile first.")
    if not wallet_link.valid_address(body.kind, body.token):
        raise HTTPException(status_code=422, detail="Unknown token address.")
    slip = max(10, min(user_swaps.MAX_SLIPPAGE_BPS, body.slippage_bps))
    try:
        if body.kind == "evm":
            allowed = {user_swaps.USDG.lower(): user_swaps.USDG}
            if onchain_autotrader._meta["usdc"]:
                allowed[onchain_autotrader._meta["usdc"].lower()] = onchain_autotrader._meta["usdc"]
            pay = allowed.get((body.pay or user_swaps.USDG).lower())
            if body.side == "buy" and pay is None:
                raise user_swaps.SwapError("Pay with USDG or USDC.")
            return await user_swaps.build_evm(body.owner, body.side, body.token, body.amount, body.pct, pay, slip)
        pay = body.pay or user_swaps.SOL_MINT
        if body.side == "buy" and pay not in (user_swaps.SOL_MINT, user_swaps.USDC_SOLANA):
            raise user_swaps.SwapError("Pay with SOL or USDC.")
        return await user_swaps.build_solana(body.owner, body.side, body.token, body.amount, body.pct, pay, slip)
    except user_swaps.SwapError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — RPC / Jupiter down: say so, don't 500
        raise HTTPException(status_code=502, detail=f"Couldn't prepare the swap: {exc}"[:300]) from exc


@app.delete("/api/wallets/link/{kind}/{address}")
async def wallet_unlink(kind: Literal["evm", "solana"], address: str, user: User = Depends(auth.get_current_user)) -> dict:
    return {"profile": profiles.public(user.id, profiles.remove_wallet(user.id, kind, address))}


@app.get("/api/profile/avatar/{uid}")
async def get_avatar(uid: str) -> FileResponse:
    path = profiles.avatar_path(uid)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No picture.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _charts_on_screen() -> list[tuple[str, str, str, str]]:
    """The charts a tap is likely to open, at the interval the token page
    starts on: the top memes (5m), the newest alerts (5m), the top gems (1h)."""
    out = []
    for m in (onchain_outcomes.meme_board().get("items") or [])[:8]:
        out.append((m.get("chain"), m.get("pair_address"), m.get("symbol"), "5m"))
    for a in (onchain_alert_track.board("meme", limit=5).get("items") or []):
        out.append((a.get("chain"), a.get("pair_address"), a.get("symbol"), "5m"))
    for e in (onchain_leaderboard.board().get("entries") or [])[:6]:
        out.append((e.get("chain"), e.get("pair_address"), e.get("symbol"), "1h"))
    seen, uniq = set(), []
    for t in out:
        if t[0] and t[1] and (t[0], t[1], t[3]) not in seen:
            seen.add((t[0], t[1], t[3]))
            uniq.append(t)
    return uniq


async def _backfill_alert_track() -> None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            n = await onchain_alert_track.backfill(client, _past_meme_alerts(), "meme")
        logger.info("alert track: %d past meme alerts rebuilt from candles", n)
    except Exception:
        logger.exception("alert track: backfill failed")


async def _onchain_signals_loop() -> None:
    """Prices for the alert track record, the listings' best strategy next
    to each meme alert, and once a day the reviewer."""
    from app.notify import telegram
    _fire(_backfill_alert_track())
    warmed_at = 0.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await onchain_alert_track.sample(client)
                await onchain_alert_track.refresh_system(client)
            await onchain_autotrader.tick(auth.list_users(), notify=_notify_autotrade)
            if onchain_autotrader.trend_keys():
                try:
                    marks = await onchain_live_prices.prices(onchain_autotrader.trend_keys())
                    onchain_autotrader.trend_marks({k: v["p"] for k, v in marks.items()})
                except Exception:
                    logger.exception("trend book marks")
            if _time.time() - warmed_at >= onchain_radar.WARM_EVERY_S:
                warmed_at = _time.time()
                _fire(onchain_radar.warm_charts(_charts_on_screen()))
            for item in onchain_listings.recent():
                onchain_alert_track.set_strategy("meme", item.get("chain"),
                                                 item.get("token_address") or item.get("pair_address"),
                                                 item.get("backtest"))
            if (onchain_ai_review.enabled() and onchain_ai_review.due()
                    and _time.time() - _signals_started >= onchain_ai_review.FIRST_DELAY_S):
                entry = await onchain_ai_review.review()
                if entry:
                    await telegram.send_message(onchain_ai_review.telegram_text(entry))
        except Exception:
            logger.exception("segnali on-chain: giro fallito")
        await asyncio.sleep(_SIGNALS_TICK_S)


@app.get("/api/onchain/scan-feed")
async def onchain_scan_feed(user: User = Depends(auth.get_current_user)) -> dict:
    """The scanners' live tape: every pool judged, with the hour's counts."""
    _require_onchain_enabled()
    from app.trading.onchain import scan_feed
    return scan_feed.snapshot()


@app.get("/api/onchain/alerts/{kind}")
async def onchain_alerts_view(kind: Literal["meme", "gem"], user: User = Depends(auth.get_current_user)) -> dict:
    """Every Telegram alert and what entering on the message would pay: 1-48h
    checkpoints, live, and the best backtested strategy. See alert_track.py."""
    _require_trading()
    _require_onchain_enabled()
    out = onchain_alert_track.board(kind)
    if kind == "meme":
        out.update(feed=onchain_alert_track.feed(kind), reviving=onchain_alert_track.reviving(kind),
                   upcoming=onchain_listings.upcoming())
    return out


@app.get("/api/onchain/ai-review")
async def onchain_ai_review_view(user: User = Depends(auth.get_current_user)) -> dict:
    """The daily reviewer's lessons (ai_review.py) and its token budget."""
    _require_trading()
    _require_onchain_enabled()
    reviews = onchain_ai_review._state["reviews"][:7]
    return {"status": onchain_ai_review.status(), "latest": reviews[0] if reviews else None, "history": reviews[1:]}


@app.get("/api/onchain/chart")
async def onchain_token_chart(
    chain: str, pool: str = Query(..., pattern=r"^([0-9A-Za-z]{20,66}|(mexc|binance):[A-Z0-9]{2,20}USDT)$"),
    symbol: str = Query("", max_length=40),
    interval: Literal["1m", "5m", "15m", "1h", "4h", "1d", "1w"] = "15m", peek: bool = False,
    user: User = Depends(auth.get_current_user),
) -> dict:
    """The token page's chart: candles only, any chain the hunts scan. With
    peek, only a copy the server already has (instant, maybe empty): the
    browser fetches GeckoTerminal itself in parallel."""
    _require_onchain_enabled()
    if chain not in onchain_legit.CHAIN_LABEL:
        raise HTTPException(status_code=422, detail="unknown chain")
    if peek:
        return onchain_radar.chart_peek(chain, pool, interval)
    try:
        return await onchain_radar.chart_candles(chain, pool, symbol or "TOKEN", interval)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/onchain/radar/chart")
async def onchain_radar_chart(
    chain: Literal["solana", "robinhood"], pool: str, symbol: str,
    interval: Literal["1m", "5m", "15m", "1h", "4h"] = "15m", user: User = Depends(auth.get_current_user),
) -> dict:
    _require_trading()
    _require_onchain_enabled()
    try:
        payload = await onchain_radar.chart_payload(chain, pool, symbol, interval)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    payload["active"] = _radar_watch_strategies(user.id, onchain_radar.watch_symbol(symbol, chain, pool))
    return payload


def _radar_watch_strategies(owner_id: str, symbol: str) -> list[str]:
    entry = next((e for e in watchlist_store.list_entries(owner_id) if e.symbol == symbol), None)
    return list(entry.strategies) if entry else []


class RadarFollowRequest(BaseModel):
    chain: Literal["solana", "robinhood"]
    pool: str
    symbol: str
    token_address: str
    interval: Literal["1m", "5m", "15m", "1h", "4h"]
    strategy_name: str
    params: dict | None = None
    enabled: bool


@app.post("/api/onchain/radar/follow")
async def onchain_radar_follow(req: RadarFollowRequest, user: User = Depends(auth.get_current_user)) -> dict:
    """Attiva (o spegne) una strategia su un token del radar: solo alert.
    L'acquisto automatico resta una scelta a parte, del pilota."""
    _require_trading()
    _require_onchain_enabled()
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail="Unknown strategy")
    symbol = onchain_radar.watch_symbol(req.symbol, req.chain, req.pool)
    entry = next((e for e in watchlist_store.list_entries(user.id) if e.symbol == symbol), None)
    if entry is None and req.enabled:
        entry = watchlist_store.add_entry(user.id, symbol, "dex", interval=req.interval, on_close=True,
                                          base_token_address=req.token_address)
    if entry is not None:
        watchlist_store.set_strategy_enabled(entry.id, req.strategy_name, req.enabled)
        if req.enabled:
            watchlist_store.set_strategy_params(entry.id, req.strategy_name, req.params)
    return {"active": _radar_watch_strategies(user.id, symbol)}


@app.get("/api/onchain/pilot/{chain}")
async def onchain_pilot_status(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    """Tutto quello che il pilota fa da solo su una chain, in una risposta:
    se e' acceso, quando ha guardato l'ultima volta, cosa ha esaminato e
    scartato, i token che segue e perche', e — prima di tutto — cosa gli
    impedirebbe di lavorare. Lorenzo: "voglio avere il completo controllo
    di quello che sta succedendo"."""
    _require_trading()
    _require_onchain_enabled()
    enabled = chain in user.sniper_autotrade_chains
    breaker = onchain_ledger.breaker_status(user.id, chain)
    spent_today = onchain_ledger.get_daily_spent(user.id, chain)

    # Cosa lo ferma, in ordine di gravita'. Ogni voce e' una condizione che
    # il codice controlla davvero (bridge e giro dei segnali), non un'ipotesi.
    health = [
        {"key": "wallet", "ok": chain in user.onchain_wallets, "label": "Wallet on this network",
         "detail": "created" if chain in user.onchain_wallets else "missing: create it first"},
        {"key": "scanner", "ok": settings.sniper_enabled, "label": "New-token scanner",
         "detail": "on" if settings.sniper_enabled else "off on the server: no new tokens coming in"},
        {"key": "signals", "ok": bool(settings.telegram_bot_token and user.telegram_chat_id and _telegram_allowed(user)),
         "label": "Signal check",
         "detail": "on" if settings.telegram_bot_token and user.telegram_chat_id and _telegram_allowed(user)
         else "stopped: needs Telegram linked, without it the strategy is not evaluated"},
        {"key": "execution", "ok": settings.onchain_execution_enabled, "label": "Live orders",
         "detail": "enabled" if settings.onchain_execution_enabled else "off on the server: signals are only logged"},
        {"key": "breaker", "ok": not breaker.get("tripped"), "label": "Emergency brake",
         "detail": "armed, not tripped" if not breaker.get("tripped") else "tripped: re-arm it from Limits"},
    ]

    positions = {p["token"].lower(): p for p in onchain_ledger.get_all_positions(user.id, chain)}
    journal = trading_log.list_for(user.id, limit=200)
    tokens = []
    for entry in watchlist_store.list_entries(user.id):
        if entry.asset_type != "dex" or not entry.auto_trade.get(_SNIPER_ONCHAIN_STRATEGY):
            continue
        # "BASE@chain:pool", lo stesso formato che scrive il ponte.
        if entry.symbol.partition("@")[2].partition(":")[0] != chain:
            continue
        tokens.append(_pilot_token_view(user, entry, chain, positions, journal))
    order = {"in": 0, "blocked": 1, "watch": 2}
    tokens.sort(key=lambda t: (order.get(t["state"], 3), -(t["added_at"] or 0)))

    return {
        "chain": chain,
        "enabled": enabled,
        "strategy": _SNIPER_ONCHAIN_STRATEGY,
        "filters": {"min_liquidity_usd": _SNIPER_ONCHAIN_MIN_LIQUIDITY_USD, "max_age_hours": _SNIPER_ONCHAIN_MAX_AGE_HOURS},
        "limits": {
            "per_trade_usd": settings.onchain_max_usd_per_trade,
            "daily_usd": settings.onchain_max_usd_per_day,
            "spent_today_usd": spent_today,
        },
        "scan": onchain_pilot.scan_state(chain),
        "signals_every_s": WATCHLIST_POLL_SECONDS,
        "health": health,
        "working": enabled and all(h["ok"] for h in health),
        "tokens": tokens,
        "rejected": onchain_pilot.rejected(chain)[:8],
    }


@app.post("/api/onchain/risk/circuit-breaker/{chain}/reset")
async def onchain_reset_circuit_breaker(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    """Riattivazione MANUALE apposta — mai automatica, vedi il piano."""
    _require_onchain_enabled()
    onchain_ledger.reset_breaker(user.id, chain)
    return onchain_ledger.breaker_status(user.id, chain)


def _require_onchain_admin(user: User) -> None:
    allowed = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    if user.email.lower() not in allowed:
        raise HTTPException(status_code=403, detail="Only a configured admin (settings.admin_emails) "
                                                      "can read /admin/status.")


# ---------------------------------------------------------------- link ospite

class GuestLinkRequest(BaseModel):
    label: str = ""
    days: int = guest_links.DEFAULT_DAYS


def _require_admin(user: User) -> None:
    allowed = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    if guest_links.is_guest(user) or user.email.lower() not in allowed:
        raise HTTPException(status_code=403, detail="Only an admin can manage guest links.")


def _guest_link_out(link: guest_links.GuestLink, request: Request) -> dict:
    base = f"{'https' if _is_https(request) else 'http'}://{request.headers.get('host', request.url.netloc)}"
    return {
        "id": link.id,
        "label": link.label,
        "created_at": link.created_at,
        "expires_at": link.expires_at,
        "active": link.active(),
        "url": f"{base}/ospite?t={link.token}" if link.active() else None,
    }


@app.get("/api/guest-links")
async def guest_links_list(request: Request, user: User = Depends(auth.get_current_user)) -> list[dict]:
    _require_admin(user)
    return [_guest_link_out(link, request) for link in guest_links.list_links()]


@app.post("/api/guest-links")
async def guest_links_create(
    req: GuestLinkRequest, request: Request, user: User = Depends(auth.get_current_user)
) -> dict:
    _require_admin(user)
    return _guest_link_out(guest_links.create_link(user.email, req.label, req.days), request)


@app.delete("/api/guest-links/{link_id}")
async def guest_links_revoke(link_id: str, request: Request, user: User = Depends(auth.get_current_user)) -> dict:
    _require_admin(user)
    link = guest_links.revoke_link(link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    return _guest_link_out(link, request)


@app.get("/ospite")
async def guest_enter(request: Request, t: str = "") -> Response:
    link = guest_links.find_by_token(t) if t else None
    if link is None:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Invalid link</title><body style='font:16px system-ui;padding:32px;background:#0b0b0c;color:#ddd'>"
            "<p>This guest link has expired or been revoked.</p></body>",
            status_code=410,
            headers=_HTML_HEADERS,
        )
    response = RedirectResponse("/onchain-app.html", status_code=303)
    response.set_cookie(
        guest_links.GUEST_COOKIE,
        guest_links.make_cookie(link),
        max_age=max(60, int(link.expires_at - _time.time())),
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )
    return response


_GUEST_ADMIN_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Guest links</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px;background:#0b0b0c;color:#e6e6e6;max-width:640px}
h1{font-size:18px;margin:0 0 16px}form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
input,button{font:inherit;font-size:16px;padding:8px 10px;border-radius:8px;border:1px solid #333;background:#151517;color:inherit}
button{cursor:pointer}ul{list-style:none;padding:0;margin:0}li{padding:10px 0;border-top:1px solid #222}
.off{opacity:.45}code{word-break:break-all;font-size:13px;color:#9ad}small{color:#888}
</style>
<h1>Guest links &middot; read-only</h1>
<form id="f"><input id="label" placeholder="For whom (e.g. Marco)" maxlength="60">
<input id="days" type="number" min="1" max="90" value="7" style="width:5em"> days
<button>Create link</button></form>
<ul id="list"></ul>
<script>
const $=s=>document.querySelector(s);
const day=t=>new Date(t*1000).toLocaleDateString("en-US",{day:"numeric",month:"short"});
async function load(){
  const r=await fetch("/api/guest-links");const rows=r.ok?await r.json():[];
  $("#list").innerHTML="";
  for(const l of rows){const li=document.createElement("li");if(!l.active)li.className="off";
    li.innerHTML=`<b></b> <small>${l.active?"expires "+day(l.expires_at):"closed"}</small><br>`;
    li.querySelector("b").textContent=l.label||"unnamed";
    if(l.url){const c=document.createElement("code");c.textContent=l.url;li.append(c," ");
      const cp=document.createElement("button");cp.textContent="Copy";cp.onclick=()=>navigator.clipboard.writeText(l.url);
      const rv=document.createElement("button");rv.textContent="Revoke";
      rv.onclick=async()=>{await fetch("/api/guest-links/"+l.id,{method:"DELETE"});load();};li.append(cp," ",rv);}
    $("#list").append(li);}
}
$("#f").onsubmit=async e=>{e.preventDefault();
  const r=await fetch("/api/guest-links",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({label:$("#label").value,days:+$("#days").value||7})});
  if(r.ok){const l=await r.json();try{await navigator.clipboard.writeText(l.url)}catch{};$("#label").value="";}
  load();};
load();
</script></html>"""


@app.get("/admin/ospiti")
async def guest_links_page(user: User = Depends(auth.get_current_user)) -> HTMLResponse:
    _require_admin(user)
    return HTMLResponse(_GUEST_ADMIN_HTML, headers={"Cache-Control": "no-store"})


_GUEST_BANNER = (
    '<div role="status" style="position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:2147483647;'
    'padding:6px 12px;border-radius:999px;background:rgba(20,20,22,.88);color:#e6e6e6;'
    'font:12px/1.2 system-ui,sans-serif;letter-spacing:.02em;pointer-events:none;'
    'border:1px solid rgba(255,255,255,.12)">Guest &middot; read-only</div>'
)


def _with_guest_banner(html: str, user: User | None) -> str:
    if not guest_links.is_guest(user):
        return html
    return re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _GUEST_BANNER, html, count=1)


@app.get("/admin/status")
async def admin_status(user: User = Depends(auth.get_current_user)) -> dict:
    """Piano SRE on-chain, punto 3: paginetta di osservabilita' — ordini
    eseguiti/rifiutati per causa nelle ultime 24h, stato del circuit
    breaker per chain, ultimo saldo noto per ogni wallet dell'utente
    corrente, stato del fallback RPC. Query dirette su ledger._connect(),
    niente framework di metriche esterno — come chiesto.

    Protezione: nessun concetto di owner/admin esiste altrove in questo
    sito (User non ha un ruolo), quindi qui si usa una allowlist email
    dedicata (settings.admin_emails) sopra il normale
    Depends(auth.get_current_user) — chi non e' in lista prende 403, non
    solo un utente qualunque loggato.

    Le metriche ordini/circuit-breaker sono AGGREGATE su tutti gli owner
    del ledger (il "sistema" che il piano chiede di osservare, non solo
    l'account di chi guarda) — il saldo wallet invece resta per forza per
    l'utente che chiama (le chiavi/indirizzi sono suoi)."""
    _require_onchain_admin(user)

    since = _time.time() - 24 * 3600
    with onchain_ledger._lock, onchain_ledger._connect() as conn:
        rows = conn.execute(
            "SELECT chain, status, reason FROM orders WHERE created_at >= ?", (since,),
        ).fetchall()

    orders_24h = {"executed": 0, "rejected_by_cause": {}, "failed_by_cause": {}, "pending": 0}
    for row in rows:
        status = row["status"]
        if status == "confirmed":
            orders_24h["executed"] += 1
        elif status == "failed":
            cause = (row["reason"] or "sconosciuta").strip()
            orders_24h["failed_by_cause"][cause] = orders_24h["failed_by_cause"].get(cause, 0) + 1
        elif status in ("pending", "submitted"):
            orders_24h["pending"] += 1
    # I rifiuti dal risk guardrail (run_all_checks) non arrivano mai al
    # ledger — simulate_order/execute_order tornano prima di ledger.reserve()
    # — quindi non compaiono come righe qui. Nota onesta, non un bug: il
    # ledger e' la fonte di verita' per gli ordini RISERVATI, non per ogni
    # richiesta rifiutata a monte (quelle sono nei log applicativi con
    # correlation_id, vedi execute.py/simulate.py).

    circuit_breakers = {
        chain: onchain_ledger.breaker_status(user.id, chain) for chain in ("solana", "robinhood")
    }

    wallet_balances = {}
    for chain, wallet in user.onchain_wallets.items():
        try:
            wallet_balances[chain] = {
                "address": wallet.address,
                "native_balance": await onchain_balances.native_balance(chain, wallet.address),
                "read_at": _time.time(),
            }
        except Exception as exc:  # noqa: BLE001 — un RPC irraggiungibile non deve rompere la pagina intera
            wallet_balances[chain] = {"address": wallet.address, "native_balance": None, "error": str(exc)}

    return {
        "orders_last_24h": orders_24h,
        "circuit_breakers": circuit_breakers,
        "wallet_balances": wallet_balances,
        "rpc_fallback": onchain_rpc_client.circuit_status(),
    }


class FuturesQuoteRequest(BaseModel):
    exchange: str
    symbol: str
    side: Literal["BUY", "SELL"]
    size_mode: Literal["fixed_usdt", "pct_balance", "base_qty"] = "fixed_usdt"
    size_value: float
    leverage: int = 1


@app.get("/api/trading/futures/positions")
async def list_futures_positions(user: User = Depends(auth.get_current_user)):
    """Sola lettura: le posizioni a leva aperte, con la distanza dalla
    liquidazione gia' calcolata. Non puo' in nessun caso inviare un ordine —
    per questo non chiede conferma e non tocca l'executor."""
    out: list[dict] = []
    for exchange in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        cred = user.futures_credentials.get(exchange)
        if not cred:
            continue
        api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, exchange)
        if not api_key or not api_secret:
            continue
        adapter = futures_registry.get(exchange)
        creds = FuturesCreds(api_key=api_key, api_secret=api_secret, passphrase=passphrase)
        try:
            positions = await adapter.open_positions(creds)
        except FuturesExecutionError:
            # Un exchange che non risponde non deve nascondere gli altri.
            continue
        out.extend(p.as_dict() for p in positions)
    return out


@app.post("/api/trading/futures/quote")
async def quote_futures_order(body: FuturesQuoteRequest, user: User = Depends(auth.get_current_user)):
    """Anteprima obbligatoria prima di un ordine futures manuale: leva,
    margine impegnato e prezzo di liquidazione stimato — mai un click
    singolo come per lo spot (vedi il piano "Operazioni futures", Fase 4)."""
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures not supported yet for this exchange: {body.exchange}")
    cred = user.futures_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="No futures credentials configured for this exchange")
    api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key or secret missing")
    adapter = futures_registry.get(body.exchange)
    if adapter.needs_passphrase and not passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} requires a passphrase, not saved yet")
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leverage {body.leverage}x outside {adapter.name}'s limit (1-{max_lev}x)")

    symbol = body.symbol.strip().upper()
    creds = FuturesCreds(api_key=api_key, api_secret=api_secret, passphrase=passphrase)
    try:
        mark = await adapter.mark_price(symbol, creds)
        available = await adapter.available_margin(creds)
    except FuturesExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if body.size_mode == "fixed_usdt":
        margin_used = body.size_value
    elif body.size_mode == "base_qty":
        # size_value e' gia' una quantita' di moneta base (es. 0.05 BTC):
        # il margine e' il nozionale a quel prezzo, diviso la leva — non un
        # importo da moltiplicare come negli altri due modi. Vedi
        # FuturesAdapter._notional per lo stesso calcolo lato ordine reale.
        margin_used = (body.size_value * mark) / body.leverage
    else:
        margin_used = available * body.size_value / 100
    # Cosa verrebbe impegnato DAVVERO: su MEXC la dimensione si arrotonda a
    # contratti interi, quindi l'importo chiesto e quello eseguito possono non
    # coincidere. Mostrare il primo e spedire il secondo e' esattamente il
    # difetto segnalato ("doveva essere 40 dollari ma ne ha usati 34 e
    # qualcosa"): l'anteprima esiste per non avere sorprese dopo il click.
    rounding_note = ""
    try:
        effective, rounding_note = await adapter.effective_notional(
            symbol, margin_used * body.leverage, mark, creds,
        )
        margin_used = effective / body.leverage
    except FuturesExecutionError:
        # L'anteprima resta utile anche se la specifica del contratto non e'
        # leggibile: meglio il numero teorico che nessun numero.
        pass

    direction = "long" if body.side == "BUY" else "short"
    # Kraken non ha isolated margin: la liquidazione qui e' comunque
    # calcolata come se lo fosse, perche' e' il numero che dice quanto puo'
    # muoversi il prezzo, ma senza isolated la perdita puo' non fermarsi li'
    # — per questo l'anteprima porta anche `isolated_margin`.
    liq_price = liquidation_price(mark, direction, body.leverage, DEFAULT_MAINTENANCE_MARGIN_RATE)
    sufficient = margin_used <= available
    return {
        "symbol": symbol, "mark_price": mark, "leverage": body.leverage,
        "margin_used": margin_used, "notional": margin_used * body.leverage,
        "liquidation_price": liq_price, "available_margin": available,
        "margin_sufficient": sufficient, "isolated_margin": adapter.isolated_margin,
        "rounding_note": rounding_note,
    }


class FuturesOrderRequest(BaseModel):
    exchange: str
    symbol: str
    side: Literal["BUY", "SELL"]
    size_mode: Literal["fixed_usdt", "pct_balance", "base_qty"] = "fixed_usdt"
    size_value: float
    leverage: int = 1
    confirm: bool = False  # deve essere vero: nessun ordine futures parte senza conferma esplicita


@app.post("/api/trading/futures/order")
async def place_manual_futures_order(body: FuturesOrderRequest, user: User = Depends(auth.get_current_user)):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required: show the preview first (/api/trading/futures/quote)")
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures not supported yet for this exchange: {body.exchange}")
    cred = user.futures_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="No futures credentials configured for this exchange")
    adapter = futures_registry.get(body.exchange)
    if not cred.enabled:
        # Kill switch: spegnibile senza toccare le chiavi — vale anche per
        # l'ordine manuale, non solo per l'esecuzione automatica. Il
        # messaggio nomina l'exchange e dove si accende, invece di lasciare
        # che chi legge debba andare a cercarlo da solo (segnalazione
        # 6237708e: "devi attivare l'esecuzione" — il messaggio prima non
        # lo diceva).
        raise HTTPException(
            status_code=400,
            detail=f"Futures execution disabled for {adapter.name} — turn it on with the \"Active\" toggle on its card above, or press \"Enable now\" below.",
        )
    api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key or secret missing")
    if adapter.needs_passphrase and not passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} requires a passphrase, not saved yet")
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leverage {body.leverage}x outside {adapter.name}'s limit (1-{max_lev}x)")

    symbol = body.symbol.strip().upper()
    creds = FuturesCreds(api_key=api_key, api_secret=api_secret, passphrase=passphrase)

    # Controllo pre-trade del margine disponibile: letto per davvero
    # dall'exchange, non dalla cache.
    try:
        available = await adapter.available_margin(creds)
    except FuturesExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if body.size_mode == "fixed_usdt":
        margin_needed = body.size_value
    elif body.size_mode == "base_qty":
        # Senza questo ramo, size_value (una quantita' di moneta base, es.
        # 0.03 BTC) sarebbe caduto nell'else e letto come percentuale del
        # saldo — 0.03% di qualunque cifra e' minuscolo, quindi il controllo
        # di margine qui sotto sarebbe passato SEMPRE, indipendentemente dal
        # nozionale reale dell'ordine. Lo stesso calcolo di
        # FuturesAdapter._notional, ripetuto qui perche' il controllo
        # pre-trade e l'esecuzione vera restano deliberatamente due letture
        # separate del margine (mai una cache condivisa fra le due).
        try:
            mark = await adapter.mark_price(symbol, creds)
        except FuturesExecutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        margin_needed = (body.size_value * mark) / body.leverage
    else:
        margin_needed = available * body.size_value / 100
    if margin_needed > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient margin: {margin_needed:.2f} USDT needed, {available:.2f} USDT available",
        )

    from app.models import StrategySignal
    signal_type = "entry_long" if body.side == "BUY" else "entry_short"
    fake_signal = StrategySignal(time=int(_time.time()), type=signal_type, price=0.0, label="manual")
    detail_prefix = f"leva {body.leverage}x — "
    try:
        result = await place_futures_order_for_signal(
            body.exchange, symbol, fake_signal,
            body.size_mode, body.size_value, body.leverage,
            api_key, api_secret, passphrase=passphrase,
        )
    except RuntimeError as exc:
        trading_log.record(
            user.id, symbol=symbol, exchange=body.exchange,
            strategy_name="manuale futures", signal_type=signal_type,
            outcome="fallito", detail=detail_prefix + str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    trading_log.record(
        user.id, symbol=symbol, exchange=body.exchange,
        strategy_name="manuale futures", signal_type=signal_type,
        outcome="eseguito", detail=(detail_prefix + str(result))[:400],
    )
    return {"ok": True, "result": result, "symbol": symbol}


@app.post("/api/trading/futures/close")
async def close_manual_futures_position(body: ClosePositionRequest, user: User = Depends(auth.get_current_user)):
    """Chiudere a mano una posizione a leva aperta.

    Esisteva solo la chiusura automatica sui segnali di uscita: chi apriva
    una posizione — anche dall'ordine manuale qui sopra — poteva poi solo
    guardarla, e per uscire doveva andare sull'app dell'exchange. Con la
    leva quella distanza si paga.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required to close a position")
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures not supported for this exchange: {body.exchange}")
    cred = user.futures_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="No futures credentials configured for this exchange")
    adapter = futures_registry.get(body.exchange)
    # Nessun controllo su cred.enabled, a differenza dell'apertura: il kill
    # switch esiste per impedire NUOVA esposizione, e una chiusura la riduce
    # sempre. Spegnerlo con una posizione aperta e poi non poterla piu'
    # chiudere da qui sarebbe esattamente il modo di restare intrappolati in
    # una posizione a leva — il contrario di cio' per cui quel toggle c'e'.
    api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key or secret missing")
    if adapter.needs_passphrase and not passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} requires a passphrase, not saved yet")

    symbol = body.symbol.strip().upper()
    creds = FuturesCreds(api_key=api_key, api_secret=api_secret, passphrase=passphrase)
    try:
        result = await adapter.close_position(symbol, creds)
    except FuturesExecutionError as exc:
        trading_log.record(
            user.id, symbol=symbol, exchange=body.exchange,
            strategy_name="chiusura manuale futures", signal_type="exit",
            outcome="fallito", detail=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # close_position() torna None quando non c'e' niente da chiudere: dirlo
    # com'e', invece di un "chiusa" che farebbe credere di essere usciti da
    # una posizione che qualcun altro (o un segnale di uscita) aveva gia'
    # chiuso.
    if result is None:
        return {"ok": True, "closed": False, "symbol": symbol,
                "detail": f"No open position on {symbol} to close."}
    trading_log.record(
        user.id, symbol=symbol, exchange=body.exchange,
        strategy_name="chiusura manuale futures", signal_type="exit",
        outcome="eseguito", detail=str(result)[:400],
    )
    return {"ok": True, "closed": True, "result": result, "symbol": symbol}


class HaltRequest(BaseModel):
    halt: bool


@app.post("/api/trading/halt")
async def set_trading_halt(body: HaltRequest, user: User = Depends(auth.get_current_user)):
    """Kill switch: mette in pausa o riprende l'esecuzione automatica degli ordini.

    Non tocca alert, backtest o ottimizzatore — solo l'esecuzione automatica.
    La chiusura manuale di una posizione esistente ignora il kill switch
    (vedi /api/trading/futures/close).
    """
    allowed_emails = {e.strip().lower() for e in settings.registration_allowed_emails.split(",") if e.strip()}
    if user.email.lower() not in allowed_emails:
        raise HTTPException(status_code=403, detail="Not authorized")
    settings.trading_halt = body.halt
    state = "paused" if body.halt else "active"
    return {"ok": True, "trading_halt": body.halt, "detail": f"Auto-execution {state}"}


@app.get("/api/trading/halt")
async def get_trading_halt(user: User = Depends(auth.get_current_user)):
    """Stato corrente del kill switch."""
    allowed_emails = {e.strip().lower() for e in settings.registration_allowed_emails.split(",") if e.strip()}
    if user.email.lower() not in allowed_emails:
        raise HTTPException(status_code=403, detail="Not authorized")
    return {"trading_halt": settings.trading_halt}


@app.get("/api/balances")
async def get_balances(user: User = Depends(auth.get_current_user)) -> dict:
    if not _balance_visible(user):
        raise HTTPException(status_code=403, detail="Not available for this account")

    async def _safe(fetch_fn, configured: bool):
        if not configured:
            return {"configured": False, "balances": [], "error": None}
        try:
            balances = await fetch_fn()
            return {"configured": True, "balances": balances, "error": None}
        except BalanceFetchError as exc:
            return {"configured": True, "balances": [], "error": str(exc)}

    mexc, kucoin = await asyncio.gather(
        _safe(fetch_mexc_balances, bool(settings.mexc_api_key and settings.mexc_api_secret)),
        _safe(
            fetch_kucoin_balances,
            bool(settings.kucoin_api_key and settings.kucoin_api_secret and settings.kucoin_api_passphrase),
        ),
    )
    return {"mexc": mexc, "kucoin": kucoin}


def _owned_entry_or_404(entry_id: str, user: User) -> WatchlistEntry:
    entry = watchlist_store.get_entry(entry_id)
    if entry is None or entry.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


@app.get("/api/preferences")
async def get_preferences(user: User = Depends(auth.get_current_user)) -> dict:
    return user.preferences or {}


@app.put("/api/preferences")
async def put_preferences(
    body: dict,
    user: User = Depends(auth.get_current_user),
) -> dict:
    # Allowlist the keys we actually care about — ignore anything unknown
    allowed = {"ticker", "asset_type", "exchange", "interval", "strategy", "strategy_params",
               "layers", "log_scale", "lab_ticker", "lab_asset_type", "lab_exchange", "lab_interval",
               "theme"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    auth.set_preferences(user.id, filtered)
    return filtered


@app.get("/api/watchlist", response_model=list[WatchlistEntry])
async def get_watchlist(user: User = Depends(auth.get_current_user)) -> list[WatchlistEntry]:
    return watchlist_store.list_entries(owner_id=user.id)


class WatchlistAddRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    interval: IntervalChoice = "1d"
    on_close: bool = True
    # Solo per asset_type="dex", passato dal risultato della ricerca
    # DexScreener (o da una entry watchlist gia' esistente che lo porta).
    base_token_address: str | None = None


@app.post("/api/watchlist", response_model=WatchlistEntry)
async def add_watchlist_entry(
    req: WatchlistAddRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")

    # Re-verify the pair actually resolves on this exchange before storing
    # it — the frontend already confirms this via the suggestions list, but
    # a symbol could still be typed/submitted without going through it, or
    # get delisted between being suggested and being added.
    provider = get_provider(req.asset_type, req.exchange)
    try:
        probe = await provider.fetch_ohlcv(symbol, "1d", 2)
    except ProviderError as exc:
        raise HTTPException(
            status_code=400, detail=f"Ticker not found on this exchange: {exc}"
        ) from exc
    if probe.empty:
        raise HTTPException(status_code=400, detail="Ticker not found on this exchange.")

    return watchlist_store.add_entry(
        user.id, symbol, req.asset_type, exchange=req.exchange, interval=req.interval, on_close=req.on_close,
        base_token_address=req.base_token_address,
    )


class WatchlistIntervalRequest(BaseModel):
    interval: IntervalChoice


@app.patch("/api/watchlist/{entry_id}/interval", response_model=WatchlistEntry)
async def set_watchlist_interval(
    entry_id: str, req: WatchlistIntervalRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_interval(entry_id, req.interval)


@app.delete("/api/watchlist/{entry_id}")
async def delete_watchlist_entry(entry_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    _owned_entry_or_404(entry_id, user)
    watchlist_store.remove_entry(entry_id)
    return {"ok": True}


class WatchlistStrategyToggleRequest(BaseModel):
    strategy_name: str
    enabled: bool


@app.patch("/api/watchlist/{entry_id}/strategy", response_model=WatchlistEntry)
async def toggle_watchlist_strategy(
    entry_id: str, req: WatchlistStrategyToggleRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    before = _owned_entry_or_404(entry_id, user)
    was_enabled = req.strategy_name in before.strategies
    entry = watchlist_store.set_strategy_enabled(entry_id, req.strategy_name, req.enabled)
    if was_enabled and not req.enabled:
        # Spegnendo l'alert si dimentica da quando era armato: riaccendendolo
        # deve ripartire da adesso, non dalla prima attivazione — altrimenti
        # tutto quello che e' successo nel frattempo arriverebbe in blocco.
        alerts.disarm(entry_id, req.strategy_name)
    if req.enabled and not was_enabled:
        # Fotografia della configurazione con cui l'alert parte. E' il momento
        # in cui smette di essere un esperimento: da qui lavora per settimane,
        # e il risultato dell'ottimizzazione che l'ha prodotta vive quanto la
        # scheda del browser. Spegnere e riaccendere non duplica la voce; una
        # riattivazione con parametri diversi si', perche' e' un'altra strategia.
        params = entry.strategy_params.get(req.strategy_name, {}) or {}
        if not archive_store.already_archived(user.id, entry_id, req.strategy_name, params):
            snapshot = archive_store.create(
                owner_id=user.id, symbol=entry.symbol, asset_type=entry.asset_type,
                exchange=entry.exchange,
                interval=entry.strategy_intervals.get(req.strategy_name, entry.interval),
                strategy_name=req.strategy_name, params=params,
                backtest_params={}, source="alert", watchlist_entry_id=entry_id,
            )
            _fire(_run_archive_backtest(
                snapshot["id"], entry.symbol, entry.asset_type, entry.exchange,
                snapshot["interval"], req.strategy_name, params, {},
            ))

    if req.enabled and not was_enabled and user.telegram_chat_id and _telegram_allowed(user):
        # Confirms the watch is actually active — sent immediately on
        # activation, before any real signal, so silence afterward reads as
        # "no condition yet" rather than "is this even running?".
        _fire(
            telegram.send_to(
                user.telegram_chat_id,
                f"✅ <b>{entry.symbol}</b> — {req.strategy_name}\n"
                f"Timeframe: {entry.interval}\n"
                "Monitoraggio attivato. Riceverai un messaggio quando viene rilevata una condizione.",
            )
        )
    return entry


class WatchlistOnCloseRequest(BaseModel):
    on_close: bool


@app.patch("/api/watchlist/{entry_id}/on_close", response_model=WatchlistEntry)
async def set_watchlist_on_close(
    entry_id: str, req: WatchlistOnCloseRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_on_close(entry_id, req.on_close)


class WatchlistStrategyParamsRequest(BaseModel):
    strategy_name: str
    params: dict = {}  # {} resets this strategy back to its global defaults


@app.patch("/api/watchlist/{entry_id}/strategy_params", response_model=WatchlistEntry)
async def set_watchlist_strategy_params(
    entry_id: str, req: WatchlistStrategyParamsRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_strategy_params(entry_id, req.strategy_name, req.params)


class WatchlistFrequencyRequest(BaseModel):
    strategy_name: str
    frequency: Literal["once", "every"]


@app.patch("/api/watchlist/{entry_id}/alert_frequency", response_model=WatchlistEntry)
async def set_watchlist_alert_frequency(
    entry_id: str, req: WatchlistFrequencyRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_alert_frequency(entry_id, req.strategy_name, req.frequency)


class WatchlistAutoTradeRequest(BaseModel):
    strategy_name: str
    enabled: bool


@app.patch("/api/watchlist/{entry_id}/auto_trade", response_model=WatchlistEntry)
async def set_watchlist_auto_trade(
    entry_id: str, req: WatchlistAutoTradeRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_auto_trade(entry_id, req.strategy_name, req.enabled)


class WatchlistSizeCapRequest(BaseModel):
    strategy_name: str
    cap_usdt: float = 0.0  # 0 = nessun tetto


@app.patch("/api/watchlist/{entry_id}/size_cap", response_model=WatchlistEntry)
async def set_watchlist_size_cap(
    entry_id: str, req: WatchlistSizeCapRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    if req.cap_usdt < 0:
        raise HTTPException(status_code=400, detail="The cap can't be negative")
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_size_cap(entry_id, req.strategy_name, req.cap_usdt)


class WatchlistMarketRequest(BaseModel):
    strategy_name: str
    market: Literal["spot", "futures"]


@app.patch("/api/watchlist/{entry_id}/market", response_model=WatchlistEntry)
async def set_watchlist_market(
    entry_id: str, req: WatchlistMarketRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    entry = _owned_entry_or_404(entry_id, user)
    if req.market == "futures" and entry.exchange not in futures_registry.SUPPORTED_FUTURES_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"{entry.exchange} has no registered futures adapter",
        )
    return watchlist_store.set_market(entry_id, req.strategy_name, req.market)


class WatchlistStrategyIntervalRequest(BaseModel):
    strategy_name: str
    interval: str | None = None  # None resets to entry default


@app.patch("/api/watchlist/{entry_id}/strategy_interval", response_model=WatchlistEntry)
async def set_watchlist_strategy_interval(
    entry_id: str, req: WatchlistStrategyIntervalRequest, user: User = Depends(auth.get_current_user)
) -> WatchlistEntry:
    _owned_entry_or_404(entry_id, user)
    return watchlist_store.set_strategy_interval(entry_id, req.strategy_name, req.interval)


@app.get("/api/watchlist/{entry_id}/stats")
async def get_watchlist_entry_stats(
    entry_id: str, user: User = Depends(auth.get_current_user)
) -> dict:
    _owned_entry_or_404(entry_id, user)
    return get_all_stats_for_entry(entry_id)


@app.post("/api/watchlist/{entry_id}/test-alert")
async def test_watchlist_alert(
    entry_id: str,
    body: dict,
    user: User = Depends(auth.get_current_user),
) -> dict:
    """Fetch current candles, run the strategy, send the most recent signal as a test."""
    strategy_name = body.get("strategy_name", "")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")
    entry = _owned_entry_or_404(entry_id, user)
    if not user.telegram_chat_id:
        raise HTTPException(status_code=400, detail="No Telegram chat_id configured")
    provider = get_provider(entry.asset_type, entry.exchange)
    interval = entry.strategy_intervals.get(strategy_name, entry.interval)
    try:
        df = await fetch_ohlcv_canonical(provider, entry.symbol, interval, settings.candles_per_timeframe)
        if entry.on_close:
            df = drop_unclosed_candle(df, interval)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail="No data available")
    params = entry.strategy_params.get(strategy_name) or None
    strategy = build_strategy_state(strategy_name, df, params=params)
    if not strategy.signals:
        return {"sent": False, "signal": None}
    signal = strategy.signals[-1]
    msg = f"🧪 TEST — {format_signal_message(entry.symbol, interval, strategy_name, signal)}"
    # Atteso, non lanciato e dimenticato: e' il pulsante che serve proprio a
    # sapere se l'invio funziona, quindi deve riportare l'esito vero — non
    # "sent: true" a prescindere da come va la chiamata a Telegram.
    ok = await telegram.send_to(user.telegram_chat_id, msg)
    return {"sent": ok, "signal": signal.type}


# ── Alert log ──────────────────────────────────────────────────────────────────

@app.get("/api/alerts/log")
async def get_alert_log(
    limit: int = 100,
    since: int = 0,
    user: User = Depends(auth.get_current_user),
) -> list:
    return alert_log.list_entries(user.id, limit=limit, since=since)


@app.delete("/api/alerts/log")
async def clear_alert_log(user: User = Depends(auth.get_current_user)) -> dict:
    alert_log.clear(user.id)
    return {"ok": True}


# ── Price alerts ───────────────────────────────────────────────────────────────

class PriceAlertCreateRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    condition: PriceAlertCondition
    price: float
    note: str = ""
    once_only: bool = True


@app.get("/api/price-alerts", response_model=list[PriceAlert])
async def list_price_alerts(user: User = Depends(auth.get_current_user)) -> list[PriceAlert]:
    return price_alerts_store.list_alerts(user.id)


@app.post("/api/price-alerts", response_model=PriceAlert)
async def create_price_alert(
    req: PriceAlertCreateRequest, user: User = Depends(auth.get_current_user)
) -> PriceAlert:
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    return price_alerts_store.add_alert(
        user.id, symbol, req.asset_type, req.exchange,
        req.condition, req.price, req.note, req.once_only,
    )


@app.delete("/api/price-alerts/{alert_id}")
async def delete_price_alert(alert_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    a = price_alerts_store.get_alert(alert_id)
    if a is None or a.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    price_alerts_store.remove_alert(alert_id)
    return {"ok": True}


class PriceAlertActiveRequest(BaseModel):
    active: bool


@app.patch("/api/price-alerts/{alert_id}/active", response_model=PriceAlert)
async def set_price_alert_active(
    alert_id: str, req: PriceAlertActiveRequest, user: User = Depends(auth.get_current_user)
) -> PriceAlert:
    a = price_alerts_store.get_alert(alert_id)
    if a is None or a.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    return price_alerts_store.set_active(alert_id, req.active)


def _owned_favorite_or_404(entry_id: str, user: User) -> FavoriteEntry:
    entry = favorites_store.get_entry(entry_id)
    if entry is None or entry.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


@app.get("/api/favorites", response_model=list[FavoriteEntry])
async def get_favorites(user: User = Depends(auth.get_current_user)) -> list[FavoriteEntry]:
    return favorites_store.list_entries(owner_id=user.id)


class FavoriteAddRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"


@app.post("/api/favorites", response_model=FavoriteEntry)
async def add_favorite_entry(
    req: FavoriteAddRequest, user: User = Depends(auth.get_current_user)
) -> FavoriteEntry:
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")

    provider = get_provider(req.asset_type, req.exchange)
    try:
        probe = await provider.fetch_ohlcv(symbol, "1d", 2)
    except ProviderError as exc:
        raise HTTPException(
            status_code=400, detail=f"Ticker not found on this exchange: {exc}"
        ) from exc
    if probe.empty:
        raise HTTPException(status_code=400, detail="Ticker not found on this exchange.")

    return favorites_store.add_entry(user.id, symbol, req.asset_type, exchange=req.exchange)


@app.delete("/api/favorites/{entry_id}")
async def delete_favorite_entry(entry_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    _owned_favorite_or_404(entry_id, user)
    favorites_store.remove_entry(entry_id)
    return {"ok": True}


class FavoriteQuote(BaseModel):
    id: str
    price: float | None = None
    change_pct: float | None = None


@app.get("/api/favorites/quotes", response_model=list[FavoriteQuote])
async def get_favorite_quotes(user: User = Depends(auth.get_current_user)) -> list[FavoriteQuote]:
    """Last price + day-over-day change for every favorite, fetched
    concurrently. A quote that fails to resolve (delisted ticker, provider
    hiccup) comes back with null price/change rather than failing the
    whole batch — the frontend just shows a dash for that row."""
    entries = favorites_store.list_entries(owner_id=user.id)

    async def quote_for(entry: FavoriteEntry) -> FavoriteQuote:
        provider = get_provider(entry.asset_type, entry.exchange)

        try:
            df = await _fetch_cached(
                (f"favquote:{entry.asset_type}:{entry.exchange}:{entry.symbol}", "1d", 2),
                provider, entry.symbol, "1d", 2,
            )
        except ProviderError:
            return FavoriteQuote(id=entry.id)
        if df.empty:
            return FavoriteQuote(id=entry.id)
        closes = df["close"].tolist()
        price = closes[-1]
        change_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 and closes[-2] else None
        return FavoriteQuote(id=entry.id, price=price, change_pct=change_pct)

    return list(await asyncio.gather(*(quote_for(e) for e in entries)))


@app.get("/api/analyze", response_model=AnalysisResponse)
async def analyze(
    # 80, non 20: un pair DEX e' "BASE@chain:pool_address", e un indirizzo
    # Solana da solo arriva a 44 caratteri base58 — lo stesso limite vale
    # per ogni altro endpoint GET che accetta un symbol qui sotto.
    symbol: str = Query(..., min_length=1, max_length=80),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    htf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["htf"]),
    mtf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["mtf"]),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    strategy_params: StrategyParamsQuery = Depends(),
    backtest_params: BacktestParamsQuery = Depends(),
) -> AnalysisResponse:
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")

    strategy_params_dict = strategy_params.model_dump()
    backtest_params_dict = backtest_params.model_dump()

    provider = get_provider(asset_type, exchange)
    intervals: dict[TimeframeKey, str] = {"htf": htf_interval, "mtf": mtf_interval, "ltf": ltf_interval}

    async def fetch_timeframe(tf_key: TimeframeKey):
        interval = intervals[tf_key]
        outputsize = settings.ltf_candles if tf_key == "ltf" else settings.candles_per_timeframe
        cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}", interval, outputsize)

        try:
            df = await _fetch_cached(cache_key, provider, symbol, interval, outputsize)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return tf_key, df

    fetched = await asyncio.gather(*(fetch_timeframe(tf_key) for tf_key in settings.timeframe_keys))

    timeframes: dict[str, TimeframeAnalysis] = {}
    strategies: dict[str, list[StrategyState]] = {}
    backtests: dict[str, object] = {}
    for tf_key, df in fetched:
        if df.empty:
            raise HTTPException(status_code=502, detail=f"No data for {symbol} on timeframe {tf_key}")

        swings = compute_swings(df, tf_key)
        levels = compute_levels(df, swings)
        indicators = compute_indicators(df)
        bias = determine_bias(levels)
        dealing_range = compute_dealing_range(df, swings)
        fib_zones = compute_fib_zones(df, tf_key, intervals[tf_key])
        # Sul DEX si compra e si vende il token, niente short: gli stessi
        # segnali spot-only che usa l'alert watcher, cosi' il grafico non
        # mostra trade diversi dal backtest on-chain della classifica.
        sig = None
        if asset_type == "dex" and strategy_name in STRATEGY_REGISTRY:
            sig = rug_stop(df, spot_only(STRATEGY_REGISTRY[strategy_name](df, strategy_params_dict)))
        strategy = build_strategy_state(strategy_name, df, strategy_params_dict, signals=sig)
        strategies[tf_key] = [strategy]
        # The frontend only ever charts the ltf timeframe (state.activeTimeframe
        # is a hardcoded "ltf", never reassigned) — htf/mtf backtests are never
        # read (only strategies[htf|mtf] is, for cross-timeframe signal markers).
        # Running the bar-by-bar backtest simulation twice for data nothing
        # displays was pure wasted latency on every single analyze call.
        if tf_key == "ltf":
            # This is the fast pass, on whatever the chart already fetched —
            # instant, but only as deep as ltf_candles. The frontend follows
            # up with a separate /api/backtest/deep call (see that endpoint)
            # that swaps this out for a backtest over each exchange's full
            # history once that slower, paginated fetch resolves — without
            # that split, every timeframe change would block on it too.
            result = run_backtest(
                df,
                strategy_params_dict,
                backtest_params_dict,
                strategy_name=strategy_name,
                signals=sig,
            )
            backtests[tf_key] = result.model_copy(update={"interval": intervals[tf_key]})
        else:
            backtests[tf_key] = BacktestResult(
                interval=intervals[tf_key],
                initial_capital=backtest_params_dict.get("initial_capital", 10000.0),
                final_equity=backtest_params_dict.get("initial_capital", 10000.0),
                total_return_pct=0.0,
                num_trades=0,
                win_rate_pct=None,
                max_drawdown_pct=0.0,
                profit_factor=None,
                equity_curve=[],
                trades=[],
            )
        candles = [
            Candle(
                time=int(ts.timestamp()),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            )
            for ts, row in df.iterrows()
        ]
        # Same story for the raw candle/indicator time series: the chart only
        # ever plots ltf, and build_summary's per-timeframe indicator readout
        # (below) only reads each series' *last* value. htf/mtf keep full
        # levels/dealing_range/fib_zones (compute_confluence and the summary
        # both need those) but only their tail candle/indicator points, not
        # the full 500-bar history — that history was ~2/3 of this endpoint's
        # response size for two timeframes nothing ever renders.
        if tf_key != "ltf":
            # Keep enough bars for frontend MTF trend computation (needs ~10+)
            # while still trimming the bulk of the history to save bandwidth.
            candles = candles[-50:]
            indicators = [ind.model_copy(update={"values": ind.values[-50:]}) for ind in indicators]
        timeframes[tf_key] = TimeframeAnalysis(
            interval=intervals[tf_key],
            candles=candles,
            levels=levels,
            indicators=indicators,
            bias=bias,
            dealing_range=dealing_range,
            fib_zones=fib_zones,
        )

    confluence_zones = compute_confluence(timeframes)
    current_price = timeframes["ltf"].candles[-1].close
    summary = build_summary(current_price, timeframes, confluence_zones)

    return AnalysisResponse(
        symbol=canonical_symbol(symbol, asset_type, exchange),
        asset_type=asset_type,
        current_price=current_price,
        timeframes=timeframes,
        confluence_zones=confluence_zones,
        summary=summary,
        strategies=strategies,
        backtests=backtests,
    )


def _slice_to_period(df, symbol: str, start_date: str | None, end_date: str | None):
    """Ritaglia la serie al periodo chiesto, PRIMA che venga usata.

    Sta qui, condiviso, perche' il periodo del Lab deve valere sia per il
    backtest sia per l'ottimizzazione: applicarlo a uno solo dei due significa
    ottimizzare su anni di storia e poi mostrare il risultato accanto a un
    backtest fatto su tre mesi, come se fossero la stessa misura
    (segnalazione 6ba09078).
    """
    if not (start_date or end_date):
        return df
    # Ogni provider fa gia' df.set_index("datetime") prima di tornare —
    # il ritaglio e' sull'indice, non su una colonna "datetime" che qui
    # non esiste piu'.
    if start_date:
        df = df[df.index >= pd.Timestamp(start_date, tz="UTC")]
    if end_date:
        df = df[df.index < pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)]
    if df.empty:
        raise HTTPException(
            status_code=400,
            detail=f"No candles for {symbol} in the requested period — try a wider range.",
        )
    return df


@app.get("/api/backtest/deep", response_model=BacktestResult)
async def deep_backtest(
    symbol: str = Query(..., min_length=1, max_length=80),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    strategy_params: StrategyParamsQuery = Depends(),
    backtest_params: BacktestParamsQuery = Depends(),
    start_date: str | None = Query(None, description="YYYY-MM-DD, inclusive — trims the series before the backtest"),
    end_date: str | None = Query(None, description="YYYY-MM-DD, inclusive — trims the series before the backtest"),
) -> BacktestResult:
    """The backtest's own, deliberately coarser fetch (see
    backtest_interval_for) instead of the ltf df the chart uses — reaches
    each exchange's actual listing date instead of stopping wherever
    ltf_candles does. Split out of /api/analyze on purpose: this can take
    several seconds on a cold cache (a multi-page fetch), and gating the
    whole chart response on it made every timeframe switch pay that cost
    too. The frontend calls this right after /api/analyze already rendered
    the chart with the fast pass, and swaps the backtest panel over once
    this resolves."""
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")

    provider = get_provider(asset_type, exchange)
    backtest_interval = backtest_interval_for(ltf_interval)
    # When the user gives a start date, fetch from that date to now — no count
    # cap. Otherwise fall back to the cached recent-N path.
    if start_date:
        since_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
        cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest:since", backtest_interval, since_ms)
        try:
            df = await _fetch_cached_since(cache_key, provider, symbol, backtest_interval, since_ms)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest", backtest_interval, settings.backtest_candles)
        try:
            df = await _fetch_cached(cache_key, provider, symbol, backtest_interval, settings.backtest_candles)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"No data for {symbol}")

    df = _slice_to_period(df, symbol, start_date, end_date)

    # Sul DEX si compra e si vende il token, niente short (vedi /api/analyze).
    sig = None
    if asset_type == "dex" and strategy_name in STRATEGY_REGISTRY:
        sig = rug_stop(df, spot_only(STRATEGY_REGISTRY[strategy_name](df, strategy_params.model_dump())))
    result = run_backtest(
        df,
        strategy_params.model_dump(),
        backtest_params.model_dump(),
        strategy_name=strategy_name,
        signals=sig,
    )
    return result.model_copy(update={"interval": backtest_interval})


class OptimizeCandidate(BaseModel):
    """One entry of the optimizer's ranked shortlist.

    Exposing the whole shortlist — rather than only the winner — lets the UI
    show the dispersion between the top candidates. A shortlist whose members
    disagree wildly is itself the signal that the winner is a lucky draw
    rather than a stable optimum."""

    rank: int
    params: dict
    score: float
    total_return_pct: float
    max_drawdown_pct: float
    num_trades: int
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    out_of_sample_return_pct: float | None = None
    overfitted: bool = False


class Overfitting(BaseModel):
    """Quanto spesso, fra i candidati considerati, il migliore in campione
    finisce sotto la mediana fuori campione.

    Non e' un doppione dello Sharpe deflazionato: quello corregge un numero,
    questo giudica la selezione. Un risultato puo' superare l'uno e fallire
    l'altro, e i due casi vanno letti diversamente."""

    pbo: float | None = None
    acceptable: bool = False
    n_candidates: int = 0
    n_splits: int = 0
    note: str = ""


class RegimeInfo(BaseModel):
    """Whether the strategy had an edge to find on this series at all.

    Surfaced rather than enforced: a result produced under a failed gate is
    fitted to a series where the strategy has no measurable edge, and the
    caller needs to be able to see that instead of reading the return figure
    as if it meant the same thing."""

    passed: bool
    family: str | None = None
    reason: str = ""
    variance_ratio: float | None = None
    vr_z: float | None = None
    vr_p_one_sided: float | None = None
    hurst: float | None = None
    verdict: str = ""


class BranchInfo(BaseModel):
    """One branch of the parameter space, with the result it reached.

    Shown side by side because that comparison is the finding: on the series
    measured, branches with more free parameters scored higher in-sample and
    worse out-of-sample. A single returned number hides exactly that."""

    label: str
    dims: int
    enumerated: bool
    chosen: bool
    total_return_pct: float
    max_drawdown_pct: float
    num_trades: int
    validation_return_pct: float | None = None
    validation_trades: int | None = None
    params: dict = {}


class DeflatedSharpe(BaseModel):
    """The winner's Sharpe, corrected for how many configurations were tried.

    The expected maximum Sharpe under no skill grows with the number of
    attempts, so a figure produced by searching thousands of candidates is not
    comparable to one from a single hypothesis. `deflated` is the probability
    the result survives that correction; below 0.95 it is not distinguishable
    from the best of that many attempts on noise."""

    sharpe: float | None = None
    deflated: float | None = None
    sharpe_threshold: float | None = None
    n_trials: int = 0
    acceptable: bool = False


class DataUsed(BaseModel):
    """Su quali dati e' realmente girata l'ottimizzazione.

    Senza questi campi il risultato non e' riproducibile a mano: due feed
    diversi danno lo stesso ordine di rendimento ma trade completamente diversi.
    Mostrato sempre, anche quando il timeframe coincide con quello del grafico."""

    interval: str
    candles: int
    chart_interval: str
    differs_from_chart: bool
    note: str = ""


class OptimizeResult(BaseModel):
    params: dict
    backtest: BacktestResult
    trials_evaluated: int
    data_used: DataUsed | None = None
    out_of_sample_return_pct: float | None = None
    overfitted: bool = False
    mode: str = "random"
    top_candidates: list[OptimizeCandidate] = []
    # Populated by mode="plateau" only.
    deterministic: bool | None = None
    regime: RegimeInfo | None = None
    branches: list[BranchInfo] = []
    chosen_branch: str | None = None
    pipeline_notes: list[str] = []
    deflated_sharpe: DeflatedSharpe | None = None
    overfitting: Overfitting | None = None


async def _optimize_payload(
    strategy_name: str,
    df,
    backtest_params: "BacktestParamsQuery",
    mode: str,
    ltf_interval: str,
    backtest_interval: str,
    constraints: "Constraints",
    say=None,
    should_stop=None,
    strategy_params: dict | None = None,
) -> "OptimizeResult":
    """Costruisce la risposta dell'ottimizzazione, una volta sola.

    La usano sia la GET diretta sia il lavoro seguito in diretta: due copie di
    questa costruzione divergerebbero al primo campo aggiunto, e il campo
    mancherebbe proprio nel percorso che non si guarda mai a mano.

    `say` riceve le righe di avanzamento; senza, la ricerca gira muta come
    prima.
    """
    say = say or (lambda _m: None)
    data_used = DataUsed(
        interval=backtest_interval,
        candles=len(df),
        chart_interval=ltf_interval,
        differs_from_chart=backtest_interval != ltf_interval,
        note=(
            f"Optimized on {len(df)} {backtest_interval} candles, not on the chart's "
            f"{ltf_interval} timeframe. To reproduce this result by hand, "
            f"set the chart to {backtest_interval}."
            if backtest_interval != ltf_interval
            else f"Optimized on {len(df)} {backtest_interval} candles, "
                 f"the same timeframe as the chart."
        ),
    )

    if mode == "plateau":
        # Deterministic path: same inputs give the same answer, because the
        # cell grid is enumerated rather than sampled wherever it fits the
        # budget. Measured against random search on identical data with only
        # the seed varying, cv of the out-of-sample return went from 66.5% to
        # 0% (Gabriele) and from 225.1% to 0% (Lorenzo).
        try:
            pr = await asyncio.to_thread(
                partial(
                    run_pipeline, strategy_name, df, backtest_params.model_dump(),
                    accept=(constraints.accepts if constraints.active else None),
                    progress=say, should_stop=should_stop,
                    locked_params=strategy_params,
                )
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422 if constraints.active else 400, detail=str(exc)
            ) from exc
        rep = pr.gate.report
        return OptimizeResult(
            params=pr.params,
            backtest=pr.backtest.model_copy(update={"interval": backtest_interval}),
            trials_evaluated=pr.evaluations,
            out_of_sample_return_pct=pr.out_of_sample_return_pct,
            overfitted=pr.overfitted,
            mode=mode,
            data_used=data_used,
            deterministic=pr.deterministic,
            chosen_branch=pr.chosen_branch,
            pipeline_notes=pr.notes,
            deflated_sharpe=(
                None if pr.dsr is None else DeflatedSharpe(
                    sharpe=pr.dsr.sharpe, deflated=pr.dsr.deflated,
                    sharpe_threshold=pr.dsr.sharpe_threshold,
                    n_trials=pr.dsr.n_trials, acceptable=pr.dsr.acceptable,
                )
            ),
            overfitting=(
                None if pr.pbo is None else Overfitting(
                    pbo=pr.pbo.pbo, acceptable=pr.pbo.acceptable,
                    n_candidates=pr.pbo.n_candidates, n_splits=pr.pbo.n_splits,
                    note=pr.pbo.note,
                )
            ),
            regime=RegimeInfo(
                passed=pr.gate.passed, family=pr.gate.family, reason=pr.gate.reason,
                variance_ratio=rep.variance_ratio, vr_z=rep.vr_z,
                vr_p_one_sided=rep.vr_p_one_sided, hurst=rep.hurst,
                verdict=rep.verdict,
            ),
            branches=[
                BranchInfo(
                    label=b.label, dims=b.dims, enumerated=b.enumerated,
                    chosen=b.label == pr.chosen_branch,
                    total_return_pct=b.best.backtest.total_return_pct,
                    max_drawdown_pct=b.best.backtest.max_drawdown_pct,
                    num_trades=b.best.backtest.num_trades,
                    validation_return_pct=b.validation_return_pct,
                    validation_trades=b.validation_trades,
                    params=b.best.params,
                )
                for b in pr.branches if b.best is not None
            ],
        )

    try:
        best_params, best_bt, trials, oos_return, overfitted, top_results = await asyncio.to_thread(
            partial(
                optimize_strategy, strategy_name, df, backtest_params.model_dump(),
                mode=mode, constraints=constraints, progress=say,
                should_stop=should_stop, base_params=strategy_params,
            )
        )
    except NoCandidateMeetsConstraints as exc:
        # 422 e non 500: la richiesta e' valida, e' il risultato a non esistere
        # con quei vincoli. Il messaggio dice quante configurazioni sono state
        # escluse, cosi' si capisce se allentare di poco o di molto.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    best_bt = best_bt.model_copy(update={"interval": backtest_interval})
    return OptimizeResult(
        params=best_params,
        backtest=best_bt,
        trials_evaluated=trials,
        out_of_sample_return_pct=oos_return,
        overfitted=overfitted,
        mode=mode,
        data_used=data_used,
        top_candidates=[
            OptimizeCandidate(
                rank=c["rank"],
                params=c["params"],
                score=c["score"],
                total_return_pct=c["backtest"].total_return_pct,
                max_drawdown_pct=c["backtest"].max_drawdown_pct,
                num_trades=c["backtest"].num_trades,
                win_rate_pct=c["backtest"].win_rate_pct,
                profit_factor=c["backtest"].profit_factor,
                out_of_sample_return_pct=c["out_of_sample_return_pct"],
                overfitted=c["overfitted"],
            )
            for c in top_results
        ],
    )


@app.get("/api/export/backtest.xlsx")
async def export_backtest(
    symbol: str = Query(..., min_length=1, max_length=80),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    strategy_params: StrategyParamsQuery = Depends(),
    backtest_params: BacktestParamsQuery = Depends(),
) -> StreamingResponse:
    """Il backtest come foglio di calcolo, con i grafici dentro.

    Rieseguito qui invece di ricevere i numeri dal browser: un foglio costruito
    su quello che il client afferma sarebbe un foglio di affermazioni. Costa un
    backtest, che e' la stessa cosa che l'utente ha appena guardato.
    """
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")

    provider = get_provider(asset_type, exchange)
    backtest_interval = backtest_interval_for(ltf_interval)
    cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest",
                 backtest_interval, settings.backtest_candles)
    try:
        df = await _fetch_cached(cache_key, provider, symbol, backtest_interval,
                                 settings.backtest_candles)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"No data for {symbol}")

    sp = strategy_params.model_dump()
    bp = backtest_params.model_dump()
    bt = await asyncio.to_thread(
        partial(run_backtest, df, sp, bp, strategy_name=strategy_name)
    )
    bt = bt.model_copy(update={"interval": backtest_interval})
    blob = await asyncio.to_thread(
        partial(build_backtest_workbook, bt, symbol=symbol, strategy_name=strategy_name,
                chart_interval=ltf_interval, params=sp, backtest_params=bp)
    )
    stamp = _time.strftime("%Y%m%d-%H%M")
    name = f"backtest-{symbol.upper()}-{backtest_interval}-{stamp}.xlsx"
    return StreamingResponse(
        io.BytesIO(blob),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )



# ── Un lavoro pesante per volta, per utente ───────────────────────────────
#
# Una ricerca stabile su Lorenzo vale circa 5000 backtest e tiene occupato un
# core per minuti. La macchina ne ha due, e uno serve a rispondere alle
# richieste e a far girare il ciclo che manda gli avvisi.
#
# Senza questo limite, dieci richieste lanciate insieme — per errore, per
# impazienza, o apposta — mettono in coda dieci ricerche e il sito smette di
# rispondere. Il limite non impedisce di ottimizzare: impedisce di
# ottimizzare dieci cose contemporaneamente, che non serviva a nessuno.
_ricerche_in_corso: dict[str, float] = {}
_LIMITE_RICERCA_SECONDI = 30 * 60   # oltre, si presume che sia morta


def _inizia_lavoro_pesante(owner_id: str) -> None:
    """Alza la mano per questo utente, o spiega perche' non si puo'."""
    adesso = _time.time()
    # Le voci vecchie si scartano qui invece che con un compito periodico:
    # un processo riavviato non deve lasciare nessuno bloccato per sempre.
    for chiave, quando in list(_ricerche_in_corso.items()):
        if adesso - quando > _LIMITE_RICERCA_SECONDI:
            _ricerche_in_corso.pop(chiave, None)

    if owner_id in _ricerche_in_corso:
        da_quanto = int(adesso - _ricerche_in_corso[owner_id])
        raise HTTPException(
            status_code=429,
            detail=(
                f"You already have a search running for {da_quanto // 60} min {da_quanto % 60} s. "
                "Wait for it to finish: two searches at once aren't faster, "
                "they just steal CPU from each other."
            ),
        )
    _ricerche_in_corso[owner_id] = adesso


def _finisci_lavoro_pesante(owner_id: str) -> None:
    _ricerche_in_corso.pop(owner_id, None)


@app.get("/api/optimize", response_model=OptimizeResult)
async def optimize_strategy_endpoint(
    symbol: str = Query(..., min_length=1, max_length=80),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    strategy_params: StrategyParamsQuery = Depends(),
    backtest_params: BacktestParamsQuery = Depends(),
    mode: str = Query("random", pattern="^(random|grid|plateau)$"),
    # Filtri opzionali sul risultato. `None` = nessun vincolo, ed e' distinto
    # da 0, che e' il vincolo piu' stringente ("nessun trade in perdita",
    # "drawdown nullo") e va potuto chiedere.
    min_losing_trades: int | None = Query(None, ge=0, le=1000),
    max_losing_trades: int | None = Query(None, ge=0, le=1000),
    max_drawdown_pct: float | None = Query(None, ge=0.0, le=100.0),
    start_date: str | None = Query(None, description="YYYY-MM-DD, inclusive — same period as the backtest"),
    end_date: str | None = Query(None, description="YYYY-MM-DD, inclusive — same period as the backtest"),
    user: User = Depends(auth.get_current_user),
) -> OptimizeResult:
    """Random-search parameter tuning for any strategy in the registry —
    same deep-history df the backtest itself uses (see
    backtest_interval_for), so results here match what re-running the
    backtest with the winning params will show. CPU-bound (~120 backtest
    simulations, ~15-25s), so it runs off the event loop via a thread —
    otherwise it would stall every other request (including other users')
    for its whole duration."""
    _inizia_lavoro_pesante(user.id)
    try:
        return await _optimize_body(
            symbol, asset_type, exchange, ltf_interval, strategy_name,
            backtest_params, mode, min_losing_trades, max_losing_trades, max_drawdown_pct,
            start_date, end_date, strategy_params.model_dump(),
        )
    finally:
        _finisci_lavoro_pesante(user.id)


async def _optimize_body(
    symbol: str,
    asset_type: AssetType,
    exchange: CryptoExchange,
    ltf_interval: IntervalChoice,
    strategy_name: str,
    backtest_params: "BacktestParamsQuery",
    mode: str,
    min_losing_trades: int | None,
    max_losing_trades: int | None,
    max_drawdown_pct: float | None,
    start_date: str | None = None,
    end_date: str | None = None,
    strategy_params: dict | None = None,
) -> OptimizeResult:
    """Il lavoro vero. Sta a parte perche' il contatore delle ricerche in
    corso va riabbassato anche quando qualcosa esplode a meta', e un
    `finally` avvolto attorno a duecento righe non si legge."""
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    if not is_optimizable(strategy_name):
        raise HTTPException(status_code=400, detail=f"Optimization not available for {strategy_name}")

    provider = get_provider(asset_type, exchange)
    backtest_interval = backtest_interval_for(ltf_interval)
    if start_date:
        since_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
        cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest:since", backtest_interval, since_ms)
        try:
            df = await _fetch_cached_since(cache_key, provider, symbol, backtest_interval, since_ms)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest", backtest_interval, settings.backtest_candles)
        try:
            df = await _fetch_cached(cache_key, provider, symbol, backtest_interval, settings.backtest_candles)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"No data for {symbol}")
    df = _slice_to_period(df, symbol, start_date, end_date)

    constraints = Constraints(
        min_losing_trades=min_losing_trades,
        max_losing_trades=max_losing_trades,
        max_drawdown_pct=max_drawdown_pct,
    )
    return await _optimize_payload(
        strategy_name, df, backtest_params, mode, ltf_interval,
        backtest_interval, constraints, strategy_params=strategy_params,
    )


# --- Ottimizzazione seguita in diretta ---------------------------------------
# Una ricerca stabile puo' durare minuti, e tenerla dentro una GET significa
# tenere aperta quella richiesta per tutto il tempo: il proxy davanti all'app la
# chiude, il browser la lascia cadere, e quello che torna non e' piu' JSON ma una
# pagina d'errore. Safari prova a leggerla come JSON e risponde "The string did
# not match the expected pattern" — il messaggio finito sulla bacheca, che non
# nomina l'ottimizzazione perche' non e' l'ottimizzazione ad aver fallito.
#
# Qui la richiesta apre un lavoro e torna subito; il lavoro prosegue per conto
# suo e accumula righe di log che il client chiede quando vuole.


def _archive_optimizer_result(owner_id: str, symbol: str, asset_type: str,
                              exchange: str, ltf_interval: str, strategy_name: str,
                              mode: str, body: "OptimizeResult") -> dict:
    """Mette il risultato nell'archivio, gia' completo di numeri.

    Le metriche non vengono ricalcolate: sono quelle del backtest che la ricerca
    ha appena eseguito, quindi rieseguirlo significherebbe rifare lo stesso
    lavoro e rischiare un numero diverso per via del feed che nel frattempo si
    e' allungato.
    """
    bt = body.backtest
    entry = archive_store.create(
        owner_id=owner_id, symbol=symbol, asset_type=asset_type, exchange=exchange,
        interval=ltf_interval, strategy_name=strategy_name, params=body.params,
        backtest_params={}, source="ottimizzazione",
        note=f"{_MODE_LABEL_EN.get(mode, mode)} search",
        optimizer={
            "mode": mode,
            "evaluations": body.trials_evaluated,
            "out_of_sample_return_pct": body.out_of_sample_return_pct,
            "overfitted": body.overfitted,
            "deterministic": body.deterministic,
            "chosen_branch": body.chosen_branch,
            "regime_passed": None if body.regime is None else body.regime.passed,
            "regime_reason": "" if body.regime is None else body.regime.reason,
            "deflated_sharpe": None if body.deflated_sharpe is None else body.deflated_sharpe.deflated,
            "pbo": None if body.overfitting is None else body.overfitting.pbo,
        },
    )
    archive_store.attach_result(entry["id"], {
        "interval": bt.interval,
        "candles": None if body.data_used is None else body.data_used.candles,
        "total_return_pct": bt.total_return_pct,
        "num_trades": bt.num_trades,
        "win_rate_pct": bt.win_rate_pct,
        "max_drawdown_pct": bt.max_drawdown_pct,
        "profit_factor": bt.profit_factor,
        "sharpe_ratio": bt.sharpe_ratio,
        "initial_capital": bt.initial_capital,
        "final_equity": bt.final_equity,
        "data_start": bt.data_start,
        "data_end": bt.data_end,
    })
    return entry


_MODE_LABEL = {"random": "rapida", "grid": "griglia", "plateau": "stabile"}
_MODE_LABEL_EN = {"random": "Quick", "grid": "Grid", "plateau": "Stable"}


def _optimizer_summary(symbol: str, strategy_name: str, mode: str,
                       body: "OptimizeResult") -> str:
    """Il riepilogo che arriva a ricerca finita.

    Porta anche i verdetti, non solo il rendimento: un +646% con il gate non
    superato e una stabilita' dello 0% e' un numero da non usare, e mandare
    solo il primo dei tre lo farebbe sembrare un risultato.
    """
    bt = body.backtest
    lines = [
        f"🔬 <b>Ottimizzazione conclusa</b> — {symbol.upper()}",
        f"{strategy_name} · ricerca {_MODE_LABEL.get(mode, mode)} · {body.trials_evaluated} backtest",
        "",
        f"In campione: <b>{bt.total_return_pct:+.1f}%</b> su {bt.num_trades} trade "
        f"(drawdown {bt.max_drawdown_pct:.1f}%)",
    ]
    if body.out_of_sample_return_pct is not None:
        lines.append(f"Fuori campione: <b>{body.out_of_sample_return_pct:+.1f}%</b>"
                     + (" ⚠️ sovradattato" if body.overfitted else ""))
    if body.regime is not None and not body.regime.passed:
        lines.append("⚠️ Gate di regime non superato: su questa serie la strategia "
                     "non ha un edge misurabile, il risultato è adattamento al rumore.")
    if body.deflated_sharpe is not None and body.deflated_sharpe.deflated is not None \
            and not body.deflated_sharpe.acceptable:
        lines.append(f"⚠️ Affidabilità {body.deflated_sharpe.deflated * 100:.0f}% "
                     f"dopo correzione per {body.deflated_sharpe.n_trials} tentativi.")
    if body.overfitting is not None and body.overfitting.pbo is not None \
            and not body.overfitting.acceptable:
        lines.append(f"⚠️ Sovra-ottimizzazione {body.overfitting.pbo * 100:.0f}%.")
    if body.deterministic is False:
        lines.append("ℹ️ Parte della griglia è stata campionata: quel pezzo non è riproducibile.")
    lines.append("")
    lines.append("Salvata nell'archivio strategie, scheda Watchlist.")
    return "\n".join(lines)


@app.post("/api/optimize/start")
async def start_optimize(
    symbol: str = Query(..., min_length=1, max_length=80),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    strategy_params: StrategyParamsQuery = Depends(),
    backtest_params: BacktestParamsQuery = Depends(),
    mode: str = Query("random", pattern="^(random|grid|plateau)$"),
    min_losing_trades: int | None = Query(None, ge=0, le=1000),
    max_losing_trades: int | None = Query(None, ge=0, le=1000),
    max_drawdown_pct: float | None = Query(None, ge=0.0, le=100.0),
    start_date: str | None = Query(None, description="YYYY-MM-DD, inclusive — same period as the backtest"),
    end_date: str | None = Query(None, description="YYYY-MM-DD, inclusive — same period as the backtest"),
    user: User | None = Depends(auth.get_current_user_optional),
) -> dict:
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    if not is_optimizable(strategy_name):
        raise HTTPException(status_code=400, detail=f"Optimization not available for {strategy_name}")

    owner_id = user.id if user else ""
    job_id = jobs_store.create(owner_id, "optimize", f"{symbol.upper()} · {strategy_name} · {mode}")

    constraints = Constraints(
        min_losing_trades=min_losing_trades,
        max_losing_trades=max_losing_trades,
        max_drawdown_pct=max_drawdown_pct,
    )

    async def _run() -> None:
        try:
            jobs_store.log(job_id, f"downloading {symbol.upper()} data…")
            provider = get_provider(asset_type, exchange)
            backtest_interval = backtest_interval_for(ltf_interval)
            if start_date:
                _since_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
                _ck = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest:since", backtest_interval, _since_ms)
                df = await _fetch_cached_since(_ck, provider, symbol, backtest_interval, _since_ms)
            else:
                cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest",
                             backtest_interval, settings.backtest_candles)
                df = await _fetch_cached(cache_key, provider, symbol, backtest_interval,
                                         settings.backtest_candles)
            if df.empty:
                jobs_store.fail(job_id, f"No data for {symbol}")
                return
            if start_date or end_date:
                try:
                    df = _slice_to_period(df, symbol, start_date, end_date)
                except HTTPException as exc:
                    # Qui non c'e' nessuna risposta HTTP da restituire: il job
                    # e' gia' partito e il suo unico canale e' il proprio log.
                    jobs_store.fail(job_id, exc.detail)
                    return
                jobs_store.log(job_id, f"period {start_date or 'start'} → {end_date or 'end'}")
            jobs_store.log(job_id, f"{len(df)} candele a {backtest_interval}")

            def say(message: str) -> None:
                jobs_store.log(job_id, message)

            body = await _optimize_payload(
                strategy_name, df, backtest_params, mode, ltf_interval,
                backtest_interval, constraints, say,
                should_stop=lambda: jobs_store.cancel_requested(job_id),
                strategy_params=strategy_params.model_dump(),
            )
            jobs_store.finish(job_id, body.model_dump(mode="json"))

            # Il lavoro e' finito anche se nessuno stava guardando: la ricerca
            # gira sul server, quindi chiudere Safari o perdere la linea non la
            # ferma. Perche' quel risultato non si perda con la scheda, viene
            # archiviato e annunciato.
            if owner_id:
                archived = _archive_optimizer_result(owner_id, symbol, asset_type,
                                                     exchange, ltf_interval, strategy_name,
                                                     mode, body)
                jobs_store.log(job_id, f"saved to archive ({archived['id']})")
                user_now = auth.get_user(owner_id)
                if user_now and user_now.telegram_chat_id and _telegram_allowed(user_now):
                    await telegram.send_to(user_now.telegram_chat_id,
                                           _optimizer_summary(symbol, strategy_name, mode, body))
                    jobs_store.log(job_id, "summary sent to Telegram")
            jobs_store.log(job_id, "fatto")
        except SearchCancelled:
            jobs_store.log(job_id, "stopped on request")
            jobs_store.cancelled(job_id)
        except NoCandidateMeetsConstraints as exc:
            jobs_store.fail(job_id, str(exc))
        except ProviderError as exc:
            jobs_store.fail(job_id, f"Data not available: {exc}")
        except Exception as exc:  # noqa: BLE001
            # Ampio di proposito: gira staccato dalla richiesta, quindi
            # un'eccezione non intercettata qui non raggiunge nessuno e il
            # lavoro resterebbe "in corso" per sempre.
            logger.error("ottimizzazione fallita per %s: %s", symbol, exc, exc_info=True)
            jobs_store.fail(job_id, str(exc))
            if owner_id:
                user_now = auth.get_user(owner_id)
                if user_now and user_now.telegram_chat_id and _telegram_allowed(user_now):
                    await telegram.send_to(
                        user_now.telegram_chat_id,
                        f"⚠️ <b>Ottimizzazione fallita</b> — {symbol.upper()} · {strategy_name}\n{exc}",
                    )

    _fire(_run())
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, user: User | None = Depends(auth.get_current_user_optional)
) -> dict:
    """Ferma una ricerca in corso.

    Segna soltanto la richiesta: il lavoro se ne accorge al backtest successivo,
    quindi si ferma entro una frazione di secondo invece che a fase finita.
    """
    if not jobs_store.request_cancel(job_id, owner_id=user.id if user else None):
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def job_status(
    job_id: str,
    since: int = Query(0, ge=0),
    user: User | None = Depends(auth.get_current_user_optional),
) -> dict:
    job = jobs_store.get(job_id, owner_id=user.id if user else None, since=since)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return job


# --- Diario degli ordini automatici ------------------------------------------


@app.get("/api/trading/journal")
async def trading_journal(user: User = Depends(auth.get_current_user)) -> dict:
    """Cosa ha fatto davvero l'esecuzione automatica.

    Distinguere "nessun segnale" da "c'era un segnale e l'ordine non e' partito"
    e' l'intera informazione utile, e prima non era da nessuna parte.
    """
    return {"items": trading_log.list_for(user.id, limit=50)}


@app.delete("/api/trading/journal")
async def clear_trading_journal(user: User = Depends(auth.get_current_user)) -> dict:
    return {"removed": trading_log.clear(user.id)}


# --- Archivio strategie ------------------------------------------------------
# Attivare un alert e' il momento in cui una configurazione smette di essere un
# esperimento e comincia a lavorare per settimane. Senza una fotografia presa
# in quel momento non resta traccia ne' di com'era fatta ne' di cosa prometteva:
# il risultato dell'ottimizzazione vive quanto la scheda del browser.


class ArchiveIn(BaseModel):
    symbol: str
    asset_type: AssetType = "crypto"
    exchange: CryptoExchange = "mexc"
    ltf_interval: IntervalChoice = DEFAULT_INTERVALS["ltf"]
    strategy_name: str
    params: dict = {}
    backtest_params: dict = {}
    note: str = ""


class ArchiveNoteIn(BaseModel):
    note: str = ""


async def _run_archive_backtest(entry_id: str, symbol: str, asset_type: str,
                                exchange: str, ltf_interval: str,
                                strategy_name: str, params: dict,
                                backtest_params: dict) -> None:
    """Riempie la voce d'archivio con le metriche misurate dal server.

    I numeri non arrivano mai dal client: un archivio che accetta le metriche
    che gli vengono passate archivia affermazioni, non misure. Gira in
    background perche' l'attivazione di un alert non deve aspettare un
    backtest, e un fallimento resta scritto sulla voce invece di cancellarla.
    """
    try:
        provider = get_provider(asset_type, exchange)
        backtest_interval = backtest_interval_for(ltf_interval)
        cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest",
                     backtest_interval, settings.backtest_candles)
        df = await _fetch_cached(cache_key, provider, symbol, backtest_interval,
                                 settings.backtest_candles)
        if df.empty:
            archive_store.attach_error(entry_id, f"No data for {symbol}")
            return
        bt = await asyncio.to_thread(
            partial(run_backtest, df, params, backtest_params, strategy_name=strategy_name)
        )
        archive_store.attach_result(entry_id, {
            "interval": backtest_interval,
            "candles": len(df),
            "total_return_pct": bt.total_return_pct,
            "num_trades": bt.num_trades,
            "win_rate_pct": bt.win_rate_pct,
            "max_drawdown_pct": bt.max_drawdown_pct,
            "profit_factor": bt.profit_factor,
            "sharpe_ratio": bt.sharpe_ratio,
            "initial_capital": bt.initial_capital,
            "final_equity": bt.final_equity,
            "data_start": bt.data_start,
            "data_end": bt.data_end,
        })
    except Exception as exc:  # noqa: BLE001
        # Volutamente ampio: gira come task in background, quindi un'eccezione
        # non intercettata qui non raggiunge nessuno e la voce resterebbe "in
        # corso" per sempre — che e' il modo peggiore di fallire, perche' sembra
        # ancora in lavorazione.
        logger.warning("archivio: backtest fallito per %s (%s)", symbol, exc)
        archive_store.attach_error(entry_id, str(exc))


@app.get("/api/archive")
async def list_archive(user: User = Depends(auth.get_current_user)) -> dict:
    return {"items": archive_store.list_for(user.id)}


@app.post("/api/archive")
async def add_archive_entry(
    req: ArchiveIn, user: User = Depends(auth.get_current_user)
) -> dict:
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    entry = archive_store.create(
        owner_id=user.id, symbol=symbol, asset_type=req.asset_type,
        exchange=req.exchange, interval=req.ltf_interval,
        strategy_name=req.strategy_name, params=req.params,
        backtest_params=req.backtest_params, source="manuale", note=req.note,
    )
    _fire(_run_archive_backtest(
        entry["id"], symbol, req.asset_type, req.exchange, req.ltf_interval,
        req.strategy_name, req.params, req.backtest_params,
    ))
    return entry


@app.get("/api/archive/{entry_id}/export")
async def export_archive_entry(entry_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    """Un file scaricabile che porta 'ogni dettaglio' di una voce d'archivio,
    per ricrearla identica su un altro account (seed-0031, `1c2880d2`).

    Deliberatamente un file, non un link o un accesso condiviso: chi riceve
    decide se e quando importarlo, e non c'e' nessun permesso cross-account
    da disegnare o da poter sbagliare."""
    entry = archive_store.get(entry_id, user.id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    return {
        "liquidity_engine_archive_export": 1,
        "symbol": entry["symbol"], "asset_type": entry["asset_type"],
        "exchange": entry["exchange"], "interval": entry["interval"],
        "strategy_name": entry["strategy_name"], "params": entry.get("params") or {},
        "backtest_params": entry.get("backtest_params") or {},
        "metrics": entry.get("metrics"), "optimizer": entry.get("optimizer") or {},
        "note": entry.get("note", ""),
        "exported_at": int(_time.time()),
    }


class ArchiveImportIn(BaseModel):
    liquidity_engine_archive_export: int = 0
    symbol: str
    asset_type: AssetType = "crypto"
    exchange: CryptoExchange = "mexc"
    interval: IntervalChoice = DEFAULT_INTERVALS["ltf"]
    strategy_name: str
    params: dict = {}
    backtest_params: dict = {}
    metrics: dict | None = None
    optimizer: dict = {}
    note: str = ""


@app.post("/api/archive/import")
async def import_archive_entry(req: ArchiveImportIn, user: User = Depends(auth.get_current_user)) -> dict:
    """Ricrea una voce d'archivio da un file esportato da un altro account.

    Le metriche nel file non sono verificate da chi importa — sono la misura
    di chi ha esportato, su dati che nel frattempo possono essere cambiati.
    Restano nella voce (altrimenti l'importazione sarebbe inutile), ma la
    nota lo dice esplicitamente, e chi vuole un numero suo puo' sempre
    riapplicare la voce e rigirare il backtest."""
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy_name}")
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker missing")
    note = f"Imported from another account{': ' + req.note if req.note else ''} — metrics not verified here"
    entry = archive_store.create(
        owner_id=user.id, symbol=symbol, asset_type=req.asset_type,
        exchange=req.exchange, interval=req.interval,
        strategy_name=req.strategy_name, params=req.params,
        backtest_params=req.backtest_params, source="manuale", note=note[:500],
        optimizer=req.optimizer,
    )
    if req.metrics:
        archive_store.attach_result(entry["id"], req.metrics)
    else:
        archive_store.attach_error(entry["id"], "No metrics in the imported file")
    return archive_store.get(entry["id"], user.id)


class ApplyToAlertRequest(BaseModel):
    # False di default: attivare da archivio senza dirlo esplicitamente non
    # deve mettere in moto ordini reali. La Modalita' Semplice e la
    # diramazione nella watchlist lo mandano True solo dopo che l'utente ha
    # scelto esplicitamente "operazioni automatiche".
    auto_trade: bool = False
    market: Literal["spot", "futures"] = "spot"


@app.post("/api/archive/{entry_id}/apply-to-alert")
async def apply_archive_entry_to_alert(
    entry_id: str, req: ApplyToAlertRequest = ApplyToAlertRequest(), user: User = Depends(auth.get_current_user)
) -> dict:
    """Accende l'alert con ESATTAMENTE la configurazione archiviata.

    Il ponte che mancava. L'archivio conserva i parametri trovati da una
    ricerca, ma restava una fotografia: per farli lavorare bisognava riscriverli
    a mano nella scheda del ticker, e chi non lo faceva si ritrovava un alert
    acceso sui valori di default della strategia — identico d'aspetto a uno
    ottimizzato, e diverso in tutto il resto. Qui i parametri li prende dalla
    voce d'archivio, non dal client: la stessa regola per cui le metriche
    l'archivio le misura invece di accettarle.
    """
    entry = archive_store.get(entry_id, user.id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    strategy_name = entry.get("strategy_name", "")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")

    watch = watchlist_store.add_entry(
        user.id, entry["symbol"], entry["asset_type"],
        exchange=entry.get("exchange", "mexc"),
        interval=entry.get("interval", DEFAULT_INTERVALS["ltf"]),
        on_close=True,
    )
    # Prima i parametri, poi l'interruttore: accendere scatta la fotografia
    # d'archivio, e farla prima registrerebbe i default — l'archivio direbbe
    # che l'alert e' partito con una configurazione diversa da quella che sta
    # girando.
    watchlist_store.set_strategy_params(watch.id, strategy_name, entry.get("params") or {})
    watchlist_store.set_strategy_interval(watch.id, strategy_name, entry.get("interval"))
    updated = await toggle_watchlist_strategy(
        watch.id, WatchlistStrategyToggleRequest(strategy_name=strategy_name, enabled=True), user
    )
    if req.auto_trade:
        updated = watchlist_store.set_auto_trade(watch.id, strategy_name, True)
        if req.market == "futures":
            if watch.exchange not in futures_registry.SUPPORTED_FUTURES_EXCHANGES:
                raise HTTPException(
                    status_code=400,
                    detail=f"{watch.exchange} has no registered futures adapter",
                )
            updated = watchlist_store.set_market(watch.id, strategy_name, "futures")
    return {
        "ok": True,
        "watchlist_entry": updated,
        "strategy_name": strategy_name,
        "params": entry.get("params") or {},
        "interval": entry.get("interval"),
    }


@app.patch("/api/archive/{entry_id}")
async def update_archive_note(
    entry_id: str, req: ArchiveNoteIn, user: User = Depends(auth.get_current_user)
) -> dict:
    entry = archive_store.set_note(entry_id, user.id, req.note)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


@app.delete("/api/archive/{entry_id}")
async def delete_archive_entry(
    entry_id: str, user: User = Depends(auth.get_current_user)
) -> dict:
    if not archive_store.remove(entry_id, user.id):
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


# --- Segnalazioni ------------------------------------------------------------
# Lettura pubblica di proposito: la bacheca serve anche a chi non ha ancora
# fatto login, e non contiene nulla di sensibile. La scrittura resta dietro
# autenticazione, cosi' le segnalazioni portano un autore e non chiunque passi
# di qui puo' riempirle.


class IssueIn(BaseModel):
    title: str
    detail: str = ""
    priority: str = "media"


class IssueReplyIn(BaseModel):
    text: str
    # "risolto" lo decide chi ha scritto la segnalazione, non chi ha scritto la
    # correzione: se il problema si vede ancora, la voce torna aperta.
    reopen: bool = False


class IssuePatch(BaseModel):
    status: str | None = None
    priority: str | None = None
    detail: str | None = None


@app.post("/api/issues/{issue_id}/replies")
async def reply_to_issue(
    issue_id: str, req: IssueReplyIn, user: User = Depends(auth.get_current_user)
) -> dict:
    if not any(i["id"] == issue_id for i in issues_store.list_issues()):
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        return issues_store.add_reply(issue_id, req.text, author=user.email,
                                      reopen=req.reopen)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/deepseek/studies")
async def list_deepseek_studies() -> list[dict]:
    """Deep studies Claude asked DeepSeek through the repo: read-only, public
    so a session without a login can read the answers."""
    return deepseek_studies.listing()


@app.get("/api/deepseek/studies/{study_id}")
async def get_deepseek_study(study_id: str) -> dict:
    rec = deepseek_studies.get(study_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Study not found")
    return rec


@app.get("/api/issues")
async def list_issues() -> dict:
    items = issues_store.list_issues()
    return {
        "items": items,
        "counts": {
            "aperto": sum(1 for i in items if i.get("status") == "aperto"),
            "in corso": sum(1 for i in items if i.get("status") == "in corso"),
            "risolto": sum(1 for i in items if i.get("status") == "risolto"),
            "riaperto": sum(1 for i in items if i.get("status") == "riaperto"),
        },
    }


@app.post("/api/issues")
async def create_issue(body: IssueIn, user: User = Depends(auth.get_current_user)) -> dict:
    try:
        return issues_store.add_issue(
            title=body.title, detail=body.detail,
            priority=body.priority, author=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/issues/{issue_id}")
async def patch_issue(issue_id: str, body: IssuePatch,
                      user: User = Depends(auth.get_current_user)) -> dict:
    try:
        updated = issues_store.update_issue(
            issue_id, status=body.status, priority=body.priority, detail=body.detail
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return updated


@app.delete("/api/issues/{issue_id}")
async def remove_issue(issue_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    if not issues_store.delete_issue(issue_id):
        raise HTTPException(status_code=404, detail="Report not found")
    return {"deleted": issue_id}


@app.get("/api/sniper/candidates")
async def sniper_candidates(
    chain: str = Query("", description="Filter by chain_id"),
    min_score: int = Query(0, ge=0, description="Minimum score_total"),
    min_liq: float = Query(0.0, ge=0, description="Minimum liquidity_usd"),
    max_mc: float = Query(0.0, ge=0, description="Maximum market_cap (0 = no limit)"),
    exclude_cex: bool = Query(False, description="Exclude CEX-listed tokens"),
    serious_only: bool = Query(True, description="Serious projects only: excludes meme coins"),
    limit: int = Query(0, ge=0, description="Max results (0 = all)"),
) -> dict:
    from app.sniper import cache as sniper_cache
    from app.sniper.quality import is_serious
    candidates = sniper_cache.get_candidates()
    quality: dict[str, str] = {}
    if serious_only:
        kept = []
        for c in candidates:
            ok, why = is_serious(
                symbol=c.symbol,
                name=c.name,
                liquidity_usd=c.liquidity_usd,
                market_cap=c.market_cap,
                volume_h24=c.volume_h24,
                age_hours=c.age_hours,
                website=c.website,
                cex_listed=c.cex_listed,
            )
            if ok:
                quality[c.token_address] = why
                kept.append(c)
        candidates = kept
    if chain:
        candidates = [c for c in candidates if c.chain_id == chain]
    if min_score:
        candidates = [c for c in candidates if c.score_total >= min_score]
    if min_liq:
        candidates = [c for c in candidates if c.liquidity_usd >= min_liq]
    if max_mc:
        candidates = [c for c in candidates if c.market_cap is None or c.market_cap <= max_mc]
    if exclude_cex:
        candidates = [c for c in candidates if c.cex_listed is not True]
    if limit:
        candidates = candidates[:limit]
    return {
        "candidates": [
            {
                "chain_id": c.chain_id,
                "token_address": c.token_address,
                "pair_address": c.pair_address,
                "symbol": c.symbol,
                "name": c.name,
                "dex_id": c.dex_id,
                "pair_url": c.pair_url,
                "price_usd": c.price_usd,
                "market_cap": c.market_cap,
                "liquidity_usd": c.liquidity_usd,
                "volume_h24": c.volume_h24,
                "volume_h1": c.volume_h1,
                "age_hours": round(c.age_hours, 1),
                "buy_sell_ratio": round(c.buy_sell_ratio, 2),
                "website": c.website,
                "socials": c.socials,
                "score_total": c.score_total,
                "score_max": c.score_max,
                "score_items": c.score_items,
                "first_seen": c.first_seen,
                "last_updated": c.last_updated,
                "cex_listed": c.cex_listed,
                "quality_reason": quality.get(c.token_address),
            }
            for c in candidates
        ],
        "last_scan": sniper_cache.last_scan_ts(),
        "count": len(candidates),
        "serious_only": serious_only,
    }


@app.post("/api/sniper/scan")
async def sniper_trigger_scan() -> dict:
    _require_trading()
    if not settings.sniper_enabled:
        raise HTTPException(status_code=503, detail="Sniper spento")
    from app.sniper import cache as sniper_cache
    added = await sniper_cache.run_scan()
    return {"ok": True, "added": added, "total": len(sniper_cache.get_candidates())}


@app.get("/api/sniper/log")
async def sniper_log(limit: int = Query(500, ge=1, le=5000)) -> dict:
    """Persisted scan-log history — survives a page reload / redeploy, so the
    console can show what happened before this browser session connected."""
    from app.sniper import cache as sniper_cache
    return {"events": sniper_cache.get_log(limit)}


@app.get("/api/sniper/scan-stream")
async def sniper_scan_stream() -> StreamingResponse:
    """SSE feed of scan log events. Scanning itself runs continuously in the
    background (polling_loop, every 5 min) regardless of whether anyone is
    watching — this just relays whatever scan is happening, automatic or
    manually triggered, to any open browser tab for as long as it stays
    connected.

    Since two scans can be up to 5 minutes apart, the connection can sit
    with nothing to send for a while — long enough for an intermediate
    proxy to decide it's dead and silently drop it, which left the page
    stuck showing "Connessione…" and reconnecting in a loop. A periodic
    SSE comment (ignored by EventSource, invisible to the console) keeps
    bytes flowing so the connection — and the browser's "open" state —
    survives the quiet stretches."""
    from app.sniper import cache as sniper_cache

    async def event_gen():
        q = sniper_cache.subscribe()
        try:
            yield f"data: {json.dumps({'type': 'connected', 'ts': _time.time()})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            sniper_cache.unsubscribe(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sniper/market")
async def sniper_market(
    chain: str = Query("all"),
    q: str = Query(""),
    sort: str = Query("liquidity"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Browse the market screener's store — every pool GeckoTerminal handed
    us on the last market scan, not just new ones. No score here (see
    sniper/market.py docstring for why); this is filter/sort/browse only."""
    from app.sniper import market as sniper_market_mod

    items, total = sniper_market_mod.get_tokens(
        chain=chain or None, query=q or None, sort=sort, limit=limit, offset=offset
    )
    return {
        "tokens": [
            {
                "chain_id": t.chain_id,
                "token_address": t.token_address,
                "pair_address": t.pair_address,
                "symbol": t.symbol,
                "name": t.name,
                "dex_id": t.dex_id,
                "pair_url": t.pair_url,
                "price_usd": t.price_usd,
                "market_cap": t.market_cap,
                "liquidity_usd": t.liquidity_usd,
                "volume_h24": t.volume_h24,
                "age_hours": round(t.age_hours, 1),
                "buy_sell_ratio": round(t.buy_sell_ratio, 2),
                "last_updated": t.last_updated,
            }
            for t in items
        ],
        "total": total,
        "last_scan": sniper_market_mod.last_scan_ts(),
    }


@app.post("/api/sniper/market-scan")
async def sniper_market_trigger_scan() -> dict:
    _require_trading()
    if not settings.sniper_enabled:
        raise HTTPException(status_code=503, detail="Sniper spento")
    from app.sniper import market as sniper_market_mod

    added = await sniper_market_mod.run_market_scan()
    return {"ok": True, "added": added}


@app.post("/api/sniper/market-import")
async def sniper_market_import() -> dict:
    """Apply the sniper's primary filter + on-chain scoring to every token
    currently in the market store and add qualifying ones to the sniper
    candidate archive. Progress is streamed via /api/sniper/market-stream."""
    _require_trading()
    if not settings.sniper_enabled:
        raise HTTPException(status_code=503, detail="Sniper spento")
    from app.sniper import market as sniper_market_mod

    if sniper_market_mod.import_in_progress():
        return {"ok": False, "error": "Import already in progress"}
    added = await sniper_market_mod.run_import()
    return {"ok": True, "added": added}


@app.get("/api/sniper/market-log")
async def sniper_market_log(limit: int = 500) -> dict:
    from app.sniper import market as sniper_market_mod
    return {"events": sniper_market_mod.get_log(limit=limit)}


@app.get("/api/sniper/agents")
async def sniper_agents_status() -> dict:
    from app.sniper import agents as sniper_agents
    return {"agents": sniper_agents.get_status()}


@app.get("/api/sniper/research/auto/{token_address}")
async def sniper_research_auto(token_address: str) -> dict:
    """Auto-detect chain via DexScreener, then run research."""
    import httpx as _httpx

    chain_id = None
    async with _httpx.AsyncClient(timeout=12.0) as client:
        try:
            r = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            )
            if r.status_code == 200:
                data = r.json()
                pairs = data.get("pairs") or []
                if pairs:
                    chain_id = pairs[0].get("chainId")
        except Exception:
            pass

    if not chain_id:
        return {
            "chain_id": None,
            "token_address": token_address,
            "research_score": 0,
            "description": "Token not found on DexScreener — check the address.",
            "checks": [{"label": "Not found on DexScreener", "passed": False}],
            "links": [],
        }

    return await sniper_research(chain_id, token_address)


@app.get("/api/sniper/research/{chain_id}/{token_address}")
async def sniper_research(chain_id: str, token_address: str) -> dict:
    """Query CoinGecko + DexScreener for a token and return a research report."""
    import httpx as _httpx
    from app.sniper.cex_check import _CHAIN_TO_PLATFORM, _CEX_IDS

    checks = []
    links = []
    description = None
    research_score = 0

    platform = _CHAIN_TO_PLATFORM.get(chain_id)

    async with _httpx.AsyncClient(timeout=12.0) as client:
        # ── CoinGecko contract lookup ──
        cg_data = None
        if platform:
            try:
                r = await client.get(
                    f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{token_address}"
                )
                if r.status_code == 200:
                    cg_data = r.json()
            except Exception:
                pass

        if cg_data:
            research_score += 2
            checks.append({"label": "Found on CoinGecko", "passed": True, "note": cg_data.get("name", "")})

            description = (cg_data.get("description") or {}).get("en", "")
            if description and len(description) > 400:
                description = description[:400] + "…"

            # market data quality
            md = cg_data.get("market_data") or {}
            mc = (md.get("market_cap") or {}).get("usd")
            if mc and mc > 0:
                research_score += 1
                checks.append({"label": "On-chain market cap verified", "passed": True, "note": f"${mc:,.0f}"})

            # community / social
            comm = cg_data.get("community_data") or {}
            tw = comm.get("twitter_followers", 0) or 0
            tg = comm.get("telegram_channel_user_count", 0) or 0
            if tw > 1000:
                research_score += 1
                checks.append({"label": "Active Twitter", "passed": True, "note": f"{tw:,} followers"})
            elif tw > 0:
                checks.append({"label": "Twitter present", "warning": True, "note": f"{tw:,} followers"})
            if tg > 500:
                research_score += 1
                checks.append({"label": "Active Telegram", "passed": True, "note": f"{tg:,} members"})
            elif tg > 0:
                checks.append({"label": "Telegram present", "warning": True, "note": f"{tg:,} members"})

            # dev activity
            dev = cg_data.get("developer_data") or {}
            commits = (dev.get("commit_count_4_weeks") or 0)
            if commits > 5:
                research_score += 1
                checks.append({"label": "Recent GitHub activity", "passed": True, "note": f"{commits} commit/4wk"})
            elif commits > 0:
                checks.append({"label": "GitHub repository", "warning": True, "note": f"{commits} commit/4wk"})

            # links
            lnks = cg_data.get("links") or {}
            if lnks.get("homepage"):
                for h in (lnks["homepage"] or []):
                    if h:
                        links.append({"label": "Official website", "url": h})
                        break
            if lnks.get("twitter_screen_name"):
                links.append({"label": "Twitter", "url": f"https://twitter.com/{lnks['twitter_screen_name']}"})
            if lnks.get("telegram_channel_identifier"):
                links.append({"label": "Telegram", "url": f"https://t.me/{lnks['telegram_channel_identifier']}"})

            # CEX tickers check
            tickers = cg_data.get("tickers") or []
            cex_names = [t["market"]["name"] for t in tickers if t.get("market", {}).get("identifier", "") in _CEX_IDS]
            if cex_names:
                checks.append({"label": f"Listed on {cex_names[0]}", "warning": True, "note": "already on a CEX"})
            else:
                research_score += 1
                checks.append({"label": "Not on a CEX yet", "passed": True, "note": "early opportunity"})
        else:
            checks.append({"label": "Not found on CoinGecko", "passed": False, "note": "token not indexed"})

        # ── DexScreener pair check ──
        try:
            r2 = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            )
            if r2.status_code == 200:
                pairs = (r2.json().get("pairs") or [])
                if pairs:
                    best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                    liq = float((best.get("liquidity") or {}).get("usd", 0) or 0)
                    vol = float((best.get("volume") or {}).get("h24", 0) or 0)
                    if liq >= 10_000:
                        research_score += 1
                        checks.append({"label": "Solid DEX liquidity", "passed": True, "note": f"${liq:,.0f}"})
                    elif liq >= 1_000:
                        checks.append({"label": "Low DEX liquidity", "warning": True, "note": f"${liq:,.0f}"})
                    else:
                        checks.append({"label": "Very low liquidity", "passed": False, "note": f"${liq:,.0f}"})
                    if liq > 0 and vol / liq > 0.05:
                        research_score += 1
                        checks.append({"label": "Real volume (>5% liq)", "passed": True, "note": f"${vol:,.0f}/24h"})
                    if best.get("pairUrl"):
                        links.append({"label": "DexScreener", "url": best["pairUrl"]})
        except Exception:
            pass

    research_score = min(research_score, 10)
    return {
        "chain_id": chain_id,
        "token_address": token_address,
        "research_score": research_score,
        "description": description,
        "checks": checks,
        "links": links,
    }


@app.get("/api/sniper/market-stream")
async def sniper_market_stream() -> StreamingResponse:
    """SSE feed of market-scan progress (per-chain, since this is a heavier
    on-demand pull — up to 10 pages per chain — not the every-5-minutes
    sniper scan)."""
    from app.sniper import market as sniper_market_mod

    async def event_gen():
        q = sniper_market_mod.subscribe()
        try:
            yield f"data: {json.dumps({'type': 'connected', 'ts': _time.time()})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            sniper_market_mod.unsubscribe(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.websocket("/ws/live")
async def live_feed(websocket: WebSocket) -> None:
    params = websocket.query_params
    symbol = params.get("symbol", "").strip()
    if not symbol:
        await websocket.close(code=4000)
        return

    # Default mexc per client vecchi che non mandano ancora il parametro.
    exchange = params.get("exchange", "mexc")
    manager = _LIVE_STREAM_MANAGERS.get(exchange)
    if manager is None:
        await websocket.close(code=4000)
        return

    intervals = {
        "htf": params.get("htf_interval", DEFAULT_INTERVALS["htf"]),
        "mtf": params.get("mtf_interval", DEFAULT_INTERVALS["mtf"]),
        "ltf": params.get("ltf_interval", DEFAULT_INTERVALS["ltf"]),
    }

    await websocket.accept()
    await manager.add_client(websocket, symbol, intervals)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.remove_client(websocket)


class _VersionedStatic(StaticFiles):
    """File statici con la cache decisa dall'URL.

    Con `?v=<hash>` il contenuto non puo' cambiare senza cambiare l'URL, quindi
    si puo' tenere per sempre. Senza, si rivalida ogni volta: e' l'URL che
    qualcuno puo' aver salvato o che arriva da una pagina vecchia, e servirlo
    dalla cache significherebbe servire codice di due deploy fa.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        query = scope.get("query_string", b"").decode("latin-1")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if "v=" in query else "no-cache"
        )
        return response


app.mount("/static", _VersionedStatic(directory=str(FRONTEND_DIR / "static")), name="static")


def _compute_asset_version() -> str:
    """Hash of the actual CSS/JS bytes being served, not the git commit —
    correct even in the Docker image, which doesn't ship .git. Computed once
    at process startup: every deploy restarts the process, so the version
    (and therefore the /static/*.css?v=... URL) always changes exactly when
    the files do. Without this, browsers — mobile Safari especially — keep
    serving a stale cached style.css/app.js after a deploy since neither the
    URL nor any cache-control header ever changed.

    I moduli di static/onchain/ entrano nello stesso hash: sono serviti dagli
    stessi URL versionati (via import map, vedi _onchain_importmap) e senza di
    loro un deploy che tocca solo il wallet non cambierebbe il `?v=`."""
    try:
        parts = [
            (FRONTEND_DIR / "static" / "style.css").read_bytes(),
            (FRONTEND_DIR / "static" / "app.js").read_bytes(),
            (FRONTEND_DIR / "static" / "onchain-embed.css").read_bytes(),
            (FRONTEND_DIR / "static" / "engn-system.css").read_bytes(),
            (FRONTEND_DIR / "static" / "engn-landing.css").read_bytes(),
        ]
        parts += [m.read_bytes() for m in sorted((FRONTEND_DIR / "static" / "onchain").glob("*.js"))]
        # The app's skin (static/onchain/app.css) too: a CSS-only deploy must change the ?v=.
        parts += [m.read_bytes() for m in sorted((FRONTEND_DIR / "static" / "onchain").glob("*.css"))]
        return hashlib.sha256(b"".join(parts)).hexdigest()[:10]
    except OSError:
        return "0"


_ASSET_VERSION = _compute_asset_version()

# Pagina a se', niente app.js/style.css: e' quella che si apre scansionando
# il QR per rivelare una chiave on-chain, deve restare piccola e verificabile
# a colpo d'occhio — tutto inline, nessuna dipendenza dal resto del sito.
_ONCHAIN_APPROVE_HTML = (FRONTEND_DIR / "onchain-approve.html").read_text(encoding="utf-8")
_ONCHAIN_PASSKEY_SETUP_HTML = (FRONTEND_DIR / "onchain-passkey-setup.html").read_text(encoding="utf-8")
# I moduli ES del wallet on-chain stanno in static/onchain/ e si importano
# per nome (`@onchain/api.js`) attraverso un import map generato qui: e'
# l'unico modo senza bundler di mettere il `?v=` anche sugli import interni
# di un modulo, che il browser risolve da se' e che nessuna replace
# sull'HTML raggiunge. Senza, dopo un deploy Safari continuerebbe a servire
# i vecchi moduli. Serve sia a onchain-app.html (pagina a se') sia alla
# sotto-sezione "Wallet on-chain" fusa dentro la tab Trading di index.html.
_ONCHAIN_MODULE_DIR = FRONTEND_DIR / "static" / "onchain"


def _onchain_modules() -> list[Path]:
    try:
        return sorted(_ONCHAIN_MODULE_DIR.glob("*.js"))
    except OSError:
        return []


def _onchain_importmap(version: str) -> str:
    imports = {
        f"@onchain/{module.name}": f"/static/onchain/{module.name}?v={version}"
        for module in _onchain_modules()
    }
    return (
        '<script type="importmap">'
        + json.dumps({"imports": imports}, separators=(",", ":"))
        + "</script>"
    )


_INDEX_HTML = (
    (FRONTEND_DIR / "index.html")
    .read_text(encoding="utf-8")
    .replace('href="/static/style.css"', f'href="/static/style.css?v={_ASSET_VERSION}"')
    .replace('src="/static/app.js"', f'src="/static/app.js?v={_ASSET_VERSION}"')
    .replace(
        "</head>",
        f'<meta name="lae-asset-version" content="{_ASSET_VERSION}" />\n'
        f'<link rel="stylesheet" href="/static/onchain-embed.css?v={_ASSET_VERSION}" />\n'
        f"{_onchain_importmap(_ASSET_VERSION)}\n</head>",
    )
)


_SNIPER_HTML = (
    (FRONTEND_DIR / "sniper.html")
    .read_text(encoding="utf-8")
    .replace('href="/static/style.css"', f'href="/static/style.css?v={_ASSET_VERSION}"')
)

def _onchain_modulepreloads(version: str) -> str:
    """Un <link rel="modulepreload"> per ogni modulo del wallet.

    Senza, il browser scopre gli import di main.js solo dopo averlo scaricato
    e letto: un secondo giro di rete in fila al primo (~0,8 s misurati dal
    vivo). Con questi li chiede tutti insieme all'HTML."""
    return "\n".join(
        f'<link rel="modulepreload" href="/static/onchain/{module.name}?v={version}" />'
        for module in _onchain_modules()
    )


_ONCHAIN_APP_HTML = (
    (FRONTEND_DIR / "onchain-app.html")
    .read_text(encoding="utf-8")
    .replace("<!--onchain-importmap-->", _onchain_importmap(_ASSET_VERSION))
    .replace("<!--onchain-modulepreload-->", _onchain_modulepreloads(_ASSET_VERSION))
    .replace('src="/static/onchain/main.js"', f'src="/static/onchain/main.js?v={_ASSET_VERSION}"')
    .replace('href="/static/onchain/app.css"', f'href="/static/onchain/app.css?v={_ASSET_VERSION}"')
    .replace('href="/static/engn-system.css"', f'href="/static/engn-system.css?v={_ASSET_VERSION}"')
    .replace('href="/static/onchain/ui.css"', f'href="/static/onchain/ui.css?v={_ASSET_VERSION}"')
    .replace('href="/static/onchain/ui-refinements.css"', f'href="/static/onchain/ui-refinements.css?v={_ASSET_VERSION}"')
    .replace('href="/static/onchain/scout-cube.css"', f'href="/static/onchain/scout-cube.css?v={_ASSET_VERSION}"')
    .replace('href="/static/onchain/classic.css"', f'href="/static/onchain/classic.css?v={_ASSET_VERSION}"')
    # La libreria del grafico non cambia fra un deploy e l'altro: con il `?v=`
    # finisce in cache per sempre invece di rivalidarsi a ogni apertura.
    .replace(
        'src="/static/lightweight-charts.standalone.production.js"',
        f'src="/static/lightweight-charts.standalone.production.js?v={_ASSET_VERSION}"',
    )
)


# L'HTML non si mette in cache: e' l'unico posto dove vive il numero di
# versione degli asset. Se il browser tiene la pagina, tiene anche il vecchio
# `?v=`, e continua a chiedere il vecchio app.js — che e' esattamente cosa
# succedeva: il deploy era andato, la pagina no. Gli asset restano cacheabili
# per sempre proprio perche' l'URL cambia con il contenuto.
_HTML_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/api/version")
async def asset_version() -> dict:
    """La versione degli asset che il server sta servendo adesso.

    Serve alla pagina per accorgersi di essere vecchia. Il `no-cache` sull'HTML
    risolve il problema dal deploy in poi, ma chi aveva gia' la pagina in cache
    da prima puo' restarci per ore — ed e' successo: uno vedeva il grafico nero
    dopo che la correzione era in produzione da un pezzo.

    `deployed_at` e' quando QUESTO processo e' partito, non quando il commit
    e' stato scritto: un deploy riavvia sempre il container, quindi coincide
    con l'ultimo deploy riuscito — ed e' la risposta diretta alla domanda
    "il sito e' aggiornato o sto guardando una versione vecchia?", senza
    doverlo dedurre dal fatto che un messaggio Telegram sia arrivato o no.
    """
    return {"asset_version": _ASSET_VERSION, "deployed_at": int(_app_start)}


@app.get("/")
async def index(user: User | None = Depends(auth.get_current_user_optional)) -> HTMLResponse:
    if guest_links.is_guest(user):
        return HTMLResponse(
            _with_guest_banner(_INDEX_HTML, user),
            headers={"Cache-Control": "private, no-cache", "Vary": "Cookie"},
        )
    return HTMLResponse(_INDEX_HTML, headers=_HTML_HEADERS)


@app.get("/sniper")
async def sniper_app() -> HTMLResponse:
    _require_trading()
    return HTMLResponse(_SNIPER_HTML, headers=_HTML_HEADERS)


def _render_engn_page() -> str:
    """La presentazione pubblica: le regole mostrate sono le costanti del
    radar, non numeri copiati a mano — se una soglia cambia, cambia anche qui."""
    r = onchain_radar
    values = {
        "SCAN_MIN": f"{r.SCAN_INTERVAL_S // 60}",
        "AGE_MAX_DAYS": f"{r.AGE_MAX_DAYS}",
        "LIQ_MIN": f"{r.LIQ_MIN_USD / 1000:g}k",
        "LIQ_RATIO": f"{r.LIQ_TO_MCAP_MIN * 100:g}",
        "MCAP_MAX": f"${r.MCAP_MAX_USD / 1e6:g}M",
        "MIN_BARS": f"{r.MIN_BARS}",
        "MIN_TRADES": f"{r.MIN_TRADES}",
        "MIN_PF": f"{r.MIN_PROFIT_FACTOR:g}",
        "MAX_DD": f"{r.MAX_DRAWDOWN_PCT:g}",
        "EXC_MCAP": f"{r.EXC_MCAP_MAX_USD / 1e6:g}M",
        "EXC_LIQ": f"{r.EXC_LIQ_MIN_USD / 1000:g}k",
    }
    html = (FRONTEND_DIR / "engn.html").read_text(encoding="utf-8")
    for asset in ("engn-system.css", "engn-landing.css", "onchain/motion.js"):
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={_ASSET_VERSION}"')
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", value)
    return html


_ENGN_HTML = _render_engn_page()


def _engn_contract_box() -> str:
    """L'indirizzo compare solo dopo il lancio vero (app/token_launch.py)."""
    done = token_launch.launched()
    if not done:
        return '<span class="ca">CA <b>not launched yet</b> · only this page will publish it</span>'
    mint = _html.escape(done["mint"], quote=True)
    return (f'<a class="ca" href="https://pump.fun/coin/{mint}" rel="noopener">CA <b>{mint}</b></a>')


@app.get("/engn")
async def engn_page() -> HTMLResponse:
    return HTMLResponse(_ENGN_HTML.replace("<!--engn-ca-->", _engn_contract_box()), headers=_HTML_HEADERS)


@app.get("/onchain-approve.html")
async def onchain_approve_page() -> HTMLResponse:
    return HTMLResponse(_ONCHAIN_APPROVE_HTML, headers=_HTML_HEADERS)


@app.get("/onchain-passkey-setup.html")
async def onchain_passkey_setup_page() -> HTMLResponse:
    return HTMLResponse(_ONCHAIN_PASSKEY_SETUP_HTML, headers=_HTML_HEADERS)


def _onchain_boot_payload(user: User | None) -> dict:
    """Quello che main.js chiedeva con due richieste in fila (/api/auth/me,
    poi /api/onchain/wallets), gia' dentro l'HTML. Stessi campi degli
    endpoint: solo indirizzo e stato, mai una chiave."""
    if user is None:
        return {"user": None}
    payload: dict = {"user": _user_out(user)}
    if settings.onchain_enabled:
        payload["wallets"] = [
            {"chain": chain, "address": w.address, "enabled": w.enabled, "created_at": w.created_at}
            for chain, w in user.onchain_wallets.items()
        ]
    return payload


def _render_onchain_app(user: User | None) -> str:
    # `<` escapato: un valore con "</script>" non puo' chiudere il blocco.
    data = json.dumps(_onchain_boot_payload(user), separators=(",", ":")).replace("<", "\\u003c")
    html = _ONCHAIN_APP_HTML.replace(
        "<!--onchain-boot-->", f'<script type="application/json" id="onchain-boot">{data}</script>'
    )
    # La sezione giusta e' visibile dal primo disegno, non dopo che il JS ha
    # chiesto chi sei: prima la pagina restava vuota fino a quel momento.
    # Si cerca per id, non per l'intero tag: un attributo aggiunto al tag
    # (data-view del Pilota) bastava a far fallire il replace in silenzio.
    target = "app-shell" if user else "login-screen"
    html = re.sub(rf'(<[^>]*\bid="{target}"[^>]*?) hidden>', r"\1>", html, count=1)
    return _with_guest_banner(html, user)


@app.get("/onchain-app.html")
async def onchain_app_page(user: User | None = Depends(auth.get_current_user_optional)) -> HTMLResponse:
    # La pagina ora contiene chi sei: nessuna cache condivisa deve tenerla.
    return HTMLResponse(
        _render_onchain_app(user),
        headers={"Cache-Control": "private, no-cache", "Vary": "Cookie"},
    )

