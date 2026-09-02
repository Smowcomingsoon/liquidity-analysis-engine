import asyncio
import hashlib
import itertools
import json
import logging
import sys
import time as _time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
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
from app import rate_limit
from app.trading.executor import place_order_for_signal
from app import favorites as favorites_store
from app import watchlist as watchlist_store
from app.analysis.backtest import run_backtest
from app.analysis.confluence import compute_confluence
from app.analysis.indicators import compute_indicators
from app.analysis.fibonacci import compute_fib_zones
from app.analysis.optimize import is_optimizable, optimize_strategy
from app.analysis.smc_engine import compute_dealing_range, compute_levels, compute_swings
from app.analysis.strategies import AVAILABLE_STRATEGIES, DEFAULT_STRATEGY, build_strategy_state
from app.analysis.summary import build_summary, determine_bias
from app.cache import get_or_fetch
from app.config import settings
from app.live.mexc_stream import mexc_stream_manager
from app.models import (
    AnalysisResponse,
    AssetType,
    BacktestResult,
    Candle,
    CryptoExchange,
    FavoriteEntry,
    StrategyState,
    SymbolSuggestion,
    TimeframeAnalysis,
    TimeframeKey,
    User,
    WatchlistEntry,
)
from app.exchange_balances import BalanceFetchError, fetch_kucoin_balances, fetch_mexc_balances
from app.notify import telegram
from app.notify.alerts import find_new_signals, format_signal_message
from app.pine import PineError, PineInterpreter, is_strategy_style, parse_program, scan_inputs
from app.pine import storage as pine_storage
from app.providers.base import DataProvider, ProviderError
from app.providers.binance import BinanceProvider
from app.providers.binance import format_symbol as format_binance_symbol
from app.providers.kucoin import KucoinProvider
from app.providers.kucoin import format_symbol as format_kucoin_symbol
from app.providers.mexc import MexcProvider
from app.providers.mexc import format_symbol as format_mexc_symbol
from app.providers.twelvedata import TwelveDataProvider
from app.resample import resample_by_count
from app.timeframes import (
    CANONICAL_INTERVALS,
    DEFAULT_INTERVALS,
    SYNTHETIC_DAY_INTERVALS,
    IntervalChoice,
    drop_unclosed_candle,
)
from starlette.middleware.gzip import GZipMiddleware

logger = logging.getLogger(__name__)
if getattr(sys, "frozen", False):
    FRONTEND_DIR = Path(sys._MEIPASS) / "frontend"  # type: ignore[attr-defined]
else:
    FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
WATCHLIST_POLL_SECONDS = 60

twelvedata_provider = TwelveDataProvider()
mexc_provider = MexcProvider()
binance_provider = BinanceProvider()
kucoin_provider = KucoinProvider()


def _telegram_allowed(user: User) -> bool:
    allowed = {e.strip().lower() for e in settings.telegram_allowed_emails.split(",") if e.strip()}
    return not allowed or user.email.lower() in allowed


def _balance_visible(user: User) -> bool:
    allowed = {e.strip().lower() for e in settings.balance_visible_emails.split(",") if e.strip()}
    return not allowed or user.email.lower() in allowed


_BACKTEST_MIN_INTERVAL = "1d"


def backtest_interval_for(ltf_interval: str) -> str:
    """The backtest deliberately runs on a coarser feed than whatever the
    chart happens to be set to — a fixed candle budget covers years at "1d"
    but only hours at "1m", and the user is after the deepest history the
    exchange has, not a simulation tied to the chart's own granularity.
    Never goes coarser than the chart itself, though: if ltf is already
    "1w"/"1M" there's nothing to gain by stepping back down to daily."""
    if CANONICAL_INTERVALS.index(ltf_interval) >= CANONICAL_INTERVALS.index(_BACKTEST_MIN_INTERVAL):
        return ltf_interval
    return _BACKTEST_MIN_INTERVAL


def get_provider(asset_type: AssetType, exchange: CryptoExchange = "mexc") -> DataProvider:
    if asset_type != "crypto":
        return twelvedata_provider
    if exchange == "binance":
        return binance_provider
    if exchange == "kucoin":
        return kucoin_provider
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
    if asset_type != "crypto":
        return symbol.strip().upper()
    if exchange == "binance":
        return format_binance_symbol(symbol)
    if exchange == "kucoin":
        return format_kucoin_symbol(symbol)
    return format_mexc_symbol(symbol)


