"""
Parameter search for every strategy in the registry.

Scoring: Calmar × profit_factor_bonus × sharpe_bonus × trade_confidence,
with drawdown cap and min-trade floor scaled to the ticker's own volatility
(ATR-based) so the same thresholds are not applied blindly to BTC and a
meme-coin with 10× the daily range.

Search modes:
  random — 200-trial random search over the strategy's continuous space
  grid   — exhaustive search over a discrete grid (Gabriele, Exhaustion only)

Walk-forward: optimises on the first N-60 candles (guaranteeing at least 60
out-of-sample candles regardless of dataset length), validates on the rest.

Top-N: the search keeps the N highest-scoring in-sample candidates (min-heap),
then runs each of them through the OOS period so the caller can show a ranked
table. The seed is derived deterministically from (symbol, strategy, interval)
so the same query always produces the same ranking — results no longer change
between runs.
"""

import heapq
import itertools
import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from app.analysis.backtest import run_backtest
from app.analysis.cancel import stop_if_asked
from app.analysis.strategies import (
    DEFAULT_PARAMS,
    EXHAUSTION_DEFAULT_PARAMS,
    LKI2_DEFAULT_PARAMS,
    LKI_DEFAULT_PARAMS,
)
from app.models import BacktestResult

_TRIALS = 200
_MIN_OOS_CANDLES = 60
_TOP_N = 10


@dataclass(frozen=True)
class Constraints:
    """Vincoli scelti da chi cerca, applicati prima del punteggio.

    Sono un filtro, non un peso: chiedere "al massimo zero trade in perdita"
    vuol dire escludere le configurazioni che ne hanno, non preferire quelle
    che ne hanno pochi. Una preferenza qui verrebbe comunque sopraffatta dal
    Calmar, che varia di ordini di grandezza mentre il conteggio delle perdite
    varia di poche unita'.

    `None` significa "nessun vincolo" e va tenuto distinto da 0, che e' un
    vincolo legittimo e il piu' stringente: "nessun trade in perdita",
    "drawdown nullo" sono esattamente cio' che e' stato chiesto di poter
    selezionare.
    """

    min_losing_trades: int | None = None
    max_losing_trades: int | None = None
    max_drawdown_pct: float | None = None

    @property
    def active(self) -> bool:
        return any(v is not None for v in
                   (self.min_losing_trades, self.max_losing_trades, self.max_drawdown_pct))

    def accepts(self, bt: BacktestResult) -> bool:
        """Filtro riusabile: la stessa regola vale per ogni modalita' di ricerca."""
        return _passes(bt, self)

    def describe(self) -> str:
        parts = []
        if self.min_losing_trades is not None:
            parts.append(f"at least {self.min_losing_trades} losing trades")
        if self.max_losing_trades is not None:
            parts.append(f"at most {self.max_losing_trades} losing trades")
        if self.max_drawdown_pct is not None:
            parts.append(f"drawdown within {self.max_drawdown_pct:g}%")
        return ", ".join(parts)


_NO_CONSTRAINTS = Constraints()


class NoCandidateMeetsConstraints(Exception):
    """Nessuna configurazione rispetta i filtri richiesti.

    Sollevata invece di ripiegare sui parametri di default: con un filtro
    attivo, restituire una configurazione che lo viola darebbe una risposta
    che sembra valida e non lo e'. Meglio dire che con quei vincoli, su questa
    serie, non c'e' niente.
    """

    def __init__(self, constraints: "Constraints", evaluated: int, rejected: int):
        self.constraints = constraints
        self.evaluated = evaluated
        self.rejected = rejected
        super().__init__(
            f"No configuration meets the filters ({constraints.describe()}): "
            f"{rejected} of {evaluated} configurations excluded. "
            f"Try loosening the constraints."
        )


def losing_trades(bt: BacktestResult) -> int:
    """Quanti trade chiusi in perdita. Contati dai trade, non dal win rate:
    il win rate e' arrotondato e con pochi trade non permette di risalire al
    conteggio esatto, che qui invece serve esatto perche' il vincolo puo'
    essere 0."""
    return sum(1 for t in bt.trades if t.pnl_abs < 0)


