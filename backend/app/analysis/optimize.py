"""
Parameter search for every strategy in the registry — random search over
each strategy's own parameter space, scored by profit factor on the
deep-history backtest df (see backtest_interval_for in app.main). A full
grid over these dimensions (5 to 20, depending on the strategy) is
intractable; random sampling with a generous trial budget finds a strong
combination in a bounded, predictable amount of time instead.
"""

import random

from app.analysis.backtest import run_backtest
from app.analysis.strategies import DEFAULT_PARAMS, EXHAUSTION_DEFAULT_PARAMS, LKI_DEFAULT_PARAMS
from app.models import BacktestResult

_TRIALS = 200
# Below this many trades a great score is likely luck — disqualified.
_MIN_TRADES = 8
# Hard drawdown cap: any candidate exceeding this is disqualified regardless
# of how good its return looks. Keeps the optimizer from picking a strategy
# that triples equity but gives back -60% in a single losing streak.
_MAX_DRAWDOWN_PCT = 35.0


def _random_lorenzo_params(rng: random.Random) -> dict:
    slope_fast = rng.randint(2, 30)
    # Slow must meaningfully trail fast, same relationship the strategy's
    # own inputs imply (fast/slow crossover) — otherwise the "slope" signal
    # degenerates into near-constant noise.
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


def _random_exhaustion_params(rng: random.Random) -> dict:
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
        # At least one entry condition must stay on, or nothing ever fires.
        "enable_divergence": True,
        "enable_contraction": rng.choice([True, False]),
        "enable_range_bounce": rng.choice([True, False]),
    }


def _random_gabriele_params(rng: random.Random) -> dict:
    return {
        "rsi_length": rng.randint(7, 25),
        "bb_length": rng.randint(10, 40),
        "bb_mult": round(rng.uniform(1.0, 3.5), 2),
        "rsi_oversold": rng.randint(15, 40),
        "rsi_overbought": rng.randint(60, 85),
    }


# One entry per strategy in app.analysis.strategies.STRATEGY_REGISTRY:
# (default params, random-candidate generator). Keyed by the exact strategy
# name returned from /api/strategies.
_STRATEGY_SPACES: dict[str, tuple[dict, "random.Random"]] = {
    "Lorenzo (trend-follower)": (DEFAULT_PARAMS, _random_lorenzo_params),
    "Accumulo / Distribuzione (long-term)": (EXHAUSTION_DEFAULT_PARAMS, _random_exhaustion_params),
    "Gabriele (RSI+BB reversal)": (LKI_DEFAULT_PARAMS, _random_gabriele_params),
}


def is_optimizable(strategy_name: str) -> bool:
    return strategy_name in _STRATEGY_SPACES


def _score(bt: BacktestResult) -> float:
    """Composite score that targets high final equity with controlled drawdown.

    Formula: Calmar ratio × profit_factor_bonus × trade_confidence
    - Calmar ratio = total_return_pct / abs(max_drawdown_pct): directly
      rewards return per unit of pain — a strategy that earns +200% with -20%
      DD beats one that earns +300% with -60% DD (10 vs 5).
    - Hard cap at _MAX_DRAWDOWN_PCT: candidates above it are disqualified
      outright, never just downscored — a -50% drawdown is a margin call, not
      a bad-but-acceptable trade-off.
    - profit_factor_bonus (capped at 5): secondary weight for trade quality.
    - trade_confidence: log-normalised trade count so 8 trades and 80 trades
      are not treated equally; more trades = more evidence.
    """
    if bt.num_trades < _MIN_TRADES:
        return float("-inf")
    if bt.total_return_pct <= 0:
        return float("-inf")
    if abs(bt.max_drawdown_pct) > _MAX_DRAWDOWN_PCT:
        return float("-inf")

    drawdown = max(abs(bt.max_drawdown_pct), 0.5)  # guard against 0
    calmar = bt.total_return_pct / drawdown

    # Profit factor contribution: capped so a PF of 50 on 1 lucky trade
    # doesn't dominate; anything above 5 is treated as 5.
    pf = bt.profit_factor if bt.profit_factor is not None else 10.0
    pf_bonus = min(pf, 5.0) / 5.0  # normalised to [0, 1]

    # More trades = more confidence the edge is real.
    import math
    trade_conf = math.log(bt.num_trades + 1) / math.log(_MIN_TRADES + 1)

    return calmar * (0.65 + 0.25 * pf_bonus + 0.10 * trade_conf)


def optimize_strategy(
    strategy_name: str, df, backtest_params: dict, trials: int = _TRIALS, seed: int | None = None
) -> tuple[dict, BacktestResult, int]:
    """Returns (best_params, best_backtest_result, trials_evaluated). Always
    evaluated at least once against the strategy's own defaults, so the
    result is never worse than what "no optimization" would have given."""
    if strategy_name not in _STRATEGY_SPACES:
        raise ValueError(f"Nessuno spazio di parametri definito per {strategy_name!r}")
    default_params, random_params_fn = _STRATEGY_SPACES[strategy_name]

    rng = random.Random(seed)

    best_params = dict(default_params)
    best_bt = run_backtest(df, best_params, backtest_params, strategy_name=strategy_name)
    best_score = _score(best_bt)
    evaluated = 1

    for _ in range(trials):
        candidate = random_params_fn(rng)
        bt = run_backtest(df, candidate, backtest_params, strategy_name=strategy_name)
        evaluated += 1
        score = _score(bt)
        if score > best_score:
            best_score = score
            best_params = candidate
            best_bt = bt

    return best_params, best_bt, evaluated
