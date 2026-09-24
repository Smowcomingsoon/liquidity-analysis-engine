"""
Sensitivity screening via Morris elementary effects (pipeline stage S1).

Purpose: find out which parameters actually move the objective, so the search
in later stages runs in the handful of dimensions that matter instead of the
full declared space. This is what makes the problem tractable — a few hundred
points is a dense grid in 4 dimensions and nothing at all in 10.

Cost is linear in dimension, not exponential: r trajectories of (d+1)
evaluations each. For Lorenzo, 10 trajectories over 10 parameters is 110
backtests, a few seconds.

Unit-cube parameterisation
--------------------------
Every strategy declares a mapping from [0,1]^d to its parameter dict. Two
reasons this is not incidental:

  * Dependent constraints (Lorenzo needs slopeSlen > slopeFlen) are satisfied
    BY CONSTRUCTION, because the dependent coordinate is mapped into the range
    the earlier coordinate leaves available. Repairing an infeasible point
    afterwards would silently alter the very perturbation whose effect is
    being measured, and corrupt the estimate.
  * Elementary effects are then comparable across parameters: every dimension
    is perturbed by the same step in the same normalised units, so mu* can be
    ranked directly without rescaling.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.analysis.backtest import run_backtest
from app.models import BacktestResult

# --- unit-cube scaling helpers ----------------------------------------------


def _i(u: float, lo: int, hi: int) -> int:
    """Map u in [0,1] to an integer in [lo, hi] inclusive."""
    if hi <= lo:
        return int(lo)
    return int(np.clip(math.floor(lo + u * (hi - lo + 1)), lo, hi))


def _f(u: float, lo: float, hi: float, nd: int = 2) -> float:
    return round(float(lo + u * (hi - lo)), nd)


def _b(u: float) -> bool:
    return bool(u >= 0.5)


def _c(u: float, options: Sequence):
    return options[int(np.clip(math.floor(u * len(options)), 0, len(options) - 1))]


# --- strategy parameter spaces ----------------------------------------------


def _lorenzo(u: np.ndarray) -> dict:
    slope_fast = _i(u[2], 2, 30)
    # Dependent by construction: the slow line is placed in whatever range the
    # fast line leaves, so every point in the cube is feasible.
    slope_slow = _i(u[3], slope_fast + 3, 80)
    return {
        "average": _c(u[0], ["EMA", "SMA"]),
        "len": _i(u[1], 20, 300),
        "slopeFlen": slope_fast,
        "slopeSlen": slope_slow,
        "trendfilter": _b(u[4]),
        "trendfilterperiod": _i(u[5], 50, 400),
        "trendfiltertype": _c(u[6], ["EMA", "SMA"]),
        "volatilityfilter": _b(u[7]),
        "volatilitystdevlength": _i(u[8], 5, 40),
        "volatilitystdevmalength": _i(u[9], 10, 80),
    }


_LORENZO_DIMS = [
    "average", "len", "slopeFlen", "slopeSlen", "trendfilter",
    "trendfilterperiod", "trendfiltertype", "volatilityfilter",
    "volatilitystdevlength", "volatilitystdevmalength",
]


def _lorenzo_hf(u: np.ndarray) -> dict:
    slope_fast = _i(u[1], 2, 10)
    return {
        "len": _i(u[0], 10, 80),
        "slopeFlen": slope_fast,
        "slopeSlen": _i(u[2], slope_fast + 3, 40),
        "slope_threshold_sigma": _f(u[3], 0.0, 1.0),
        "volume_mult": _f(u[4], 0.0, 3.0),
        "er_min": _f(u[5], 0.0, 0.7),
        "trail_atr": _f(u[6], 1.0, 6.0),
    }


_LORENZO_HF_DIMS = [
    "len", "slopeFlen", "slopeSlen", "slope_threshold_sigma", "volume_mult", "er_min", "trail_atr",
]


def _gabriele(u: np.ndarray) -> dict:
    return {
        "rsi_length": _i(u[0], 7, 25),
        "bb_length": _i(u[1], 10, 40),
        "bb_mult": _f(u[2], 1.0, 3.5),
        "rsi_oversold": _i(u[3], 15, 40),
        "rsi_overbought": _i(u[4], 60, 85),
    }


_GABRIELE_DIMS = ["rsi_length", "bb_length", "bb_mult", "rsi_oversold", "rsi_overbought"]


def _exhaustion(u: np.ndarray) -> dict:
    return {
        "swing_length": _i(u[0], 3, 20),
        "rsi_length": _i(u[1], 7, 25),
        "rsi_oversold": _i(u[2], 20, 45),
        "rsi_overbought": _i(u[3], 55, 80),
        "rsi_min_gap": _f(u[4], 1.0, 10.0),
        "low_tolerance_pct": _f(u[5], 0.2, 3.0),
        "divergence_lookback": _i(u[6], 2, 8),
        "volume_ma_length": _i(u[7], 10, 40),
        "volume_multiplier": _f(u[8], 1.1, 2.5),
        "trend_filter": _b(u[9]),
        "trend_ema_length": _i(u[10], 20, 100),
        "confirmation_lookahead": _i(u[11], 3, 20),
        "contraction_lookback": _i(u[12], 5, 20),
        "contraction_prior_lookback": _i(u[13], 20, 60),
        "contraction_ratio": _f(u[14], 0.3, 0.8),
        "range_lookback": _i(u[15], 20, 80),
        "range_zone_pct": _f(u[16], 0.1, 0.4),
        "enable_divergence": True,   # at least one entry condition must stay on
        "enable_contraction": _b(u[17]),
        "enable_range_bounce": _b(u[18]),
    }


_EXHAUSTION_DIMS = [
    "swing_length", "rsi_length", "rsi_oversold", "rsi_overbought", "rsi_min_gap",
    "low_tolerance_pct", "divergence_lookback", "volume_ma_length", "volume_multiplier",
    "trend_filter", "trend_ema_length", "confirmation_lookahead", "contraction_lookback",
    "contraction_prior_lookback", "contraction_ratio", "range_lookback", "range_zone_pct",
    "enable_contraction", "enable_range_bounce",
]


@dataclass(frozen=True)
class ParamSpace:
    dims: tuple[str, ...]
    to_params: Callable[[np.ndarray], dict]

    @property
    def d(self) -> int:
        return len(self.dims)


PARAM_SPACES: dict[str, ParamSpace] = {
    "Lorenzo (trend-follower)": ParamSpace(tuple(_LORENZO_DIMS), _lorenzo),
    "Gabriele (RSI+BB reversal)": ParamSpace(tuple(_GABRIELE_DIMS), _gabriele),
    "Gabriele 2 (RSI+BB long/short)": ParamSpace(tuple(_GABRIELE_DIMS), _gabriele),
    "Lorenzo HF (minuti on-chain)": ParamSpace(tuple(_LORENZO_HF_DIMS), _lorenzo_hf),
    "Accumulo / Distribuzione (long-term)": ParamSpace(tuple(_EXHAUSTION_DIMS), _exhaustion),
}


# --- objective ---------------------------------------------------------------


def soft_score(bt: BacktestResult, min_trades: int = 8) -> float:
    """Continuous stand-in for optimize._score, for sensitivity analysis only.

    The production score returns -inf when a candidate trips a filter. That is
    correct for selection and useless for screening: an elementary effect
    computed across an infinite step carries no information, and a single
    filtered point would poison every trajectory that touches it. Here the
    same preferences are expressed as smooth penalties, so the surface stays
    differentiable enough to difference.
    """
    ret = bt.total_return_pct
    dd = max(abs(bt.max_drawdown_pct), 0.5)
    calmar = ret / dd

    # Smooth ramp instead of a cliff at min_trades.
    conf = bt.num_trades / (bt.num_trades + min_trades)

    pf = bt.profit_factor if bt.profit_factor is not None else 1.0
    pf_bonus = min(max(pf, 0.0), 5.0) / 5.0

    return calmar * (0.6 + 0.4 * pf_bonus) * conf


def _total_return(bt: BacktestResult) -> float:
    return bt.total_return_pct


OBJECTIVES: dict[str, Callable[[BacktestResult], float]] = {
    "soft_score": soft_score,
    "total_return": _total_return,
}


# --- Morris elementary effects ----------------------------------------------


@dataclass
class EffectStats:
    """Screening statistics for one parameter.

    mu_star — mean |elementary effect|: overall influence. This is the ranking
              quantity, and the one the freeze decision uses.
    sigma   — spread of the elementary effects: high sigma means the parameter
              interacts with others or acts non-linearly, so its influence
              depends on where in the space you are.
    mu      — signed mean: a large |mu| close to mu_star indicates a monotone
              effect in a consistent direction.
    """

    name: str
    mu_star: float
    sigma: float
    mu: float
    n_effects: int
    share: float = 0.0     # mu_star as a fraction of the total across params
    rank: int = 0
    frozen: bool = False


@dataclass
class ScreeningResult:
    strategy: str
    effects: list[EffectStats]
    trajectories: int
    levels: int
    evaluations: int
    objective: str
    failures: int = 0

    @property
    def influential(self) -> list[str]:
        return [e.name for e in self.effects if not e.frozen]

    @property
    def frozen(self) -> list[str]:
        return [e.name for e in self.effects if e.frozen]

    def summary(self) -> str:
        w = max(len(e.name) for e in self.effects) + 2
        lines = [
            f"Morris screening — {self.strategy}",
            f"  objective={self.objective}  trajectories={self.trajectories} "
            f"levels={self.levels}  evaluations={self.evaluations}"
            + (f"  failures={self.failures}" if self.failures else ""),
            "",
            f"  {'#':>2} {'parameter':<{w}}{'mu*':>12}{'sigma':>12}{'share':>9}  keep",
            "  " + "-" * (w + 40),
        ]
        for e in self.effects:
            lines.append(
                f"  {e.rank:>2} {e.name:<{w}}{e.mu_star:>12.4g}{e.sigma:>12.4g}"
                f"{e.share:>8.1%}  {'.' if e.frozen else 'YES'}"
            )
        lines.append("")
        lines.append(f"  effective dimensions: {len(self.influential)} of {len(self.effects)}"
                     f"  -> {', '.join(self.influential)}")
        return "\n".join(lines)


def _trajectory(
    d: int,
    levels: int,
    rng: np.random.Generator,
    delta: float | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """One Morris trajectory: d+1 points, each differing from the previous in
    exactly one coordinate, by a fixed step delta.

    Returns (points, order, delta) where order[k] is the coordinate changed
    between points[k] and points[k+1].

    delta defaults to the standard Morris step p/(2(p-1)), which is ~0.5 of the
    range whatever the level count — Morris is a screening design, not a
    gradient estimator. Pass an explicit smaller delta to probe whether an
    effect is scale-dependent: a surface that is rugged only at large steps
    still admits local refinement, one that is rugged at every scale does not.
    """
    if delta is None:
        delta = levels / (2.0 * (levels - 1))
    # Base point on the grid, low enough that a +delta step stays inside [0,1].
    grid = np.arange(levels // 2) / (levels - 1.0)
    base = rng.choice(grid, size=d)

    order = rng.permutation(d)
    pts = np.empty((d + 1, d), dtype=float)
    pts[0] = base
    cur = base.copy()
    for k, dim in enumerate(order):
        cur = cur.copy()
        cur[dim] = cur[dim] + delta
        if cur[dim] > 1.0:
            cur[dim] = pts[k][dim] - delta
        pts[k + 1] = cur
    return pts, order, delta


def morris_screening(
    strategy_name: str,
    df: pd.DataFrame,
    backtest_params: dict | None = None,
    trajectories: int = 10,
    levels: int = 8,
    seed: int = 0,
    objective: str = "soft_score",
    freeze_share: float = 0.05,
    delta: float | None = None,
) -> ScreeningResult:
    """Rank parameters by influence on the objective.

    freeze_share: a parameter whose mu* is below this fraction of the total
    mu* across parameters is marked frozen — it is proposed for pinning at its
    default so later stages do not spend search budget on it.
    """
    if strategy_name not in PARAM_SPACES:
        raise ValueError(f"No parameter space for {strategy_name!r}")
    space = PARAM_SPACES[strategy_name]
    obj = OBJECTIVES[objective]
    bp = backtest_params or {}
    rng = np.random.default_rng(seed)

    d = space.d
    effects: list[list[float]] = [[] for _ in range(d)]
    evaluations = 0
    failures = 0

    def evaluate(u: np.ndarray) -> float | None:
        nonlocal evaluations, failures
        try:
            bt = run_backtest(df, space.to_params(u), bp, strategy_name=strategy_name)
            evaluations += 1
            v = obj(bt)
            return v if math.isfinite(v) else None
        except Exception:
            failures += 1
            return None

    for _ in range(trajectories):
        pts, order, _step = _trajectory(d, levels, rng, delta=delta)
        prev = evaluate(pts[0])
        for k, dim in enumerate(order):
            cur = evaluate(pts[k + 1])
            if prev is not None and cur is not None:
                step = pts[k + 1][dim] - pts[k][dim]
                if step != 0:
                    effects[dim].append((cur - prev) / step)
            prev = cur

    stats: list[EffectStats] = []
    for i, name in enumerate(space.dims):
        e = np.asarray(effects[i], dtype=float)
        if e.size == 0:
            stats.append(EffectStats(name, 0.0, 0.0, 0.0, 0))
            continue
        stats.append(EffectStats(
            name=name,
            mu_star=float(np.mean(np.abs(e))),
            sigma=float(np.std(e)) if e.size > 1 else 0.0,
            mu=float(np.mean(e)),
            n_effects=int(e.size),
        ))

    total = sum(s.mu_star for s in stats)
    for s in stats:
        s.share = (s.mu_star / total) if total > 0 else 0.0
    stats.sort(key=lambda s: s.mu_star, reverse=True)
    for r, s in enumerate(stats, start=1):
        s.rank = r
        s.frozen = s.share < freeze_share

    return ScreeningResult(
        strategy=strategy_name, effects=stats, trajectories=trajectories,
        levels=levels, evaluations=evaluations, objective=objective,
        failures=failures,
    )
