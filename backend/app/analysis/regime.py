"""
Regime characterisation and the family-conditional gate (pipeline stage S0).

Before any parameter search runs, this module answers a prior question: does
this series have the statistical property the strategy needs in order to have
an edge at all? A trend-follower needs persistence; a mean-reversion strategy
needs anti-persistence. On a random walk neither has one, and any parameter
set that appears profitable is fitted noise.

The gate is deliberately built on the Lo–MacKinlay variance-ratio test rather
than on a bare Hurst point estimate, because the decision needs a confidence
statement ("can we reject a random walk?") and not just a number. Hurst is
still reported, as an interpretable summary.

Dependency note: this module imports nothing from app.analysis.strategies and
uses only numpy/pandas. A gate that must decide whether the machinery may run
should not be able to fail because that machinery failed to import — hence the
strategy→family map below is keyed by literal name and must be kept in sync
with STRATEGY_REGISTRY (guarded by a test).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# --- Strategy families -------------------------------------------------------
# TREND wants persistence (variance ratio > 1); REVERSION wants anti-persistence
# (variance ratio < 1). The inequality flips between families — a single
# "needs persistence" gate applied to all three would block the reversion
# strategies in exactly the regimes where they work.
FAMILY_TREND = "trend"
FAMILY_REVERSION = "reversion"

STRATEGY_FAMILY: dict[str, str] = {
    "Lorenzo (trend-follower)": FAMILY_TREND,
    "Gabriele (RSI+BB reversal)": FAMILY_REVERSION,
    "Gabriele 2 (RSI+BB long/short)": FAMILY_REVERSION,
    "Lorenzo HF (minuti on-chain)": FAMILY_TREND,
    "Accumulo / Distribuzione (long-term)": FAMILY_REVERSION,
}

# Asymmetric significance thresholds. A trend-follower in a ranging market
# bleeds slowly through many small false signals; a reversion strategy in a
# trending market sells every leg up, so its losses are correlated with each
# other and land inside the largest move of the sample. The drawdown cap does
# not protect against that, because the whole blow-up can happen inside one
# run of bars. The asymmetry in the cost of being wrong is therefore priced
# into the gate: the reversion family must clear a stricter bar.
_ALPHA = {
    FAMILY_TREND: 0.10,
    FAMILY_REVERSION: 0.05,
}

# Below this many returns the variance-ratio test is not trustworthy.
_MIN_OBS = 120

VERDICT_TRENDING = "trending"
VERDICT_MEAN_REVERTING = "mean_reverting"
VERDICT_RANDOM_WALK = "random_walk"
VERDICT_INSUFFICIENT = "insufficient_data"


@dataclass
class RegimeReport:
    """What the series looks like, independently of any strategy."""

    n_obs: int
    hurst: float | None
    variance_ratio: float | None
    vr_z: float | None
    vr_p_one_sided: float | None      # p of the observed direction being noise
    autocorr_1: float | None
    adx: float | None
    verdict: str
    vr_lag: int = 8

    def as_dict(self) -> dict:
        return {
            "n_obs": self.n_obs,
            "hurst": _round(self.hurst, 4),
            "variance_ratio": _round(self.variance_ratio, 4),
            "vr_z": _round(self.vr_z, 3),
            "vr_p_one_sided": _round(self.vr_p_one_sided, 5),
            "autocorr_1": _round(self.autocorr_1, 4),
            "adx": _round(self.adx, 2),
            "verdict": self.verdict,
            "vr_lag": self.vr_lag,
        }


@dataclass
class GateDecision:
    """Whether a given strategy is allowed to be optimised on this series."""

    passed: bool
    family: str | None
    reason: str                       # Italian, user-facing
    report: RegimeReport
    alpha: float | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "family": self.family,
            "reason": self.reason,
            "alpha": self.alpha,
            "warnings": self.warnings,
            "regime": self.report.as_dict(),
        }


def _round(v: float | None, nd: int) -> float | None:
    if v is None or not math.isfinite(v):
        return None
    return round(float(v), nd)


def _norm_cdf(z: float) -> float:
    """Standard normal CDF via erfc — avoids a scipy dependency."""
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


# --- Hurst exponent, via detrended fluctuation analysis ----------------------


def hurst_dfa(returns: np.ndarray, min_window: int = 8, n_scales: int = 12) -> float | None:
    """DFA-1 estimate of the Hurst exponent of a return series.

    Applied to returns (not prices): H = 0.5 is a random walk, H > 0.5 is
    persistent, H < 0.5 is anti-persistent. DFA is used in preference to plain
    R/S because it removes a local linear trend inside each window, which
    matters on price series that drift.

    Read this number with care, and do not gate on it. DFA measures LONG-RANGE
    dependence (fractional integration). A short-memory process — an AR(1),
    which is what most tradable autocorrelation looks like — has exponentially
    decaying autocorrelation and therefore converges to H ≈ 0.5 at the scales
    DFA fits, whatever its sign at lag 1. Measured on synthetic AR(1) series
    at n=1200: phi=0 gives 0.486, phi=+0.25 gives 0.477, phi=-0.25 gives 0.434
    — the three regimes are NOT separated. The variance-ratio test at lag q is
    what captures the short-lag structure these strategies actually trade, and
    it is what the gate uses. Hurst is reported as a descriptive summary only.
    """
    x = np.asarray(returns, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    max_window = n // 4
    if n < _MIN_OBS or max_window <= min_window:
        return None

    y = np.cumsum(x - x.mean())
    scales = np.unique(
        np.logspace(np.log10(min_window), np.log10(max_window), n_scales).astype(int)
    )
    scales = scales[scales >= min_window]
    if scales.size < 4:
        return None

    fluct: list[float] = []
    used: list[int] = []
    for s in scales:
        n_seg = n // s
        if n_seg < 2:
            continue
        seg = y[: n_seg * s].reshape(n_seg, s)
        t = np.arange(s, dtype=float)
        # Least-squares linear detrend of every segment at once.
        design = np.vstack([t, np.ones_like(t)]).T
        coef, *_ = np.linalg.lstsq(design, seg.T, rcond=None)
        resid = seg.T - design @ coef
        rms = np.sqrt(np.mean(resid**2, axis=0))
        f = float(np.mean(rms))
        if f > 0:
            fluct.append(f)
            used.append(int(s))

    if len(used) < 4:
        return None
    slope = float(np.polyfit(np.log(used), np.log(fluct), 1)[0])
    return slope if math.isfinite(slope) else None


# --- Lo-MacKinlay variance ratio --------------------------------------------


def variance_ratio(returns: np.ndarray, q: int = 8) -> tuple[float | None, float | None]:
    """Heteroskedasticity-robust variance-ratio test (Lo & MacKinlay, 1988).

    Returns (VR, z). VR = 1 under a random walk; VR > 1 indicates positive
    return autocorrelation (trending), VR < 1 indicates mean reversion. The
    z statistic is robust to heteroskedasticity, which matters here because
    crypto volatility clusters heavily and the homoskedastic version would
    reject a random walk far too often on volatility alone.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < max(_MIN_OBS, q * 8) or q < 2:
        return None, None

    mu = r.mean()
    dev = r - mu
    var_1 = np.sum(dev**2) / (n - 1)
    if var_1 <= 0:
        return None, None

    # Overlapping q-period sums.
    cs = np.cumsum(r)
    q_sums = cs[q - 1:] - np.concatenate(([0.0], cs[:-q]))
    m = q * (n - q + 1) * (1.0 - q / n)
    if m <= 0:
        return None, None
    # m carries the factor q, so var_q is already a PER-PERIOD variance
    # estimated from q-period returns. Dividing by q again here would double
    # count it and drive VR to ~1/q on a pure random walk.
    var_q = np.sum((q_sums - q * mu) ** 2) / m

    vr = var_q / var_1
    if not math.isfinite(vr):
        return None, None

    # Heteroskedasticity-robust asymptotic variance.
    dev2 = dev**2
    denom = float(np.sum(dev2)) ** 2
    if denom <= 0:
        return float(vr), None
    theta = 0.0
    for j in range(1, q):
        delta_j = n * float(np.sum(dev2[j:] * dev2[:-j])) / denom
        theta += ((2.0 * (q - j)) / q) ** 2 * delta_j
    if theta <= 0:
        return float(vr), None

    z = math.sqrt(n) * (vr - 1.0) / math.sqrt(theta)
    return float(vr), float(z) if math.isfinite(z) else None