async def fetch_ohlcv_canonical(provider: DataProvider, symbol: str, interval: str, outputsize: int):
    """Like provider.fetch_ohlcv, but transparently synthesizes intervals no
    provider natively supports (2D/3D/5D) by fetching extra 1D candles and
    folding them down — every call site gets these for free."""
    multiple = SYNTHETIC_DAY_INTERVALS.get(interval)
    if multiple is None:
        return await provider.fetch_ohlcv(symbol, interval, outputsize)
    raw = await provider.fetch_ohlcv(symbol, "1d", outputsize * multiple)
    return resample_by_count(raw, multiple).tail(outputsize)


async def _fetch_cached(key: tuple, provider: DataProvider, symbol: str, interval: str, outputsize: int):
    """Shared helper: wraps fetch_ohlcv_canonical + get_or_fetch so each
    route doesn't need to declare its own inner do_fetch closure."""
    async def _do():
        return await fetch_ohlcv_canonical(provider, symbol, interval, outputsize)
    return await get_or_fetch(key, _do)



async def _watchlist_alert_loop() -> None:
    """Background loop, independent of any browser tab: evaluates every
    watchlist ticker against each of its enabled strategies and fires
    Telegram alerts — never for whatever symbol happens to be open on
    screen. One fetch per (ticker, timeframe), reused across every strategy
    enabled on that ticker rather than refetching per strategy."""
    while True:
        await asyncio.sleep(WATCHLIST_POLL_SECONDS)
        if not settings.telegram_bot_token:
            continue
        for entry in watchlist_store.list_entries():
            if not entry.strategies:
                continue
            owner = auth.get_user(entry.owner_id)
            if owner is None or not owner.telegram_chat_id or not _telegram_allowed(owner):
                continue  # no chat_id yet, or this account isn't on the Telegram delivery allowlist
            provider = get_provider(entry.asset_type, entry.exchange)
            try:
                df = await fetch_ohlcv_canonical(
                    provider, entry.symbol, entry.interval, settings.candles_per_timeframe
                )
                if entry.on_close:
                    df = drop_unclosed_candle(df, entry.interval)
                if df.empty:
                    continue
                for strategy_name in entry.strategies:
                    params = entry.strategy_params.get(strategy_name) or None
                    strategy = build_strategy_state(strategy_name, df, params=params)
                    _dispatch_telegram_alerts(owner.telegram_chat_id, entry.symbol, entry.interval, strategy)
                    # Auto-execute orders if trading credentials are configured for this exchange
                    cred = owner.trading_credentials.get(entry.exchange)
                    if cred and cred.enabled:
                        api_key, api_secret, passphrase = auth.get_decrypted_credential(owner, entry.exchange)
                        if api_key and api_secret:
                            fresh = find_new_signals(entry.symbol, entry.interval, strategy)
                            for signal in fresh:
                                try:
                                    result = await place_order_for_signal(
                                        entry.exchange, entry.symbol, signal,
                                        cred.size_mode, cred.size_value,
                                        api_key, api_secret, passphrase,
                                    )
                                    logger.info("Order placed for %s %s: %s", entry.symbol, signal.type, result)
                                except Exception as exc:
                                    logger.error("Order failed for %s %s: %s", entry.symbol, signal.type, exc)
                                    if owner.telegram_chat_id and _telegram_allowed(owner):
                                        asyncio.create_task(telegram.send_to(
                                            owner.telegram_chat_id,
                                            f"⚠️ Ordine FALLITO <b>{entry.symbol}</b> [{signal.type}]: {exc}",
                                        ))
            except Exception as exc:  # noqa: BLE001
                logger.error("Alert loop error for %s: %s", entry.symbol, exc, exc_info=True)
                if settings.telegram_bot_token and owner.telegram_chat_id and _telegram_allowed(owner):
                    asyncio.create_task(
                        telegram.send_to(
                            owner.telegram_chat_id,
                            f"⚠️ Errore monitoraggio <b>{entry.symbol}</b> [{entry.interval}]: {exc}",
                        )
                    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.sniper import cache as sniper_cache
    from app.sniper import agents as sniper_agents
    from app.sniper.cex_check import enrichment_loop
    t1 = asyncio.create_task(_watchlist_alert_loop())
    t2 = asyncio.create_task(sniper_cache.polling_loop())
    t3 = asyncio.create_task(enrichment_loop())
    agent_tasks = await sniper_agents.start_all()
    yield
    t1.cancel()
    t2.cancel()
    t3.cancel()
    for t in agent_tasks:
        t.cancel()


app = FastAPI(title="Liquidity Analysis Engine", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)

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


class BacktestParamsQuery(BaseModel):
    initial_capital: float = 10000.0
    qty_pct_of_equity: float = 30.0
    commission_pct: float = 0.06
    slippage_pct: float = 0.02
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0


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


async def _safe_search(
    provider: DataProvider, query: str, asset_type: AssetType, provider_id: str
) -> list[SymbolSuggestion]:
    try:
        results = await provider.search_symbols(query)
    except Exception:  # noqa: BLE001 — any provider failure must not sink combined search
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
    stock_res, mexc_res, kucoin_res = await asyncio.gather(
        _safe_search(twelvedata_provider, query, "stock", "stock"),
        _safe_search(mexc_provider, query, "crypto", "mexc"),
        _safe_search(kucoin_provider, query, "crypto", "kucoin"),
    )
    merged = _interleave(stock_res, mexc_res, kucoin_res)
    return _rank_suggestions(merged, query)[:25]


def _dispatch_telegram_alerts(chat_id: str, symbol: str, timeframe_key: str, strategy: StrategyState) -> None:
    fresh = find_new_signals(symbol, timeframe_key, strategy)
    for signal in fresh:
        message = format_signal_message(symbol, timeframe_key, strategy.name, signal)
        asyncio.create_task(telegram.send_to(chat_id, message))


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


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TelegramChatIdRequest(BaseModel):
    telegram_chat_id: str


def _user_out(user: User) -> dict:
    return {"id": user.id, "email": user.email, "telegram_chat_id": user.telegram_chat_id}


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
    for exchange in ("binance", "mexc", "kucoin"):
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
    from app.models import StrategySignal
    signal_type = "entry_long" if body.side == "BUY" else "entry_short"
    fake_signal = StrategySignal(time=int(_time.time()), type=signal_type, price=0.0, label="manual")
    try:
        result = await place_order_for_signal(
            body.exchange, body.symbol, fake_signal,
            body.size_mode, body.size_value,
            api_key, api_secret, passphrase,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "result": result}


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


@app.get("/api/watchlist", response_model=list[WatchlistEntry])
async def get_watchlist(user: User = Depends(auth.get_current_user)) -> list[WatchlistEntry]:
    return watchlist_store.list_entries(owner_id=user.id)


class WatchlistAddRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    interval: IntervalChoice = "1d"
    on_close: bool = True


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
        user.id, symbol, req.asset_type, exchange=req.exchange, interval=req.interval, on_close=req.on_close
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
    symbol: str = Query(..., min_length=1, max_length=20),
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
            candles = candles[-2:]
            indicators = [ind.model_copy(update={"values": ind.values[-2:]}) for ind in indicators]
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


@app.get("/api/backtest/deep", response_model=BacktestResult)
async def deep_backtest(
    symbol: str = Query(..., min_length=1, max_length=20),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    strategy_params: StrategyParamsQuery = Depends(),
    backtest_params: BacktestParamsQuery = Depends(),
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
    cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest", backtest_interval, settings.backtest_candles)

    try:
        df = await _fetch_cached(cache_key, provider, symbol, backtest_interval, settings.backtest_candles)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol}")

    result = run_backtest(df, strategy_params.model_dump(), backtest_params.model_dump(), strategy_name=strategy_name)
    return result.model_copy(update={"interval": backtest_interval})


class OptimizeResult(BaseModel):
    params: dict
    backtest: BacktestResult
    trials_evaluated: int
    out_of_sample_return_pct: float | None = None
    overfitted: bool = False


@app.get("/api/optimize", response_model=OptimizeResult)
async def optimize_strategy_endpoint(
    symbol: str = Query(..., min_length=1, max_length=20),
    asset_type: AssetType = Query("stock"),
    exchange: CryptoExchange = Query("mexc"),
    ltf_interval: IntervalChoice = Query(DEFAULT_INTERVALS["ltf"]),
    strategy_name: str = Query(DEFAULT_STRATEGY),
    backtest_params: BacktestParamsQuery = Depends(),
) -> OptimizeResult:
    """Random-search parameter tuning for any strategy in the registry —
    same deep-history df the backtest itself uses (see
    backtest_interval_for), so results here match what re-running the
    backtest with the winning params will show. CPU-bound (~120 backtest
    simulations, ~15-25s), so it runs off the event loop via a thread —
    otherwise it would stall every other request (including other users')
    for its whole duration."""
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Ticker mancante")
    if not is_optimizable(strategy_name):
        raise HTTPException(status_code=400, detail=f"Ottimizzazione non disponibile per {strategy_name}")

    provider = get_provider(asset_type, exchange)
    backtest_interval = backtest_interval_for(ltf_interval)
    cache_key = (f"{asset_type}:{exchange}:{symbol.upper()}:backtest", backtest_interval, settings.backtest_candles)

    try:
        df = await _fetch_cached(cache_key, provider, symbol, backtest_interval, settings.backtest_candles)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if df.empty:
        raise HTTPException(status_code=502, detail=f"Nessun dato per {symbol}")

    best_params, best_bt, trials, oos_return, overfitted = await asyncio.to_thread(
        optimize_strategy, strategy_name, df, backtest_params.model_dump()
    )
    best_bt = best_bt.model_copy(update={"interval": backtest_interval})
    return OptimizeResult(
        params=best_params,
        backtest=best_bt,
        trials_evaluated=trials,
        out_of_sample_return_pct=oos_return,
        overfitted=overfitted,
    )


@app.get("/api/sniper/candidates")
async def sniper_candidates() -> dict:
    from app.sniper import cache as sniper_cache
    candidates = sniper_cache.get_candidates()
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
            }
            for c in candidates
        ],
        "last_scan": sniper_cache.last_scan_ts(),
        "count": len(candidates),
    }


