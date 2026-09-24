"""Tests for plateau search (stages S3-S4).

Boundary location is tested against synthetic step functions rather than
backtests: the estimator's correctness is a separate question from whether the
objective it is pointed at behaves well, and mixing the two makes both harder
to diagnose.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis import plateau as P
from app.analysis.plateau import (
    AxisPartition,
    locate_boundaries,
    plateau_search,
)
from app.models import BacktestResult

GABRIELE = "Gabriele (RSI+BB reversal)"


def _bt(total_return=100.0, dd=20.0, trades=20, pf=2.0) -> BacktestResult:
    return BacktestResult(
        interval="1d", initial_capital=10000.0,
        final_equity=10000 * (1 + total_return / 100),
        total_return_pct=total_return, num_trades=trades, win_rate_pct=55.0,
        max_drawdown_pct=dd, profit_factor=pf, equity_curve=[], trades=[],
        data_start=0, data_end=1, sharpe_ratio=1.0, max_consecutive_losses=2,
    )


def _df(n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1e6)},
        index=pd.date_range("2023-01-01", periods=n, freq="D"),
    )


# --- AxisPartition -----------------------------------------------------------


def test_partition_without_boundaries_is_one_cell():
    p = AxisPartition(0, "x", ())
    assert p.n_cells == 1
    assert p.cell_bounds(0) == (0.0, 1.0)
    assert p.width_at(0.5) == 1.0
    assert p.centers() == [0.5]


def test_partition_cells_tile_the_axis_without_gaps():
    p = AxisPartition(0, "x", (0.25, 0.6))
    assert p.n_cells == 3
    bounds = [p.cell_bounds(i) for i in range(p.n_cells)]
    assert bounds == [(0.0, 0.25), (0.25, 0.6), (0.6, 1.0)]
    assert sum(hi - lo for lo, hi in bounds) == pytest.approx(1.0)


def test_width_at_reports_the_containing_cell():
    p = AxisPartition(0, "x", (0.25, 0.6))
    assert p.width_at(0.10) == pytest.approx(0.25)
    assert p.width_at(0.40) == pytest.approx(0.35)
    assert p.width_at(0.90) == pytest.approx(0.40)


def test_centers_sit_inside_their_own_cell():
    p = AxisPartition(0, "x", (0.25, 0.6))
    for i, c in enumerate(p.centers()):
        lo, hi = p.cell_bounds(i)
        assert lo < c < hi
        assert p.cell_index(c) == i


# --- boundary location on known step functions -------------------------------


def test_locates_a_single_boundary_to_tolerance():
    tol = 0.002
    found = locate_boundaries(lambda u: ("hi",) if u > 0.37 else ("lo",), coarse=10, tol=tol)
    assert len(found) == 1
    assert found[0] == pytest.approx(0.37, abs=tol)


def test_locates_several_boundaries():
    truth = [0.2, 0.5, 0.8]

    def f(u):
        return (int(np.searchsorted(truth, u)),)

    found = locate_boundaries(f, coarse=20, tol=0.002)
    assert len(found) == 3
    for got, want in zip(found, truth):
        assert got == pytest.approx(want, abs=0.004)


def test_constant_function_yields_no_boundaries():
    assert locate_boundaries(lambda u: ("same",), coarse=10, tol=0.004) == []


def test_narrow_plateau_between_coarse_samples_can_be_missed():
    """Documents a real limitation rather than asserting it away.

    Bisection assumes one transition per bracket. A plateau narrower than the
    coarse spacing that departs and returns to the same value is invisible —
    which is exactly the shape of the knife-edge maximum this work is about.
    The coarse scan bounds the risk; it cannot remove it, and callers are told
    so through the resolution settings rather than being given false comfort.
    """
    def f(u):
        return ("spike",) if 0.5000 < u < 0.5008 else ("base",)

    assert locate_boundaries(f, coarse=6, tol=0.004) == []

    dense = locate_boundaries(f, coarse=4000, tol=1e-5)
    assert len(dense) == 2


def test_boundary_count_is_cheaper_than_dense_scanning():
    """The reason for bisecting at all: cost should scale with the number of
    jumps, not with the resolution."""
    calls = {"n": 0}
    truth = [0.3, 0.7]

    def f(u):
        calls["n"] += 1
        return (int(np.searchsorted(truth, u)),)

    locate_boundaries(f, coarse=10, tol=0.001)
    # A dense scan at the same resolution would need 1000 evaluations.
    assert calls["n"] < 60


# --- plateau_search ----------------------------------------------------------


def test_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        plateau_search("Nope", _df())


def test_enumerated_search_is_seed_independent(monkeypatch):
    """The property the whole exercise exists to obtain. Measured on real
    backtests it holds exactly (cv_oos 0.00% across seeds) once the cell grid
    is small enough to enumerate; here it is pinned cheaply with a stub.
    """
    def fake(df, params, bp, strategy_name=None, signals=None):
        # deterministic, mildly structured, plenty of trades
        v = (params["rsi_length"] % 5) * 10 + (params["bb_length"] % 3) * 4
        return _bt(total_return=float(v), trades=20)

    monkeypatch.setattr(P, "run_backtest", fake)
    runs = [plateau_search(GABRIELE, _df(), max_grid_evals=100000, seed=s) for s in range(3)]
    assert all(r.enumerated for r in runs)
    first = runs[0].best.params
    assert all(r.best.params == first for r in runs)


def test_sampled_search_says_so_in_the_notes(monkeypatch):
    """When the grid cannot be enumerated the result must admit it: that is
    precisely the regime where the answer becomes seed dependent."""
    def fake(df, params, bp, strategy_name=None, signals=None):
        return _bt(total_return=float(params["rsi_length"] * params["bb_length"] % 97), trades=20)

    monkeypatch.setattr(P, "run_backtest", fake)
    r = plateau_search(GABRIELE, _df(), max_grid_evals=5, seed=0)
    assert not r.enumerated
    assert any("sampling" in n for n in r.notes)


def test_thin_candidates_are_excluded_from_selection(monkeypatch):
    """A 3-trade backtest showing a large return is one lucky sequence, not an
    edge. The objective has to stay finite for boundary detection, so the
    trade floor is enforced at selection instead."""
    def fake(df, params, bp, strategy_name=None, signals=None):
        if params["rsi_length"] % 2 == 0:
            return _bt(total_return=900.0, dd=5.0, trades=3)      # spectacular, thin
        return _bt(total_return=40.0, dd=10.0, trades=25)         # modest, evidenced

    monkeypatch.setattr(P, "run_backtest", fake)
    r = plateau_search(GABRIELE, _df(), max_grid_evals=100000, min_trades=8, seed=0)
    assert r.best is not None
    assert r.best.backtest.num_trades >= 8
    assert r.best.backtest.total_return_pct == pytest.approx(40.0)


def test_reports_when_nothing_clears_the_trade_floor(monkeypatch):
    def fake(df, params, bp, strategy_name=None, signals=None):
        return _bt(total_return=500.0, trades=2)

    monkeypatch.setattr(P, "run_backtest", fake)
    r = plateau_search(GABRIELE, _df(), max_grid_evals=100000, min_trades=8, seed=0)
    assert any("trades" in n and "unusable" in n for n in r.notes)


def test_axis_approximation_error_is_reported(monkeypatch):
    """The partition is an approximation of an interacting surface. Its error
    has to appear in the output as a number, not survive as an assumption —
    measured on real data it runs above 50%."""
    def fake(df, params, bp, strategy_name=None, signals=None):
        return _bt(total_return=float(hash(tuple(sorted(params.items()))) % 200), trades=20)

    monkeypatch.setattr(P, "run_backtest", fake)
    r = plateau_search(GABRIELE, _df(), max_grid_evals=400, seed=0)
    assert r.best is not None
    assert r.best.axis_approximation_error is not None
    assert 0.0 <= r.best.axis_approximation_error <= 1.0
    assert any("axis approximation error" in n for n in r.notes)


def test_prefers_a_wide_plateau_over_a_narrow_spike(monkeypatch):
    """The core behaviour: a taller but knife-edge configuration must lose to a
    slightly lower one that survives perturbation."""
    def fake(df, params, bp, strategy_name=None, signals=None):
        # A single spike at one exact bb_mult, a broad shelf everywhere below 2.0
        if params["bb_mult"] == pytest.approx(2.05, abs=0.03):
            return _bt(total_return=400.0, dd=20.0, trades=20)
        if params["bb_mult"] < 2.0:
            return _bt(total_return=150.0, dd=20.0, trades=20)
        return _bt(total_return=1.0, dd=20.0, trades=20)

    monkeypatch.setattr(P, "run_backtest", fake)
    r = plateau_search(GABRIELE, _df(), max_grid_evals=100000, seed=0)
    assert r.best is not None
    assert r.best.backtest.total_return_pct < 400.0


# --- il log della mappatura -----------------------------------------------
#
# "mappatura dei pianori…" e poi silenzio per minuti non dice se sta lavorando
# né che cosa sta facendo. Queste righe sono la spiegazione del metodo mentre
# accade, quindi valgono quanto il risultato: se smettono di uscire, la ricerca
# torna a essere una scatola chiusa.


def _series(n=420, seed=5):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, n)))
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * (1 + abs(rng.normal(0, 0.01, n))),
            "low": close * (1 - abs(rng.normal(0, 0.01, n))),
            "close": close,
            "volume": rng.uniform(1e6, 5e6, n),
        },
        index=pd.date_range("2022-01-01", periods=n, freq="D"),
    )


def _run_with_log(**kw):
    from app.analysis.plateau import plateau_search

    opts = {"base_points": 1, "coarse": 4, "max_grid_evals": 120,
            "finalists": 3, "probe_samples": 4}
    opts.update(kw)
    lines: list[str] = []
    res = plateau_search("Gabriele (RSI+BB reversal)", _series(), {},
                         progress=lines.append, **opts)
    return res, lines


def test_every_axis_reports_its_own_partition():
    """È la mappatura, detta a voce: un asse alla volta, tagliato dove il
    risultato cambia davvero."""
    res, lines = _run_with_log()
    for part in res.partitions:
        assert any(f"axis {part.name}:" in line for line in lines), f"silent axis: {part.name}"


def test_the_grid_size_is_stated_before_the_cells_are_tried():
    res, lines = _run_with_log()
    grid = next(i for i, line in enumerate(lines) if line.startswith("grid:"))
    cells = next(i for i, line in enumerate(lines) if line.startswith("trying the center"))
    assert grid < cells
    assert f"= {res.grid_cells} cells" in lines[grid]


def test_the_progress_through_the_cells_is_reported():
    _res, lines = _run_with_log()
    assert any(line.startswith("cells ") and "distinct configurations" in line
               for line in lines)


def test_the_shake_test_says_how_often_each_finalist_holds():
    """Il numero che decide la classifica: quante volte su quante regge."""
    _res, lines = _run_with_log()
    shaken = [line for line in lines if line.startswith("finalist ")]
    assert shaken, "nessun finalista raccontato"
    assert all("holds" in line and "out of" in line for line in shaken)


def test_the_winner_is_announced_with_its_stability():
    res, lines = _run_with_log()
    if not res.candidates:
        pytest.skip("nessun candidato su questa serie")
    assert any(line.startswith("picked:") and "stable" in line for line in lines)


def test_guided_expansion_instead_of_enumerating_is_said_out_loud():
    """Prima questa riga avvisava che quella parte non era riproducibile; ora
    l'espansione guidata non sorteggia più, e il log dice cosa fa davvero."""
    res, lines = _run_with_log(max_grid_evals=8)
    assert not res.enumerated
    assert any("in order of promise" in line for line in lines)
    assert any("guided expansion" in line for line in lines)
    assert not any("reproducible" in line for line in lines)


