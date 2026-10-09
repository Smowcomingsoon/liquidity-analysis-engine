"""Tests for structural priors and space reduction (stage S2)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.analysis.priors import (
    _narrow,
    band_multiplier_for_frequency,
    build_spaces,
    estimate_series_stats,
    unconstrained_dims,
    verify_inert,
)
from app.analysis.strategies import DEFAULT_PARAMS

LORENZO = "Lorenzo (trend-follower)"
GABRIELE = "Gabriele (RSI+BB reversal)"
EXHAUSTION = "Accumulo / Distribuzione (long-term)"


def _series(close: np.ndarray) -> pd.DataFrame:
    n = len(close)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1e6)},
        index=pd.date_range("2022-01-01", periods=n, freq="D"),
    )


def _ar1(n, phi, seed, sd=0.02):
    rng = np.random.default_rng(seed)
    e = rng.normal(0, sd, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    return 100 * np.exp(np.cumsum(r))


# --- derived band multiplier -------------------------------------------------


def test_band_multiplier_matches_the_normal_tail():
    """k is defined by 2(1-Phi(k)) = N/T, so it must invert exactly."""
    k = band_multiplier_for_frequency(40, 1200)
    tail = 2 * (1 - 0.5 * math.erfc(-k / math.sqrt(2)))
    assert tail == pytest.approx(40 / 1200, rel=0.02)


def test_fewer_entries_means_a_wider_band():
    assert band_multiplier_for_frequency(10, 1200) > band_multiplier_for_frequency(100, 1200)


def test_band_multiplier_stays_inside_the_strategy_inputs():
    for n in (1, 5, 50, 500, 5000):
        assert 1.0 <= band_multiplier_for_frequency(n, 1200) <= 3.5


def test_band_multiplier_survives_degenerate_input():
    assert band_multiplier_for_frequency(0, 0) == 2.0


# --- refusing to collapse a range --------------------------------------------


def test_narrow_applies_a_measured_scale_that_fits():
    lo, hi, note = _narrow((10, 40), (15.0, 30.0), "x")
    assert (lo, hi) == (15, 30)
    assert "unnarrowed" not in note


def test_narrow_refuses_to_pin_a_parameter_to_one_value():
    """When the measured scale sits outside what the strategy allows, clipping
    both ends lands them on the same bound and silently fixes the parameter.
    That is a different strategy chosen by an estimator that has just shown it
    does not apply here — so the declared range must survive instead."""
    lo, hi, note = _narrow((10, 40), (152.0, 457.0), "bb_length")
    assert (lo, hi) == (10, 40)
    assert "unnarrowed" in note
    assert "152" in note


def test_narrow_refuses_a_range_too_thin_to_search():
    lo, hi, note = _narrow((3, 20), (19.0, 20.0), "swing")
    assert (lo, hi) == (3, 20)
    assert "unnarrowed" in note


# --- series statistics -------------------------------------------------------


def test_tau_int_tracks_autocorrelation():
    pos = estimate_series_stats(_series(_ar1(800, +0.30, 1)))
    neg = estimate_series_stats(_series(_ar1(800, -0.30, 2)))
    assert pos.rho1 > 0.15 and pos.tau_int > 1.3
    assert neg.rho1 < -0.15 and neg.tau_int < 0.8


def test_dominant_period_rejects_residual_trend():
    """The lowest frequencies always dominate the periodogram of a detrended
    price series. A reported period equal to half the sample is that artefact,
    and anchoring parameters to it is worse than having no prior."""
    st = estimate_series_stats(_series(_ar1(1200, 0.0, 3)))
    assert st.dominant_period is None or st.dominant_period <= 1200 / 4


def test_dominant_period_finds_a_real_cycle():
    n, period = 1200, 50
    t = np.arange(n)
    close = 100 * np.exp(0.25 * np.sin(2 * np.pi * t / period)
                         + np.cumsum(np.random.default_rng(4).normal(0, 0.001, n)))
    st = estimate_series_stats(_series(close))
    assert st.dominant_period is not None
    assert st.dominant_period == pytest.approx(period, rel=0.25)


def test_short_series_reports_no_scales():
    st = estimate_series_stats(_series(_ar1(30, 0.0, 5)))
    assert st.ou_half_life is None and st.dominant_period is None


# --- constrained spaces ------------------------------------------------------


@pytest.mark.parametrize("strategy", [LORENZO, GABRIELE, EXHAUSTION])
def test_constrained_spaces_produce_feasible_params(strategy):
    st = estimate_series_stats(_series(_ar1(900, 0.2, 6)))
    rng = np.random.default_rng(7)
    for cs in build_spaces(strategy, st):
        for u in [np.zeros(cs.d), np.ones(cs.d)] + [rng.random(cs.d) for _ in range(80)]:
            p = cs.to_params(u)
            assert isinstance(p, dict) and p


def test_lorenzo_slope_constraint_survives_the_ratio_parameterisation():
    st = estimate_series_stats(_series(_ar1(900, 0.2, 8)))
    rng = np.random.default_rng(9)
    for cs in build_spaces(LORENZO, st):
        for u in [np.zeros(cs.d), np.ones(cs.d)] + [rng.random(cs.d) for _ in range(200)]:
            p = cs.to_params(u)
            assert p["slopeSlen"] >= p["slopeFlen"] + 3


def test_lorenzo_branches_drop_the_parameters_that_are_switched_off():
    """The branch decomposition is the whole reason Lorenzo becomes tractable.

    The counts moved by one when `slope_threshold_sigma` was added — a searched
    dimension, not a filter's — so what is pinned here is the property rather
    than the arithmetic: a switched-off filter contributes no dimension, and
    turning both on costs exactly four.
    """
    st = estimate_series_stats(_series(_ar1(900, 0.2, 10)))
    spaces = {cs.label: cs for cs in build_spaces(LORENZO, st)}
    assert len(spaces) == 4

    plain = spaces["trendfilter=False volatilityfilter=False"]
    assert not any(d.startswith("trendfilter") or d.startswith("volatility") for d in plain.dims)

    full = spaces["trendfilter=True volatilityfilter=True"]
    assert full.d == plain.d + 4
    assert set(plain.dims) < set(full.dims)


def test_branch_decomposition_shrinks_the_grid_by_orders_of_magnitude():
    """Il confronto è a parità di assi: la decomposizione va misurata su quello
    che toglie, non sommata a una dimensione aggiunta dopo per un'altra ragione.

    `slope_threshold_sigma` è cercata in più — costa un asse e se lo merita —
    ma metterla nel conto qui trasformerebbe questo test in una misura di due
    cose insieme, e nessuna delle due si leggerebbe più.
    """
    st = estimate_series_stats(_series(_ar1(900, 0.2, 11)))
    for strategy in (LORENZO, EXHAUSTION):
        spaces = build_spaces(strategy, st)
        added = {"slope_threshold_sigma"}
        before = 4.0 ** unconstrained_dims(strategy)
        after = sum(4.0 ** len(set(cs.dims) - added) for cs in spaces)
        assert after < before / 10


def test_gabriele_only_searches_rsi_overbought_and_oversold():
    """72f880af: il tasto Ottimizza tocca solo RSI ipercomprato/ipervenduto —
    lunghezza RSI, lunghezza Bollinger e moltiplicatore restano ai valori di
    chi cerca, non sono dimensioni della ricerca."""
    st = estimate_series_stats(_series(_ar1(900, 0.2, 12)))
    cs = build_spaces(GABRIELE, st, target_entries=40)[0]
    assert set(cs.dims) == {"rsi_oversold", "rsi_overbought"}
    assert cs.d == 2
    for key in ("rsi_length", "bb_length", "bb_mult"):
        fixed = cs.to_params(np.full(cs.d, 0.5))[key]
        assert fixed == pytest.approx(cs.fixed[key])
        # every point in the cube agrees on it — it is held, not searched
        rng = np.random.default_rng(13)
        for u in [rng.random(cs.d) for _ in range(20)]:
            assert cs.to_params(u)[key] == pytest.approx(fixed)


def test_gabriele_always_holds_14_20_2():
    """6453f4df: i valori nei campi non cambiano i tre parametri fissi."""
    st = estimate_series_stats(_series(_ar1(900, 0.2, 21)))
    cs = build_spaces(GABRIELE, st, locked={"rsi_length": 9, "bb_length": 25, "bb_mult": 1.4})[0]
    params = cs.to_params(np.full(cs.d, 0.3))
    assert params["rsi_length"] == 14
    assert params["bb_length"] == 20
    assert params["bb_mult"] == pytest.approx(2.0)


def test_spaces_record_why_each_constraint_applied_or_did_not():
    st = estimate_series_stats(_series(_ar1(900, 0.2, 14)))
    for strategy in (LORENZO, GABRIELE, EXHAUSTION):
        for cs in build_spaces(strategy, st):
            assert cs.notes and all(isinstance(n, str) and n for n in cs.notes)


def test_unknown_strategy_raises():
    st = estimate_series_stats(_series(_ar1(300, 0.0, 15)))
    with pytest.raises(ValueError):
        build_spaces("Nope", st)


# --- inertness ---------------------------------------------------------------


def test_switched_off_filters_are_verifiably_inert():
    """The branch decomposition is only sound if a dropped parameter truly does
    not enter the computation. Reading the strategy is not evidence; a value
    read on some unfollowed path would corrupt the decomposition and the
    failure would look like noise rather than like a bug."""
    df = _series(_ar1(700, 0.15, 16))
    off = verify_inert(
        df, LORENZO, {**DEFAULT_PARAMS, "trendfilter": False, "volatilityfilter": False},
        ["trendfilterperiod", "trendfiltertype", "volatilitystdevlength"],
        {"trendfilterperiod": [50, 200, 400], "trendfiltertype": ["EMA", "SMA"],
         "volatilitystdevlength": [5, 20, 40]},
    )
    assert all(off.values()), f"expected all inert when switched off, got {off}"


def test_switched_on_filters_are_not_inert():
    df = _series(_ar1(700, 0.15, 17))
    on = verify_inert(
        df, LORENZO, {**DEFAULT_PARAMS, "trendfilter": True, "volatilityfilter": True},
        ["trendfilterperiod"], {"trendfilterperiod": [50, 200, 400]},
    )
    assert on["trendfilterperiod"] is False
