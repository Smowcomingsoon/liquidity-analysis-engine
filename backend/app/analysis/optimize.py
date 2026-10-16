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
    LKI4H_DEFAULT_PARAMS,
    RSI_BB_4H,
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
_MIN_TRADES_FLOOR = {"Gabriele (RSI+BB reversal)": 4, "Gabriele 2 (RSI+BB long/short)": 4, RSI_BB_4H: 4}


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


# Segnalazioni 72f880af e 6453f4df (tester): l'ottimizzazione di
# Gabriele tiene sempre lunghezza RSI 14, lunghezza Bollinger 20 e
# moltiplicatore 2, qualunque valore ci sia nei campi del Lab, e cerca solo
# RSI ipercomprato/ipervenduto.
GABRIELE_FIXED = {"rsi_length": 14, "bb_length": 20, "bb_mult": 2.0}
GABRIELE_STRATEGIES = ("Gabriele (RSI+BB reversal)", "Gabriele 2 (RSI+BB long/short)", RSI_BB_4H)


def gabriele_fixed(strategy_name: str) -> dict:
    """The three values the RSI+BB searches never move: 14/20/2.0, and the
    multiplier at 2.5 for RSI+BB 4H."""
    if strategy_name == RSI_BB_4H:
        return {**GABRIELE_FIXED, "bb_mult": LKI4H_DEFAULT_PARAMS["bb_mult"]}
    return dict(GABRIELE_FIXED)


def _random_gabriele_params(rng: random.Random, base: dict | None = None) -> dict:
    """Solo RSI ipercomprato/ipervenduto sono cercati; gli altri tre sono
    sempre GABRIELE_FIXED."""
    return {
        **GABRIELE_FIXED,
        "rsi_oversold": rng.randint(15, 50),
        "rsi_overbought": rng.randint(50, 85),
    }


# Kept on every candidate the searches build (see optimize_strategy and
# pipeline.run_pipeline): the user's switch, not a dimension of the search.
STICKY_PARAMS = ("btc_filter", "btc_filter_exit")

_STRATEGY_SPACES: dict[str, tuple[dict, callable]] = {
    "Lorenzo (trend-follower)": (DEFAULT_PARAMS, _random_lorenzo_params),
    "Lorenzo reversal (long only)": (DEFAULT_PARAMS, _random_lorenzo_params),
    "Accumulo / Distribuzione (long-term)": (EXHAUSTION_DEFAULT_PARAMS, _random_exhaustion_params),
    "Gabriele (RSI+BB reversal)": (LKI_DEFAULT_PARAMS, _random_gabriele_params),
    "Gabriele 2 (RSI+BB long/short)": (LKI2_DEFAULT_PARAMS, _random_gabriele_params),
    RSI_BB_4H: (LKI4H_DEFAULT_PARAMS, _random_gabriele_params),
}

# Every whole value, not steps of 5: with only two dimensions the full grid
# is 36 x 36 = 1 296 backtests, and a coarser one is beaten by hand by anyone
# who tries 32/68 (5571a9f1: "changing the two values by hand I got better
# results than the optimization in every mode"). Same range as the random
# search, which used to reach 15-19 while the grid started at 20. RSI and
# Bollinger lengths and the multiplier are always GABRIELE_FIXED, not a
# dimension of the grid.
_GRID_GABRIELE = {
    "rsi_oversold":   list(range(15, 51)),
    "rsi_overbought": list(range(50, 86)),
}

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
    RSI_BB_4H: _GRID_GABRIELE,
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
        default_params = {**default_params, **gabriele_fixed(strategy_name)}

    # Switches the search never changes but every candidate must carry: the
    # random draws build fresh dicts and would drop them (BTC filter, 15/10).
    sticky = {k: base_params[k] for k in STICKY_PARAMS if base_params and k in base_params}

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
            if strategy_name in GABRIELE_STRATEGIES:   # 2.5 for RSI+BB 4H
                candidate = {**candidate, **gabriele_fixed(strategy_name)}
            candidate = {**candidate, **sticky}
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