# --- ADX (Wilder) ------------------------------------------------------------


def _rma(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def adx(df: pd.DataFrame, length: int = 14) -> float | None:
    """Wilder's ADX, last value. Implemented locally to keep this module free
    of the indicator stack whose availability the gate must not depend on."""
    try:
        high, low, close = df["high"], df["low"], df["close"]
    except KeyError:
        return None
    if len(df) < length * 3:
        return None

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)).astype(float) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)).astype(float) * down.clip(lower=0)

    atr = _rma(tr, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * _rma(plus_dm, length) / atr
        minus_di = 100 * _rma(minus_dm, length) / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    value = _rma(dx.replace([np.inf, -np.inf], np.nan).dropna(), length)
    if value.empty:
        return None
    last = float(value.iloc[-1])
    return last if math.isfinite(last) else None


# --- Public API --------------------------------------------------------------


def analyze_regime(df: pd.DataFrame, vr_lag: int = 8) -> RegimeReport:
    """Characterise the series. Strategy-agnostic: no family is assumed here."""
    if df is None or len(df) == 0 or "close" not in df:
        return RegimeReport(0, None, None, None, None, None, None, VERDICT_INSUFFICIENT, vr_lag)

    close = pd.to_numeric(df["close"], errors="coerce").astype(float)
    close = close[close > 0]
    log_ret = np.diff(np.log(close.to_numpy()))
    log_ret = log_ret[np.isfinite(log_ret)]
    n = log_ret.size

    if n < _MIN_OBS:
        return RegimeReport(n, None, None, None, None, None, adx(df), VERDICT_INSUFFICIENT, vr_lag)

    h = hurst_dfa(log_ret)
    vr, z = variance_ratio(log_ret, q=vr_lag)
    rho1 = float(np.corrcoef(log_ret[:-1], log_ret[1:])[0, 1]) if n > 2 else None
    if rho1 is not None and not math.isfinite(rho1):
        rho1 = None

    # One-sided p-value in the direction the data actually points.
    p_one = None
    verdict = VERDICT_RANDOM_WALK
    if z is not None:
        p_one = 1.0 - _norm_cdf(abs(z))
        # Provisional verdict at the laxer of the two thresholds; the gate
        # applies the family-specific alpha itself.
        if p_one < max(_ALPHA.values()):
            verdict = VERDICT_TRENDING if z > 0 else VERDICT_MEAN_REVERTING

    return RegimeReport(
        n_obs=n, hurst=h, variance_ratio=vr, vr_z=z, vr_p_one_sided=p_one,
        autocorr_1=rho1, adx=adx(df), verdict=verdict, vr_lag=vr_lag,
    )


def regime_gate(
    df: pd.DataFrame,
    strategy_name: str,
    report: RegimeReport | None = None,
    vr_lag: int = 8,
) -> GateDecision:
    """Decide whether `strategy_name` may be optimised on this series.

    Returns a decision object; it does not raise and does not itself block
    anything. Enforcement policy belongs to the caller, so that the same
    measurement can drive a hard block, a warning banner, or a report field
    without this module taking that product decision.
    """
    rep = report if report is not None else analyze_regime(df, vr_lag=vr_lag)
    family = STRATEGY_FAMILY.get(strategy_name)
    warnings: list[str] = []

    if family is None:
        return GateDecision(
            passed=True, family=None,
            reason=f"Strategy {strategy_name!r} has no known family — no gate applied.",
            report=rep, warnings=["strategy_not_in_family_map"],
        )

    alpha = _ALPHA[family]

    if rep.verdict == VERDICT_INSUFFICIENT or rep.vr_z is None or rep.vr_p_one_sided is None:
        return GateDecision(
            passed=False, family=family, alpha=alpha,
            reason=(
                f"Not enough history to establish the regime "
                f"({rep.n_obs} returns, at least {_MIN_OBS} needed)."
            ),
            report=rep,
        )

    wants_persistence = family == FAMILY_TREND
    direction_ok = (rep.vr_z > 0) if wants_persistence else (rep.vr_z < 0)
    significant = rep.vr_p_one_sided < alpha

    if rep.adx is not None and rep.adx < 15 and wants_persistence:
        warnings.append("adx_basso_mercato_laterale")
    if rep.adx is not None and rep.adx > 35 and not wants_persistence:
        warnings.append("adx_alto_mercato_in_trend")

    needed = "persistence" if wants_persistence else "mean reversion"
    if direction_ok and significant:
        return GateDecision(
            passed=True, family=family, alpha=alpha,
            reason=(
                f"Compatible regime: the series shows {needed} "
                f"(VR={rep.variance_ratio:.3f}, z={rep.vr_z:+.2f}, p={rep.vr_p_one_sided:.4f} < {alpha})."
            ),
            report=rep, warnings=warnings,
        )

    if not direction_ok and significant:
        observed = "persistence" if rep.vr_z > 0 else "mean reversion"
        return GateDecision(
            passed=False, family=family, alpha=alpha,
            reason=(
                f"Regime opposite to the one required: the strategy needs {needed}, "
                f"but the series shows statistically significant {observed} "
                f"(VR={rep.variance_ratio:.3f}, z={rep.vr_z:+.2f}). "
                f"Optimizing here would produce a configuration fitted to noise."
            ),
            report=rep, warnings=warnings,
        )

    return GateDecision(
        passed=False, family=family, alpha=alpha,
        reason=(
            f"No measurable edge on this timeframe: a random walk can't be ruled out "
            f"(VR={rep.variance_ratio:.3f}, z={rep.vr_z:+.2f}, p={rep.vr_p_one_sided:.4f} ≥ {alpha}). "
            f"Any optimizer result would be fitting to noise."
        ),
        report=rep, warnings=warnings,
    )
