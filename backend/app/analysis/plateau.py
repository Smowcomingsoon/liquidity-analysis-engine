"""
Plateau search (pipeline stages S3-S4).

Built on the structure measured in phase C: the objective is not a rugged
continuous surface, it is a step function. Sweeping one parameter across 200
evenly spaced values produced 5 distinct objective values, with 98% of steps
changing nothing at all. The reason is mechanical — a backtest is a function of
the trade sequence, which is discrete, so a parameter only matters when it
crosses a threshold that adds or removes a signal. Between two thresholds the
result is identical to the last decimal.

Three consequences drive the design here:

  * Sampling on a fixed lattice wastes almost every evaluation. In the measured
    sweep, 80 consecutive points returned the same number. What carries
    information is the location of the BOUNDARIES, and a step function with k
    jumps over n values is mapped in O(k log n) evaluations by bisection
    instead of O(n).

  * Local refinement has nothing to descend. 98% of local moves have exactly
    zero gradient and the rest are discontinuities, so Nelder-Mead, CMA-ES and
    smooth-kernel Bayesian optimisation are all inapplicable.

  * Plateau width becomes free. Once boundaries are known, the width of the
    cell containing a candidate is read off the partition rather than
    estimated by sampling around it — and width is exactly the quantity that
    separates a robust configuration from a lucky one.

The known weakness is stated in the research note and handled explicitly here:
boundaries along one axis depend on where the other axes sit, so an axis-wise
partition is an approximation, not an identity. It is used as a sampling
structure, never trusted as an exact decomposition, and every finalist is
re-checked by direct perturbation. The disagreement between the width the
partition predicts and the stability actually measured is reported as
`axis_approximation_error`, so the cost of the approximation is a number in the
output instead of an assumption in the design.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.analysis.backtest import run_backtest
from app.analysis.cancel import stop_if_asked
from app.analysis.sensitivity import PARAM_SPACES, ParamSpace
from app.models import BacktestResult

# A candidate is identified by what its trades actually were, not by its
# nominal parameters: non-binding parameters produce byte-identical sequences.
Fingerprint = tuple


# Fixed seed for the drift perturbations. Not a tunable: changing it changes
# the answer, and the point of this module is that the answer does not move.
# Quanti candidati la ricerca tiene in memoria prima di potare. I finalisti
# sono una dozzina, quindi una riserva di qualche centinaio e' abbondante; oltre
# si accumulano solo configurazioni che non entreranno mai in classifica.
_MAX_KEPT = 400

_DRIFT_SEED = 20260903

# Quanti backtest al massimo fra una riga di avanzamento e la prossima.
# Usato sia mentre si mappa un asse sia mentre si aprono le celle — in
# entrambi i posti il sintomo segnalato dal vivo era lo stesso: minuti di
# silenzio con una strategia pesante, perche' "un ottavo del totale" su una
# ricerca grande (le migliaia di celle non sono rare) puo' voler dire
# centinaia di backtest fra una riga e la prossima.
_PROGRESS_STEP = 12


def _report_interval(total: int) -> int:
    """Un ottavo del totale, ma non oltre _PROGRESS_STEP: basta per non
    intasare di righe una ricerca piccola, che gia' arriva in fretta."""
    return max(1, min(_PROGRESS_STEP, total // 8))


def _fingerprint(bt: BacktestResult) -> Fingerprint:
    return (round(bt.total_return_pct, 6), bt.num_trades, round(bt.max_drawdown_pct, 6))


# Tolleranza per il confronto di robustezza (S4b), separata da _fingerprint:
# quest'ultimo resta esatto perche' deduplica candidati durante la ricerca (S4a),
# dove due celle che producono davvero la stessa sequenza di trade DEVONO
# coincidere bit per bit. Il confronto "regge N su 14" e' un'altra domanda —
# "questa scossa ha dato sostanzialmente lo stesso risultato?" — e l'uguaglianza
# esatta al sesto decimale era piu' severa di quanto serve: un singolo tick di
# differenza sul prezzo di uscita basta a spostare il rendimento oltre quella
# soglia anche quando il trade e' lo stesso. Bande larghe quanto un cambiamento
# che un essere umano giudicherebbe "diverso davvero".
_ROBUSTNESS_RETURN_TOL = 0.01      # ±1% RELATIVO al rendimento del candidato (scala da poche a migliaia di punti)
_ROBUSTNESS_DRAWDOWN_TOL_PP = 1.0  # ±1 punto percentuale ASSOLUTO — il drawdown e' gia' una percentuale
                                   # piccola (tipicamente a una o due cifre): una tolleranza RELATIVA a
                                   # quel numero (1% di 7% = 0.07 punti) sarebbe severa quanto il confronto
                                   # esatto che questa funzione doveva sostituire. Misurato: la prima
                                   # versione, con tolleranza relativa anche sul drawdown, dava ancora
                                   # 'regge 0 volte' anche dopo il fix — il bug era proprio qui.
_ROBUSTNESS_TRADES_TOL = 1         # ±1 trade


def _robustly_close(bt: BacktestResult, base: BacktestResult) -> bool:
    if abs(bt.num_trades - base.num_trades) > _ROBUSTNESS_TRADES_TOL:
        return False
    ret_scale = max(abs(base.total_return_pct), 1.0)
    if abs(bt.total_return_pct - base.total_return_pct) > _ROBUSTNESS_RETURN_TOL * ret_scale:
        return False
    return abs(bt.max_drawdown_pct - base.max_drawdown_pct) <= _ROBUSTNESS_DRAWDOWN_TOL_PP


@dataclass
class AxisPartition:
    """The cells one axis is divided into, in unit-cube coordinates."""

    dim: int
    name: str
    boundaries: tuple[float, ...]     # interior boundaries, sorted, in (0,1)

    @property
    def edges(self) -> tuple[float, ...]:
        return (0.0,) + self.boundaries + (1.0,)

    @property
    def n_cells(self) -> int:
        return len(self.boundaries) + 1

    def cell_index(self, u: float) -> int:
        return int(np.searchsorted(np.asarray(self.boundaries), u, side="right"))

    def cell_bounds(self, idx: int) -> tuple[float, float]:
        e = self.edges
        return e[idx], e[idx + 1]

    def width_at(self, u: float) -> float:
        lo, hi = self.cell_bounds(self.cell_index(u))
        return hi - lo

    def centers(self) -> list[float]:
        e = self.edges
        return [(e[i] + e[i + 1]) / 2.0 for i in range(len(e) - 1)]


@dataclass
class PlateauCandidate:
    u: np.ndarray
    params: dict
    backtest: BacktestResult
    score: float
    cell_widths: np.ndarray
    log_volume: float
    # Filled in only for finalists that go through direct verification.
    neighbourhood_mean: float | None = None
    neighbourhood_std: float | None = None
    neighbourhood_agreement: float | None = None   # fraction of probes unchanged
    robust_score: float | None = None
    axis_approximation_error: float | None = None

    @property
    def min_width(self) -> float:
        return float(np.min(self.cell_widths)) if self.cell_widths.size else 0.0


@dataclass
class PlateauResult:
    strategy: str
    partitions: list[AxisPartition]
    candidates: list[PlateauCandidate]        # finalists, best robust first
    evaluations: int
    boundary_evaluations: int
    grid_cells: int
    enumerated: bool
    naive_best: PlateauCandidate | None = None   # argmax of raw score, for contrast
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> PlateauCandidate | None:
        return self.candidates[0] if self.candidates else None

    def summary(self) -> str:
        lines = [
            f"Plateau search — {self.strategy}",
            f"  {self.evaluations} backtests "
            f"({self.boundary_evaluations} locating boundaries)"
            f"  ·  grid {self.grid_cells} cells"
            f"  ·  {'enumerated' if self.enumerated else 'sampled'}",
            "",
            "  axis partition (cells per dimension):",
        ]
        for p in self.partitions:
            widths = [f"{hi - lo:.3f}" for lo, hi in
                      (p.cell_bounds(i) for i in range(p.n_cells))]
            lines.append(f"    {p.name:<26} {p.n_cells:>3} cells   widths {', '.join(widths[:8])}"
                         + (" …" if len(widths) > 8 else ""))
        lines.append("")
        lines.append(f"  {'#':>2} {'score':>9} {'robust':>9} {'min_w':>8} {'agree':>7} {'ret%':>9} {'trades':>7}")
        lines.append("  " + "-" * 60)
        for i, c in enumerate(self.candidates[:10], start=1):
            rs = f"{c.robust_score:9.3f}" if c.robust_score is not None else "        —"
            ag = f"{c.neighbourhood_agreement:6.0%}" if c.neighbourhood_agreement is not None else "     —"
            lines.append(
                f"  {i:>2} {c.score:>9.3f} {rs} {c.min_width:>8.3f} {ag:>7} "
                f"{c.backtest.total_return_pct:>+9.1f} {c.backtest.num_trades:>7}"
            )
        if self.naive_best is not None and self.candidates:
            nb, rb = self.naive_best, self.candidates[0]
            lines.append("")
            lines.append(
                f"  naive argmax score={nb.score:.3f} min_width={nb.min_width:.3f}"
                f"  vs  plateau pick score={rb.score:.3f} min_width={rb.min_width:.3f}"
            )
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


# --- boundary location -------------------------------------------------------


def _bisect_boundary(
    probe: Callable[[float], Fingerprint],
    a: float, fa: Fingerprint,
    b: float,
    tol: float,
) -> float:
    """Locate the single transition between a and b, assuming fa != fb."""
    while (b - a) > tol:
        m = 0.5 * (a + b)
        fm = probe(m)
        if fm == fa:
            a = m
        else:
            b = m
    return 0.5 * (a + b)


def locate_boundaries(
    probe: Callable[[float], Fingerprint],
    coarse: int = 10,
    tol: float = 0.004,
) -> list[float]:
    """Find where a step function along one axis changes value.

    A coarse scan seeds the search, then every interval whose endpoints differ
    is bisected. Bisection alone would be unsound here: it assumes a single
    transition per bracket, and a plateau that departs and returns to the same
    value inside one bracket is invisible to it. The coarse scan bounds how
    much can hide — it cannot eliminate the risk, and the resolution limit is
    reported rather than papered over.
    """
    xs = np.linspace(0.0, 1.0, coarse)
    vals = [probe(float(x)) for x in xs]
    out: list[float] = []
    for i in range(len(xs) - 1):
        if vals[i] != vals[i + 1]:
            out.append(_bisect_boundary(probe, float(xs[i]), vals[i],
                                        float(xs[i + 1]), tol))
    return sorted(out)


def partition_axes(
    df: pd.DataFrame,
    strategy_name: str,
    space: ParamSpace,
    backtest_params: dict,
    base_points: np.ndarray,
    coarse: int = 10,
    tol: float = 0.004,
    counter: dict | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[AxisPartition]:
    """Partition every axis into cells, taking the union of boundaries found
    from several base points.

    The union is deliberate. Boundaries move when the other coordinates move,
    so a partition built from one base point describes one slice of the space.
    Taking the union over several slices yields a refinement that is at least
    as fine as any single slice — cells may be split more than strictly
    necessary, which costs a little search budget, but a boundary that matters
    somewhere is not silently erased.
    """
    say = progress or (lambda _m: None)
    parts: list[AxisPartition] = []
    for dim, name in enumerate(space.dims):
        before = (counter or {}).get("n", 0)
        found: list[float] = []
        for base in base_points:
            def probe(u: float, _dim=dim, _base=base) -> Fingerprint:
                stop_if_asked(should_stop)
                point = np.array(_base, dtype=float)
                point[_dim] = u
                bt = run_backtest(df, space.to_params(point), backtest_params,
                                  strategy_name=strategy_name)
                if counter is not None:
                    counter["n"] = counter.get("n", 0) + 1
                    counter["boundary"] = counter.get("boundary", 0) + 1
                    # Un asse pesante (piu' punti base, piu' bisezioni) puo'
                    # metterci decine di secondi prima della prima riga —
                    # "asse X: ..." arriva solo a fine ricerca su quell'asse.
                    # Nel frattempo il log restava fermo, ed e' sembrato un
                    # blocco: segnalato dal vivo. Questa non cambia la
                    # mappatura, e' solo il segno che si sta ancora cercando.
                    local = counter["n"] - before
                    if local % _PROGRESS_STEP == 0:
                        say(f"axis {name}: still working — {local} backtests so far")
                return _fingerprint(bt)

            found.extend(locate_boundaries(probe, coarse=coarse, tol=tol))

        # Merge boundaries that are closer together than the bisection tolerance
        # can distinguish — keeping both would create cells narrower than the
        # resolution that produced them.
        merged: list[float] = []
        for b in sorted(found):
            if not merged or (b - merged[-1]) > tol * 2:
                merged.append(b)
        part = AxisPartition(dim=dim, name=name, boundaries=tuple(merged))
        parts.append(part)
        # Questa riga e' la mappatura, detta a voce: l'obiettivo e' una
        # funzione a gradini, quindi ogni asse viene tagliato dove il risultato
        # cambia davvero. Un asse con un solo intervallo e' un parametro che su
        # questa serie non sposta nulla — informazione, non rumore.
        used = (counter or {}).get("n", 0) - before
        say(f"axis {name}: {len(merged)} boundar{'y' if len(merged) == 1 else 'ies'} → "
            f"{part.n_cells} {'interval' if part.n_cells == 1 else 'intervals'} "
            f"({used} backtests)")
    return parts


# --- scoring -----------------------------------------------------------------


def _plateau_objective(bt: BacktestResult, min_trades: int = 8) -> float:
    """Calmar, tempered by trade count and profit factor. Finite everywhere:
    ranking happens later, so a hard filter here would only destroy the
    ordering information the plateau geometry needs."""
    dd = max(abs(bt.max_drawdown_pct), 0.5)
    calmar = bt.total_return_pct / dd
    conf = bt.num_trades / (bt.num_trades + min_trades)
    pf = bt.profit_factor if bt.profit_factor is not None else 1.0
    pf_bonus = min(max(pf, 0.0), 5.0) / 5.0
    return calmar * (0.6 + 0.4 * pf_bonus) * conf


# --- search ------------------------------------------------------------------


# --- espansione guidata ------------------------------------------------------


def _lattice_starts(sizes: list[int], n_starts: int) -> list[tuple[int, ...]]:
    """Punti di partenza sparsi sul reticolo, senza sorteggio.

    Servono piu' partenze perche' un'espansione avida da un punto solo si
    accontenta della prima regione buona che incontra. Sparse pero' in modo
    deterministico — multipli interi di numeri primi presi modulo il numero di
    celle, un reticolo alla Korobov — e non casuali: la ricerca guidata esiste
    anche per togliere la variabilita' fra corse, e ripescarla nel punto di
    partenza sarebbe autolesionista.
    """
    primes = (1, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43)
    out: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()
    for k in range(n_starts * 4):
        node = tuple((k * primes[d % len(primes)]) % sizes[d] for d in range(len(sizes)))
        if node in seen:
            continue
        seen.add(node)
        out.append(node)
        if len(out) >= n_starts:
            break
    return out


def _guided_cells(
    sizes: list[int],
    centers: list[np.ndarray],
    parts: list[AxisPartition],
    budget: int,
    consider: Callable[[np.ndarray], float],
    n_starts: int = 8,
    say: Callable[[str], None] = lambda _m: None,
    patience: int | None = None,
) -> int:
    """Espansione best-first sulle celle, invece di estrarne a caso.

    Il reticolo delle celle e' un grafo: due celle sono vicine se differiscono
    di un passo su un asse solo. Si tiene una coda delle celle gia' viste
    ordinata per quanto promettono, si apre sempre la migliore e si valutano le
    sue vicine. Le regioni che vanno male restano in fondo alla coda e non
    vengono mai aperte — non per una regola che le esclude, ma perche' il budget
    finisce prima di arrivarci. E' la stessa ragione per cui un navigatore non
    percorre tutte le strade: ne apre poche, sempre la piu' promettente.

    Due proprieta' che il sorteggio non poteva dare: a parita' di backtest si
    guarda dove il risultato e' buono invece che ovunque con la stessa
    probabilita', e non essendoci estrazioni due corse sulla stessa serie danno
    lo stesso risultato.

    La priorita' e' il punteggio del centro, con il logaritmo del volume della
    cella come spareggio: fra due celle che valgono uguale si apre prima la piu'
    larga, perche' una cella larga e' gia' un pianoro.

    `patience`: se impostato, ogni partenza (vedi `_lattice_starts`) smette di
    generare nuovi vicini una volta che le sue ultime `patience` celle aperte
    non hanno migliorato il suo stesso record — non un contatore globale.
    Un tentativo precedente con un contatore globale si e' rivelato sbagliato:
    su un paesaggio a gradini con piu' partenze indipendenti, una partenza puo'
    restare 'ferma' a lungo mentre un'altra sta ancora trovando terreno buono, e
    fermare TUTTA la ricerca quando una sola si ferma tagliava via pianori veri
    prima di trovarli (misurato dal vivo: 12 finalisti su 12 con 'regge 0 volte
    su 14'). Qui ogni partenza si esaurisce per conto suo; le altre continuano
    finche' non si esauriscono a loro volta o finisce il budget — nessun taglio
    globale, quindi lo stesso rischio non si ripresenta."""
    d = len(sizes)
    visited: set[tuple[int, ...]] = set()
    # Ogni voce in coda porta anche la partenza (root) da cui discende, per
    # poter tracciare l'esaurimento partenza per partenza invece che in globale.
    heap: list[tuple[float, float, int, tuple[int, ...], int]] = []
    order = 0
    used = 0
    best_score: dict[int, float] = {}
    stall: dict[int, int] = {}
    exhausted: set[int] = set()

    def volume_of(node: tuple[int, ...]) -> float:
        widths = [parts[i].cell_bounds(node[i])[1] - parts[i].cell_bounds(node[i])[0]
                  for i in range(d)]
        return float(np.sum(np.log(np.maximum(widths, 1e-12))))

    def push(node: tuple[int, ...], root: int) -> None:
        nonlocal order, used
        if node in visited or used >= budget:
            return
        visited.add(node)
        score = consider(np.array([centers[i][node[i]] for i in range(d)]))
        used += 1
        order += 1
        if patience is not None and math.isfinite(score):
            prev_best = best_score.get(root, -math.inf)
            if score > prev_best * (1.01 if prev_best > 0 else 0.99):
                best_score[root] = score
                stall[root] = 0
            else:
                stall[root] = stall.get(root, 0) + 1
                if stall[root] >= patience:
                    exhausted.add(root)
        if math.isfinite(score):
            heapq.heappush(heap, (-score, -volume_of(node), order, node, root))

    starts = _lattice_starts(sizes, n_starts)
    for root, node in enumerate(starts):
        push(node, root)

    opened = 0
    while heap and used < budget:
        _s, _v, _o, node, root = heapq.heappop(heap)
        opened += 1
        if patience is not None and root in exhausted:
            continue
        for axis in range(d):
            for step in (-1, 1):
                nxt = list(node)
                nxt[axis] += step
                if 0 <= nxt[axis] < sizes[axis]:
                    push(tuple(nxt), root)
            if used >= budget:
                break

    stopped_early = patience is not None and len(exhausted) >= len(starts) and used < budget
    say(f"guided expansion: {used} cells evaluated by opening the {opened} most promising "
        f"from {n_starts} starts — no random draws, same series same result"
        + (f" (all starts exhausted after {patience} cells without local improvement)"
           if stopped_early else ""))
    return used


def plateau_search(
    strategy_name: str,
    df: pd.DataFrame,
    backtest_params: dict | None = None,
    base_points: int = 2,
    coarse: int = 8,
    tol: float = 0.004,
    max_grid_evals: int = 1200,
    finalists: int = 12,
    min_trades: int = 8,
    probe_radius: float = 0.05,
    probe_samples: int = 14,
    lam: float = 0.75,
    patience: int | None = 150,
    seed: int = 0,
    space: ParamSpace | None = None,
    objective: Callable[[BacktestResult], float] = _plateau_objective,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> PlateauResult:
    """Find the widest good plateau rather than the highest point.

    Deterministic given (strategy, df, settings, seed): the sampling of cell
    combinations is seeded, and nothing else draws randomness. Two runs on the
    same inputs return the same answer — which is the property the whole
    exercise exists to obtain.
    """
    if space is None:
        if strategy_name not in PARAM_SPACES:
            raise ValueError(f"No parameter space for {strategy_name!r}")
        space = PARAM_SPACES[strategy_name]
    bp = backtest_params or {}
    say = progress or (lambda _m: None)
    rng = np.random.default_rng(seed)
    counter: dict = {"n": 0, "boundary": 0}
    notes: list[str] = []

    def evaluate(u: np.ndarray) -> tuple[BacktestResult, float]:
        # Il controllo sta qui e non alle sole svolte del log: e' il punto per
        # cui passa ogni backtest, quindi fermarsi costa al massimo il tempo di
        # quello in corso invece di un ottavo del budget.
        stop_if_asked(should_stop)
        bt = run_backtest(df, space.to_params(u), bp, strategy_name=strategy_name)
        counter["n"] += 1
        return bt, objective(bt)

    # --- S3: partition every axis
    say(f"mapping boundaries on {space.d} axes: where the result changes, not where the parameter changes")
    bases = np.vstack([np.full(space.d, 0.5), rng.random((max(base_points - 1, 0), space.d))])
    parts = partition_axes(df, strategy_name, space, bp, bases,
                           coarse=coarse, tol=tol, counter=counter, progress=say,
                           should_stop=should_stop)

    grid_cells = 1
    for p in parts:
        grid_cells *= p.n_cells

    inerti = [p.name for p in parts if p.n_cells == 1]
    say("grid: " + " × ".join(f"{p.n_cells}" for p in parts) + f" = {grid_cells} cells"
        + (f" · inert on this series: {', '.join(inerti)}" if inerti else ""))

    # --- S4a: evaluate cell centres, exhaustively when that fits the budget
    centers = [p.centers() for p in parts]
    enumerated = grid_cells <= max_grid_evals

    points: list[np.ndarray] = []
    if enumerated:
        idx = np.zeros(space.d, dtype=int)
        for _ in range(grid_cells):
            points.append(np.array([centers[d][idx[d]] for d in range(space.d)]))
            for d in range(space.d - 1, -1, -1):
                idx[d] += 1
                if idx[d] < len(centers[d]):
                    break
                idx[d] = 0
    else:
        say(f"{grid_cells} cells over the budget of {max_grid_evals}: instead of "
            f"sampling {max_grid_evals} I open them in order of promise")
        notes.append(
            f"grid of {grid_cells} cells exceeds budget {max_grid_evals}; "
            f"expanded best-first from the most promising cells instead of "
            f"enumerating — deterministic, no sampling"
        )

    seen_fp: set[Fingerprint] = set()
    cands: list[PlateauCandidate] = []
    tried = {"n": 0}
    step_report = _report_interval(len(points) if enumerated else max_grid_evals)

    def consider_point(u: np.ndarray) -> float:
        """Valuta il centro di una cella e lo tiene se porta qualcosa di nuovo.

        Restituisce il punteggio anche per un duplicato: all'espansione guidata
        serve per decidere dove andare, e togliergliela la lascerebbe cieca
        proprio dove il terreno e' piatto.
        """
        bt, sc = evaluate(u)
        tried["n"] += 1
        if tried["n"] % step_report == 0:
            say(f"cells {tried['n']} — {len(cands)} distinct configurations so far")
        if not math.isfinite(sc):
            return sc
        fp = _fingerprint(bt)
        if fp in seen_fp:
            return sc
        seen_fp.add(fp)
        widths = np.array([parts[d].width_at(float(u[d])) for d in range(space.d)])
        cands.append(PlateauCandidate(
            # L'elenco dei trade non serve a nessuno qui: la classifica si fa
            # sui numeri, la curva di equity serve al PBO, e i trade del
            # vincitore vengono ricalcolati alla fine con un backtest solo.
            # Tenerli per migliaia di candidati significa centinaia di MB su una
            # macchina che ne ha 512 — e una macchina senza memoria non e' lenta,
            # e' spenta.
            u=u, params=space.to_params(u), score=sc,
            backtest=bt.model_copy(update={"trades": []}),
            cell_widths=widths,
            log_volume=float(np.sum(np.log(np.maximum(widths, 1e-12)))),
        ))
        if len(cands) > _MAX_KEPT:
            # Si tiene solo la parte alta: i finalisti escono da li', e i
            # candidati scartati non rientrerebbero comunque in classifica.
            cands.sort(key=lambda c: (c.score, c.log_volume), reverse=True)
            del cands[_MAX_KEPT // 2:]
        return sc

    if enumerated:
        say(f"trying the center of {len(points)} cells (all enumerated)")
        for u in points:
            consider_point(u)
    else:
        _guided_cells([len(c) for c in centers], centers, parts,
                      max_grid_evals, consider_point, say=say, patience=patience)

    if not cands:
        return PlateauResult(strategy_name, parts, [], counter["n"], counter["boundary"],
                             grid_cells, enumerated, None,
                             notes + ["no finite-score candidate found"])

    say(f"{len(cands)} distinct configurations over {tried['n']} cells tried: the others "
        f"produce exactly the same trades")

    _naive_pool = [c for c in cands if c.backtest.num_trades >= min_trades] or cands
    naive_best = max(_naive_pool, key=lambda c: c.score)

    # --- S4b: verify the finalists by direct perturbation
    #
    # The partition predicts each candidate sits in a cell of a given width.
    # That prediction comes from an axis-wise decomposition of a surface known
    # to have interactions, so it is checked rather than believed: probe each
    # axis at +/- half the cell width and see whether the result actually holds.
    # Trade-count floor, applied HERE and not in the objective. The objective
    # has to stay finite everywhere for boundary detection to work at all, so
    # the filter cannot live there — but without it the search happily returns
    # a 3-trade configuration showing +226%, which is one lucky sequence and
    # not an edge. Enforcing it at selection keeps both properties.
    eligible = [c for c in cands if c.backtest.num_trades >= min_trades]
    if not eligible:
        notes.append(
            f"no candidate reached {min_trades} trades "
            f"(best was {max(c.backtest.num_trades for c in cands)}); "
            f"reporting unfiltered, treat as unusable"
        )
        eligible = cands

    drift_rng = np.random.default_rng(_DRIFT_SEED)
    drift_set = [drift_rng.uniform(-probe_radius, probe_radius, size=space.d)
                 for _ in range(probe_samples)]

    by_promise = sorted(eligible, key=lambda c: (c.score, c.log_volume), reverse=True)
    shortlist = by_promise[:finalists]
    say(f"{len(eligible)} candidate{'' if len(eligible) == 1 else 's'} with at least "
        f"{min_trades} trades; shaking {len(shortlist)} with {probe_samples} "
        f"random shifts of ±{probe_radius:.0%}")
    for rank, c in enumerate(shortlist, start=1):
        # Estimate J = (f * K)(theta) by sampling JOINT perturbations, not one
        # parameter at a time. One-at-a-time probing measures the wrong thing
        # here: with d dimensions, a configuration that is a knife edge in a
        # single parameter still scores (2d-2)/2d agreement because the probes
        # along every other axis leave it untouched. Drift does not arrive one
        # coordinate at a time, and the objective in the research note is an
        # expectation over the joint kernel — so the probes have to be joint
        # too, or they estimate a quantity nobody cares about.
        #
        # The perturbation set is fixed and shared by every candidate, and is
        # deliberately NOT derived from `seed`. Drift is a property of the
        # problem, not of the search that happens to be running, so tying it to
        # the search seed would reintroduce exactly the run-to-run variance
        # this module exists to remove — measured: it took cv_oos from 0% back
        # up to 46%. Sharing one set across candidates also pairs the
        # comparison, so differences in robust_score reflect the candidates
        # rather than the draw.
        probes: list[float] = []
        unchanged = 0
        for eps in drift_set:
            probe_u = np.clip(c.u + eps, 0.0, 1.0)
            bt, sc = evaluate(probe_u)
            probes.append(sc)
            if _robustly_close(bt, c.backtest):
                unchanged += 1

        say(f"finalist {rank}/{len(shortlist)}: holds {unchanged} "
            f"{'time' if unchanged == 1 else 'times'} out of {len(drift_set)} "
            f"— {c.backtest.total_return_pct:+.1f}% over {c.backtest.num_trades} trades")

        if probes:
            arr = np.array(probes + [c.score])
            c.neighbourhood_mean = float(np.mean(arr))
            c.neighbourhood_std = float(np.std(arr))
            c.neighbourhood_agreement = unchanged / len(probes)
            c.robust_score = c.neighbourhood_mean - lam * c.neighbourhood_std
            predicted_stable = float(np.mean(c.cell_widths >= 2.0 * probe_radius))
            c.axis_approximation_error = abs(predicted_stable - c.neighbourhood_agreement)
        else:
            c.neighbourhood_mean = c.score
            c.neighbourhood_std = 0.0
            c.neighbourhood_agreement = 1.0
            c.robust_score = c.score
            c.axis_approximation_error = 0.0

    verified = [c for c in by_promise[:finalists] if c.robust_score is not None]
    # Rank on what was measured, not on what the partition predicted. Phase-E
    # measurement put the axis-approximation error above 50%, so cell width is
    # demoted to a tiebreak. `neighbourhood_agreement` ("regge N su 14") e'
    # ORA il criterio primario, non piu' lo spareggio: e' il numero che
    # l'interfaccia mostra come garanzia di solidita', quindi il vincitore deve
    # essere davvero quello che regge di piu' — non uno che regge poco ma ha
    # un punteggio medio-meno-varianza migliore, che e' cio' che poteva succedere
    # prima (un candidato con agreement basso vinceva comunque su robust_score).
    verified.sort(
        key=lambda c: (c.neighbourhood_agreement or 0.0, c.robust_score, c.log_volume),
        reverse=True,
    )

    if verified:
        w = verified[0]
        say(f"picked: {w.backtest.total_return_pct:+.1f}% over {w.backtest.num_trades} trades, "
            f"stable {w.neighbourhood_agreement:.0%} of the time"
            + ("" if naive_best is w else
               f" (the raw maximum did {naive_best.backtest.total_return_pct:+.1f}% "
               f"but holds up less)"))

    errs = [c.axis_approximation_error for c in verified if c.axis_approximation_error is not None]
    if errs:
        notes.append(
            f"axis approximation error: mean {np.mean(errs):.1%}, worst {max(errs):.1%} "
            f"disagreement between predicted and measured stability"
        )

    return PlateauResult(
        strategy=strategy_name, partitions=parts, candidates=verified,
        evaluations=counter["n"], boundary_evaluations=counter["boundary"],
        grid_cells=grid_cells, enumerated=enumerated,
        naive_best=naive_best, notes=notes,
    )
