"""Backtest per operazioni futures/perpetui — leva, margine isolated,
liquidazione, funding.

Modulo separato da backtest.py apposta (vedi il piano "Operazioni futures"):
riusa la stessa firma di segnali e lo stesso pattern di controllo intrabar
(stop/target valutati contro high/low della barra PRIMA del segnale di
quella barra), ma non modifica in alcun modo run_backtest() — un bug qui
non deve poter cambiare il comportamento del motore spot esistente.

Margine isolated soltanto: un conto cross fa esplodere posizioni non
correlate a catena, quindi non e' un caso che questo motore modelli.

Leva 1 di default e' economicamente equivalente allo spot (a parte
funding/commissioni): e' il primo test di correttezza in
test_futures_backtest.py, prima di qualunque altro.
"""

from __future__ import annotations

import math

import pandas as pd

from app.analysis.futures_math import DEFAULT_MAINTENANCE_MARGIN_RATE, liquidation_price
from app.analysis.strategies import DEFAULT_STRATEGY, confirmed_events
from app.models import MAX_SIMULATED_LEVERAGE, BacktestResult, BacktestTrade, StrategySignal

DEFAULT_FUTURES_PARAMS = {
    "initial_capital": 10000.0,
    "qty_pct_of_equity": 30.0,
    "commission_pct": 0.06,
    "slippage_pct": 0.02,
    "stop_loss_pct": 0.0,
    "take_profit_pct": 0.0,
    "leverage": 1,
    # Vedi app.analysis.futures_math per la fonte del default.
    "maintenance_margin_rate": DEFAULT_MAINTENANCE_MARGIN_RATE,
    # Funding rate per 8h, in frazione. 0 = nessun dato di funding
    # disponibile per questa serie storica: il costo NON viene inventato,
    # resta zero e basta.
    "funding_rate_8h": 0.0,
}

_ENTRY_TYPES = {"entry_long", "entry_short"}
_EXIT_TYPES = {"exit_long", "exit_short"}
_FLIP_TYPES = {"flip_to_long", "flip_to_short"}

_FUNDING_HOURS = {0, 8, 16}  # UTC


class InvalidLeverageError(ValueError):
    pass


def _fill_price(price: float, pct: float, side: str) -> float:
    return price * (1 + pct / 100) if side == "buy" else price * (1 - pct / 100)