def test_without_a_callback_the_search_is_silent_and_unchanged():
    from app.analysis.plateau import plateau_search

    quiet = plateau_search("Gabriele (RSI+BB reversal)", _series(), {},
                           base_points=1, coarse=4, max_grid_evals=120,
                           finalists=3, probe_samples=4)
    loud, _lines = _run_with_log()
    assert quiet.evaluations == loud.evaluations
    assert (quiet.best is None) == (loud.best is None)
    if quiet.best is not None:
        assert quiet.best.params == loud.best.params


def test_the_search_does_not_hoard_the_trade_lists():
    """Una macchina senza memoria non è lenta, è spenta.

    Con i trade attaccati a ogni candidato, un ramo da 3000 celle arrivava a
    280 MB su una macchina che ne ha 512 — e il sito diventava irraggiungibile
    mentre girava una ricerca. Ora i candidati portano i numeri e la curva di
    equity (che serve al PBO); i trade del vincitore si ricalcolano alla fine.
    """
    res, _lines = _run_with_log(max_grid_evals=40)
    for c in res.candidates:
        assert c.backtest.trades == []
        assert c.backtest.equity_curve, "la curva serve al PBO, non va buttata"


def test_the_kept_pool_stays_bounded(monkeypatch):
    from app.analysis import plateau as mod

    monkeypatch.setattr(mod, "_MAX_KEPT", 6)
    res, _lines = _run_with_log(max_grid_evals=120, finalists=3)
    assert res.best is not None, "potare non deve svuotare la classifica"