# ── Max return on the whole chart ───────────────────────────────────────────
# Lorenzo, 12/10: "add a mode that only looks for the maximum return on the
# whole chart [...] oversold only from 10 to 40, overbought only from 60 to
# 90 [...] every value crossed with the other, one by one [...] picking the
# best performance with a maximum allowed drawdown of 60%".
#
# Declared for what it is: no out-of-sample slice, no composite score. It
# answers "which pair of values made the most on this history", the question
# a value tried by hand on the chart answers; it says nothing about the next
# candles. The other modes stay for that.
#
# Lorenzo, 15/10: "Imposta la riduzione del range delle RSI che abbiamo
# adottato nella strategia 4H anche alle altre due strategie": the same
# 20-40 x 65-85 as RSI+BB 4H (441 backtests instead of 961). Only the ranges:
# the yearly-trade rule of these two stays at one a year.
MAX_RETURN_OVERSOLD = range(20, 41)      # 20, 21, ..., 40
MAX_RETURN_OVERBOUGHT = range(65, 86)    # 65, 66, ..., 85
MAX_RETURN_DRAWDOWN_CAP = 60.0           # percent


MAX_RETURN_REFINE = 20                   # pairs re-run with in-candle fills
MAX_RETURN_TOP = 2                       # Lorenzo, 13/10: "i due migliori risultati"
def calendar_years(index) -> list[int]:
    """The calendar years in which a trade is required: every year of the
    series except the first and the last, which are always partial.

    Lorenzo, 13/10: "eliminare tutti i risultati che non ottengono almeno un
    trade per anno solare testato", then "il primo e l'ultimo anno vengono
    ignorati completamente dal filtro [...] dal 2022 al 2026 [...] solamente
    gli anni 2023, 2024 e 2025". A series inside one or two years has none."""
    import pandas as pd
    if len(index) == 0:
        return []
    idx = pd.DatetimeIndex(index)
    return list(range(idx[0].year + 1, idx[-1].year))


def years_without_trades(bt: BacktestResult, years: list[int]) -> list[int]:
    """The required years in which no trade was opened (by entry time, UTC)."""
    from datetime import datetime, timezone
    traded = {datetime.fromtimestamp(t.entry_time, timezone.utc).year for t in bt.trades}
    return [y for y in years if y not in traded]


def trades_in_years(bt: BacktestResult, years: list[int]) -> int:
    """Trades opened (UTC) inside the judged years only."""
    from datetime import datetime, timezone
    wanted = set(years)
    return sum(1 for t in bt.trades if datetime.fromtimestamp(t.entry_time, timezone.utc).year in wanted)


def max_return_size() -> int:
    return len(MAX_RETURN_OVERSOLD) * len(MAX_RETURN_OVERBOUGHT)


def ranked_return(bt: BacktestResult) -> float:
    """What Max return ranks by: the SIMPLE return (Lorenzo, 15/10: "Max
    Return deve scegliere il migliore in base al rendimento semplice"), every
    trade on the same stake, not the compounded one — on reversal strategies
    the gains are not necessarily all put back into the next trade. The
    drawdown cap stays on the compounded drawdown."""
    return bt.simple_return_pct if bt.simple_return_pct is not None else bt.total_return_pct