def _passes(bt: BacktestResult, c: Constraints) -> bool:
    if c.max_drawdown_pct is not None and abs(bt.max_drawdown_pct) > c.max_drawdown_pct:
        return False
    if c.min_losing_trades is None and c.max_losing_trades is None:
        return True
    n = losing_trades(bt)
    if c.min_losing_trades is not None and n < c.min_losing_trades:
        return False
    if c.max_losing_trades is not None and n > c.max_losing_trades:
        return False
    return True


def _volatility_thresholds(volatility_pct: float) -> tuple[float, int]:
    """Return (max_drawdown_cap, min_trades) scaled to ticker volatility."""
    max_dd = 25.0 + min(volatility_pct * 4.0, 20.0)   # [25, 45]
    min_trades = max(6, 12 - int(volatility_pct))       # [6, 12]
    return max_dd, min_trades


# Gabriele with RSI 14, Bollinger 20 and 2 held fixed (GABRIELE_FIXED) needs a
# close under the 2-sigma band AND RSI oversold: on 1000 daily candles that is
# 0-9 trades whatever the thresholds (BTC, KAS, SOL 4h, measured 09/10), so the
# usual 6-12 floor rejected every candidate and the search returned nothing
# (segnalazione 6453f4df, "non produce risultati").
_MIN_TRADES_FLOOR = {"Gabriele (RSI+BB reversal)": 4, "Gabriele 2 (RSI+BB long/short)": 4}


def _score(bt: BacktestResult, volatility_pct: float = 2.0, min_trades: int | None = None) -> float:
    """Composite score: Calmar × PF bonus × Sharpe bonus × trade confidence."""
    max_dd_cap, vol_min_trades = _volatility_thresholds(volatility_pct)
    min_trades = vol_min_trades if min_trades is None else min_trades

    if bt.num_trades < min_trades:
        return float("-inf")
    if bt.total_return_pct <= 0:
        return float("-inf")
    if abs(bt.max_drawdown_pct) > max_dd_cap:
        return float("-inf")

    drawdown = max(abs(bt.max_drawdown_pct), 0.5)
    calmar = bt.total_return_pct / drawdown

    pf = bt.profit_factor if bt.profit_factor is not None else 1.0
    pf_bonus = min(pf, 5.0) / 5.0  # [0, 1]

    sharpe = bt.sharpe_ratio if bt.sharpe_ratio is not None else 0.0
    sharpe_bonus = min(max(sharpe / 3.0, 0.0), 1.0)  # [0, 1]

    trade_conf = math.log(bt.num_trades + 1) / math.log(min_trades + 1)

    return calmar * (0.55 + 0.20 * pf_bonus + 0.15 * sharpe_bonus + 0.10 * trade_conf)


def _random_lorenzo_params(rng: random.Random, base: dict | None = None) -> dict:
    slope_fast = rng.randint(2, 30)
    slope_slow = rng.randint(slope_fast + 3, 80)
    return {
        "average": rng.choice(["EMA", "SMA"]),
        "len": rng.randint(20, 300),
        "slopeFlen": slope_fast,
        "slopeSlen": slope_slow,
        "trendfilter": rng.choice([True, False]),
        "trendfilterperiod": rng.randint(50, 400),
        "trendfiltertype": rng.choice(["EMA", "SMA"]),
        "volatilityfilter": rng.choice([True, False]),
        "volatilitystdevlength": rng.randint(5, 40),
        "volatilitystdevmalength": rng.randint(10, 80),
    }


def _random_exhaustion_params(rng: random.Random, base: dict | None = None) -> dict:
    rsi_oversold = rng.randint(20, 45)
    rsi_overbought = rng.randint(55, 80)
    return {
        "swing_length": rng.randint(3, 20),
        "rsi_length": rng.randint(7, 25),
        "rsi_oversold": rsi_oversold,
        "rsi_overbought": rsi_overbought,
        "rsi_min_gap": round(rng.uniform(1.0, 10.0), 2),
        "low_tolerance_pct": round(rng.uniform(0.2, 3.0), 2),
        "divergence_lookback": rng.randint(2, 8),
        "volume_ma_length": rng.randint(10, 40),
        "volume_multiplier": round(rng.uniform(1.1, 2.5), 2),
        "trend_filter": rng.choice([True, False]),
        "trend_ema_length": rng.randint(20, 100),
        "confirmation_lookahead": rng.randint(3, 20),
        "contraction_lookback": rng.randint(5, 20),
        "contraction_prior_lookback": rng.randint(20, 60),
        "contraction_ratio": round(rng.uniform(0.3, 0.8), 2),
        "range_lookback": rng.randint(20, 80),
        "range_zone_pct": round(rng.uniform(0.1, 0.4), 2),
        "enable_divergence": True,
        "enable_contraction": rng.choice([True, False]),
        "enable_range_bounce": rng.choice([True, False]),
    }


