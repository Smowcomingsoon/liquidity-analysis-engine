"""Filtri sul risultato dell'ottimizzazione: trade in perdita e drawdown.

La richiesta era poter chiedere "solo configurazioni con al massimo N trade in
perdita (anche 0)" o "entro il X% di drawdown (anche 0%)". I due casi limite —
zero e zero — sono il motivo per cui questi test esistono: sono esattamente
quelli che un'implementazione con valori di default falsy lascia passare senza
filtrare nulla.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.optimize import (
    Constraints,
    NoCandidateMeetsConstraints,
    losing_trades,
    optimize_strategy,
)
from app.models import BacktestResult, BacktestTrade

# Lorenzo su una serie in trend: serve una configurazione che superi i filtri
# di punteggio già esistenti, altrimenti la classifica resta vuota per ragioni
# che non hanno nulla a che vedere con i vincoli sotto esame.
STRATEGY = "Lorenzo (trend-follower)"


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    n = 800
    close = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.02, n)))
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
            "low": close * (1 - np.abs(rng.normal(0, 0.01, n))),
            "close": close,
            "volume": rng.uniform(1e6, 5e6, n),
        },
        index=pd.date_range("2021-01-01", periods=n, freq="D"),
    )


def _trade(pnl: float) -> BacktestTrade:
    return BacktestTrade(
        direction="long", entry_time=0, exit_time=1, entry_price=1.0,
        exit_price=1.0 + pnl, pnl_pct=pnl, pnl_abs=pnl, equity_after=100.0,
    )


def _bt(pnls: list[float], drawdown: float = -10.0) -> BacktestResult:
    return BacktestResult(
        interval="1d", initial_capital=100.0, final_equity=110.0,
        total_return_pct=10.0, num_trades=len(pnls), win_rate_pct=None,
        max_drawdown_pct=drawdown, profit_factor=None, equity_curve=[],
        trades=[_trade(p) for p in pnls],
    )


# --- il conteggio ---------------------------------------------------------


def test_losing_trades_counts_only_negatives():
    assert losing_trades(_bt([1.0, -1.0, -2.0, 0.0, 3.0])) == 2


def test_a_flat_trade_is_not_a_loss():
    assert losing_trades(_bt([0.0, 0.0])) == 0


# --- i casi limite, che sono il punto ------------------------------------


def test_zero_losing_trades_is_a_real_constraint():
    """`max_losing_trades=0` non deve essere confuso con "non impostato"."""
    c = Constraints(max_losing_trades=0)
    assert c.active
    assert c.accepts(_bt([1.0, 2.0]))
    assert not c.accepts(_bt([1.0, -0.5]))


def test_zero_drawdown_is_a_real_constraint():
    c = Constraints(max_drawdown_pct=0.0)
    assert c.active
    assert c.accepts(_bt([1.0], drawdown=0.0))
    assert not c.accepts(_bt([1.0], drawdown=-0.4))


def test_no_constraints_accepts_everything():
    c = Constraints()
    assert not c.active
    assert c.accepts(_bt([-5.0, -6.0], drawdown=-90.0))


def test_drawdown_is_compared_in_absolute_value():
    """Il campo è negativo nel backtest; il vincolo è espresso in positivo."""
    c = Constraints(max_drawdown_pct=20.0)
    assert c.accepts(_bt([1.0], drawdown=-15.0))
    assert not c.accepts(_bt([1.0], drawdown=-25.0))


def test_minimum_and_maximum_together():
    c = Constraints(min_losing_trades=2, max_losing_trades=3)
    assert not c.accepts(_bt([-1.0]))
    assert c.accepts(_bt([-1.0, -1.0]))
    assert not c.accepts(_bt([-1.0] * 4))


# --- l'effetto sulla ricerca ---------------------------------------------


def test_the_search_respects_the_drawdown_ceiling(df):
    _p, bt, _ev, _oos, _ovf, top = optimize_strategy(
        STRATEGY, df, {}, trials=50, seed=3,
        constraints=Constraints(max_drawdown_pct=4.0),
    )
    assert top, "nessun candidato: il test non sta verificando il filtro"
    for c in top:
        assert abs(c["backtest"].max_drawdown_pct) <= 4.0


def test_the_search_respects_the_losing_trades_ceiling(df):
    _p, bt, _ev, _oos, _ovf, top = optimize_strategy(
        STRATEGY, df, {}, trials=50, seed=3,
        constraints=Constraints(max_losing_trades=10),
    )
    assert top
    for c in top:
        assert losing_trades(c["backtest"]) <= 10


def test_an_impossible_constraint_is_reported_not_silently_ignored(df):
    """Ripiegare sui default darebbe una risposta che sembra valida e viola
    il filtro appena chiesto: il caso peggiore, perché è invisibile."""
    with pytest.raises(NoCandidateMeetsConstraints) as exc:
        optimize_strategy(
            STRATEGY, df, {}, trials=50, seed=3,
            constraints=Constraints(max_drawdown_pct=0.0, max_losing_trades=0),
        )
    assert "filters" in str(exc.value)


def test_without_constraints_the_result_is_unchanged(df):
    a = optimize_strategy(STRATEGY, df, {}, trials=50, seed=5)
    b = optimize_strategy(STRATEGY, df, {}, trials=50, seed=5, constraints=Constraints())
    assert a[0] == b[0]
    assert a[1].total_return_pct == b[1].total_return_pct
