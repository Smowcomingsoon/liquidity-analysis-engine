"""
Structural priors and space reduction (pipeline stage S2).

Phase E left one blocker: plateau search is deterministic only when the cell
grid can be enumerated, and Lorenzo's ten free parameters put enumeration many
orders of magnitude out of reach. Narrowing ranges does not fix that — it
reduces cells per axis while the exponent stays at ten. What fixes it is
removing axes, and there are two ways to do that honestly.

  1. Derive a parameter instead of searching it. Gabriele's band width follows
     from the entry frequency the user wants: under approximate normality the
     fraction of bars beyond k sigma is 2(1 - Phi(k)), so fixing N entries over
     T bars fixes k. That converts a searched dimension into a stated
     preference, which is both smaller and more interpretable.

  2. Exploit inert parameters. When Lorenzo's trendfilter is off, its period
     and type do not enter the computation at all — the backtest is
     byte-identical whatever they hold. These are not weak parameters to be
     frozen at a default, they are absent, and the space genuinely decomposes
     into branches of different dimension. Inertness is verified against the
     backtest rather than assumed from reading the code, because a parameter
     that is inert in principle but read somewhere in practice would silently
     corrupt the decomposition.

Data-dependent ranges (from the estimators below) sit on top of both: they
narrow what remains, using scales measured from the series rather than the
arbitrary bounds the random sampler happened to declare.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.analysis.backtest import run_backtest
from app.analysis.sensitivity import PARAM_SPACES, ParamSpace, _b, _c, _f, _i

_NORM = statistics.NormalDist()


# --- series statistics -------------------------------------------------------


@dataclass
class SeriesStats:
    """Scales measured from the series, used to place parameter ranges."""

    n: int
    rho1: float                  # lag-1 autocorrelation of log returns
    tau_int: float               # integrated autocorrelation time (1+r)/(1-r)
    ou_half_life: float | None   # mean-reversion half-life of detrended price
    dominant_period: float | None
    # --- scale della coppia, non della sua struttura temporale ---------------
    # Le tre sopra dicono su che orizzonte la serie ha memoria. Queste dicono
    # quanto si muove e quanto di quel movimento e' direzione. Servono perche'
    # la stessa finestra che su BTC copre un trend, su una coppia che fa il 15%
    # al giorno copre rumore — ed e' il caso in cui il sistema entra e esce su
    # oscillazioni che non sono direzione.
    sigma_bar: float = 0.0           # deviazione standard dei rendimenti per barra
    efficiency_ratio: float = 0.0    # |P_fine - P_inizio| / somma |dP| (Kaufman)
    illiquidity: float | None = None  # Amihud: |r| medio per unita' di volume scambiato

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "rho1": round(self.rho1, 4),
            "tau_int": round(self.tau_int, 2),
            "ou_half_life": None if self.ou_half_life is None else round(self.ou_half_life, 2),
            "dominant_period": None if self.dominant_period is None else round(self.dominant_period, 2),
        }


def _detrended_log_price(close: np.ndarray) -> np.ndarray:
    y = np.log(close)
    t = np.arange(y.size, dtype=float)
    slope, intercept = np.polyfit(t, y, 1)
    return y - (slope * t + intercept)


def estimate_series_stats(df: pd.DataFrame) -> SeriesStats:
    close = pd.to_numeric(df["close"], errors="coerce").astype(float).to_numpy()
    close = close[np.isfinite(close) & (close > 0)]
    n = close.size
    if n < 60:
        return SeriesStats(n=n, rho1=0.0, tau_int=1.0, ou_half_life=None, dominant_period=None)

    r = np.diff(np.log(close))
    rho1 = float(np.corrcoef(r[:-1], r[1:])[0, 1]) if r.size > 2 else 0.0
    if not math.isfinite(rho1):
        rho1 = 0.0
    # Keep tau_int finite: rho1 -> 1 is a random walk, where no lookback is
    # implied by the autocorrelation and the caller must fall back to defaults.
    rho_c = float(np.clip(rho1, -0.95, 0.95))
    tau_int = (1.0 + rho_c) / (1.0 - rho_c)

    # Mean-reversion half-life from an AR(1) fit on detrended log price.
    dev = _detrended_log_price(close)
    half_life: float | None = None
    if dev.size > 10:
        phi = float(np.corrcoef(dev[:-1], dev[1:])[0, 1])
        if math.isfinite(phi) and 0.0 < phi < 1.0:
            hl = -math.log(2.0) / math.log(phi)
            if math.isfinite(hl) and hl > 0:
                half_life = float(np.clip(hl, 2.0, 500.0))

    # Dominant cycle from the periodogram peak of the detrended series.
    dominant: float | None = None
    if dev.size >= 64:
        spec = np.abs(np.fft.rfft(dev - dev.mean())) ** 2
        freqs = np.fft.rfftfreq(dev.size, d=1.0)
        if spec.size > 3:
            # Only accept a peak that is genuinely a cycle. The lowest
            # frequencies carry whatever trend the linear detrend left behind
            # and always dominate the periodogram; a "period" equal to half the
            # sample is that artefact, not a cycle, and anchoring parameters to
            # it would be worse than having no prior at all. Require the period
            # to be at most a quarter of the sample, else report None and let
            # the caller keep the unnarrowed range.
            max_period = dev.size / 4.0
            min_k = max(2, int(math.ceil(dev.size / max_period)))
            if min_k < spec.size:
                k = int(np.argmax(spec[min_k:])) + min_k
                if freqs[k] > 0:
                    cand = 1.0 / freqs[k]
                    if math.isfinite(cand) and 4.0 <= cand <= max_period:
                        dominant = float(cand)

    sigma_bar, er, illiq = _pair_scales(df, close, r)
    return SeriesStats(n=n, rho1=rho1, tau_int=tau_int,
                       ou_half_life=half_life, dominant_period=dominant,
                       sigma_bar=sigma_bar, efficiency_ratio=er, illiquidity=illiq)


def _pair_scales(df: pd.DataFrame, close: np.ndarray,
                 r: np.ndarray) -> tuple[float, float, float | None]:
    """Quanto si muove la coppia, e quanto di quel movimento e' direzione.

    * `sigma_bar` — deviazione standard dei rendimenti logaritmici per barra.
      E' l'unita' di misura naturale della coppia: una pendenza di 0.002 e'
      enorme su una coppia che fa lo 0.5% al giorno e invisibile su una che ne
      fa il 15%. Esprimere le soglie in multipli di questa e' cio' che rende la
      stessa configurazione trasportabile fra coppie diverse.

    * `efficiency_ratio` — la distanza percorsa diviso la strada fatta
      (Kaufman). Vale 1 su una salita monotona e tende a 0 su un movimento che
      va avanti e indietro senza arrivare da nessuna parte. E' la misura
      diretta di "questa coppia ha un trend vero oppure e' agitazione": e'
      esattamente cio' che manca quando una altcoin poco liquida sembra viva ma
      non va da nessuna parte.

    * `illiquidity` — Amihud: quanto prezzo si muove per unita' di volume
      scambiato. Alto significa che bastano pochi soldi per spostare il prezzo,
      cioe' che i movimenti sono meno informativi. None quando il volume non
      c'e' o e' inutilizzabile, invece di un numero inventato.
    """
    sigma = float(np.std(r, ddof=1)) if r.size > 2 else 0.0
    if not math.isfinite(sigma):
        sigma = 0.0

    moved = float(abs(close[-1] - close[0]))
    travelled = float(np.sum(np.abs(np.diff(close))))
    er = moved / travelled if travelled > 0 else 0.0
    if not math.isfinite(er):
        er = 0.0

    illiq: float | None = None
    if "volume" in df.columns:
        vol = pd.to_numeric(df["volume"], errors="coerce").astype(float).to_numpy()
        # Volume in unita' della moneta, non in valuta: moltiplicato per il
        # prezzo diventa confrontabile fra coppie con prezzi di ordini di
        # grandezza diversi.
        turnover = vol[1:] * close[1:] if vol.size == close.size else np.array([])
        usable = turnover > 0
        if usable.sum() >= 30:
            ratio = np.abs(r[usable[: r.size]]) / turnover[usable][: r.size][: np.count_nonzero(usable[: r.size])]
            ratio = ratio[np.isfinite(ratio)]
            if ratio.size:
                illiq = float(np.median(ratio))
    return sigma, float(np.clip(er, 0.0, 1.0)), illiq


# --- derived parameters ------------------------------------------------------


def band_multiplier_for_frequency(n_entries: int, n_bars: int) -> float:
    """Bollinger k implied by wanting n_entries touches over n_bars.

    Under approximate normality the fraction of bars beyond +/- k sigma is
    2(1 - Phi(k)), so k = Phi^-1(1 - N / 2T). This replaces a searched
    dimension with a stated preference: the user picks how often they want to
    trade, which is a decision they can actually reason about, and the band
    width follows. Clamped to the range the strategy's own inputs allow.
    """
    if n_bars <= 0 or n_entries <= 0:
        return 2.0
    p = 1.0 - n_entries / (2.0 * n_bars)
    p = min(max(p, 0.5 + 1e-9), 1.0 - 1e-9)
    return float(np.clip(_NORM.inv_cdf(p), 1.0, 3.5))


# --- constrained spaces ------------------------------------------------------

def _narrow(
    declared: tuple[int, int],
    measured: tuple[float, float],
    label: str,
) -> tuple[int, int, str]:
    """Narrow a declared range using a measured scale, refusing to collapse it.

    When the measured scale falls outside what the strategy's own inputs allow,
    clipping both ends lands them on the same bound and silently pins the
    parameter to a single value. That is not a narrowed search, it is a
    different strategy — and it is a decision taken by an estimator that has
    just demonstrated it does not apply here. In that case keep the declared
    range and record why.
    """
    d_lo, d_hi = declared
    lo = max(d_lo, int(measured[0]))
    hi = min(d_hi, int(measured[1]))
    if hi - lo < 3:
        return d_lo, d_hi, (
            f"{label} left unnarrowed: measured scale "
            f"[{measured[0]:.0f}, {measured[1]:.0f}] does not usefully overlap "
            f"the declared range [{d_lo}, {d_hi}]"
        )
    return lo, hi, f"{label} in [{lo}, {hi}] from measured scale"




@dataclass
class ConstrainedSpace:
    """A ParamSpace restricted by structural priors, plus what it cost."""

    label: str
    dims: tuple[str, ...]
    to_params: Callable[[np.ndarray], dict]
    fixed: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def d(self) -> int:
        return len(self.dims)

    def as_param_space(self) -> ParamSpace:
        return ParamSpace(self.dims, self.to_params)


def _lorenzo_space(stats: SeriesStats, trendfilter: bool, volatilityfilter: bool) -> ConstrainedSpace:
    """One branch of Lorenzo's space, with inert parameters removed.

    The lookback is centred on the integrated autocorrelation time, which is
    the scale on which the series actually carries trend structure; searching
    far from it is searching where the signal is not. The slow slope is
    expressed as a RATIO of the fast one rather than as a free length: below
    about 2 the two lines are the same signal and their crossings are noise,
    above about 5 the slow line never updates and the system stops exiting.
    """
    # tau_int anchors the lookback only when there is autocorrelation to
    # anchor to. On most financial return series rho1 sits near zero, tau_int
    # collapses towards 1, and any multiplier applied to it is a number the
    # author chose rather than a scale the data implied. Rather than dress that
    # up, the range is narrowed only when rho1 is large enough for tau_int to
    # mean something, and left at the declared bounds otherwise.
    if abs(stats.rho1) >= 0.10 and stats.tau_int >= 1.5:
        center = float(np.clip(stats.tau_int * 20.0, 30.0, 240.0))
        len_lo = max(20, int(center * 0.5))
        len_hi = min(300, max(len_lo + 20, int(center * 1.5)))
        len_note = f"len in [{len_lo}, {len_hi}] from tau_int={stats.tau_int:.2f}"
    else:
        len_lo, len_hi = 20, 300
        len_note = (f"len left unnarrowed: rho1={stats.rho1:.3f} too small for "
                    f"tau_int to imply a lookback scale")

    # Quando la serie percorre poca strada rispetto a quanta ne fa — cioe' si
    # muove molto senza andare da nessuna parte — una finestra corta misura
    # oscillazioni, non direzione. Si sposta allora il fondo della ricerca
    # verso l'alto invece di lasciare che l'ottimizzatore trovi il picco
    # fortunato di una finestra da 20 barre su una altcoin agitata.
    #
    # Misurato su daily reali: BTCUSDT ER=0.037, KASUSDT 0.006, PEPEUSDT 0.008.
    # Su queste serie l'efficienza e' bassa quasi ovunque, e la soglia sotto e'
    # tarata di conseguenza: non serve a distinguere i casi comuni, serve a non
    # cercare finestre corte proprio dove non possono funzionare.
    if stats.efficiency_ratio < 0.02 and stats.n >= 200:
        floor = max(len_lo, 60)
        if floor < len_hi:
            len_lo = floor
            len_note += (f" · floor raised to {len_lo}: efficiency "
                         f"{stats.efficiency_ratio:.3f}, the series moves a lot "
                         f"without covering distance")

    dims: list[str] = ["average", "len", "slopeFlen", "slope_ratio", "slope_threshold_sigma"]
    if trendfilter:
        dims += ["trendfilter_mult", "trendfiltertype"]
    if volatilityfilter:
        dims += ["volatilitystdevlength", "volatilitystdevmalength"]

    idx = {name: i for i, name in enumerate(dims)}

    def to_params(u: np.ndarray) -> dict:
        length = _i(u[idx["len"]], len_lo, len_hi)
        slope_fast = _i(u[idx["slopeFlen"]], 2, 30)
        ratio = _f(u[idx["slope_ratio"]], 2.0, 5.0)
        slope_slow = int(np.clip(round(slope_fast * ratio), slope_fast + 3, 80))
        p = {
            # Quanta pendenza serve perche' conti come direzione, in unita' di
            # sigma della pendenza stessa. Cercata e non fissata: quanto rumore
            # vada tolto dipende dalla coppia, ed e' la variabile che oggi
            # mancava del tutto. Zero resta nell'intervallo, quindi il
            # comportamento storico e' sempre raggiungibile.
            "slope_threshold_sigma": round(_f(u[idx["slope_threshold_sigma"]], 0.0, 1.2), 2),
            "average": _c(u[idx["average"]], ["EMA", "SMA"]),
            "len": length,
            "slopeFlen": slope_fast,
            "slopeSlen": slope_slow,
            "trendfilter": trendfilter,
            "volatilityfilter": volatilityfilter,
        }
        if trendfilter:
            # A filter faster than the signal it filters is not a filter, it is
            # a second and noisier signal wearing the word "confirmation".
            mult = _f(u[idx["trendfilter_mult"]], 1.5, 4.0)
            p["trendfilterperiod"] = int(np.clip(round(length * mult), 50, 400))
            p["trendfiltertype"] = _c(u[idx["trendfiltertype"]], ["EMA", "SMA"])
        else:
            p["trendfilterperiod"] = 200
            p["trendfiltertype"] = "EMA"
        if volatilityfilter:
            p["volatilitystdevlength"] = _i(u[idx["volatilitystdevlength"]], 5, 40)
            p["volatilitystdevmalength"] = _i(u[idx["volatilitystdevmalength"]], 10, 80)
        else:
            p["volatilitystdevlength"] = 20
            p["volatilitystdevmalength"] = 40
        return p

    return ConstrainedSpace(
        label=f"trendfilter={trendfilter} volatilityfilter={volatilityfilter}",
        dims=tuple(dims), to_params=to_params,
        fixed={"trendfilter": trendfilter, "volatilityfilter": volatilityfilter},
        notes=[len_note, "slopeSlen expressed as ratio in [2, 5] of slopeFlen",
               f"slope_threshold_sigma searched in [0, 1.2] · sigma={stats.sigma_bar:.4f}, "
               f"efficienza={stats.efficiency_ratio:.3f}"],
    )


def _gabriele_space(stats: SeriesStats, target_entries: int,
                    locked: dict | None = None) -> ConstrainedSpace:
    """Gabriele con lunghezza RSI, lunghezza Bollinger e moltiplicatore presi
    da chi cerca, non dalla ricerca.

    Richiesto esplicitamente (segnalazione 72f880af / whitekobra): il tasto
    Ottimizza per le due varianti Gabriele deve toccare solo RSI ipercomprato
    e ipervenduto, lasciando gli altri tre esattamente ai valori impostati nei
    campi del Lab. Prima qui c'era una derivazione statistica del
    moltiplicatore (dalla frequenza di ingresso voluta) e una ricerca di
    lunghezza RSI/Bollinger — tecnica valida ma non quello che e' stato
    chiesto: chi imposta quei tre numeri a mano vuole che restino quelli.
    """
    locked = locked or {}
    rsi_length = int(locked.get("rsi_length", 14))
    bb_length = int(locked.get("bb_length", 20))
    bb_mult = float(locked.get("bb_mult", band_multiplier_for_frequency(target_entries, stats.n)))

    # If the band already selects a 2-sigma event, an RSI threshold of 45 is
    # not a second filter: it fires on nearly everything the band caught.
    rsi_os_hi = 30 if bb_mult >= 2.0 else 40

    dims = ("rsi_oversold", "rsi_overbought")

    def to_params(u: np.ndarray) -> dict:
        return {
            "rsi_length": rsi_length,
            "bb_length": bb_length,
            "bb_mult": bb_mult,
            "rsi_oversold": _i(u[0], 15, rsi_os_hi),
            "rsi_overbought": _i(u[1], 60, 85),
        }

    return ConstrainedSpace(
        label="gabriele-constrained", dims=dims, to_params=to_params,
        fixed={"rsi_length": rsi_length, "bb_length": bb_length, "bb_mult": bb_mult},
        notes=[f"RSI length={rsi_length}, Bollinger length={bb_length}, "
               f"multiplier={bb_mult:.2f}: taken from the set fields, not searched — "
               "only RSI overbought and oversold are searched",
               f"rsi_oversold capped at {rsi_os_hi}"],
    )


def _exhaustion_space(stats: SeriesStats) -> ConstrainedSpace:
    """Accumulo/Distribuzione, anchored on the dominant cycle."""
    if stats.dominant_period is not None:
        period = stats.dominant_period
        sw_lo, sw_hi, period_note = _narrow(
            (3, 20), (period / 8.0, period / 2.0),
            f"swing_length (cycle={period:.0f})")
    else:
        # No cycle survived the trend test. Keep the declared range and say so,
        # rather than anchoring to a default period nothing measured.
        period = 40.0
        sw_lo, sw_hi = 3, 20
        period_note = "swing_length left unnarrowed: no dominant cycle detected"

    dims = ("swing_length", "rsi_length", "rsi_oversold", "rsi_overbought",
            "rsi_min_gap", "enable_contraction", "enable_range_bounce")

    def to_params(u: np.ndarray) -> dict:
        swing = _i(u[0], sw_lo, sw_hi)
        # A divergence compared inside one leg of a move is not a divergence.
        div_lb = int(np.clip(math.ceil((period / 2.0) / max(swing, 1)), 2, 8))
        return {
            "swing_length": swing,
            "rsi_length": _i(u[1], 7, 25),
            "rsi_oversold": _i(u[2], 20, 45),
            "rsi_overbought": _i(u[3], 55, 80),
            "rsi_min_gap": _f(u[4], 1.0, 10.0),
            "low_tolerance_pct": 1.0,
            "divergence_lookback": div_lb,
            "volume_ma_length": 20,
            "volume_multiplier": 1.5,
            "trend_filter": True,
            # The filter has to be slower than the swings it filters.
            "trend_ema_length": int(np.clip(swing * 6, 20, 100)),
            "confirmation_lookahead": int(np.clip(swing, 3, 20)),
            "contraction_lookback": 10,
            "contraction_prior_lookback": 40,
            "contraction_ratio": 0.55,
            "range_lookback": 50,
            "range_zone_pct": 0.25,
            "enable_divergence": True,
            "enable_contraction": _b(u[5]),
            "enable_range_bounce": _b(u[6]),
        }

    return ConstrainedSpace(
        label="exhaustion-constrained", dims=dims, to_params=to_params,
        notes=[period_note,
               "divergence_lookback derived to span half a cycle",
               "trend_ema_length = 6x swing_length"],
    )


def build_spaces(
    strategy_name: str,
    stats: SeriesStats,
    target_entries: int = 40,
    locked: dict | None = None,
) -> list[ConstrainedSpace]:
    """Constrained space(s) for a strategy.

    Lorenzo returns one space per switch combination — the branches have
    genuinely different dimension, so treating them as one 10-dimensional
    space both overstates the problem and wastes the search.

    `locked` (Gabriele only) are the values currently set in the caller's own
    param fields, held fixed instead of searched or derived.
    """
    if strategy_name == "Lorenzo (trend-follower)":
        return [_lorenzo_space(stats, tf, vf)
                for tf in (False, True) for vf in (False, True)]
    if strategy_name in ("Gabriele (RSI+BB reversal)", "Gabriele 2 (RSI+BB long/short)"):
        return [_gabriele_space(stats, target_entries, locked)]
    if strategy_name == "Accumulo / Distribuzione (long-term)":
        return [_exhaustion_space(stats)]
    raise ValueError(f"No constrained space for {strategy_name!r}")


# --- inertness verification --------------------------------------------------


def verify_inert(
    df: pd.DataFrame,
    strategy_name: str,
    base_params: dict,
    candidates: Sequence[str],
    values: dict[str, Sequence],
    backtest_params: dict | None = None,
) -> dict[str, bool]:
    """Check against the backtest which parameters are genuinely inert.

    The branch decomposition is only sound if a parameter dropped from a branch
    truly does not enter the computation. Reading the strategy source is not
    enough: a value read on some path the reader did not follow would make the
    decomposition silently wrong, and the failure would look like noise in the
    results rather than like a bug. So it is measured — vary the parameter and
    see whether the trade sequence moves at all.
    """
    bp = backtest_params or {}
    base = run_backtest(df, base_params, bp, strategy_name=strategy_name)
    base_fp = (round(base.total_return_pct, 6), base.num_trades)

    out: dict[str, bool] = {}
    for name in candidates:
        inert = True
        for v in values.get(name, ()):
            trial = {**base_params, name: v}
            bt = run_backtest(df, trial, bp, strategy_name=strategy_name)
            if (round(bt.total_return_pct, 6), bt.num_trades) != base_fp:
                inert = False
                break
        out[name] = inert
    return out


def branch_grid_estimate(spaces: Sequence[ConstrainedSpace], cells_per_dim: float) -> float:
    """Total cells across branches, for deciding whether enumeration fits."""
    return float(sum(cells_per_dim ** s.d for s in spaces))


def unconstrained_dims(strategy_name: str) -> int:
    return PARAM_SPACES[strategy_name].d
