import asyncio
import base64
import datetime as _dt
import hashlib
import io
import html as _html
import httpx
import itertools
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
from app.trading.onchain import execute as onchain_execute
from app.trading.onchain import send as onchain_send
from app.trading.onchain import jito as onchain_jito
from app.trading.onchain import keys as onchain_keys
from app.trading.onchain import legit as onchain_legit
from app.trading.onchain import ledger as onchain_ledger
from app.trading.onchain import outcomes as onchain_outcomes
from app.trading.onchain import passkey as onchain_passkey
from app.trading.onchain import pilot as onchain_pilot
from app.trading.onchain import listings as onchain_listings
from app.trading.onchain import leaderboard as onchain_leaderboard
from app.trading.onchain import radar as onchain_radar
from app import token_launch
from app.trading.onchain import prices as onchain_prices
from app.trading.onchain import token_search as onchain_tokens
from app.trading.onchain import quotes as onchain_quotes
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
                detail="Le azioni sono disattivate: questo sistema opera solo su crypto (USDT/USDC).",
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
                detail="esecuzione automatica on-chain non ancora collegata: "
                       "manca l'indirizzo del token, non solo del pool",
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
                outcome="saltato", detail=f"simbolo on-chain non riconosciuto: {exc}", price=signal.price,
            )
        return

    quote_token = _ONCHAIN_QUOTE_TOKEN.get(chain)
    if quote_token is None or chain not in owner.onchain_wallets:
        for signal in fresh:
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome="saltato", detail=f"nessun wallet on-chain configurato per la chain '{chain}'",
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
                outcome="saltato", detail=f"pilota spento su {chain}: nessun acquisto automatico",
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
                outcome="saltato", detail=f"tipo di segnale non gestito dall'auto-trade on-chain: {signal.type}",
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
                    detail="vendita saltata: decimali del token non ottenibili dal vero contratto/mint "
                           "(RPC irraggiungibile o token non riconosciuto) — mai indovinati",
                    price=signal.price,
                )
                continue
            position = onchain_ledger.get_position(owner.id, chain, entry.base_token_address)
            quantity = position["quantity"]
            if quantity <= 0:
                trading_log.record(
                    owner.id, symbol=entry.symbol, exchange=entry.exchange,
                    strategy_name=strategy_name, signal_type=signal.type,
                    outcome="saltato", detail="nessuna posizione registrata su questo token da vendere",
                    price=signal.price,
                )
                continue
            sell_amount_in = int(quantity * (10 ** decimals))
            notional_usd = quantity * signal.price

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
                        cap, quote["price_impact_pct"], signal.price, private_key_hex, quote,
                    )
                else:
                    quote = await onchain_quotes.jupiter_quote(
                        entry.base_token_address, quote_token, sell_amount_in, slippage_bps,
                    )
                    result = await onchain_execute.execute_order(
                        owner.id, "solana", entry.base_token_address, strategy_name, "sell",
                        notional_usd, quote["price_impact_pct"], signal.price, private_key_hex, quote,
                    )
            else:
                token_in, token_out = (quote_token, entry.base_token_address) if is_buy \
                    else (entry.base_token_address, quote_token)
                found = await onchain_quotes.find_pool_and_fee(token_in, token_out)
                if found is None:
                    raise RuntimeError("nessun pool Uniswap v3 trovato per questo token su nessun fee tier")
                _pool, fee = found
                amount_in = buy_amount_in if is_buy else sell_amount_in
                v3_quote = await onchain_quotes.uniswap_v3_quote(token_in, token_out, fee, amount_in)
                amount_out_minimum = int(v3_quote["amount_out"] * 0.99)  # 1% di slippage, come il default dell'endpoint manuale
                result = await onchain_execute.execute_order(
                    owner.id, "robinhood", entry.base_token_address, strategy_name, "buy" if is_buy else "sell",
                    cap if is_buy else notional_usd, v3_quote["price_impact_pct"], signal.price, private_key_hex,
                    {"token_in": token_in, "token_out": token_out, "fee": fee,
                     "amount_in": amount_in, "amount_out_minimum": amount_out_minimum},
                )
            outcome = "eseguito" if result.get("accepted") else "saltato"
            trading_log.record(
                owner.id, symbol=entry.symbol, exchange=entry.exchange,
                strategy_name=strategy_name, signal_type=signal.type,
                outcome=outcome, detail=str(result)[:400], price=signal.price,
            )
            if result.get("accepted") and owner.telegram_chat_id and _telegram_allowed(owner):
                asyncio.create_task(telegram.send_to(
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
                asyncio.create_task(telegram.send_to(
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
                        asyncio.create_task(telegram.send_to(
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
                        asyncio.create_task(telegram.send_to(owner.telegram_chat_id, msg))
                        alert_log.append_entry(
                            owner_id=owner.id,
                            symbol=pa.symbol,
                            interval=entry.interval,
                            strategy_name="Alert prezzo",
                            signal_type=pa.condition.value,
                            price=current_price,
                            label=f"Prezzo {direction} {pa.price:g}",
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
                    spot_signals = (spot_only(STRATEGY_REGISTRY[strategy_name](strat_df, params))
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
                            skip = "esecuzione automatica disattivata per questo exchange (futures)"
                        elif not (api_key and api_secret):
                            skip = "chiavi API futures mancanti o non leggibili"
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
                                        asyncio.create_task(telegram.send_to(
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
                                        asyncio.create_task(telegram.send_to(
                                            owner.telegram_chat_id,
                                            f"⚠️ Ordine FUTURES FALLITO <b>{entry.symbol}</b> [{signal.type}]: {exc}",
                                        ))
                    if cred and fresh and entry.auto_trade.get(strategy_name):
                        api_key, api_secret, passphrase = auth.get_decrypted_credential(owner, entry.exchange)
                        skip = ""
                        if not cred.enabled:
                            skip = "esecuzione automatica disattivata per questo exchange"
                        elif not (api_key and api_secret):
                            skip = "chiavi API mancanti o non leggibili"
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
                                        asyncio.create_task(telegram.send_to(
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
                                        asyncio.create_task(telegram.send_to(
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
                    asyncio.create_task(telegram.send_to(
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
                    asyncio.create_task(telegram.send_to(
                        owner.telegram_chat_id,
                        f"⏸️ <b>Crediti dati esauriti</b> ({entry.exchange if entry.asset_type == 'crypto' else 'Twelve Data'})\n"
                        f"Il monitoraggio dei titoli riprende automaticamente quando la quota si azzera. "
                        f"Le crypto non sono toccate.\n\n<i>{exc}</i>",
                    ))
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("Alert loop error for %s: %s", entry.symbol, exc, exc_info=True)
                if settings.telegram_bot_token and owner.telegram_chat_id and _telegram_allowed(owner):
                    asyncio.create_task(
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
        return {"status": "fail", "reason": "rug gia' subito", "checks": []}
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
            reason = f"liquidità ${candidate.liquidity_usd:,.0f} sotto ${_SNIPER_ONCHAIN_MIN_LIQUIDITY_USD:,.0f}"
        elif candidate.age_hours > _SNIPER_ONCHAIN_MAX_AGE_HOURS:
            reason = f"troppo vecchio ({candidate.age_hours:.0f} h, massimo {_SNIPER_ONCHAIN_MAX_AGE_HOURS:.0f})"
        elif not candidate.token_address or not candidate.pair_address:
            reason = "indirizzo del token o del pool mancante"
        elif not candidate.market_cap or candidate.market_cap > _SNIPER_ONCHAIN_MAX_MCAP_USD:
            reason = f"cap {'sconosciuta' if not candidate.market_cap else f'${candidate.market_cap:,.0f}'} oltre ${_SNIPER_ONCHAIN_MAX_MCAP_USD:,.0f}"
        elif (candidate.chain_id, candidate.token_address) not in vetted_keys:
            try:
                verdict = await _pilot_vet(candidate)
            except Exception:  # noqa: BLE001 — un filtro che non risponde non fa passare niente
                logger.exception("sniper_onchain_bridge: filtro fallito su %s", candidate.symbol)
                verdict = {"status": "wait", "reason": "controllo non riuscito"}
            if verdict["status"] != "ok":
                reason = verdict["reason"] or "non supera il filtro"
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
            asyncio.create_task(telegram.send_to(
                owner.telegram_chat_id,
                "⚠️ <b>Watchdog</b>: il loop degli alert è silenzioso da più di "
                f"{int(_WATCHDOG_STALE_S // 60)} minuti. Verificare lo stato del server.",
            ))


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_notify_deploy())
    from app.trading.onchain import twap_jobs as onchain_twap_jobs_resume
    asyncio.create_task(onchain_twap_jobs_resume.resume_running_twap_jobs())
    t1 = asyncio.create_task(_watchlist_alert_loop())
    t6 = asyncio.create_task(limit_order_loop())
    t7 = asyncio.create_task(_sniper_onchain_bridge_loop())
    t4 = asyncio.create_task(_weekly_recap_loop())
    t5 = asyncio.create_task(_watchdog_loop())
    radar_tasks = [asyncio.create_task(onchain_radar.radar_loop()),
                   asyncio.create_task(onchain_listings.listings_loop()),
                   asyncio.create_task(onchain_radar.listings_backtest_loop()),
                   asyncio.create_task(onchain_leaderboard.leaderboard_loop()),
                   asyncio.create_task(onchain_outcomes.outcomes_loop())] if settings.onchain_radar_enabled else []
    sniper_tasks: list[asyncio.Task] = []
    if settings.sniper_enabled:
        from app.sniper import cache as sniper_cache
        from app.sniper import agents as sniper_agents
        from app.sniper.cex_check import enrichment_loop
        sniper_tasks.append(asyncio.create_task(sniper_cache.polling_loop()))
        sniper_tasks.append(asyncio.create_task(enrichment_loop()))
        sniper_tasks.extend(await sniper_agents.start_all())
    yield
    t1.cancel()
    t6.cancel()
    t7.cancel()
    t4.cancel()
    t5.cancel()
    for t in radar_tasks:
        t.cancel()
    for t in sniper_tasks:
        t.cancel()


app = FastAPI(title="Liquidity Analysis Engine", lifespan=lifespan)

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
})

_PUBLIC_PREFIXES = ("/static/",)


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
            content={"detail": "Modalita' ospite: sola lettura, questa azione non e' disponibile."},
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
        content={"detail": "Accesso richiesto: questa funzione e' riservata agli utenti registrati."},
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
    "int_parsing": "serve un numero intero",
    "int_from_float": "serve un numero intero, senza decimali",
    "int_type": "serve un numero intero",
    "float_parsing": "serve un numero",
    "float_type": "serve un numero",
    "bool_parsing": "serve vero o falso",
    "literal_error": "valore non ammesso",
    "enum": "valore non ammesso",
    "missing": "parametro obbligatorio mancante",
    "string_too_short": "troppo corto",
    "string_too_long": "troppo lungo",
    "greater_than_equal": "troppo piccolo",
    "less_than_equal": "troppo grande",
    "string_pattern_mismatch": "valore non ammesso",
}


def _validation_message(errors: list[dict]) -> str:
    parts: list[str] = []
    for err in errors[:4]:
        loc = [str(x) for x in err.get("loc", ()) if x not in ("query", "body", "path")]
        field = ".".join(loc) or "richiesta"
        wording = _VALIDATION_WORDING.get(err.get("type", ""), err.get("msg", "valore non valido"))
        got = err.get("input", None)
        # Il valore ricevuto e' meta' della diagnosi: "serve un numero intero"
        # senza il "ricevuto ''" non fa capire che il campo era rimasto vuoto.
        if isinstance(got, str):
            shown = f' (ricevuto "{got[:40]}")' if got != "" else " (campo vuoto)"
        elif got is None:
            shown = ""
        else:
            shown = f" (ricevuto {str(got)[:40]})"
        parts.append(f"{field}: {wording}{shown}")
    if len(errors) > 4:
        parts.append(f"e altri {len(errors) - 4}")
    return "Parametri non validi — " + " · ".join(parts)


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
        asyncio.create_task(telegram.send_message(
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
        asyncio.create_task(telegram.send_to(chat_id, message))
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
    name = req.name.strip() or "Script senza nome"
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
        raise HTTPException(status_code=404, detail="Script non trovato")
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
        raise HTTPException(status_code=404, detail="Script non trovato")
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
        raise HTTPException(status_code=404, detail="Script non trovato")
    _meta, source = loaded
    stmts, params_out, strategy_style = _parse_pine_or_400(source)

    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker mancante")
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
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol} su {interval}")

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
                    "Questo script non usa strategy.entry/close: seleziona quali variabili booleane "
                    "corrispondono a entry_long / entry_short (exit_long / exit_short sono opzionali — "
                    "se non li mappi, la posizione resta aperta fino al segnale opposto)."
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
        raise HTTPException(status_code=400, detail="Ticker mancante")
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
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol} su {interval}")

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
        raise HTTPException(status_code=429, detail="Troppi tentativi. Riprova tra qualche minuto.")

    email = req.email.strip().lower()
    if "@" not in email or len(email) < 5:
        rate_limit.register_failure(ip_key)
        raise HTTPException(status_code=400, detail="Email non valida")

    consentite = {e.strip().lower() for e in settings.registration_allowed_emails.split(",") if e.strip()}
    if consentite and email not in consentite:
        rate_limit.register_failure(ip_key)
        # Stesso messaggio che si da' a un'email gia' presente: due risposte
        # diverse direbbero a un estraneo quali indirizzi esistono.
        raise HTTPException(status_code=403, detail="Registrazione non consentita per questo indirizzo")
    if not auth.is_strong_password(req.password):
        rate_limit.register_failure(ip_key)
        raise HTTPException(
            status_code=400,
            detail="La password deve avere almeno 8 caratteri, con lettere e numeri, e non essere troppo comune",
        )
    if auth.find_by_email(email):
        rate_limit.register_failure(ip_key)
        raise HTTPException(status_code=400, detail="Email già registrata")

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
        raise HTTPException(status_code=429, detail="Troppi tentativi falliti. Riprova tra qualche minuto.")

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
async def logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE)
    response.delete_cookie(guest_links.GUEST_COOKIE)
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
        asyncio.create_task(
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
        raise HTTPException(status_code=400, detail=f"Exchange non supportato: {body.exchange}")
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
        raise HTTPException(status_code=400, detail=f"Exchange non supportato: {body.exchange}")
    cred = user.trading_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="Credenziali non configurate per questo exchange")
    api_key, api_secret, passphrase = auth.get_decrypted_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key o secret mancanti — configurali prima nella sezione Trading automatico")

    # Il pair si verifica prima di mandare l'ordine, dove abbiamo un provider
    # per quell'exchange. Scriverlo a mano e' l'unico modo che c'e' di
    # indicarlo, e un pair inesistente o quotato altrove tornava indietro come
    # errore dell'exchange — cioe' dopo aver provato a comprare qualcosa. Un
    # controllo qui costa una chiamata e dice il nome esatto atteso.
    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Pair mancante")
    if body.exchange in ("mexc", "binance", "kucoin"):
        symbol = canonical_symbol(symbol, "crypto", body.exchange)
        provider = get_provider("crypto", body.exchange)
        try:
            probe = await provider.fetch_ohlcv(symbol, "1d", 2)
        except ProviderError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Pair {symbol} non trovato su {body.exchange}: {exc}",
            ) from exc
        if probe.empty:
            raise HTTPException(
                status_code=400,
                detail=f"Pair {symbol} non trovato su {body.exchange}. Controlla il nome esatto "
                       "(es. BTCUSDT) e che sia quotato su questo exchange.",
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
        raise HTTPException(status_code=400, detail="Conferma richiesta per vendere tutta la posizione")
    supported = {"binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"}
    if body.exchange not in supported:
        raise HTTPException(status_code=400, detail=f"Exchange non supportato: {body.exchange}")
    cred = user.trading_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="Credenziali non configurate per questo exchange")
    api_key, api_secret, passphrase = auth.get_decrypted_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key o secret mancanti")

    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Pair mancante")

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
        raise HTTPException(status_code=400, detail=f"Futures non ancora supportati per questo exchange: {body.exchange}")
    adapter = futures_registry.get(body.exchange)
    existing_cred = user.futures_credentials.get(body.exchange)
    has_passphrase = bool(body.passphrase) or bool(existing_cred and existing_cred.passphrase_enc)
    if adapter.needs_passphrase and not has_passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} richiede una passphrase oltre a chiave e secret")
    # Il tetto e' quello di questo exchange, non il massimo globale: un
    # cursore che arriva a 10x su un adattatore che ne sopporta solo 5 deve
    # fermarsi prima di raggiungere il server, ma il server e' l'ultima
    # parola in ogni caso.
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leva {body.leverage}x fuori dal limite di {adapter.name} (1-{max_lev}x)")
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
                detail=f"Nessuna chiave salvata per {body.exchange} ({body.market}) — inseriscine una per provarla.",
            )
    try:
        if body.market == "futures":
            margin = await diagnostics.test_futures_connection(body.exchange, api_key, api_secret, passphrase)
            return {"ok": True, "market": "futures", "exchange": body.exchange, "margin_usdt": margin}
        balances = await diagnostics.test_spot_connection(body.exchange, api_key, api_secret, passphrase)
        return {"ok": True, "market": "spot", "exchange": body.exchange, "balances": balances}
    except diagnostics.DiagnosticsError as exc:
        return {"ok": False, "market": body.market, "exchange": body.exchange, "error": str(exc)}


@app.post("/api/auth/passkey/register/options")
async def passkey_register_options(user: User = Depends(auth.get_current_user)) -> dict:
    """Chiamata da un browser gia' loggato — e' l'iscrizione, non il
    flusso di rivelazione: Face ID/Touch ID qui autorizza solo se stesso
    a diventare un modo per approvare in futuro, non rivela niente."""
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
    return {"ok": True, "label": passkey.label or "Passkey", "created_at": passkey.created_at}


@app.get("/api/auth/passkey")
async def list_passkeys(user: User = Depends(auth.get_current_user)) -> list[dict]:
    return [{"credential_id": pk.credential_id, "label": pk.label, "created_at": pk.created_at} for pk in user.passkeys]


@app.delete("/api/auth/passkey/{credential_id}")
async def remove_passkey(credential_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    auth.delete_passkey(user.id, credential_id)
    return {"ok": True}


def _require_onchain_enabled() -> None:
    if not settings.onchain_enabled:
        raise HTTPException(
            status_code=400,
            detail="Il sistema wallet on-chain e' spento (interruttore generale) — nessuna chiave, saldo o "
                   "rivelazione e' raggiungibile finche' non viene riacceso.",
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
        raise HTTPException(status_code=404, detail="Utente non trovato")
    w = updated.onchain_wallets[body.chain]
    return {
        "chain": body.chain, "address": w.address, "enabled": w.enabled, "created_at": w.created_at,
        "backup_reminder": "Salva questa chiave altrove appena puoi (bottone 'Rivela chiave'): se il "
                            "server perde questo dato, il wallet non e' recuperabile in nessun altro modo.",
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
        raise HTTPException(status_code=404, detail="Nessun wallet su questa chain — creane uno prima")
    if not body.enabled:
        # Kill switch attivato — alert immediato (piano SRE punto 5).
        asyncio.create_task(telegram.send_infra_alert(
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
        raise HTTPException(status_code=404, detail="Nessun wallet su questa chain — creane uno prima")
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
async def approve_onchain_reveal(body: OnchainRevealApproveRequest) -> dict:
    """Chiamata dalla pagina sul TELEFONO, apposta senza richiedere il
    cookie di sessione del browser (e' un altro dispositivo) — l'identita'
    la stabilisce la password, come al login. Un token che non appartiene
    a chi ha inserito quella password non si approva."""
    verified = auth.verify_login(body.email, body.password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Email o password errate")
    if not onchain_reveal.approve(body.token, verified.id):
        raise HTTPException(status_code=400, detail="Richiesta scaduta o non valida — riprova dal browser")
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
        raise HTTPException(status_code=400, detail="Nessun passkey registrato per questa email — usa la password.")
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
        raise HTTPException(status_code=401, detail="Email non riconosciuta")
    try:
        new_sign_count = onchain_passkey.finish_authentication(user, body.state_token, body.credential)
    except Exception as exc:  # noqa: BLE001 — errore di verifica WebAuthn, mai un 500 muto
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    auth.update_passkey_sign_count(user.id, body.credential.get("id", ""), new_sign_count)
    if not onchain_reveal.approve(body.token, user.id):
        raise HTTPException(status_code=400, detail="Richiesta scaduta o non valida — riprova dal browser")
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
        raise HTTPException(status_code=404, detail="Nessun wallet su questa chain — creane uno prima")
    if not onchain_reveal.consume(body.token, user.id, chain):
        raise HTTPException(
            status_code=403,
            detail="Nessuna approvazione valida per questo wallet — scansiona di nuovo il QR dal telefono.",
        )
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, chain)
    except auth.OnchainKeyIntegrityError as exc:
        # Notifica sempre, non solo un log: un dato corrotto qui non e' un
        # bug da correggere con calma, e' un wallet potenzialmente
        # irraggiungibile — chi possiede l'account deve saperlo subito.
        if user.telegram_chat_id and _telegram_allowed(user):
            asyncio.create_task(telegram.send_to(
                user.telegram_chat_id, f"🚨 Wallet on-chain ({chain}): {exc}",
            ))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if user.telegram_chat_id and _telegram_allowed(user):
        asyncio.create_task(telegram.send_to(
            user.telegram_chat_id, f"🔓 Chiave on-chain rivelata ({chain}, {user.onchain_wallets[chain].address})",
        ))
    return {
        "chain": chain,
        "address": user.onchain_wallets[chain].address,
        "private_key": onchain_keys.to_import_format(chain, private_key_hex),
        "import_as": "Phantom/Solflare — Importa chiave privata" if chain == "solana"
        else "MetaMask/Rabby — Importa account",
        "warning": "Chiunque veda questa chiave puo' svuotare il wallet. Non farne screenshot, "
                   "non condividerla in chat: importala e chiudi questa schermata.",
    }


@app.get("/api/onchain/wallets/{chain}/balance")
async def onchain_wallet_balance(chain: OnchainChain, user: User = Depends(auth.get_current_user)) -> dict:
    _require_onchain_enabled()
    wallet = user.onchain_wallets.get(chain)
    if wallet is None:
        raise HTTPException(status_code=404, detail="Nessun wallet su questa chain — creane uno prima")
    try:
        native = await onchain_balances.native_balance(chain, wallet.address)
    except Exception as exc:  # noqa: BLE001 — un RPC pubblico irraggiungibile non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"RPC non raggiungibile: {exc}") from exc
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
        raise HTTPException(status_code=404, detail="Nessun wallet su questa chain — creane uno prima")
    try:
        result = await onchain_balances.recent_transactions(chain, wallet.address)
    except Exception as exc:  # noqa: BLE001 — un RPC pubblico irraggiungibile non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"RPC non raggiungibile: {exc}") from exc
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
    _require_onchain_enabled()
    try:
        quote = await onchain_quotes.jupiter_quote(
            body.input_mint, body.output_mint, body.amount_lamports, body.slippage_bps,
        )
    except Exception as exc:  # noqa: BLE001 — un aggregatore irraggiungibile non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"Jupiter non raggiungibile: {exc}") from exc
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
            raise HTTPException(status_code=400, detail="Nessun mercato per questa coppia in questo momento.") from exc
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
        raise HTTPException(status_code=400, detail="Chain non supportata.")
    try:
        return await _onchain_token_meta(chain, address)
    except Exception as exc:  # noqa: BLE001 — un indirizzo sbagliato non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"Decimali non recuperabili: {exc}") from exc


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
        raise HTTPException(status_code=400, detail="Chain non supportata.")
    wallet = user.onchain_wallets.get(chain)
    if wallet is None:
        raise HTTPException(status_code=400, detail=f"Nessun wallet {chain} per questo utente.")

    known_evm_tokens = []
    if chain == "robinhood":
        known_evm_tokens.append(_ONCHAIN_QUOTE_TOKEN["robinhood"])
        for order in onchain_ledger.list_limit_orders(user.id, "robinhood"):
            known_evm_tokens += [order["token_in"], order["token_out"]]

    try:
        raw_balances = await onchain_balances.all_token_balances(chain, wallet.address, known_evm_tokens)
    except Exception as exc:  # noqa: BLE001 — l'RPC che non risponde non deve essere un 500 muto
        raise HTTPException(status_code=502, detail=f"RPC non raggiungibile: {exc}") from exc

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

    holdings = await asyncio.gather(*(_enrich(e) for e in raw_balances))
    return {
        "chain": chain, "holdings": holdings,
        "partial": chain == "robinhood",  # vedi la docstring: qui non e' un elenco esaustivo
    }


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
        raise HTTPException(status_code=400, detail="Chain non supportata.")
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
            raise HTTPException(status_code=502, detail="Decimali del token non recuperabili.")
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
                    raise HTTPException(status_code=404, detail="Prezzo non disponibile per questo token.")
                return {"price_usd": price}
            _pool, fee = found
            quote = await onchain_quotes.uniswap_v3_quote(address, quote_token, fee, one_unit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — un quote fallito non deve essere un 500 muto
        logger.info("onchain live price failed for %s/%s: %s", chain, address, exc)
        raise HTTPException(status_code=502, detail="Prezzo non disponibile, riprovo tra poco.") from exc

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
        raise HTTPException(status_code=502, detail="DexScreener non risponde, riprovo tra poco.") from exc
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
        raise HTTPException(status_code=400, detail="Chain non supportata.")
    pairs = await _dexscreener_token_pairs(chain, address)
    if not pairs:
        raise HTTPException(status_code=404, detail="Nessun mercato per questo token su questa chain.")
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
        raise HTTPException(status_code=502, detail="Grafico non disponibile per questo token, riprovo tra poco.")
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
    _require_onchain_enabled()
    if body.chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"Nessun wallet {body.chain} per questo utente.")
    native = onchain_prices.NATIVE_TOKEN_ADDRESS[body.chain]
    if native.lower() not in (body.token_in.lower(), body.token_out.lower()):
        raise HTTPException(
            status_code=400,
            detail="Un ordine limite richiede il token nativo su un lato: e' l'unico prezzo che sappiamo verificare.",
        )
    if body.amount_in <= 0 or body.target_price_usd <= 0:
        raise HTTPException(status_code=400, detail="Size e prezzo target devono essere positivi.")
    return onchain_ledger.create_limit_order(
        user.id, body.chain, body.token_in, body.token_out,
        body.token_in_symbol, body.token_out_symbol, body.token_in_decimals, body.token_out_decimals,
        body.amount_in, body.direction, body.target_price_usd,
    )


@app.get("/api/onchain/limit-orders/{chain}")
async def list_limit_orders(chain: str, user: User = Depends(auth.get_current_user)) -> list[dict]:
    return onchain_ledger.list_limit_orders(user.id, chain)


@app.delete("/api/onchain/limit-orders/{order_id}")
async def cancel_limit_order(order_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    cancelled = onchain_ledger.cancel_limit_order(order_id, user.id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Ordine non trovato o non piu' annullabile.")
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
            onchain_ledger.mark_limit_order_failed(order["id"], "utente non trovato")
            return
        private_key_hex = auth.get_decrypted_onchain_private_key(user, chain)

        notional_usd = order["amount_in"] * native_usd if native_is_in else out_amount_units * native_usd
        side = "buy" if order["direction"] == "buy_below" else "sell"
        token = order["token_out_symbol"] if order["direction"] == "buy_below" else order["token_in_symbol"]

        result = await onchain_execute.execute_order(
            order["owner_id"], chain, token, "ordine limite", side,
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
        onchain_ledger.mark_limit_order_failed(order["id"], f"chiave non leggibile: {exc}")
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
    if "circuit breaker attivo" in reason:
        asyncio.create_task(telegram.send_infra_alert(
            f"Circuit breaker scattato su {chain} (utente {owner_email}): {reason}"
        ))
        return
    if "tetto giornaliero" in reason:
        asyncio.create_task(telegram.send_infra_alert(
            f"Tetto giornaliero raggiunto su {chain} (utente {owner_email}): {reason}"
        ))
        return
    if result.get("accepted") and notional_usd > 0.30 * settings.onchain_max_usd_per_trade:
        asyncio.create_task(telegram.send_infra_alert(
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
    amount_lamports: int
    notional_usd: float
    price_usd: float
    slippage_bps: int = 50
    pool_address: str | None = None  # opzionale: indirizzo del pool/pair per l'allowlist (DexScreener, vedi pool_safety.py)


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
    _require_onchain_enabled()
    if "solana" not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail="Nessun wallet Solana per questo utente.")
    try:
        quote = await onchain_quotes.jupiter_quote(
            body.input_mint, body.output_mint, body.amount_lamports, body.slippage_bps,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Jupiter non raggiungibile: {exc}") from exc
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, "solana")
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    result = await onchain_execute.execute_order(
        user.id, "solana", body.token, body.strategy_name, body.side,
        body.notional_usd, quote["price_impact_pct"], body.price_usd,
        private_key_hex, quote, pool_address=body.pool_address,
    )
    _maybe_alert_onchain_risk_event(user.email, "solana", result, body.notional_usd)
    return result


class OnchainExecuteRobinhoodRequest(BaseModel):
    token: str
    strategy_name: str
    side: Literal["buy", "sell"]
    token_in: str
    token_out: str
    fee: int | None = None  # None = trova da solo il fee tier giusto, come /quote/robinhood
    amount_in: int
    notional_usd: float
    price_usd: float
    max_slippage_pct: float = 1.0


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
    _require_onchain_enabled()
    if "robinhood" not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail="Nessun wallet Robinhood Chain per questo utente.")
    try:
        found = await onchain_quotes.find_pool_and_fee(body.token_in, body.token_out, body.fee)
        if found is None:
            raise HTTPException(status_code=400, detail="Nessun pool per questa coppia su nessun fee tier di Uniswap v3.")
        pool, fee = found
        quote = await onchain_quotes.uniswap_v3_quote(body.token_in, body.token_out, fee, body.amount_in)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Robinhood Chain non raggiungibile: {exc}") from exc
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, "robinhood")
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    amount_out_minimum = int(quote["amount_out"] * (1 - body.max_slippage_pct / 100))
    result = await onchain_execute.execute_order(
        user.id, "robinhood", body.token, body.strategy_name, body.side,
        body.notional_usd, quote["price_impact_pct"], body.price_usd,
        private_key_hex,
        {"token_in": body.token_in, "token_out": body.token_out, "fee": fee,
         "amount_in": body.amount_in, "amount_out_minimum": amount_out_minimum},
    )
    _maybe_alert_onchain_risk_event(user.email, "robinhood", result, body.notional_usd)
    return result


class OnchainSendRequest(BaseModel):
    chain: OnchainChain
    to_address: str
    token_address: str | None = None  # None = il nativo (SOL/ETH)
    amount: float  # unita' umane, non raw
    price_usd: float  # per il tetto USD e per il ledger — mai fabbricato: chi chiama lo prende dal prezzo gia' mostrato in UI


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
        raise HTTPException(status_code=400, detail=f"Nessun wallet {body.chain} per questo utente.")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="La size deve essere positiva.")

    is_native = body.token_address is None or body.token_address.lower() == onchain_prices.NATIVE_TOKEN_ADDRESS[body.chain].lower()
    if is_native:
        decimals = _NATIVE_DECIMALS[body.chain]
        token_label = "SOL" if body.chain == "solana" else "ETH"
    else:
        try:
            decimals = await onchain_token_metadata.get_token_decimals(body.chain, body.token_address)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Decimali non recuperabili: {exc}") from exc
        if decimals is None:
            raise HTTPException(status_code=502, detail="Decimali del token non recuperabili.")
        token_label = body.token_address

    amount_raw = int(round(body.amount * 10 ** decimals))
    notional_usd = body.amount * body.price_usd

    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, body.chain)
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = await onchain_send.send_token(
        user.id, body.chain, token_label, body.to_address, body.amount, amount_raw,
        notional_usd, private_key_hex, is_native, body.token_address,
    )
    _maybe_alert_onchain_risk_event(user.email, body.chain, result, notional_usd)
    return result


class OnchainExecuteTwapRequest(BaseModel):
    chain: OnchainChain
    token: str
    strategy_name: str
    side: Literal["buy", "sell"]
    total_usd: float
    price_usd: float
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
    _require_onchain_enabled()
    if body.chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"Nessun wallet {body.chain} per questo utente.")
    try:
        private_key_hex = auth.get_decrypted_onchain_private_key(user, body.chain)
    except auth.OnchainKeyIntegrityError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if body.chain == "solana":
        if not (body.input_mint and body.output_mint and body.total_amount_lamports):
            raise HTTPException(status_code=400, detail="input_mint/output_mint/total_amount_lamports richiesti per solana.")

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
            raise HTTPException(status_code=400, detail="token_in/token_out/fee/total_amount_in richiesti per robinhood.")

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

    return await onchain_twap_jobs.execute_twap_job(
        user.id, body.chain, body.token, body.strategy_name, body.side,
        body.total_usd, body.price_usd, private_key_hex, body.pool_liquidity_usd, quote_for_slice,
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
    return {"chains": user.sniper_autotrade_chains}


@app.patch("/api/onchain/sniper-autotrade/{chain}")
async def set_sniper_autotrade(
    chain: OnchainChain, body: SniperAutotradeRequest, user: User = Depends(auth.get_current_user),
) -> dict:
    """Opt-in per lo sniper che aggiunge da solo nuovi arrivi on-chain alla
    watchlist con la strategia di Lorenzo attiva — vedi
    _sniper_onchain_bridge_loop. Spento di default per ogni utente,
    acceso solo qui, mai da un default globale."""
    if chain not in user.onchain_wallets:
        raise HTTPException(status_code=400, detail=f"Nessun wallet {chain} per questo utente — crealo prima.")
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
        {"ts": j["ts"], "text": f"Segnale {_PILOT_SIGNAL_WORDS.get(j['signal_type'], j['signal_type'])}: "
                                f"{j['outcome']}" + (f" — {j['detail']}" if j.get("detail") else "")}
        for j in mine[:6]
    ]
    if found:
        events.append({"ts": found["added_at"], "text": "Entrato in osservazione (scoperto dallo sniper)"})

    if position:
        state, label = "in", "In posizione"
        reason = f"{position['quantity']:g} token, costati ${position['cost_usd']:,.2f}"
    elif mine and mine[0]["outcome"] != "eseguito":
        state, label = "blocked", "Segnale non eseguito"
        reason = mine[0].get("detail") or mine[0]["outcome"]
    elif mine:
        state, label = "watch", "Uscito, osservo"
        reason = f"ultimo ordine: {_PILOT_SIGNAL_WORDS.get(mine[0]['signal_type'], mine[0]['signal_type'])}"
    else:
        state, label = "watch", "Osservo il trend"
        reason = "nessun segnale della strategia finora"

    return {
        "symbol": base_symbol,
        "watch_symbol": entry.symbol,
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
    "entry_long": "di acquisto", "flip_to_long": "di acquisto",
    "exit_long": "di vendita", "flip_to_short": "di vendita",
    "entry_short": "short", "exit_short": "chiusura short",
}


@app.get("/api/onchain/radar")
async def onchain_radar_view(user: User = Depends(auth.get_current_user)) -> dict:
    """Token giovani su Solana e Robinhood che una strategia dell'archivio
    avrebbe tradato bene. Proposte, non ordini: vedi app/trading/onchain/radar.py."""
    _require_onchain_enabled()
    snap, gone = onchain_radar.snapshot(), onchain_outcomes.shamed()
    return {**snap, "proposals": onchain_outcomes.without_shamed(snap.get("proposals") or [], gone),
            "tested": onchain_outcomes.without_shamed(snap.get("tested") or [], gone),
            "listings": onchain_outcomes.without_shamed(onchain_listings.recent(), gone),
            "rejected": onchain_listings.rejected(), "pulse": onchain_listings.pulse(),
            "listing_status": onchain_listings.status(), "shame": onchain_outcomes.shame()}


@app.get("/api/onchain/radar/lessons")
async def onchain_radar_lessons(user: User = Depends(auth.get_current_user)) -> dict:
    """Cosa e' successo davvero ai token proposti e scartati, e quali
    controlli lo prevedevano. Vedi app/trading/onchain/outcomes.py."""
    _require_onchain_enabled()
    return {**onchain_outcomes.lessons(), "gate": settings.onchain_memory_gate}


@app.get("/api/onchain/radar/memes")
async def onchain_radar_memes(user: User = Depends(auth.get_current_user)) -> dict:
    """La corsia meme: token scartati per nome, sito o GitHub, non per
    sicurezza, ordinati per somiglianza alle salite vere gia' viste. Gioco
    d'azzardo, e lo dice. Solo lettura: il pilota non li compra."""
    _require_onchain_enabled()
    return {**onchain_outcomes.meme_board(), "study": onchain_outcomes.study()}


@app.get("/api/onchain/leaderboard")
async def onchain_leaderboard_view(user: User = Depends(auth.get_current_user)) -> dict:
    """La classifica dei progetti migliori: GitHub, sito e tokenomics
    analizzati a fondo. Vedi app/trading/onchain/leaderboard.py."""
    _require_onchain_enabled()
    board = onchain_leaderboard.board()
    return {**board, "entries": onchain_outcomes.without_shamed(board.get("entries") or [])}


@app.get("/api/onchain/radar/stream")
async def onchain_radar_stream(user: User = Depends(auth.get_current_user)) -> StreamingResponse:
    """I listing nuovi spinti al browser appena passano le soglie: niente
    attesa del prossimo giro di lettura."""
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


_LISTING_NOTIFY_PER_HOUR = 12
_listing_notified: dict[str, list[float]] = {}


async def _notify_listing(item: dict) -> None:
    """Telegram a chi ha un wallet on-chain e Telegram collegato: l'app
    chiusa non deve voler dire saperlo tardi. Al massimo 12 all'ora."""
    from app.notify import telegram
    now = _time.time()
    link = f"{settings.public_base_url}/onchain-app.html#listing={item['chain']}:{item['pair_address']}"
    text = onchain_listings.telegram_text(item, now, link)
    for user in auth.list_users():
        if not user.telegram_chat_id or not user.onchain_wallets or not _telegram_allowed(user):
            continue
        sent = [t for t in _listing_notified.get(user.id, []) if now - t < 3600]
        if len(sent) >= _LISTING_NOTIFY_PER_HOUR:
            continue
        _listing_notified[user.id] = sent + [now]
        asyncio.create_task(telegram.send_to(user.telegram_chat_id, text))


onchain_listings.on_listing(_notify_listing)


@app.get("/api/onchain/radar/chart")
async def onchain_radar_chart(
    chain: Literal["solana", "robinhood"], pool: str, symbol: str,
    interval: Literal["1m", "5m", "15m", "1h", "4h"] = "15m", user: User = Depends(auth.get_current_user),
) -> dict:
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
    _require_onchain_enabled()
    if req.strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail="Strategia sconosciuta")
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
    _require_onchain_enabled()
    enabled = chain in user.sniper_autotrade_chains
    breaker = onchain_ledger.breaker_status(user.id, chain)
    spent_today = onchain_ledger.get_daily_spent(user.id, chain)

    # Cosa lo ferma, in ordine di gravita'. Ogni voce e' una condizione che
    # il codice controlla davvero (bridge e giro dei segnali), non un'ipotesi.
    health = [
        {"key": "wallet", "ok": chain in user.onchain_wallets, "label": "Wallet su questa rete",
         "detail": "creato" if chain in user.onchain_wallets else "manca: crealo prima"},
        {"key": "scanner", "ok": settings.sniper_enabled, "label": "Scanner dei token nuovi",
         "detail": "attivo" if settings.sniper_enabled else "spento sul server: nessun token nuovo arriva"},
        {"key": "signals", "ok": bool(settings.telegram_bot_token and user.telegram_chat_id and _telegram_allowed(user)),
         "label": "Controllo dei segnali",
         "detail": "attivo" if settings.telegram_bot_token and user.telegram_chat_id and _telegram_allowed(user)
         else "fermo: serve Telegram collegato, senza la strategia non viene valutata"},
        {"key": "execution", "ok": settings.onchain_execution_enabled, "label": "Ordini reali",
         "detail": "abilitati" if settings.onchain_execution_enabled else "spenti sul server: i segnali restano solo registrati"},
        {"key": "breaker", "ok": not breaker.get("tripped"), "label": "Freno di emergenza",
         "detail": "armato, non scattato" if not breaker.get("tripped") else "scattato: riattivalo da Limiti"},
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
        raise HTTPException(status_code=403, detail="Solo un amministratore configurato (settings.admin_emails) "
                                                      "puo' leggere /admin/status.")


# ---------------------------------------------------------------- link ospite

class GuestLinkRequest(BaseModel):
    label: str = ""
    days: int = guest_links.DEFAULT_DAYS


def _require_admin(user: User) -> None:
    allowed = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    if guest_links.is_guest(user) or user.email.lower() not in allowed:
        raise HTTPException(status_code=403, detail="Solo un amministratore puo' gestire i link ospite.")


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
        raise HTTPException(status_code=404, detail="Link non trovato")
    return _guest_link_out(link, request)


@app.get("/ospite")
async def guest_enter(request: Request, t: str = "") -> Response:
    link = guest_links.find_by_token(t) if t else None
    if link is None:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Link non valido</title><body style='font:16px system-ui;padding:32px;background:#0b0b0c;color:#ddd'>"
            "<p>Questo link ospite e' scaduto o e' stato revocato.</p></body>",
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


_GUEST_ADMIN_HTML = """<!doctype html><html lang="it"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Link ospite</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px;background:#0b0b0c;color:#e6e6e6;max-width:640px}
h1{font-size:18px;margin:0 0 16px}form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
input,button{font:inherit;font-size:16px;padding:8px 10px;border-radius:8px;border:1px solid #333;background:#151517;color:inherit}
button{cursor:pointer}ul{list-style:none;padding:0;margin:0}li{padding:10px 0;border-top:1px solid #222}
.off{opacity:.45}code{word-break:break-all;font-size:13px;color:#9ad}small{color:#888}
</style>
<h1>Link ospite &middot; sola lettura</h1>
<form id="f"><input id="label" placeholder="Per chi (es. Marco)" maxlength="60">
<input id="days" type="number" min="1" max="90" value="7" style="width:5em"> giorni
<button>Crea link</button></form>
<ul id="list"></ul>
<script>
const $=s=>document.querySelector(s);
const day=t=>new Date(t*1000).toLocaleDateString("it-IT",{day:"numeric",month:"short"});
async function load(){
  const r=await fetch("/api/guest-links");const rows=r.ok?await r.json():[];
  $("#list").innerHTML="";
  for(const l of rows){const li=document.createElement("li");if(!l.active)li.className="off";
    li.innerHTML=`<b></b> <small>${l.active?"scade il "+day(l.expires_at):"chiuso"}</small><br>`;
    li.querySelector("b").textContent=l.label||"senza nome";
    if(l.url){const c=document.createElement("code");c.textContent=l.url;li.append(c," ");
      const cp=document.createElement("button");cp.textContent="Copia";cp.onclick=()=>navigator.clipboard.writeText(l.url);
      const rv=document.createElement("button");rv.textContent="Revoca";
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
    'border:1px solid rgba(255,255,255,.12)">Ospite &middot; sola lettura</div>'
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
        raise HTTPException(status_code=400, detail=f"Futures non ancora supportati per questo exchange: {body.exchange}")
    cred = user.futures_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="Credenziali futures non configurate per questo exchange")
    api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key o secret mancanti")
    adapter = futures_registry.get(body.exchange)
    if adapter.needs_passphrase and not passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} richiede una passphrase, non ancora salvata")
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leva {body.leverage}x fuori dal limite di {adapter.name} (1-{max_lev}x)")

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
        raise HTTPException(status_code=400, detail="Conferma richiesta: mostra prima l'anteprima (/api/trading/futures/quote)")
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures non ancora supportati per questo exchange: {body.exchange}")
    cred = user.futures_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="Credenziali futures non configurate per questo exchange")
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
            detail=f"Esecuzione futures disattivata per {adapter.name} — attivala col toggle \"Attivo\" sulla sua card qui sopra, oppure premi \"Attiva ora\" qui sotto.",
        )
    api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key o secret mancanti")
    if adapter.needs_passphrase and not passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} richiede una passphrase, non ancora salvata")
    max_lev = min(MAX_FUTURES_LEVERAGE, adapter.max_leverage)
    if not (1 <= body.leverage <= max_lev):
        raise HTTPException(status_code=400, detail=f"Leva {body.leverage}x fuori dal limite di {adapter.name} (1-{max_lev}x)")

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
            detail=f"Margine insufficiente: servono {margin_needed:.2f} USDT, disponibili {available:.2f} USDT",
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
        raise HTTPException(status_code=400, detail="Conferma richiesta per chiudere una posizione")
    if body.exchange not in SUPPORTED_FUTURES_EXCHANGES_ORDERED:
        raise HTTPException(status_code=400, detail=f"Futures non supportati per questo exchange: {body.exchange}")
    cred = user.futures_credentials.get(body.exchange)
    if not cred:
        raise HTTPException(status_code=400, detail="Credenziali futures non configurate per questo exchange")
    adapter = futures_registry.get(body.exchange)
    # Nessun controllo su cred.enabled, a differenza dell'apertura: il kill
    # switch esiste per impedire NUOVA esposizione, e una chiusura la riduce
    # sempre. Spegnerlo con una posizione aperta e poi non poterla piu'
    # chiudere da qui sarebbe esattamente il modo di restare intrappolati in
    # una posizione a leva — il contrario di cio' per cui quel toggle c'e'.
    api_key, api_secret, passphrase = auth.get_decrypted_futures_credential(user, body.exchange)
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key o secret mancanti")
    if adapter.needs_passphrase and not passphrase:
        raise HTTPException(status_code=400, detail=f"{adapter.name} richiede una passphrase, non ancora salvata")

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
                "detail": f"Nessuna posizione aperta su {symbol} da chiudere."}
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
        raise HTTPException(status_code=403, detail="Non autorizzato")
    settings.trading_halt = body.halt
    state = "in pausa" if body.halt else "attiva"
    return {"ok": True, "trading_halt": body.halt, "detail": f"Esecuzione automatica {state}"}


@app.get("/api/trading/halt")
async def get_trading_halt(user: User = Depends(auth.get_current_user)):
    """Stato corrente del kill switch."""
    allowed_emails = {e.strip().lower() for e in settings.registration_allowed_emails.split(",") if e.strip()}
    if user.email.lower() not in allowed_emails:
        raise HTTPException(status_code=403, detail="Non autorizzato")
    return {"trading_halt": settings.trading_halt}


@app.get("/api/balances")
async def get_balances(user: User = Depends(auth.get_current_user)) -> dict:
    if not _balance_visible(user):
        raise HTTPException(status_code=403, detail="Non disponibile per questo account")

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
        raise HTTPException(status_code=404, detail="Voce non trovata")
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
        raise HTTPException(status_code=400, detail="Ticker mancante")

    # Re-verify the pair actually resolves on this exchange before storing
    # it — the frontend already confirms this via the suggestions list, but
    # a symbol could still be typed/submitted without going through it, or
    # get delisted between being suggested and being added.
    provider = get_provider(req.asset_type, req.exchange)
    try:
        probe = await provider.fetch_ohlcv(symbol, "1d", 2)
    except ProviderError as exc:
        raise HTTPException(
            status_code=400, detail=f"Ticker non trovato su questo exchange: {exc}"
        ) from exc
    if probe.empty:
        raise HTTPException(status_code=400, detail="Ticker non trovato su questo exchange.")

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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
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
            asyncio.create_task(_run_archive_backtest(
                snapshot["id"], entry.symbol, entry.asset_type, entry.exchange,
                snapshot["interval"], req.strategy_name, params, {},
            ))

    if req.enabled and not was_enabled and user.telegram_chat_id and _telegram_allowed(user):
        # Confirms the watch is actually active — sent immediately on
        # activation, before any real signal, so silence afterward reads as
        # "no condition yet" rather than "is this even running?".
        asyncio.create_task(
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
    if req.cap_usdt < 0:
        raise HTTPException(status_code=400, detail="Il tetto non puo' essere negativo")
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
    entry = _owned_entry_or_404(entry_id, user)
    if req.market == "futures" and entry.exchange not in futures_registry.SUPPORTED_FUTURES_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"{entry.exchange} non ha un adattatore futures registrato",
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {strategy_name}")
    entry = _owned_entry_or_404(entry_id, user)
    if not user.telegram_chat_id:
        raise HTTPException(status_code=400, detail="Nessun Telegram chat_id configurato")
    provider = get_provider(entry.asset_type, entry.exchange)
    interval = entry.strategy_intervals.get(strategy_name, entry.interval)
    try:
        df = await fetch_ohlcv_canonical(provider, entry.symbol, interval, settings.candles_per_timeframe)
        if entry.on_close:
            df = drop_unclosed_candle(df, interval)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail="Nessun dato disponibile")
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
        raise HTTPException(status_code=400, detail="Ticker mancante")
    return price_alerts_store.add_alert(
        user.id, symbol, req.asset_type, req.exchange,
        req.condition, req.price, req.note, req.once_only,
    )


@app.delete("/api/price-alerts/{alert_id}")
async def delete_price_alert(alert_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    a = price_alerts_store.get_alert(alert_id)
    if a is None or a.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Alert non trovato")
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
        raise HTTPException(status_code=404, detail="Alert non trovato")
    return price_alerts_store.set_active(alert_id, req.active)


def _owned_favorite_or_404(entry_id: str, user: User) -> FavoriteEntry:
    entry = favorites_store.get_entry(entry_id)
    if entry is None or entry.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Voce non trovata")
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
        raise HTTPException(status_code=400, detail="Ticker mancante")

    provider = get_provider(req.asset_type, req.exchange)
    try:
        probe = await provider.fetch_ohlcv(symbol, "1d", 2)
    except ProviderError as exc:
        raise HTTPException(
            status_code=400, detail=f"Ticker non trovato su questo exchange: {exc}"
        ) from exc
    if probe.empty:
        raise HTTPException(status_code=400, detail="Ticker non trovato su questo exchange.")

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
        raise HTTPException(status_code=400, detail="Ticker mancante")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {strategy_name}")

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
            raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol} su timeframe {tf_key}")

        swings = compute_swings(df, tf_key)
        levels = compute_levels(df, swings)
        indicators = compute_indicators(df)
        bias = determine_bias(levels)
        dealing_range = compute_dealing_range(df, swings)
        fib_zones = compute_fib_zones(df, tf_key, intervals[tf_key])
        strategy = build_strategy_state(strategy_name, df, strategy_params_dict)
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
            result = run_backtest(df, strategy_params_dict, backtest_params_dict, strategy_name=strategy_name)
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
            detail=f"Nessuna candela per {symbol} nel periodo richiesto — prova un intervallo piu' ampio.",
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
    start_date: str | None = Query(None, description="YYYY-MM-DD, incluso — ritaglia la serie prima del backtest"),
    end_date: str | None = Query(None, description="YYYY-MM-DD, incluso — ritaglia la serie prima del backtest"),
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
        raise HTTPException(status_code=400, detail="Ticker mancante")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {strategy_name}")

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
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol}")

    df = _slice_to_period(df, symbol, start_date, end_date)

    result = run_backtest(df, strategy_params.model_dump(), backtest_params.model_dump(), strategy_name=strategy_name)
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
            f"Ottimizzato su {len(df)} candele a {backtest_interval}, non sul "
            f"timeframe {ltf_interval} del grafico. Per riprodurre a mano questo "
            f"risultato imposta il grafico su {backtest_interval}."
            if backtest_interval != ltf_interval
            else f"Ottimizzato su {len(df)} candele a {backtest_interval}, "
                 f"lo stesso timeframe del grafico."
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
        raise HTTPException(status_code=400, detail="Ticker mancante")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {strategy_name}")

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
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol}")

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
                f"Hai gia' una ricerca in corso da {da_quanto // 60} min {da_quanto % 60} s. "
                "Aspetta che finisca: due ricerche insieme non vanno piu' veloci, "
                "si rubano il processore a vicenda."
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
    start_date: str | None = Query(None, description="YYYY-MM-DD, incluso — stesso periodo del backtest"),
    end_date: str | None = Query(None, description="YYYY-MM-DD, incluso — stesso periodo del backtest"),
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
        raise HTTPException(status_code=400, detail="Ticker mancante")
    if not is_optimizable(strategy_name):
        raise HTTPException(status_code=400, detail=f"Ottimizzazione non disponibile per {strategy_name}")

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
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol}")
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
        note=f"Ricerca {_MODE_LABEL.get(mode, mode)}",
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
    start_date: str | None = Query(None, description="YYYY-MM-DD, incluso — stesso periodo del backtest"),
    end_date: str | None = Query(None, description="YYYY-MM-DD, incluso — stesso periodo del backtest"),
    user: User | None = Depends(auth.get_current_user_optional),
) -> dict:
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker mancante")
    if not is_optimizable(strategy_name):
        raise HTTPException(status_code=400, detail=f"Ottimizzazione non disponibile per {strategy_name}")

    owner_id = user.id if user else ""
    job_id = jobs_store.create(owner_id, "optimize", f"{symbol.upper()} · {strategy_name} · {mode}")

    constraints = Constraints(
        min_losing_trades=min_losing_trades,
        max_losing_trades=max_losing_trades,
        max_drawdown_pct=max_drawdown_pct,
    )

    async def _run() -> None:
        try:
            jobs_store.log(job_id, f"scarico i dati di {symbol.upper()}…")
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
                jobs_store.fail(job_id, f"Nessun dato per {symbol}")
                return
            if start_date or end_date:
                try:
                    df = _slice_to_period(df, symbol, start_date, end_date)
                except HTTPException as exc:
                    # Qui non c'e' nessuna risposta HTTP da restituire: il job
                    # e' gia' partito e il suo unico canale e' il proprio log.
                    jobs_store.fail(job_id, exc.detail)
                    return
                jobs_store.log(job_id, f"periodo {start_date or 'inizio'} → {end_date or 'fine'}")
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
                jobs_store.log(job_id, f"salvata in archivio ({archived['id']})")
                user_now = auth.get_user(owner_id)
                if user_now and user_now.telegram_chat_id and _telegram_allowed(user_now):
                    await telegram.send_to(user_now.telegram_chat_id,
                                           _optimizer_summary(symbol, strategy_name, mode, body))
                    jobs_store.log(job_id, "riepilogo mandato su Telegram")
            jobs_store.log(job_id, "fatto")
        except SearchCancelled:
            jobs_store.log(job_id, "fermata su richiesta")
            jobs_store.cancelled(job_id)
        except NoCandidateMeetsConstraints as exc:
            jobs_store.fail(job_id, str(exc))
        except ProviderError as exc:
            jobs_store.fail(job_id, f"Dati non disponibili: {exc}")
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

    asyncio.create_task(_run())
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
        raise HTTPException(status_code=404, detail="Lavoro non trovato o scaduto")
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def job_status(
    job_id: str,
    since: int = Query(0, ge=0),
    user: User | None = Depends(auth.get_current_user_optional),
) -> dict:
    job = jobs_store.get(job_id, owner_id=user.id if user else None, since=since)
    if job is None:
        raise HTTPException(status_code=404, detail="Lavoro non trovato o scaduto")
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
            archive_store.attach_error(entry_id, f"Nessun dato per {symbol}")
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker mancante")
    entry = archive_store.create(
        owner_id=user.id, symbol=symbol, asset_type=req.asset_type,
        exchange=req.exchange, interval=req.ltf_interval,
        strategy_name=req.strategy_name, params=req.params,
        backtest_params=req.backtest_params, source="manuale", note=req.note,
    )
    asyncio.create_task(_run_archive_backtest(
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
        raise HTTPException(status_code=404, detail="Voce d'archivio non trovata")
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
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {req.strategy_name}")
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker mancante")
    note = f"Importata da un altro account{': ' + req.note if req.note else ''} — metriche non verificate qui"
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
        archive_store.attach_error(entry["id"], "Nessuna metrica nel file importato")
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
        raise HTTPException(status_code=404, detail="Voce d'archivio non trovata")
    strategy_name = entry.get("strategy_name", "")
    if strategy_name not in AVAILABLE_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Strategia sconosciuta: {strategy_name}")

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
                    detail=f"{watch.exchange} non ha un adattatore futures registrato",
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
        raise HTTPException(status_code=404, detail="Voce non trovata")
    return entry


@app.delete("/api/archive/{entry_id}")
async def delete_archive_entry(
    entry_id: str, user: User = Depends(auth.get_current_user)
) -> dict:
    if not archive_store.remove(entry_id, user.id):
        raise HTTPException(status_code=404, detail="Voce non trovata")
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
        raise HTTPException(status_code=404, detail="Segnalazione non trovata")
    try:
        return issues_store.add_reply(issue_id, req.text, author=user.email,
                                      reopen=req.reopen)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        raise HTTPException(status_code=404, detail="Segnalazione non trovata")
    return updated


@app.delete("/api/issues/{issue_id}")
async def remove_issue(issue_id: str, user: User = Depends(auth.get_current_user)) -> dict:
    if not issues_store.delete_issue(issue_id):
        raise HTTPException(status_code=404, detail="Segnalazione non trovata")
    return {"deleted": issue_id}


@app.get("/api/sniper/candidates")
async def sniper_candidates(
    chain: str = Query("", description="Filter by chain_id"),
    min_score: int = Query(0, ge=0, description="Minimum score_total"),
    min_liq: float = Query(0.0, ge=0, description="Minimum liquidity_usd"),
    max_mc: float = Query(0.0, ge=0, description="Maximum market_cap (0 = no limit)"),
    exclude_cex: bool = Query(False, description="Exclude CEX-listed tokens"),
    serious_only: bool = Query(True, description="Solo progetti seri: esclude le meme coin"),
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
            "description": "Token non trovato su DexScreener — verifica l'indirizzo.",
            "checks": [{"label": "Non trovato su DexScreener", "passed": False}],
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
            checks.append({"label": "Trovato su CoinGecko", "passed": True, "note": cg_data.get("name", "")})

            description = (cg_data.get("description") or {}).get("en", "")
            if description and len(description) > 400:
                description = description[:400] + "…"

            # market data quality
            md = cg_data.get("market_data") or {}
            mc = (md.get("market_cap") or {}).get("usd")
            if mc and mc > 0:
                research_score += 1
                checks.append({"label": "Market cap on-chain verificato", "passed": True, "note": f"${mc:,.0f}"})

            # community / social
            comm = cg_data.get("community_data") or {}
            tw = comm.get("twitter_followers", 0) or 0
            tg = comm.get("telegram_channel_user_count", 0) or 0
            if tw > 1000:
                research_score += 1
                checks.append({"label": "Twitter attivo", "passed": True, "note": f"{tw:,} follower"})
            elif tw > 0:
                checks.append({"label": "Twitter presente", "warning": True, "note": f"{tw:,} follower"})
            if tg > 500:
                research_score += 1
                checks.append({"label": "Telegram attivo", "passed": True, "note": f"{tg:,} membri"})
            elif tg > 0:
                checks.append({"label": "Telegram presente", "warning": True, "note": f"{tg:,} membri"})

            # dev activity
            dev = cg_data.get("developer_data") or {}
            commits = (dev.get("commit_count_4_weeks") or 0)
            if commits > 5:
                research_score += 1
                checks.append({"label": "Attività GitHub recente", "passed": True, "note": f"{commits} commit/4wk"})
            elif commits > 0:
                checks.append({"label": "Repository GitHub", "warning": True, "note": f"{commits} commit/4wk"})

            # links
            lnks = cg_data.get("links") or {}
            if lnks.get("homepage"):
                for h in (lnks["homepage"] or []):
                    if h:
                        links.append({"label": "Sito ufficiale", "url": h})
                        break
            if lnks.get("twitter_screen_name"):
                links.append({"label": "Twitter", "url": f"https://twitter.com/{lnks['twitter_screen_name']}"})
            if lnks.get("telegram_channel_identifier"):
                links.append({"label": "Telegram", "url": f"https://t.me/{lnks['telegram_channel_identifier']}"})

            # CEX tickers check
            tickers = cg_data.get("tickers") or []
            cex_names = [t["market"]["name"] for t in tickers if t.get("market", {}).get("identifier", "") in _CEX_IDS]
            if cex_names:
                checks.append({"label": f"Listato su {cex_names[0]}", "warning": True, "note": "già su CEX"})
            else:
                research_score += 1
                checks.append({"label": "Non ancora su CEX", "passed": True, "note": "opportunità early"})
        else:
            checks.append({"label": "Non trovato su CoinGecko", "passed": False, "note": "token non indicizzato"})

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
                        checks.append({"label": "Liquidità solida su DEX", "passed": True, "note": f"${liq:,.0f}"})
                    elif liq >= 1_000:
                        checks.append({"label": "Liquidità bassa su DEX", "warning": True, "note": f"${liq:,.0f}"})
                    else:
                        checks.append({"label": "Liquidità molto bassa", "passed": False, "note": f"${liq:,.0f}"})
                    if liq > 0 and vol / liq > 0.05:
                        research_score += 1
                        checks.append({"label": "Volume reale (>5% liq)", "passed": True, "note": f"${vol:,.0f}/24h"})
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
        ]
        parts += [m.read_bytes() for m in sorted((FRONTEND_DIR / "static" / "onchain").glob("*.js"))]
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