@app.post("/api/sniper/scan")
async def sniper_trigger_scan() -> dict:
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
    from app.sniper import market as sniper_market_mod

    added = await sniper_market_mod.run_market_scan()
    return {"ok": True, "added": added}


@app.post("/api/sniper/market-import")
async def sniper_market_import() -> dict:
    """Apply the sniper's primary filter + on-chain scoring to every token
    currently in the market store and add qualifying ones to the sniper
    candidate archive. Progress is streamed via /api/sniper/market-stream."""
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

    intervals = {
        "htf": params.get("htf_interval", DEFAULT_INTERVALS["htf"]),
        "mtf": params.get("mtf_interval", DEFAULT_INTERVALS["mtf"]),
        "ltf": params.get("ltf_interval", DEFAULT_INTERVALS["ltf"]),
    }

    await websocket.accept()
    await mexc_stream_manager.add_client(websocket, symbol, intervals)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await mexc_stream_manager.remove_client(websocket)


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")


def _compute_asset_version() -> str:
    """Hash of the actual CSS/JS bytes being served, not the git commit —
    correct even in the Docker image, which doesn't ship .git. Computed once
    at process startup: every deploy restarts the process, so the version
    (and therefore the /static/*.css?v=... URL) always changes exactly when
    the files do. Without this, browsers — mobile Safari especially — keep
    serving a stale cached style.css/app.js after a deploy since neither the
    URL nor any cache-control header ever changed."""
    try:
        css = (FRONTEND_DIR / "static" / "style.css").read_bytes()
        js = (FRONTEND_DIR / "static" / "app.js").read_bytes()
        return hashlib.sha256(css + js).hexdigest()[:10]
    except OSError:
        return "0"


_ASSET_VERSION = _compute_asset_version()
_INDEX_HTML = (
    (FRONTEND_DIR / "index.html")
    .read_text(encoding="utf-8")
    .replace('href="/static/style.css"', f'href="/static/style.css?v={_ASSET_VERSION}"')
    .replace('src="/static/app.js"', f'src="/static/app.js?v={_ASSET_VERSION}"')
)


_SNIPER_HTML = (
    (FRONTEND_DIR / "sniper.html")
    .read_text(encoding="utf-8")
    .replace('href="/static/style.css"', f'href="/static/style.css?v={_ASSET_VERSION}"')
)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.get("/sniper")
async def sniper_app() -> HTMLResponse:
    return HTMLResponse(_SNIPER_HTML)
