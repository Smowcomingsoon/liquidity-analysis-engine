from typing import Literal

from pydantic import BaseModel

from app.timeframes import IntervalChoice

AssetType = Literal["stock", "crypto"]
CryptoExchange = Literal["mexc", "binance", "kucoin"]
TimeframeKey = Literal["htf", "mtf", "ltf"]
LevelType = Literal["order_block", "fvg", "eqh", "eql", "bos", "choch"]


class SymbolSuggestion(BaseModel):
    symbol: str
    name: str
    exchange: str  # human label, e.g. "NASDAQ", "MEXC (USDT)", "Binance (USDT)"
    asset_type: AssetType
    provider: Literal["stock", "mexc", "binance", "kucoin"]


class Candle(BaseModel):
    time: int  # unix seconds, required by Lightweight Charts
    open: float
    high: float
    low: float
    close: float
    volume: float


class LiquidityLevel(BaseModel):
    type: LevelType
    direction: Literal["bullish", "bearish"]
    price_high: float
    price_low: float
    start_time: int
    end_time: int | None = None
    mitigated: bool = False
    label: str


class IndicatorSeries(BaseModel):
    name: str
    values: list[float | None]


class FibZone(BaseModel):
    kind: Literal["retracement", "extension"]
    direction: Literal["bullish", "bearish"]
    anchor_time: int  # where the level lines start being drawn from (leg start for a retracement, the C pivot for an extension)
    levels: dict[str, float]  # ratio (as string, e.g. "0.618") -> price
    label: str


class TimeframeAnalysis(BaseModel):
    interval: str
    candles: list[Candle]
    levels: list[LiquidityLevel]
    indicators: list[IndicatorSeries]
    bias: Literal["bullish", "bearish", "neutral"]
    dealing_range: tuple[float, float] | None = None
    fib_zones: list[FibZone] = []


class StrategySignal(BaseModel):
    time: int
    type: Literal["entry_long", "entry_short", "exit_long", "exit_short", "flip_to_long", "flip_to_short"]
    price: float
    label: str


class StrategyState(BaseModel):
    name: str
    position: Literal["long", "short", "flat"]
    signals: list[StrategySignal]


class BacktestTrade(BaseModel):
    direction: Literal["long", "short"]
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    pnl_pct: float
    pnl_abs: float
    equity_after: float


class BacktestResult(BaseModel):
    # The interval the simulation actually ran on — deliberately coarser
    # than the chart's own timeframe when that's finer than daily, so this
    # can differ from whatever TimeframeAnalysis.interval says for "ltf".
    interval: str
    initial_capital: float
    final_equity: float
    total_return_pct: float
    num_trades: int
    win_rate_pct: float | None
    max_drawdown_pct: float
    profit_factor: float | None
    equity_curve: list[tuple[int, float]]
    trades: list[BacktestTrade]
    data_start: int | None = None   # unix ms del primo candle usato
    data_end: int | None = None     # unix ms dell'ultimo candle usato
    sharpe_ratio: float | None = None
    max_consecutive_losses: int | None = None


class ExchangeCredential(BaseModel):
    api_key_enc: str = ""      # encrypted API key (empty = not configured)
    api_secret_enc: str = ""   # encrypted API secret
    passphrase_enc: str = ""   # encrypted passphrase (KuCoin only)
    enabled: bool = False      # whether to auto-execute orders on this exchange
    size_mode: Literal["fixed_usdt", "pct_balance"] = "fixed_usdt"
    size_value: float = 50.0   # USDT amount or percentage (0-100)


class User(BaseModel):
    id: str
    email: str
    password_hash: str
    telegram_chat_id: str = ""  # user pastes this in from their own DM with the shared bot
    trading_credentials: dict[str, ExchangeCredential] = {}  # key = exchange slug ("binance", "mexc", "kucoin")


class FavoriteEntry(BaseModel):
    id: str
    owner_id: str
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"


class WatchlistEntry(BaseModel):
    id: str
    owner_id: str
    symbol: str
    asset_type: AssetType
    exchange: CryptoExchange = "mexc"
    strategies: list[str] = []  # which strategies fire Telegram alerts for this ticker
    interval: IntervalChoice = "1d"  # which timeframe those strategies are evaluated on
    on_close: bool = True  # False = also evaluate the still-forming candle (faster, but a signal can vanish before the bar closes)
    strategy_params: dict[str, dict] = {}  # per-strategy parameter overrides, keyed by strategy name; missing/empty = use that strategy's defaults


class ConfluenceZone(BaseModel):
    price_high: float
    price_low: float
    timeframes: list[TimeframeKey]
    types: list[LevelType]
    score: int
    label: str


class AnalysisSummary(BaseModel):
    htf_bias: Literal["bullish", "bearish", "neutral"]
    text: str
    indicator_readout: list[str]
    long_term_context: list[str]
    top_confluence_zones: list[ConfluenceZone]
    unmitigated_gaps: int
    nearest_liquidity_above: float | None
    nearest_liquidity_below: float | None


class AnalysisResponse(BaseModel):
    symbol: str
    asset_type: AssetType
    current_price: float
    timeframes: dict[TimeframeKey, TimeframeAnalysis]
    confluence_zones: list[ConfluenceZone]
    summary: AnalysisSummary
    strategies: dict[TimeframeKey, list[StrategyState]]
    backtests: dict[TimeframeKey, BacktestResult]
    disclaimer: str = (
        "Strumento di analisi tecnica automatica. Non costituisce consulenza "
        "finanziaria, raccomandazione di investimento o segnale operativo."
    )