def max_return_search(
    strategy_name: str,
    backtest: Callable[[dict], BacktestResult],
    constraints: Constraints | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    top_n: int = MAX_RETURN_TOP,
    years: list[int] | None = None,
    fast_backtest: Callable[[dict], BacktestResult] | None = None,
    refine: int = MAX_RETURN_REFINE,
) -> tuple[dict, BacktestResult, int, list[dict]]:
    """Every (oversold, overbought) pair, whole history, highest return wins.

    `backtest(params)` is the caller's backtest on the whole series — the same
    one Run computes — so the winner's figure is exactly what the chart will
    show. RSI and Bollinger lengths and the multiplier stay GABRIELE_FIXED.
    A configuration needs at least one trade and a drawdown within
    MAX_RETURN_DRAWDOWN_CAP; the Lab's result filters, when set, apply too.
    With `years` (see calendar_years) it also needs at least one trade opened
    in every one of those years — unless the trades opened in those years are
    at least as many as the years: a peak bought with RSI levels so wide that whole
    years go by without a trade is not a result with continuity.

    `fast_backtest`, when given, scans the 961 pairs with fills at the close
    and only the best `refine` survivors are backtested again with
    `backtest` (the in-candle replay of fill_at "on_condition", up to 8 s
    each): 14/10, 961 replays made the search crawl for an hour.

    Returns (best_params, best_bt, evaluated, top_results), top_results
    sorted by return, distinct by effect, rank starting at 1.
    """
    if strategy_name not in GABRIELE_STRATEGIES:
        raise ValueError(f"max-return search is for RSI+BB only, not {strategy_name!r}")
    constraints = constraints or _NO_CONSTRAINTS
    say = progress or (lambda _m: None)
    fixed = gabriele_fixed(strategy_name)
    total = max_return_size()
    say(f"whole chart, every pair: oversold {MAX_RETURN_OVERSOLD.start}-"
        f"{MAX_RETURN_OVERSOLD.stop - 1} x overbought {MAX_RETURN_OVERBOUGHT.start}-"
        f"{MAX_RETURN_OVERBOUGHT.stop - 1} = {total} backtests, "
        f"max drawdown {MAX_RETURN_DRAWDOWN_CAP:.0f}%")

    best: list[tuple[float, int, dict, BacktestResult]] = []   # min-heap on return
    seen: set[tuple] = set()
    evaluated = too_deep = no_trades = filtered = gaps = 0
    years = list(years or [])
    if years:
        say(f"at least one trade in every year: {', '.join(map(str, years))}")
    step = max(1, total // 20)
    scan = fast_backtest or backtest
    keep = max(refine, top_n) if fast_backtest else top_n
    for os_ in MAX_RETURN_OVERSOLD:
        for ob in MAX_RETURN_OVERBOUGHT:
            stop_if_asked(should_stop)
            params = {**fixed, "rsi_oversold": os_, "rsi_overbought": ob}
            bt = scan(params)
            evaluated += 1
            if evaluated % step == 0:
                lead = max(best, key=lambda e: e[0])[0] if best else None
                say(f"{evaluated}/{total}" + (f" — best so far {lead:+.1f}%" if lead is not None else ""))
            if bt.num_trades < 1:
                no_trades += 1
                continue
            if abs(bt.max_drawdown_pct) > MAX_RETURN_DRAWDOWN_CAP:
                too_deep += 1
                continue
            # Lorenzo, 13/10: "se il totale dei trade pareggia o supera il
            # numero degli anni presi in esame allora il risultato deve essere
            # considerato valido" — a year without trades is forgiven then,
            # counting only the trades opened in those years ("conta solo i
            # trade fatti negli anni esaminati").
            if years and trades_in_years(bt, years) < len(years) and years_without_trades(bt, years):
                gaps += 1
                continue
            if not _passes(bt, constraints):
                filtered += 1
                continue
            fp = (round(bt.total_return_pct, 4), bt.num_trades, round(bt.max_drawdown_pct, 4))
            if fp in seen:
                continue
            seen.add(fp)
            entry = (ranked_return(bt), evaluated, params, bt)
            if len(best) < keep:
                heapq.heappush(best, entry)
            elif entry[0] > best[0][0]:
                heapq.heapreplace(best, entry)

    say(f"excluded: {too_deep} over {MAX_RETURN_DRAWDOWN_CAP:.0f}% drawdown, "
        f"{no_trades} without trades"
        + (f", {gaps} with a year without trades" if years else "")
        + (f", {filtered} by the filters" if filtered else ""))
    if not best:
        if constraints.active and filtered:
            raise NoCandidateMeetsConstraints(constraints, evaluated, filtered)
        raise ValueError(
            f"no pair of values stays within {MAX_RETURN_DRAWDOWN_CAP:.0f}% drawdown "
            + (f"with a trade in every year {years[0]}-{years[-1]} " if years else "with at least one trade ")
            + f"on this chart ({evaluated} tried)")
    ranked = sorted(best, key=lambda e: e[0], reverse=True)
    if fast_backtest:
        say(f"fills inside the candle: re-checking the best {len(ranked)} exactly as Run does")
        exact, seen_exact = [], set()
        for _, n, params, _ in ranked:
            stop_if_asked(should_stop)
            bt = backtest(params)
            if (bt.num_trades < 1 or abs(bt.max_drawdown_pct) > MAX_RETURN_DRAWDOWN_CAP
                    or (years and trades_in_years(bt, years) < len(years) and years_without_trades(bt, years))
                    or not _passes(bt, constraints)):
                continue
            fp = (round(bt.total_return_pct, 4), bt.num_trades, round(bt.max_drawdown_pct, 4))
            if fp in seen_exact:
                continue
            seen_exact.add(fp)
            exact.append((ranked_return(bt), n, params, bt))
        if not exact:
            raise ValueError("none of the best pairs holds the limits once filled inside the candle")
        ranked = sorted(exact, key=lambda e: e[0], reverse=True)[:top_n]
    top = [{"rank": i, "params": p, "backtest": b, "score": round(r, 4)}
           for i, (r, _, p, b) in enumerate(ranked, start=1)]
    say(f"best: oversold {top[0]['params']['rsi_oversold']}, overbought "
        f"{top[0]['params']['rsi_overbought']} → {ranked_return(top[0]['backtest']):+.2f}% simple")
    return top[0]["params"], top[0]["backtest"], evaluated, top


# ── RSI+BB 4H: the multiplier joins the search, two kinds of winner ──────────
# Lorenzo, 14/10: "rendere ottimizzabile anche il Bollinger Multiplier con un
# range da 2.0 a 2.8 [...] 2.0, 2.1, 2.2 [...] e combinarli con i range di
# ipercomprato e ipervenduto", then "due tipi di risultati: il piu' solido in
# rapporto al profitto e il piu' profittevole anche con una minore solidita',
# ma non deve essere una solidita' completamente azzerata".
#
# Solidity of a cell = how much of its return its neighbours keep: the median
# return of the cells one step away (oversold ±1, overbought ±1, multiplier
# ±0.1) over its own return, between 0 and 1. A lone spike surrounded by
# little scores near 0; a shelf scores near 1.
MAX_RETURN_MULTS = [round(2.0 + 0.1 * i, 1) for i in range(9)]   # 2.0 … 2.8
SOLIDITY_FLOOR = 0.25            # the "most profitable" still needs this much
REFINE_PER_KIND = 10


# RSI+BB 4H (Lorenzo, 15/10): "ridurrei i range RSI: solo da 20 a 40 e da 65
# a 85" and "un minimo piu' alto di trade per anno: almeno tre invece di uno"
# — option B: at least 3 a year on average over the judged years, and no
# judged year without a trade.
MAX_RETURN_4H_OVERSOLD = range(20, 41)
MAX_RETURN_4H_OVERBOUGHT = range(65, 86)
MIN_TRADES_PER_YEAR_4H = 3


def max_return_size_3d() -> int:
    return len(MAX_RETURN_4H_OVERSOLD) * len(MAX_RETURN_4H_OVERBOUGHT) * len(MAX_RETURN_MULTS)


def _admitted(bt: BacktestResult, years: list[int], constraints: Constraints) -> str | None:
    """None when the result is acceptable, else why not."""
    if bt.num_trades < 1:
        return "no_trades"
    if abs(bt.max_drawdown_pct) > MAX_RETURN_DRAWDOWN_CAP:
        return "too_deep"
    if years and (years_without_trades(bt, years)
                  or trades_in_years(bt, years) < MIN_TRADES_PER_YEAR_4H * len(years)):
        return "gaps"
    if not _passes(bt, constraints):
        return "filtered"
    return None


def solidity(key: tuple, returns: dict[tuple, float]) -> tuple[float, list[float]]:
    """(solidity 0..1, neighbour returns) of `key` = (oversold, overbought, mult)."""
    os_, ob, m = key
    own = returns.get(key)
    near = []
    for k in ((os_ - 1, ob, m), (os_ + 1, ob, m), (os_, ob - 1, m), (os_, ob + 1, m),
              (os_, ob, round(m - 0.1, 1)), (os_, ob, round(m + 0.1, 1))):
        if k in returns:
            near.append(returns[k])
    if own is None or own <= 0 or not near:
        return 0.0, near
    med = sorted(near)[len(near) // 2] if len(near) % 2 else sum(sorted(near)[len(near) // 2 - 1: len(near) // 2 + 1]) / 2
    return max(0.0, min(1.0, med / own)), near


COARSE_SEEDS = 6               # best cells per kind refined at full resolution
FINE_RADIUS = 3                # ±3 RSI points, ±0.3 multiplier around each seed


def max_return_search_3d(
    strategy_name: str,
    backtest: Callable[[dict], BacktestResult],
    constraints: Constraints | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    years: list[int] | None = None,
    fast_backtest: Callable[[dict], BacktestResult] | None = None,
    coarse: bool = True,
) -> tuple[dict, BacktestResult, int, list[dict]]:
    """Oversold 10-40 × overbought 60-90 × multiplier 2.0-2.8, whole chart.

    Returns two results (or one, when the same cell wins both):
      * "solid": the highest return its neighbourhood keeps (return ×
        solidity) — the configuration that holds up under small changes;
      * "profitable": the highest return with solidity ≥ SOLIDITY_FLOOR.
    Same limits as max_return_search (60% drawdown, a trade every year,
    Lab filters). With `fast_backtest` the grid is scanned with fills at the
    close and only the shortlisted cells are re-run with `backtest`.

    `coarse` (Lorenzo, 15/10, speed): a first pass at RSI step 2 and
    multiplier step 0.2 over the whole range, then every cell within ±3 /
    ±0.3 of the best COARSE_SEEDS of each kind at full resolution, so the
    winners and their solidity are measured exactly like the full grid.
    A peak too narrow to show at step 2 is lost — and would have had a
    solidity near zero anyway.
    """
    if strategy_name not in GABRIELE_STRATEGIES:
        raise ValueError(f"max-return search is for RSI+BB only, not {strategy_name!r}")
    constraints = constraints or _NO_CONSTRAINTS
    say = progress or (lambda _m: None)
    fixed = gabriele_fixed(strategy_name)
    years = list(years or [])
    os_all, ob_all, m_all = list(MAX_RETURN_4H_OVERSOLD), list(MAX_RETURN_4H_OVERBOUGHT), list(MAX_RETURN_MULTS)
    say(f"oversold 20-40 × overbought 65-85 × multiplier 2.0-2.8 "
        f"({max_return_size_3d()} combinations), max drawdown {MAX_RETURN_DRAWDOWN_CAP:.0f}%"
        + (f", at least {MIN_TRADES_PER_YEAR_4H} trades a year on average and one in every year "
           f"{', '.join(map(str, years))}" if years else ""))
    scan = fast_backtest or backtest
    returns: dict[tuple, float] = {}
    ok: dict[tuple, BacktestResult] = {}
    why: dict[str, int] = {}

    def evaluate(keys) -> None:
        todo = [k for k in keys if k not in returns]
        step = max(1, len(todo) // 10)
        for i, key in enumerate(todo, 1):
            stop_if_asked(should_stop)
            os_, ob, m = key
            bt = scan({**fixed, "bb_mult": m, "rsi_oversold": os_, "rsi_overbought": ob})
            returns[key] = ranked_return(bt)
            reason = _admitted(bt, years, constraints)
            if reason:
                why[reason] = why.get(reason, 0) + 1
            else:
                ok[key] = bt
            if i % step == 0:
                say(f"{len(returns)} tried — acceptable so far: {len(ok)}")

    if coarse:
        grid = [(o, b, m) for m in m_all[::2] for o in os_all[::2] for b in ob_all[::2]]
        say(f"first pass, RSI every 2 and multiplier every 0.2: {len(grid)} combinations")
        evaluate(grid)

        def coarse_sol(k):
            os_, ob, m = k
            near = [returns[x] for x in ((os_ - 2, ob, m), (os_ + 2, ob, m), (os_, ob - 2, m), (os_, ob + 2, m),
                                         (os_, ob, round(m - 0.2, 1)), (os_, ob, round(m + 0.2, 1))) if x in returns]
            own = returns[k]
            if own <= 0 or not near:
                return 0.0
            near.sort()
            return max(0.0, min(1.0, near[len(near) // 2] / own))
        cand = list(ok)
        seeds = set(sorted(cand, key=lambda k: returns[k] * coarse_sol(k), reverse=True)[:COARSE_SEEDS])
        seeds |= set(sorted((k for k in cand if coarse_sol(k) >= SOLIDITY_FLOOR / 2),
                            key=lambda k: returns[k], reverse=True)[:COARSE_SEEDS])
        fine = set()
        for os_, ob, m in seeds:
            for o in os_all:
                if abs(o - os_) > FINE_RADIUS:
                    continue
                for b in ob_all:
                    if abs(b - ob) > FINE_RADIUS:
                        continue
                    for mm in m_all:
                        if abs(mm - m) <= FINE_RADIUS / 10 + 1e-9:
                            fine.add((o, b, mm))
        say(f"second pass, one by one around the best {len(seeds)}: {len(fine - set(returns))} more")
        evaluate(sorted(fine))
        # Only cells whose six neighbours were all measured compete.
        def full_ring(k):
            os_, ob, m = k
            ring = [(os_ - 1, ob, m), (os_ + 1, ob, m), (os_, ob - 1, m), (os_, ob + 1, m),
                    (os_, ob, round(m - 0.1, 1)), (os_, ob, round(m + 0.1, 1))]
            inside = [x for x in ring if x[0] in MAX_RETURN_4H_OVERSOLD and x[1] in MAX_RETURN_4H_OVERBOUGHT and x[2] in m_all]
            return all(x in returns for x in inside)
        eligible = [k for k in ok if k in fine and full_ring(k)] or list(ok)
    else:
        evaluate([(o, b, m) for m in m_all for o in os_all for b in ob_all])
        eligible = list(ok)
    n = len(returns)
    say("excluded: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in why.items()) if why else "nothing excluded")
    if not ok:
        if constraints.active and why.get("filtered"):
            raise NoCandidateMeetsConstraints(constraints, n, why["filtered"])
        raise ValueError(f"no combination holds the limits on this chart ({n} tried)")

    sol = {k: solidity(k, returns) for k in eligible}
    by_solid = sorted(eligible, key=lambda k: returns[k] * sol[k][0], reverse=True)
    by_profit = sorted((k for k in eligible if sol[k][0] >= SOLIDITY_FLOOR), key=lambda k: returns[k], reverse=True)

    def pick(order: list[tuple], kind: str) -> tuple | None:
        if not fast_backtest:
            return order[0] if order else None
        best = None
        for k in order[:REFINE_PER_KIND]:
            os_, ob, m = k
            bt = backtest({**fixed, "bb_mult": m, "rsi_oversold": os_, "rsi_overbought": ob})
            if _admitted(bt, years, constraints):
                continue
            score = ranked_return(bt) * (sol[k][0] if kind == "solid" else 1.0)
            if best is None or score > best[0]:
                best = (score, k, bt)
        if best:
            ok[best[1]] = best[2]
            return best[1]
        return None

    if fast_backtest:
        say("fills inside the candle: re-checking the shortlist exactly as Run does")
    solid_k = pick(by_solid, "solid")
    profit_k = pick(by_profit, "profitable")
    out, seen = [], set()
    for kind, k in (("solid", solid_k), ("profitable", profit_k)):
        if k is None:
            continue
        if k in seen:
            out[0]["kind"] = "solid+profitable"
            continue
        seen.add(k)
        s_, near = sol[k]
        os_, ob, m = k
        out.append({"rank": len(out) + 1, "kind": kind,
                    "params": {**fixed, "bb_mult": m, "rsi_oversold": os_, "rsi_overbought": ob},
                    "backtest": ok[k], "score": round(returns[k] * s_, 4),
                    "solidity": round(s_, 3),
                    "neighbours": {"mult_down": returns.get((os_, ob, round(m - 0.1, 1))),
                                   "mult_up": returns.get((os_, ob, round(m + 0.1, 1))),
                                   "median": round(sorted(near)[len(near) // 2], 2) if near else None}})
    if not out:
        raise ValueError("none of the best combinations holds the limits once filled inside the candle")
    say(f"{n} combinations backtested of {max_return_size_3d()}")
    for c in out:
        p = c["params"]
        say(f"{c['kind']}: oversold {p['rsi_oversold']}, overbought {p['rsi_overbought']}, multiplier "
            f"{p['bb_mult']} → {ranked_return(c['backtest']):+.2f}% simple (solidity {c['solidity']:.0%})")
    return out[0]["params"], out[0]["backtest"], n, out


# ── Claudio 4H: the same search on its own three dimensions ─────────────────
CLAUDIO_DIMS = (
    ("cl_rsi_os", list(range(20, 41))),                                  # 21
    ("cl_bb_mult", [round(1.8 + 0.1 * i, 1) for i in range(11)]),        # 1.8 … 2.8
    ("cl_atr_stop", [round(1.5 + 0.25 * i, 2) for i in range(11)]),      # 1.5 … 4.0
)


def grid_size_nd(dims) -> int:
    n = 1
    for _, vals in dims:
        n *= len(vals)
    return n


def max_return_search_nd(
    strategy_name: str,
    backtest: Callable[[dict], BacktestResult],
    dims=CLAUDIO_DIMS,
    fixed: dict | None = None,
    constraints: Constraints | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    years: list[int] | None = None,
    fast_backtest: Callable[[dict], BacktestResult] | None = None,
    coarse: bool = True,
) -> tuple[dict, BacktestResult, int, list[dict]]:
    """max_return_search_3d for any set of dimensions (Claudio 4H): coarse
    pass on every other value, then full resolution ±3 steps around the best
    COARSE_SEEDS of each kind; solidity = median return of the ±1-step
    neighbours over the cell's own; most solid and most profitable (solidity
    ≥ SOLIDITY_FLOOR); same limits (_admitted)."""
    constraints = constraints or _NO_CONSTRAINTS
    say = progress or (lambda _m: None)
    fixed = dict(fixed or {})
    years = list(years or [])
    names = [d[0] for d in dims]
    values = [list(d[1]) for d in dims]
    say(f"{strategy_name}: " + " × ".join(f"{nm} {v[0]}-{v[-1]}" for nm, v in zip(names, values))
        + f" ({grid_size_nd(dims)} combinations), max drawdown {MAX_RETURN_DRAWDOWN_CAP:.0f}%"
        + (f", at least {MIN_TRADES_PER_YEAR_4H} trades a year on average and one in every year "
           f"{', '.join(map(str, years))}" if years else ""))
    scan = fast_backtest or backtest
    returns: dict[tuple, float] = {}
    ok: dict[tuple, BacktestResult] = {}
    why: dict[str, int] = {}

    def params_of(key):
        return {**fixed, **{nm: values[d][i] for d, (nm, i) in enumerate(zip(names, key))}}

    def evaluate(keys) -> None:
        todo = [k for k in keys if k not in returns]
        step = max(1, len(todo) // 10)
        for j, key in enumerate(todo, 1):
            stop_if_asked(should_stop)
            bt = scan(params_of(key))
            returns[key] = ranked_return(bt)
            reason = _admitted(bt, years, constraints)
            if reason:
                why[reason] = why.get(reason, 0) + 1
            else:
                ok[key] = bt
            if j % step == 0:
                say(f"{len(returns)} tried — acceptable so far: {len(ok)}")

    def ring(key, step=1):
        out = []
        for d in range(len(key)):
            for s_ in (-step, step):
                k = list(key)
                k[d] += s_
                if 0 <= k[d] < len(values[d]):
                    out.append(tuple(k))
        return out

    def sol(key, step=1):
        own = returns.get(key)
        near = [returns[k] for k in ring(key, step) if k in returns]
        if own is None or own <= 0 or not near:
            return 0.0, near
        near.sort()
        mid = near[len(near) // 2] if len(near) % 2 else (near[len(near) // 2 - 1] + near[len(near) // 2]) / 2
        return max(0.0, min(1.0, mid / own)), near

    import itertools as _it
    if coarse:
        grid = list(_it.product(*[range(0, len(v), 2) for v in values]))
        say(f"first pass, every other value: {len(grid)} combinations")
        evaluate(grid)
        cand = list(ok)
        seeds = set(sorted(cand, key=lambda k: returns[k] * sol(k, 2)[0], reverse=True)[:COARSE_SEEDS])
        seeds |= set(sorted((k for k in cand if sol(k, 2)[0] >= SOLIDITY_FLOOR / 2),
                            key=lambda k: returns[k], reverse=True)[:COARSE_SEEDS])
        fine = set()
        for sd in seeds:
            for k in _it.product(*[range(max(0, sd[d] - FINE_RADIUS), min(len(values[d]), sd[d] + FINE_RADIUS + 1))
                                   for d in range(len(values))]):
                fine.add(k)
        say(f"second pass, one by one around the best {len(seeds)}: {len(fine - set(returns))} more")
        evaluate(sorted(fine))
        eligible = [k for k in ok if k in fine and all(x in returns for x in ring(k))] or list(ok)
    else:
        evaluate(list(_it.product(*[range(len(v)) for v in values])))
        eligible = list(ok)
    n = len(returns)
    say("excluded: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in why.items()) if why else "nothing excluded")
    if not ok:
        if constraints.active and why.get("filtered"):
            raise NoCandidateMeetsConstraints(constraints, n, why["filtered"])
        raise ValueError(f"no combination holds the limits on this chart ({n} tried)")

    sols = {k: sol(k) for k in eligible}
    by_solid = sorted(eligible, key=lambda k: returns[k] * sols[k][0], reverse=True)
    by_profit = sorted((k for k in eligible if sols[k][0] >= SOLIDITY_FLOOR), key=lambda k: returns[k], reverse=True)

    def pick(order, kind):
        if not fast_backtest:
            return order[0] if order else None
        best = None
        for k in order[:REFINE_PER_KIND]:
            bt = backtest(params_of(k))
            if _admitted(bt, years, constraints):
                continue
            score = ranked_return(bt) * (sols[k][0] if kind == "solid" else 1.0)
            if best is None or score > best[0]:
                best = (score, k, bt)
        if best:
            ok[best[1]] = best[2]
            return best[1]
        return None

    if fast_backtest:
        say("fills inside the candle: re-checking the shortlist exactly as Run does")
    out, seen = [], set()
    for kind, k in (("solid", pick(by_solid, "solid")), ("profitable", pick(by_profit, "profitable"))):
        if k is None:
            continue
        if k in seen:
            out[0]["kind"] = "solid+profitable"
            continue
        seen.add(k)
        s_, near = sols[k]
        out.append({"rank": len(out) + 1, "kind": kind, "params": params_of(k), "backtest": ok[k],
                    "score": round(returns[k] * s_, 4), "solidity": round(s_, 3), "neighbours": None})
    if not out:
        raise ValueError("none of the best combinations holds the limits once filled inside the candle")
    say(f"{n} combinations backtested of {grid_size_nd(dims)}")
    for c in out:
        say(f"{c['kind']}: " + ", ".join(f"{nm} {c['params'][nm]}" for nm in names)
            + f" → {ranked_return(c['backtest']):+.2f}% simple (solidity {c['solidity']:.0%})")
    return out[0]["params"], out[0]["backtest"], n, out
