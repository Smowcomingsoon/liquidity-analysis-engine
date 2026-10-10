"""
End-to-end deterministic optimisation pipeline.

Ties together the stages built and measured in phases A-E:

    S0  regime gate        does this strategy have an edge to find here at all?
    S2  structural priors  branch decomposition, derived parameters
    S3  boundary location  where the step function actually changes
    S4  plateau selection  widest good plateau, not the highest point

Measured against the random search it replaces, same series and split, only
the seed varying:

    strategy    method            cv_oos   param_dist   identical
    Gabriele    random             66.5%        0.605          no
    Gabriele    pipeline            0.0%        0.000         yes
    Lorenzo     random            225.1%        0.674          no
    Lorenzo     pipeline            0.0%        0.000         yes

Branch ordering carries a deliberate preference for the simpler model. A
branch with more free parameters has more capacity to fit the in-sample
period, so it has to earn its extra dimensions by a margin rather than by a
hair — and on the series measured so far the simplest branch won outright on
both sides of the split, which is the outcome that preference exists to
protect.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from app.analysis.backtest import run_backtest
from app.analysis.plateau import PlateauCandidate, plateau_search
from app.analysis.priors import ConstrainedSpace, build_spaces, estimate_series_stats
from app.analysis.regime import GateDecision, regime_gate
from app.analysis.validation import (
    DSRResult,
    PBOResult,
    align_equity_curves,
    deflated_sharpe_ratio,
    periods_per_year_from_curve,
    probability_of_backtest_overfitting,
    returns_from_equity_curve,
)
from app.models import BacktestResult

_MIN_OOS_CANDLES = 60

# A richer branch must beat the incumbent by this margin to be preferred.
# Equal performance from more parameters is not equal evidence.
_COMPLEXITY_MARGIN = 0.10

# Below this many trades on the validation slice, a return is an anecdote
# rather than a measurement, and the branch comparison falls back to the
# structural preferences.
_MIN_VALIDATION_TRADES = 5


@dataclass
class BranchOutcome:
    label: str
    dims: int
    enumerated: bool
    best: PlateauCandidate | None
    evaluations: int
    notes: list[str] = field(default_factory=list)
    validation_return_pct: float | None = None
    validation_trades: int | None = None


@dataclass
class PipelineResult:
    strategy: str
    params: dict
    backtest: BacktestResult
    out_of_sample_return_pct: float | None
    overfitted: bool
    evaluations: int
    gate: GateDecision
    chosen_branch: str
    branches: list[BranchOutcome]
    deterministic: bool
    series_stats: dict
    dsr: DSRResult | None = None
    pbo: PBOResult | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Pipeline — {self.strategy}",
            f"  gate: {'PASS' if self.gate.passed else 'BLOCK'} — {self.gate.reason}",
            f"  branch chosen: {self.chosen_branch}"
            f"   deterministic: {'yes' if self.deterministic else 'NO (sampled)'}",
            f"  {self.evaluations} backtests",
            "",
            f"  {'branch':<44}{'d':>3}{'enum':>6}{'IS%':>9}{'trades':>7}",
        ]
        for b in self.branches:
            if b.best is None:
                lines.append(f"  {b.label:<44}{b.dims:>3}{'':>6}   no candidate")
                continue
            lines.append(
                f"  {b.label:<44}{b.dims:>3}{'yes' if b.enumerated else 'no':>6}"
                f"{b.best.backtest.total_return_pct:>+9.1f}{b.best.backtest.num_trades:>7}"
            )
        oos = ("—" if self.out_of_sample_return_pct is None
               else f"{self.out_of_sample_return_pct:+.2f}%")
        lines += ["", f"  in-sample {self.backtest.total_return_pct:+.2f}%   out-of-sample {oos}"
                      f"   {'OVERFITTED' if self.overfitted else 'ok'}"]
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


def _three_way_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """train / validation / test.

    Two selections happen, and they cannot share data. Inside a branch the
    plateau criterion picks among configurations of equal capacity, which is a
    fair comparison on the training slice. ACROSS branches the candidates have
    different numbers of free parameters, and in-sample score rises with
    capacity whatever the model is worth — measured here at 25.95 for the
    8-parameter branch against 8.75 for the 4-parameter one, while their
    held-out returns ran -1.97% and +18.54% respectively. Choosing between
    them therefore needs data neither of them was fitted on, and reporting an
    honest out-of-sample number needs data the choice did not consume either.
    """
    n = len(df)
    test_n = max(int(n * 0.20), _MIN_OOS_CANDLES)
    val_n = max(int(n * 0.20), _MIN_OOS_CANDLES)
    train_n = max(n - test_n - val_n, 1)
    return (df.iloc[:train_n],
            df.iloc[train_n:train_n + val_n],
            df.iloc[train_n + val_n:])


# Same floor as random/grid (optimize._MIN_TRADES_FLOOR): with RSI 14 / BB 20 /
# 2 locked, RSI+BB rarely reaches 8 trades and the stable search reported
# every candidate as unusable (segnalazione e0bffe25).
from app.analysis.optimize import _MIN_TRADES_FLOOR  # noqa: E402

def run_pipeline(
    strategy_name: str,
    df: pd.DataFrame,
    backtest_params: dict | None = None,
    target_entries: int = 40,
    # 700 e' il budget misurato come gia' sufficiente su un ramo a 7 parametri
    # (93 312 celle): guidata 57.44 contro 50.39/26.93/37.35 di tre semi
    # diversi. Un tentativo precedente di usare questo stesso budget con uno
    # stop anticipato GLOBALE ha rotto la robustezza (vedi patience in
    # plateau.py); qui il budget basso resta sicuro perche' lo stop anticipato
    # ora e' per-partenza, non globale.
    max_evals_per_branch: int = 700,
    coarse: int = 6,
    tol: float = 0.01,
    min_trades: int = 8,
    seed: int = 0,
    accept: Callable[[BacktestResult], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    locked_params: dict | None = None,
) -> PipelineResult:
    """Deterministic parameter selection. Same inputs, same answer, always.

    `accept` is an optional hard filter chosen by the caller (no losing trades,
    a drawdown ceiling). It is applied when picking each branch's winner among
    the finalists the plateau search already ranked, not inside the search: the
    objective has to stay finite everywhere for boundary detection to work, so
    a filter pushed down there would carve holes in the surface being measured.
    A branch whose finalists all fail the filter contributes nothing."""
    bp = backtest_params or {}
    say = progress or (lambda _m: None)
    df_in, df_val, df_out = _three_way_split(df)
    say(f"split: {len(df_in)} train · {len(df_val)} validation · {len(df_out)} test")

    gate = regime_gate(df_in, strategy_name)
    say(f"regime gate: {'passed' if gate.passed else 'NOT passed'} — {gate.reason}")
    stats = estimate_series_stats(df_in)
    # Le scale della coppia, dette prima di cercare: sono quelle che decidono
    # dove ha senso cercare. Un'efficienza bassa significa che la serie fa molta
    # strada senza percorrere distanza — e' la firma di una coppia agitata e
    # senza direzione, quella su cui un incrocio puro entra sul rumore.
    say(f"pair scales: per-bar volatility {stats.sigma_bar:.2%}, "
        f"efficiency {stats.efficiency_ratio:.3f} "
        f"({'directional' if stats.efficiency_ratio >= 0.05 else 'little direction'})"
        + ("" if stats.illiquidity is None else f", illiquidity {stats.illiquidity:.1e}"))
    notes: list[str] = []
    if not gate.passed:
        # Reported, not enforced. Refusing outright would change the behaviour
        # of an endpoint callers already depend on, so the decision is surfaced
        # and the caller decides what to do with it — but a result produced
        # under a failed gate is fitted to a series where the strategy has no
        # measurable edge, and should be read that way.
        notes.append("regime gate did NOT pass — result is fitted to a series "
                     "where this strategy has no measurable edge")

    spaces: list[ConstrainedSpace] = build_spaces(strategy_name, stats, target_entries, locked_params)
    spaces.sort(key=lambda s: s.d)   # simplest branch first
    say(f"{len(spaces)} variant{'' if len(spaces) == 1 else 's'} to explore: "
        f"{', '.join(cs.label for cs in spaces)}")

    outcomes: list[BranchOutcome] = []
    total_evals = 0
    all_enumerated = True
    scored: list[tuple[float, bool, int, str, PlateauCandidate, int]] = []
    # I finalisti di tutti i rami, per il PBO: la sovra-ottimizzazione si misura
    # sull'insieme fra cui si e' scelto, non sul solo vincitore.
    finalists: list[PlateauCandidate] = []

    for cs in spaces:
        say(f"— variant {cs.label} ({cs.d} parameters)")
        res = plateau_search(
            strategy_name, df_in, bp,
            base_points=1, coarse=coarse, tol=tol,
            max_grid_evals=max_evals_per_branch,
            min_trades=_MIN_TRADES_FLOOR.get(strategy_name, min_trades),
            space=cs.as_param_space(), seed=seed,
            # Le fasi della mappatura escono nello stesso log: "mappatura dei
            # pianori…" e poi silenzio per minuti non dice se sta lavorando ne'
            # che cosa sta facendo.
            progress=say,
            should_stop=should_stop,
        )
        total_evals += res.evaluations
        say(f"variant {cs.label}: {res.evaluations} backtests, "
            f"{'grid enumerated' if res.enumerated else 'grid sampled'}, "
            f"{len(res.candidates)} finalist{'' if len(res.candidates) == 1 else 's'}")
        outcomes.append(BranchOutcome(
            label=cs.label, dims=cs.d, enumerated=res.enumerated,
            best=res.best, evaluations=res.evaluations,
            notes=list(cs.notes) + list(res.notes),
        ))
        if not res.enumerated:
            all_enumerated = False
        finalists.extend(res.candidates)
        pick = res.best
        if accept is not None:
            pick = next((c for c in res.candidates if accept(c.backtest)), None)
            if pick is not None and pick is not res.best:
                outcomes[-1].notes.append(
                    "the most stable one didn't meet the filters: picked "
                    "the first finalist that does"
                )
            outcomes[-1].best = pick
        if pick is None:
            if accept is not None:
                outcomes[-1].notes.append("no finalist meets the filters")
            continue

        # Score this branch's winner on data it never saw.
        val_bt = run_backtest(df_val, pick.params, bp, strategy_name=strategy_name)
        total_evals += 1
        outcomes[-1].validation_return_pct = val_bt.total_return_pct
        outcomes[-1].validation_trades = val_bt.num_trades
        scored.append((val_bt.total_return_pct, res.enumerated, cs.d,
                       cs.label, pick, val_bt.num_trades))

    incumbent: PlateauCandidate | None = None
    incumbent_label = ""
    if scored:
        # Structural preferences, applied in this order:
        #   1. enumerated over sampled — a sampled branch reports the maximum
        #      of a random subset, so its score carries exactly the selection
        #      bias this work exists to remove, and must not win on it
        #   2. fewer free parameters — a tie produced with more freedom is not
        #      a tie; in-sample score rises with capacity whether or not the
        #      extra capacity is worth anything (measured: 25.95 for the
        #      8-parameter branch against 8.75 for the 4-parameter one, with
        #      held-out returns of -1.97% and +18.54% respectively)
        #
        # Validation return is the capacity-neutral comparator and is preferred
        # whenever it carries evidence. On series this size it often does not:
        # a 20% slice of 1200 bars left the winning configurations with one or
        # two trades, and a one-trade return is not a measurement. So it is
        # consulted only above a trade floor, and otherwise the structural
        # preferences decide alone — with a note recording which happened,
        # because the two cases deserve different confidence.
        usable = [t for t in scored if t[5] >= _MIN_VALIDATION_TRADES]
        if usable:
            best_val = max(t[0] for t in usable)
            margin = abs(best_val) * _COMPLEXITY_MARGIN
            contenders = [t for t in usable if t[0] >= best_val - margin]
            notes.append(
                f"branch chosen with validation evidence ({len(usable)} of "
                f"{len(scored)} branches cleared {_MIN_VALIDATION_TRADES} "
                f"validation trades)"
            )
        else:
            contenders = list(scored)
            notes.append(
                "validation slice carried too few trades to compare branches "
                f"(fewer than {_MIN_VALIDATION_TRADES}); branch chosen on "
                "reproducibility and parsimony alone, not on measured "
                "out-of-sample performance"
            )
        contenders.sort(key=lambda t: (t[1], -t[2], t[0]), reverse=True)
        _, _, _, incumbent_label, incumbent, _ = contenders[0]

    if incumbent is None:
        if accept is not None:
            raise ValueError(
                f"No configuration meets the requested filters for {strategy_name!r}. "
                f"This mode selects for stability: there are few finalists by "
                f"design, so a tight filter can exclude them all even when "
                f"a configuration meeting it exists elsewhere."
            )
        raise ValueError(
            f"No configuration with at least {min_trades} trades for {strategy_name!r}"
        )

    if not all_enumerated:
        notes.append("at least one branch exceeded the enumeration budget and was "
                     "sampled; results from those branches are not seed independent")

    say(f"chosen variant: {incumbent_label}")

    # Out-of-sample validation on the held-out tail.
    oos_return: float | None = None
    overfitted = False
    oos_trades: list = []
    if len(df_out) > 0:
        oos_bt = run_backtest(df_out, incumbent.params, bp, strategy_name=strategy_name)
        oos_return = oos_bt.total_return_pct
        oos_trades = oos_bt.trades
        is_return = incumbent.backtest.total_return_pct
        if is_return > 0:
            overfitted = ((is_return - oos_return) / abs(is_return)) > 0.50
        else:
            overfitted = oos_return < is_return

    vol = stats.as_dict()
    # I trade in campione del vincitore: la ricerca non li conserva (sarebbero
    # centinaia di MB per migliaia di candidati), quindi si ricalcolano qui con
    # un backtest solo, sugli stessi parametri e sugli stessi dati.
    winner_bt = run_backtest(df_in, incumbent.params, bp, strategy_name=strategy_name)
    total_evals += 1
    best_bt = winner_bt.model_copy(update={"oos_trades": oos_trades})

    # Deflate the winner's Sharpe by how many configurations were actually
    # tried. The expected maximum Sharpe under no skill grows with the number
    # of attempts, so a figure produced by searching thousands of candidates is
    # not comparable to one from a single hypothesis — and reporting it as if
    # it were is the whole failure this work started from. total_evals is
    # passed rather than the shortlist size: understating the trial count
    # inflates the result, which is exactly the error the measure exists to
    # catch.
    curve = incumbent.backtest.equity_curve
    dsr = deflated_sharpe_ratio(
        returns_from_equity_curve(curve),
        n_trials=max(total_evals, 2),
        periods_per_year=periods_per_year_from_curve(curve),
    )
    if dsr.deflated is not None and not dsr.acceptable:
        notes.append(
            f"deflated Sharpe {dsr.deflated:.3f} (below 0.95): after correcting "
            f"for {total_evals} configurations tried, this result is not "
            f"distinguishable from the best of that many attempts on noise"
        )

    # Probabilita' di sovra-ottimizzazione (CSCV). Le curve di equity nascono
    # per trade, quindi vengono prima riportate sulla griglia temporale del
    # campione: accostarle per posizione confronterebbe il terzo trade di una
    # configurazione con il terzo di un'altra, avvenuto mesi dopo, e la misura
    # leggerebbe quel disallineamento invece della sovra-ottimizzazione.
    say("final checks: deflated Sharpe and overfitting")
    pbo: PBOResult | None = None
    if len(finalists) >= 2:
        grid = [int(ts.timestamp()) for ts in df_in.index]
        matrix = align_equity_curves([c.backtest.equity_curve for c in finalists], grid)
        if matrix.size:
            pbo = probability_of_backtest_overfitting(matrix)
            if pbo.pbo is not None and not pbo.acceptable:
                notes.append(
                    f"PBO {pbo.pbo:.2f} over {pbo.n_candidates} candidates "
                    f"({pbo.n_splits} splits): the chosen configuration "
                    f"lands below the out-of-sample median more often "
                    f"than a sound selection would"
                )
            elif pbo.pbo is None and pbo.note:
                notes.append(f"PBO not computable: {pbo.note}")

    return PipelineResult(
        strategy=strategy_name, params=incumbent.params, backtest=best_bt,
        out_of_sample_return_pct=oos_return, overfitted=overfitted,
        evaluations=total_evals, gate=gate, chosen_branch=incumbent_label,
        branches=outcomes,
        deterministic=all_enumerated,
        series_stats=vol, dsr=dsr, pbo=pbo, notes=notes,
    )


def pipeline_top_candidates(result: PipelineResult, limit: int = 10) -> list[dict]:
    """One entry per branch, ranked, for the shortlist the UI shows.

    Showing the branches side by side is the point: it is what makes visible
    that a richer model scored worse, which is the finding the complexity
    preference is built on and the sort of thing a single returned number
    hides."""
    rows = []
    for b in sorted(result.branches,
                    key=lambda x: (x.best.robust_score if x.best and x.best.robust_score
                                   is not None else -math.inf), reverse=True):
        if b.best is None:
            continue
        rows.append({
            "label": b.label,
            "dims": b.dims,
            "enumerated": b.enumerated,
            "params": b.best.params,
            "score": round(b.best.score, 4),
            "robust_score": None if b.best.robust_score is None else round(b.best.robust_score, 4),
            "agreement": None if b.best.neighbourhood_agreement is None
            else round(b.best.neighbourhood_agreement, 4),
            "total_return_pct": b.best.backtest.total_return_pct,
            "max_drawdown_pct": b.best.backtest.max_drawdown_pct,
            "num_trades": b.best.backtest.num_trades,
            "win_rate_pct": b.best.backtest.win_rate_pct,
            "profit_factor": b.best.backtest.profit_factor,
            "chosen": b.label == result.chosen_branch,
        })
    return rows[:limit]