# Segnalazioni 72f880af e 6453f4df (whitekobra): l'ottimizzazione di
# Gabriele tiene sempre lunghezza RSI 14, lunghezza Bollinger 20 e
# moltiplicatore 2, qualunque valore ci sia nei campi del Lab, e cerca solo
# RSI ipercomprato/ipervenduto.
GABRIELE_FIXED = {"rsi_length": 14, "bb_length": 20, "bb_mult": 2.0}
GABRIELE_STRATEGIES = ("Gabriele (RSI+BB reversal)", "Gabriele 2 (RSI+BB long/short)")


def _random_gabriele_params(rng: random.Random, base: dict | None = None) -> dict:
    """Solo RSI ipercomprato/ipervenduto sono cercati; gli altri tre sono
    sempre GABRIELE_FIXED."""
    return {
        **GABRIELE_FIXED,
        "rsi_oversold": rng.randint(15, 50),
        "rsi_overbought": rng.randint(50, 85),
    }


_STRATEGY_SPACES: dict[str, tuple[dict, callable]] = {
    "Lorenzo (trend-follower)": (DEFAULT_PARAMS, _random_lorenzo_params),
    "Accumulo / Distribuzione (long-term)": (EXHAUSTION_DEFAULT_PARAMS, _random_exhaustion_params),
    "Gabriele (RSI+BB reversal)": (LKI_DEFAULT_PARAMS, _random_gabriele_params),
    "Gabriele 2 (RSI+BB long/short)": (LKI2_DEFAULT_PARAMS, _random_gabriele_params),
}

_GRID_GABRIELE = {
    "rsi_oversold":   [20, 25, 30, 35, 40, 45, 50],
    "rsi_overbought": [50, 55, 60, 65, 70, 75, 80, 85],
}  # 7 x 8 = 56 combinations — lunghezza RSI/Bollinger e moltiplicatore sono
   # sempre GABRIELE_FIXED, non una dimensione della griglia

_GRID_EXHAUSTION = {
    "swing_length":        [4, 8, 12],
    "rsi_length":          [10, 14, 20],
    "rsi_oversold":        [25, 35, 45],
    "rsi_overbought":      [55, 65, 75],
    "rsi_min_gap":         [2.0, 5.0, 8.0],
    "trend_filter":        [True, False],
    "enable_contraction":  [True, False],
    "enable_range_bounce": [True, False],
}  # 3^5 × 2^3 = 1 944 combinations

_GRID_SPACES: dict[str, dict] = {
    "Gabriele (RSI+BB reversal)": _GRID_GABRIELE,
    "Gabriele 2 (RSI+BB long/short)": _GRID_GABRIELE,
    "Accumulo / Distribuzione (long-term)": _GRID_EXHAUSTION,
}


def _grid_candidates(strategy_name: str) -> Iterator[dict]:
    grid = _GRID_SPACES.get(strategy_name)
    if grid is None:
        return
    keys = list(grid.keys())
    for values in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, values))


def grid_size(strategy_name: str) -> int:
    grid = _GRID_SPACES.get(strategy_name)
    if grid is None:
        return 0
    total = 1
    for vals in grid.values():
        total *= len(vals)
    return total


def is_optimizable(strategy_name: str) -> bool:
    return strategy_name in _STRATEGY_SPACES


def _calc_volatility(df) -> float:
    """ATR(14) / avg_close × 100 as a percentage."""
    try:
        atr = float((df["high"] - df["low"]).rolling(14).mean().iloc[-1])
        avg_close = float(df["close"].mean())
        if math.isfinite(atr) and avg_close > 0:
            result = (atr / avg_close) * 100
            if math.isfinite(result):
                return result
    except Exception:
        pass
    return 2.0