def run_futures_backtest(
    df: pd.DataFrame,
    strategy_params: dict | None,
    backtest_params: dict | None,
    signals: pd.DataFrame | None = None,
    strategy_name: str = DEFAULT_STRATEGY,
) -> BacktestResult:
    bp = {**DEFAULT_FUTURES_PARAMS, **(backtest_params or {})}
    leverage = int(bp["leverage"])
    # Il tetto della simulazione, non quello dell'esecuzione: qui non parte
    # nessun ordine, e limitare lo studio al rischio consentito dal vivo
    # significava non poter piu' osservare una liquidazione.
    if leverage < 1 or leverage > MAX_SIMULATED_LEVERAGE:
        raise InvalidLeverageError(
            f"Leverage {leverage}x outside the simulatable range (1-{MAX_SIMULATED_LEVERAGE}x)"
        )
    maint_rate = bp["maintenance_margin_rate"]
    funding_rate_8h = bp["funding_rate_8h"]

    events, _final_position = confirmed_events(df, strategy_params, signals=signals, strategy_name=strategy_name)
    event_by_ts: dict[int, StrategySignal] = {s.time: s for s in events}

    equity = bp["initial_capital"]
    peak_equity = equity
    max_drawdown_pct = 0.0
    trades: list[BacktestTrade] = []
    liquidated_count = 0
    equity_curve: list[tuple[int, float]] = [(int(df.index[0].timestamp()), equity)]

    open_trade: dict | None = None  # {direction, entry_time, entry_price, margin_used, liq_price, funding_paid}

    def _open(direction: str, signal: StrategySignal) -> None:
        nonlocal open_trade
        fill = _fill_price(signal.price, bp["slippage_pct"], "buy" if direction == "long" else "sell")
        margin_used = equity * (bp["qty_pct_of_equity"] / 100)
        liq_price = liquidation_price(fill, direction, leverage, maint_rate)
        open_trade = {
            "direction": direction,
            "entry_time": signal.time,
            "entry_price": fill,
            "margin_used": margin_used,
            "liq_price": liq_price,
            "funding_paid": 0.0,
        }

    def _close_at(raw_price: float, time: int, liquidated: bool = False) -> None:
        nonlocal equity, peak_equity, max_drawdown_pct, open_trade, liquidated_count
        if open_trade is None:
            return
        direction = open_trade["direction"]
        exit_fill = raw_price if liquidated else _fill_price(
            raw_price, bp["slippage_pct"], "sell" if direction == "long" else "buy"
        )
        entry_fill = open_trade["entry_price"]
        margin_used = open_trade["margin_used"]
        notional = margin_used * leverage

        raw_return = (exit_fill - entry_fill) / entry_fill
        if direction == "short":
            raw_return = -raw_return

        commission = notional * (bp["commission_pct"] / 100) * 2  # entry + exit
        pnl_abs = notional * raw_return - commission - open_trade["funding_paid"]
        if liquidated:
            # Il margine impegnato e' perso per intero: e' quello che
            # "liquidazione" significa, non "pnl leggermente negativo".
            pnl_abs = -margin_used
            liquidated_count += 1
        equity += pnl_abs
        peak_equity = max(peak_equity, equity)
        drawdown_pct = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0.0
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        trades.append(
            BacktestTrade(
                direction=direction,
                entry_time=open_trade["entry_time"],
                exit_time=time,
                entry_price=entry_fill,
                exit_price=exit_fill,
                pnl_pct=raw_return * 100,
                pnl_abs=pnl_abs,
                equity_after=equity,
                leverage=leverage,
                liquidation_price=open_trade["liq_price"],
                funding_paid=open_trade["funding_paid"],
                margin_used=margin_used,
                liquidated=liquidated,
            )
        )
        equity_curve.append((time, equity))
        open_trade = None

    stop_pct = bp["stop_loss_pct"]
    target_pct = bp["take_profit_pct"]
    stop_loss_enabled = stop_pct > 0 or target_pct > 0

    prev_ts: pd.Timestamp | None = None
    for ts, row in df.iterrows():
        t = int(ts.timestamp())

        # Funding: accrual sulle barre che attraversano un orario di
        # liquidazione (00/08/16 UTC), sulla posizione gia' aperta a inizio
        # barra — stessa logica "prima del segnale di questa barra" usata
        # sotto per liquidazione e stop/target.
        if open_trade is not None and funding_rate_8h != 0 and prev_ts is not None:
            crossed = any(
                prev_ts < ts.normalize() + pd.Timedelta(hours=h) <= ts for h in _FUNDING_HOURS
            )
            if crossed:
                notional = open_trade["margin_used"] * leverage
                open_trade["funding_paid"] += notional * funding_rate_8h
        prev_ts = ts

        # Liquidazione: precedenza assoluta su tutto il resto in questa
        # barra, stop/target e segnale della strategia inclusi — un trade
        # liquidato un attimo prima di un'uscita "gentile" non deve essere
        # sbloccato dal segnale della strategia sulla stessa barra.
        if open_trade is not None:
            direction = open_trade["direction"]
            liq_price = open_trade["liq_price"]
            liq_hit = row["low"] <= liq_price if direction == "long" else row["high"] >= liq_price
            if liq_hit:
                _close_at(liq_price, t, liquidated=True)
                continue

        if stop_loss_enabled and open_trade is not None:
            direction = open_trade["direction"]
            entry = open_trade["entry_price"]
            if direction == "long":
                stop_level = entry * (1 - stop_pct / 100) if stop_pct > 0 else None
                target_level = entry * (1 + target_pct / 100) if target_pct > 0 else None
                stop_hit = stop_level is not None and row["low"] <= stop_level
                target_hit = target_level is not None and row["high"] >= target_level
            else:
                stop_level = entry * (1 + stop_pct / 100) if stop_pct > 0 else None
                target_level = entry * (1 - target_pct / 100) if target_pct > 0 else None
                stop_hit = stop_level is not None and row["high"] >= stop_level
                target_hit = target_level is not None and row["low"] <= target_level
            if stop_hit:
                _close_at(stop_level, t)
                continue
            if target_hit:
                _close_at(target_level, t)
                continue

        signal = event_by_ts.get(t)
        if signal is None:
            continue
        if signal.type in _ENTRY_TYPES:
            _open("long" if signal.type == "entry_long" else "short", signal)
        elif signal.type in _EXIT_TYPES:
            _close_at(signal.price, signal.time)
        elif signal.type in _FLIP_TYPES:
            _close_at(signal.price, signal.time)
            _open("long" if signal.type == "flip_to_long" else "short", signal)

    wins = [t for t in trades if t.pnl_abs > 0]
    losses = [t for t in trades if t.pnl_abs <= 0]
    gross_profit = sum(t.pnl_abs for t in wins)
    gross_loss = -sum(t.pnl_abs for t in losses)

    data_start = int(df.index[0].timestamp() * 1000)
    data_end = int(df.index[-1].timestamp() * 1000)

    sharpe_ratio: float | None = None
    if len(equity_curve) >= 3:
        eq_values = [v for _, v in equity_curve]
        daily_returns = [(eq_values[i] - eq_values[i - 1]) / eq_values[i - 1] for i in range(1, len(eq_values))]
        mean_r = sum(daily_returns) / len(daily_returns)
        var_r = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
        std_r = math.sqrt(var_r)
        if std_r > 0:
            sharpe_ratio = (mean_r / std_r) * math.sqrt(252)

    max_consec_losses: int | None = None
    if trades:
        max_consec_losses = 0
        current = 0
        for t in trades:
            if t.pnl_pct < 0:
                current += 1
                max_consec_losses = max(max_consec_losses, current)
            else:
                current = 0

    return BacktestResult(
        interval="",
        initial_capital=bp["initial_capital"],
        final_equity=equity,
        total_return_pct=(equity - bp["initial_capital"]) / bp["initial_capital"] * 100,
        num_trades=len(trades),
        win_rate_pct=(len(wins) / len(trades) * 100) if trades else None,
        max_drawdown_pct=max_drawdown_pct,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        equity_curve=equity_curve,
        trades=trades,
        data_start=data_start,
        data_end=data_end,
        sharpe_ratio=sharpe_ratio,
        max_consecutive_losses=max_consec_losses,
        liquidated_count=liquidated_count,
    )
