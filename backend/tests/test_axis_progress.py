"""
Segnalato dal vivo, due volte, con due screenshot — stessa apparenza, due
fasi diverse della ricerca.

La prima volta era "asse 0/5" fermo per una quindicina di secondi: "asse X:
N confini -> M intervalli" arriva solo a fine ricerca su QUELL'asse, e un
asse pesante (piu' punti base, piu' bisezioni) puo' metterci decine di
secondi per il primo. Corretto in partition_axes().

La seconda volta era "L'apertura guidata" ferma a "0 celle aperte" per oltre
due minuti, con 2160 celle da provare: la riga "celle N — M configurazioni"
usciva ogni budget/8 valutazioni, e budget/8 su una griglia di quella
taglia — con Lorenzo, la piu' pesante delle tre strategie — voleva dire
centinaia di backtest fra una riga e la prossima. Stesso sintomo, stessa
causa (un rapporto fisso invece di un tetto), stessa cura: un massimo di 12
backtest fra una riga e la prossima, qualunque sia la taglia della griglia.

Questi test tengono ferma la riga intermedia che risolve il sintomo, senza
toccare la mappatura vera e propria (ne' i confini trovati, ne' quanti
backtest sono costati).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.plateau import _report_interval, partition_axes, plateau_search
from app.analysis.sensitivity import ParamSpace


def _series(n=400, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
         "volume": rng.uniform(1e6, 5e6, n)},
        index=pd.date_range("2022-01-01", periods=n, freq="D"),
    )


def _busy_len_axis() -> ParamSpace:
    """Un solo asse, 'len' su un intervallo ampio: abbastanza bisezioni da
    superare di sicuro le 12 valutazioni per far scattare la riga."""
    def to_params(u: np.ndarray) -> dict:
        length = int(round(2 + u[0] * 250))
        return {
            "average": "EMA", "len": max(length, 2), "slopeFlen": 7, "slopeSlen": 27,
            "trendfilter": False, "trendfilterperiod": 200, "trendfiltertype": "EMA",
            "volatilityfilter": False, "volatilitystdevlength": 20,
            "volatilitystdevmalength": 40, "slope_threshold_sigma": 0.0,
            "slope_sigma_length": 100,
        }
    return ParamSpace(("len",), to_params)


def test_a_slow_axis_reports_progress_before_it_finishes():
    df = _series()
    log: list[str] = []
    counter: dict = {"n": 0, "boundary": 0}
    partition_axes(
        df, "Lorenzo (trend-follower)", _busy_len_axis(), {},
        np.array([[0.5]]), coarse=12, progress=log.append, counter=counter,
    )
    mid = [m for m in log if "still working" in m]
    final = [m for m in log if m.startswith("axis len:")]
    assert final, "la riga finale dell'asse deve comunque arrivare"
    if counter["n"] > 12:
        assert mid, "un asse che costa piu' di 12 backtest deve dire qualcosa nel frattempo"
    for m in mid:
        assert "len" in m


def test_the_progress_line_does_not_pretend_to_be_the_final_mapping():
    """Non deve avere ne' 'confini' ne' 'intervalli': un parser che legge il
    log (com'e' l'animazione della ricerca) non la deve confondere con la
    riga vera che chiude l'asse."""
    df = _series()
    log: list[str] = []
    partition_axes(
        df, "Lorenzo (trend-follower)", _busy_len_axis(), {},
        np.array([[0.5]]), coarse=12, progress=log.append, counter={"n": 0, "boundary": 0},
    )
    for m in log:
        if "still working" in m:
            assert "confin" not in m
            assert "intervall" not in m


def test_progress_lines_are_evenly_spaced_by_backtest_count():
    df = _series()
    log: list[str] = []
    partition_axes(
        df, "Lorenzo (trend-follower)", _busy_len_axis(), {},
        np.array([[0.5]]), coarse=12, progress=log.append, counter={"n": 0, "boundary": 0},
    )
    counts = [int(m.split("—")[1].split("backtest")[0].strip())
              for m in log if "still working" in m]
    assert all(c % 12 == 0 for c in counts)
    assert counts == sorted(counts)


def test_a_cheap_axis_still_only_says_the_final_line():
    """Pochi punti, poche bisezioni: sotto 12 backtest non deve comparire
    nessuna riga intermedia — sarebbe rumore per un asse che finisce subito."""
    df = _series()
    log: list[str] = []
    counter: dict = {"n": 0, "boundary": 0}

    def to_params(u: np.ndarray) -> dict:
        return {
            "average": "EMA", "len": 130, "slopeFlen": 7, "slopeSlen": 27,
            "trendfilter": bool(u[0] > 0.5), "trendfilterperiod": 200, "trendfiltertype": "EMA",
            "volatilityfilter": False, "volatilitystdevlength": 20,
            "volatilitystdevmalength": 40, "slope_threshold_sigma": 0.0,
            "slope_sigma_length": 100,
        }
    space = ParamSpace(("trendfilter",), to_params)
    partition_axes(df, "Lorenzo (trend-follower)", space, {},
                    np.array([[0.5]]), coarse=4, progress=log.append, counter=counter)
    if counter["n"] <= 12:
        assert not any("still working" in m for m in log)


# --- lo stesso, per l'apertura delle celle -----------------------------------
#
# _report_interval() e' la formula esatta usata da entrambe le fasi (mappare
# un asse, aprire le celle): testarla in isolamento e' piu' affidabile che
# fare girare una ricerca vera e sperare che i dati sintetici producano una
# griglia abbastanza grande — la taglia della griglia dipende dalla serie e
# dagli assi che restano inerti, non e' qualcosa che si controlla da qui.


def test_a_large_grid_reports_every_12_cells_not_every_budget_over_8():
    """La griglia da 2160 celle dello screenshot: budget // 8 farebbe 270 —
    minuti di silenzio con una strategia pesante. Deve restare a 12."""
    assert _report_interval(2160) == 12
    assert _report_interval(10_000) == 12


def test_a_small_grid_is_not_forced_to_report_more_often_than_before():
    """Il tetto di 12 e' un MASSIMO: una griglia piccola, che gia' riportava
    piu' spesso di budget//8, deve restare com'era — altrimenti una ricerca
    veloce si riempirebbe di righe inutili."""
    assert _report_interval(16) == 2      # 16 // 8, sotto il tetto: invariato
    assert _report_interval(4) == 1       # max(1, ...): mai zero


def test_the_live_search_actually_uses_this_formula():
    """Non solo la formula esiste: la ricerca la usa davvero per decidere
    quando scrivere 'celle N — M configurazioni distinte finora'."""
    df = _series(n=500, seed=11)
    log: list[str] = []
    res = plateau_search("Gabriele (RSI+BB reversal)", df, {}, max_grid_evals=300,
                         progress=log.append)
    counts = [int(m.split("—")[0].replace("celle", "").strip())
              for m in log if "configurazioni distinte finora" in m]
    expected_step = _report_interval(res.grid_cells if res.enumerated else 300)
    if len(counts) >= 2:
        gaps = {b - a for a, b in zip(counts, counts[1:])}
        assert gaps == {expected_step}