def _oos_validate(
    params: dict,
    df_out,
    backtest_params: dict,
    strategy_name: str,
    in_sample_return: float,
) -> tuple[float | None, bool, list]:
    """Run OOS backtest for a candidate; return (oos_return, overfitted, oos_trades)."""
    if len(df_out) == 0:
        return None, False, []
    oos_bt = run_backtest(df_out, params, backtest_params, strategy_name=strategy_name)
    oos_return = oos_bt.total_return_pct
    oos_trades = oos_bt.trades
    if in_sample_return > 0:
        degradation = (in_sample_return - oos_return) / abs(in_sample_return)
        overfitted = degradation > 0.50
    else:
        overfitted = oos_return < in_sample_return
    return oos_return, overfitted, oos_trades


def optimize_strategy(
    strategy_name: str,
    df,
    backtest_params: dict,
    trials: int = _TRIALS,
    seed: int | None = None,
    mode: str = "random",
    top_n: int = _TOP_N,
    constraints: Constraints | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    base_params: dict | None = None,
) -> tuple[dict, BacktestResult, int, float | None, bool, list[dict]]:
    """Search for the best parameters and return the top-N candidates.

    Returns:
        (best_params, best_bt, trials_evaluated, oos_return, overfitted, top_results)

    top_results is a list of dicts (length ≤ top_n), sorted by IS score descending:
        {rank, params, backtest, score, out_of_sample_return_pct, overfitted}

    The seed should be set deterministically by the caller (hash of symbol +
    strategy + interval) so repeated queries return a consistent ranking.
    """
    if strategy_name not in _STRATEGY_SPACES:
        raise ValueError(f"No parameter space defined for {strategy_name!r}")
    default_params, random_params_fn = _STRATEGY_SPACES[strategy_name]
    # I valori attualmente nei campi di chi cerca sostituiscono i default per
    # ogni parametro che la ricerca di questa strategia non genera da sola
    # (per Gabriele: tutto tranne RSI ipercomprato/ipervenduto).
    if base_params:
        default_params = {**default_params, **base_params}
    if strategy_name in GABRIELE_STRATEGIES:
        default_params = {**default_params, **GABRIELE_FIXED}

    rng = random.Random(seed)
    constraints = constraints or _NO_CONSTRAINTS
    # Le righe di avanzamento sono a scatti fissi e non a ogni tentativo: un
    # log che scorre piu' veloce di quanto si legga non dice piu' di uno lento,
    # e con 3000 valutazioni sarebbe solo peso.
    say = progress or (lambda _m: None)
    ticker_volatility = _calc_volatility(df)

    oos_candles = max(int(len(df) * 0.30), _MIN_OOS_CANDLES)
    split = len(df) - oos_candles
    df_in = df.iloc[:split]
    df_out = df.iloc[split:]

    # Min-heap of (score, counter, params, bt) — keeps top_n best by score.
    # counter breaks ties so BacktestResult objects are never compared.
    heap: list[tuple[float, int, dict, BacktestResult]] = []
    counter = 0
    evaluated = 0

    # Distinct-by-effect: parameters that do not bind (an rsi_overbought of 80
    # vs 85 that never triggers) produce byte-identical trade sequences. Without
    # this the shortlist fills with ten copies of one configuration, which is
    # exactly the information the shortlist exists to avoid hiding.
    seen_fingerprints: set[tuple] = set()
    rejected_by_constraints = 0

    def _fingerprint(bt: BacktestResult) -> tuple:
        return (
            round(bt.total_return_pct, 4),
            bt.num_trades,
            round(bt.max_drawdown_pct, 4),
        )

    # Runners-up kept only for the case where nothing passes the score filters
    # (positive return, trade floor, drawdown cap): an empty board read as "the
    # optimizer is broken" (segnalazione e0bffe25). Ranked by return, at least
    # one trade, and announced as not usable. RSI+BB only, where the locked
    # 14/20/2 makes an empty board common.
    fallback: list = []
    fallback_seen: set = set()

    def _consider(params: dict, bt: BacktestResult) -> None:
        nonlocal counter, rejected_by_constraints
        # Prima del punteggio: un candidato che viola i vincoli non deve poter
        # entrare nella classifica per quanto alto sia il suo Calmar.
        if not _passes(bt, constraints):
            rejected_by_constraints += 1
            return
        score = _score(bt, ticker_volatility, _MIN_TRADES_FLOOR.get(strategy_name))
        if score == float("-inf"):
            fp = _fingerprint(bt)
            if strategy_name in GABRIELE_STRATEGIES and bt.num_trades >= 1 and fp not in fallback_seen:
                fallback_seen.add(fp)
                counter += 1
                entry = (bt.total_return_pct, counter, params, bt)
                if len(fallback) < top_n:
                    heapq.heappush(fallback, entry)
                elif entry[0] > fallback[0][0]:
                    heapq.heapreplace(fallback, entry)
            return
        fp = _fingerprint(bt)
        if fp in seen_fingerprints:
            return
        seen_fingerprints.add(fp)
        counter += 1
        entry = (score, counter, params, bt)
        if len(heap) < top_n:
            heapq.heappush(heap, entry)
        elif score > heap[0][0]:
            heapq.heapreplace(heap, entry)

    say(f"{len(df_in)} in-sample candles, {len(df_out)} out-of-sample")
    if constraints.active:
        say(f"active filters: {constraints.describe()}")

    # Always evaluate defaults first
    default_bt = run_backtest(df_in, default_params, backtest_params, strategy_name=strategy_name)
    _consider(dict(default_params), default_bt)
    evaluated += 1

    use_grid = mode == "grid" and strategy_name in _GRID_SPACES
    if use_grid:
        total = grid_size(strategy_name)
        say(f"full grid: {total} combinations")
        step = max(1, total // 20)
        for candidate_delta in _grid_candidates(strategy_name):
            stop_if_asked(should_stop)
            candidate = {**default_params, **candidate_delta}
            bt = run_backtest(df_in, candidate, backtest_params, strategy_name=strategy_name)
            evaluated += 1
            _consider(candidate, bt)
            if evaluated % step == 0:
                say(f"{evaluated}/{total} — best on the board: {len(heap)}")
    else:
        say(f"random search: {trials} trials")
        step = max(1, trials // 20)
        for _ in range(trials):
            stop_if_asked(should_stop)
            candidate = random_params_fn(rng, default_params)
            bt = run_backtest(df_in, candidate, backtest_params, strategy_name=strategy_name)
            evaluated += 1
            _consider(candidate, bt)
            if evaluated % step == 0:
                say(f"{evaluated}/{trials} — best on the board: {len(heap)}")

    if rejected_by_constraints:
        say(f"{rejected_by_constraints} configurations excluded by the filters")

    if not heap and constraints.active and rejected_by_constraints:
        raise NoCandidateMeetsConstraints(constraints, evaluated, rejected_by_constraints)

    if not heap and fallback:
        say("no configuration passed the filters (positive return, "
            f"{_MIN_TRADES_FLOOR.get(strategy_name) or 'enough'} trades, drawdown cap): "
            "showing the best by return — exploratory, not a signal")
        heap = fallback

    if not heap:
        # No candidate passed the score filters — fall back to defaults with OOS
        best_bt = default_bt.model_copy(update={
            "ticker_volatility_pct": round(ticker_volatility, 2) if math.isfinite(ticker_volatility) else None,
            "oos_trades": [],
        })
        return dict(default_params), best_bt, evaluated, None, False, []

    # Sort descending by score
    top_candidates = sorted(heap, key=lambda x: x[0], reverse=True)

    say(f"{len(heap)} distinct candidates: out-of-sample check")

    # Run OOS for every top candidate
    top_results: list[dict] = []
    for rank, (score, _, params, bt) in enumerate(top_candidates, start=1):
        oos_return, overfitted, oos_trades = _oos_validate(
            params, df_out, backtest_params, strategy_name, bt.total_return_pct
        )
        top_results.append({
            "rank": rank,
            "params": params,
            "backtest": bt,
            "score": round(score, 4),
            "out_of_sample_return_pct": oos_return,
            "overfitted": overfitted,
        })

    # Best = top_results[0]
    best = top_results[0]
    best_params = best["params"]
    best_bt_raw = best["backtest"]
    best_oos = best["out_of_sample_return_pct"]
    best_overfitted = best["overfitted"]

    # OOS trades for the winner, so the UI can show them in the trades drawer
    if len(df_out) > 0:
        best_oos_trades = run_backtest(
            df_out, best_params, backtest_params, strategy_name=strategy_name
        ).trades
    else:
        best_oos_trades = []

    vol_pct = round(ticker_volatility, 2) if math.isfinite(ticker_volatility) else None
    best_bt = best_bt_raw.model_copy(update={
        "ticker_volatility_pct": vol_pct,
        "oos_trades": best_oos_trades,
    })

    return best_params, best_bt, evaluated, best_oos, best_overfitted, top_results
