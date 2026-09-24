"""Tests for the regime gate (pipeline stage S0).

The gate decides whether the optimiser is allowed to run at all, so its
failure mode is silent and expensive: a wrong verdict does not raise, it just
lets a strategy be fitted to a series where it has no edge. These tests pin
the behaviour against series whose statistical character is constructed, so
the expected answer is known independently of the implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.regime import (
    FAMILY_REVERSION,
    FAMILY_TREND,
    STRATEGY_FAMILY,
    VERDICT_INSUFFICIENT,
    VERDICT_MEAN_REVERTING,
    VERDICT_RANDOM_WALK,
    VERDICT_TRENDING,
    analyze_regime,
    regime_gate,
    variance_ratio,
)

TREND_STRATEGY = "Lorenzo (trend-follower)"
REVERSION_STRATEGY = "Gabriele (RSI+BB reversal)"
EXHAUSTION_STRATEGY = "Accumulo / Distribuzione (long-term)"

N = 1200


def _ohlc(close: np.ndarray, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(close)
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * (1 + np.abs(rng.normal(0, 0.008, n))),
            "low": close * (1 - np.abs(rng.normal(0, 0.008, n))),
            "close": close,
            "volume": rng.uniform(1e6, 5e6, n),
        },
        index=pd.date_range("2022-01-01", periods=n, freq="D"),
    )


def _ar1(n: int, phi: float, seed: int, sd: float = 0.02) -> np.ndarray:
    """Price series whose log-returns follow AR(1) with coefficient phi.

    phi > 0 -> positively autocorrelated returns (variance ratio above 1)
    phi = 0 -> random walk
    phi < 0 -> mean reverting (variance ratio below 1)
    """
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, sd, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + eps[i]
    return 100.0 * np.exp(np.cumsum(r))


@pytest.fixture(scope="module")
def random_walk() -> pd.DataFrame:
    return _ohlc(_ar1(N, 0.0, seed=1), seed=11)


@pytest.fixture(scope="module")
def persistent() -> pd.DataFrame:
    return _ohlc(_ar1(N, +0.25, seed=2), seed=12)


@pytest.fixture(scope="module")
def mean_reverting() -> pd.DataFrame:
    return _ohlc(_ar1(N, -0.25, seed=3), seed=13)


# --- variance ratio ----------------------------------------------------------


def test_variance_ratio_is_one_on_a_random_walk():
    """The normalisation is the part that is easy to get wrong: m already
    carries the factor q, so an extra division by q drives VR to ~1/q and
    every series looks mean reverting."""
    rng = np.random.default_rng(7)
    vr, z = variance_ratio(rng.normal(0, 0.02, 4000), q=8)
    assert vr == pytest.approx(1.0, abs=0.15), f"VR on white noise should be ~1, got {vr}"
    assert abs(z) < 2.6


def test_variance_ratio_sign_follows_autocorrelation():
    def rets(phi, seed):
        return np.diff(np.log(_ar1(3000, phi, seed)))

    vr_pos, z_pos = variance_ratio(rets(+0.25, 21), q=8)
    vr_neg, z_neg = variance_ratio(rets(-0.25, 22), q=8)
    assert vr_pos > 1.15 and z_pos > 2
    assert vr_neg < 0.85 and z_neg < -2


def test_variance_ratio_refuses_short_series():
    assert variance_ratio(np.random.default_rng(0).normal(size=40), q=8) == (None, None)


# --- verdicts ----------------------------------------------------------------


def test_verdicts_match_constructed_regimes(random_walk, persistent, mean_reverting):
    assert analyze_regime(random_walk).verdict == VERDICT_RANDOM_WALK
    assert analyze_regime(persistent).verdict == VERDICT_TRENDING
    assert analyze_regime(mean_reverting).verdict == VERDICT_MEAN_REVERTING


def test_short_history_is_reported_not_guessed():
    rep = analyze_regime(_ohlc(_ar1(60, 0.0, seed=5), seed=5))
    assert rep.verdict == VERDICT_INSUFFICIENT
    assert rep.variance_ratio is None


def test_empty_frame_does_not_raise():
    assert analyze_regime(pd.DataFrame()).verdict == VERDICT_INSUFFICIENT


# --- the gate, and the inversion between families ----------------------------


def test_trend_family_passes_only_on_persistence(random_walk, persistent, mean_reverting):
    assert regime_gate(persistent, TREND_STRATEGY).passed
    assert not regime_gate(mean_reverting, TREND_STRATEGY).passed
    assert not regime_gate(random_walk, TREND_STRATEGY).passed


def test_reversion_family_passes_only_on_mean_reversion(random_walk, persistent, mean_reverting):
    assert regime_gate(mean_reverting, REVERSION_STRATEGY).passed
    assert not regime_gate(persistent, REVERSION_STRATEGY).passed
    assert not regime_gate(random_walk, REVERSION_STRATEGY).passed


def test_the_inequality_actually_inverts(persistent, mean_reverting):
    """The whole point of stage S0: the same series gets opposite verdicts
    depending on which family is asking. A single 'needs persistence' gate
    applied to all strategies would block the reversion family exactly where
    it works."""
    assert regime_gate(persistent, TREND_STRATEGY).passed
    assert not regime_gate(persistent, REVERSION_STRATEGY).passed
    assert regime_gate(mean_reverting, REVERSION_STRATEGY).passed
    assert not regime_gate(mean_reverting, TREND_STRATEGY).passed


def test_exhaustion_strategy_is_treated_as_reversion(persistent, mean_reverting):
    assert regime_gate(mean_reverting, EXHAUSTION_STRATEGY).passed
    assert not regime_gate(persistent, EXHAUSTION_STRATEGY).passed


def test_reversion_family_faces_a_stricter_threshold():
    """A reversion strategy in a trending market sells every leg up, so its
    losses are correlated and land inside the largest move of the sample.
    That asymmetry in the cost of being wrong is priced into the gate."""
    trend_alpha = regime_gate(_ohlc(_ar1(N, +0.25, 31)), TREND_STRATEGY).alpha
    rev_alpha = regime_gate(_ohlc(_ar1(N, -0.25, 32)), REVERSION_STRATEGY).alpha
    assert rev_alpha < trend_alpha


def test_blocked_decisions_explain_themselves(persistent):
    d = regime_gate(persistent, REVERSION_STRATEGY)
    assert not d.passed
    assert "mean reversion" in d.reason
    assert d.report.variance_ratio is not None
    assert d.as_dict()["regime"]["variance_ratio"] is not None


def test_unknown_strategy_does_not_silently_block():
    d = regime_gate(_ohlc(_ar1(N, 0.0, 41)), "Strategia Inesistente")
    assert d.passed
    assert d.family is None
    assert "strategy_not_in_family_map" in d.warnings


# --- guard against the literal name map drifting -----------------------------


def test_family_map_covers_every_registered_strategy():
    """regime.py deliberately avoids importing the strategy stack, so the map
    is keyed by literal name. That decoupling is only safe if this test fails
    the moment a strategy is added or renamed."""
    from app.analysis.strategies import STRATEGY_REGISTRY

    missing = set(STRATEGY_REGISTRY) - set(STRATEGY_FAMILY)
    stale = set(STRATEGY_FAMILY) - set(STRATEGY_REGISTRY)
    assert not missing, f"strategies with no family assigned: {missing}"
    assert not stale, f"family map references unknown strategies: {stale}"
    assert set(STRATEGY_FAMILY.values()) <= {FAMILY_TREND, FAMILY_REVERSION}
