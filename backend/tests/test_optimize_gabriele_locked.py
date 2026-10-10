"""
Il tasto Ottimizza per le due varianti Gabriele deve toccare solo RSI
ipercomprato/ipervenduto, lasciando lunghezza RSI, lunghezza Bollinger e
moltiplicatore esattamente ai valori messi da chi cerca nei campi del Lab.

Segnalazione 72f880af (whitekobra): "lascia sempre predefiniti i parametri
LUNGHEZZA RSI (14) LUNGHEZZA BOLLINGER (20) e MOLTIPLICATORE BOLLINGER (2).
Intervieni solo sui parametri RSI IPERCOMPRATO e RSI IPERVENDUTO" — non era
un difetto da spiegare, era una richiesta: la ricerca (in tutte e tre le
modalita', random/grid/plateau) deve rispettarla davvero, non solo lasciare
quei tre parametri vicino ai default per come si comporta a caso.
"""

import random

from app.analysis.optimize import (
    _GRID_GABRIELE,
    _grid_candidates,
    _random_gabriele_params,
    grid_size,
)


def test_random_search_only_touches_overbought_oversold():
    """6453f4df: anche con altri valori nei campi, i tre restano 14/20/2."""
    rng = random.Random(0)
    base = {"rsi_length": 9, "bb_length": 33, "bb_mult": 1.7}
    for _ in range(50):
        p = _random_gabriele_params(rng, base)
        assert p["rsi_length"] == 14
        assert p["bb_length"] == 20
        assert p["bb_mult"] == 2.0
        assert 15 <= p["rsi_oversold"] <= 50
        assert 50 <= p["rsi_overbought"] <= 85


def test_random_search_falls_back_to_defaults_without_base():
    rng = random.Random(1)
    p = _random_gabriele_params(rng)
    assert p["rsi_length"] == 14
    assert p["bb_length"] == 20
    assert p["bb_mult"] == 2.0


def test_grid_only_enumerates_overbought_oversold():
    assert set(_GRID_GABRIELE.keys()) == {"rsi_oversold", "rsi_overbought"}
    candidates = list(_grid_candidates("Gabriele (RSI+BB reversal)"))
    assert len(candidates) == grid_size("Gabriele (RSI+BB reversal)")
    for c in candidates:
        assert set(c.keys()) == {"rsi_oversold", "rsi_overbought"}


def test_optimize_keeps_14_20_2_whatever_the_fields_say():
    import numpy as np
    import pandas as pd

    from app.analysis.optimize import optimize_strategy
    n = 400
    c = 100 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.03, n)))
    df = pd.DataFrame({"open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1e6},
                      index=pd.date_range("2024-01-01", periods=n, freq="D"))
    for mode in ("random", "grid"):
        best, *_rest, top = optimize_strategy(
            "Gabriele (RSI+BB reversal)", df, {"initial_capital": 10000}, mode=mode, seed=1,
            trials=30, base_params={"rsi_length": 9, "bb_length": 33, "bb_mult": 1.7})
        for p in [best] + [t["params"] for t in top]:
            assert (p["rsi_length"], p["bb_length"], p["bb_mult"]) == (14, 20, 2.0)


def test_fixed_gabriele_still_returns_candidates():
    """6453f4df "non produce risultati": with 14/20/2 fixed the strategy
    trades rarely, and the general 6-12 trade floor emptied the board."""
    import numpy as np
    import pandas as pd

    from app.analysis.optimize import optimize_strategy
    n = 1000
    c = 100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.035, n)))
    df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6},
                      index=pd.date_range("2022-01-01", periods=n, freq="D"))
    *_head, top = optimize_strategy("Gabriele (RSI+BB reversal)", df, {"initial_capital": 10000},
                                    mode="grid", seed=1)
    assert top


def test_fixed_gabriele_board_never_empty_when_nothing_passes():
    """e0bffe25: on a series where no configuration has a positive return the
    board was empty; it now shows the best by return, labelled exploratory."""
    import numpy as np
    import pandas as pd

    from app.analysis.optimize import optimize_strategy
    n = 600
    c = 100 * np.exp(np.cumsum(np.random.default_rng(5).normal(-0.004, 0.03, n)))
    df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6},
                      index=pd.date_range("2022-01-01", periods=n, freq="D"))
    log = []
    *_head, top = optimize_strategy("Gabriele (RSI+BB reversal)", df, {"initial_capital": 10000},
                                    mode="grid", seed=1, progress=log.append)
    assert top
    assert any("exploratory" in m for m in log)
