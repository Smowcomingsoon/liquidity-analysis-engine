"""The system: one trend-following strategy, followed on every Telegram alert.

Lorenzo: "con tutti i backtest che abbiamo fatto finora trova la miglior
strategia di quelle che avevamo a disposizione e rendiamola praticamente
operativa [...] che sia utilizzato i risultati di questi ingressi e di
queste uscite come risultati nella leaderboard [...] per evitare proprio i
rug pull e per prendersi i movimenti rialzisti quando questi occorrono".

What the old leaderboard called "best strategy" was chosen after the fact,
per token (the listing backtest keeps whichever of all strategies made the
most on that very token): a number no one could have had. Here the same
rules apply to every alert, from the moment the message went out:

- candidates: the trend followers we have (Lorenzo, Lorenzo HF), with their
  default parameters (CLAUDE.md: more parameters, worse results);
- 5-minute bars; a signal on a closed bar fills at the next bar's open,
  paying commission and slippage both ways (radar.BACKTEST_PARAMS);
- spot only: an entry buys, an exit sells, short signals just sell;
- a trend already on at the alert is joined at the first bar after it: the
  strategy's own entry may have landed before the alert, and waiting for a
  fresh crossover would miss the whole move (ONHAND: +1304% after the alert,
  system still "waiting");
- WINDOW_S after the alert the position, if any, is closed at that bar;
- the pool vanishing while in position counts as -100%.

The candidate with the best median over alerts whose window has ended
becomes the system; until MIN_DECIDED alerts are decided it is DEFAULT.
Both candidates' numbers stay visible, so the choice can be checked.
"""
from __future__ import annotations

import math

import pandas as pd

from app.analysis import strategies
from app.trading.onchain import radar

WINDOW_S = 48 * 3600
BAR_S = 300
MIN_DECIDED = 20
COST = (radar.BACKTEST_PARAMS["commission_pct"] + radar.BACKTEST_PARAMS["slippage_pct"]) / 100
# A fall this deep inside one bar (or between a bar's close and the next
# open) is a crash no bar-based exit can dodge: the first price to sell at
# is already the crashed one. Lorenzo, 27/09: "ci sono percentuali di meno
# 80%, pero' la strategia dovrebbe aver chiuso molto prima". Each sell says
# which it was, so a -80% explains itself.
CRASH = -0.35
# Memes bought on the alert: sell when a close falls TRAIL below the best
# close since the entry. Lorenzo, 29/09, on the simulated rugs: "facciamo in
# modo che non accada". Measured on the 38 simulated memes with bars (12
# rugs): mean -41% -> -35%, rugs -48% -> -33%, survivors unchanged; 15-25%
# change nothing more, 40% gives back the gain. Only alerts from TRAIL_FROM
# on: the results already shown never change.
TRAIL = 0.30
TRAIL_FROM = 1790726400  # 2026-09-30 00:00 UTC

CANDIDATES = [
    {"strategy_name": "Lorenzo HF (minuti on-chain)", "params": None, "label": "Trend HF", "source": "predefinita"},
    {"strategy_name": "Lorenzo (trend-follower)", "params": None, "label": "Trend EMA", "source": "predefinita"},
]
# Every strategy backtested on the alert's own chart, so each alert shows
# what each one would have made on it (Lorenzo, 27/09: "non esiste che su
# queste crypto non ci sia un backtest delle strategie [...] stessa cosa
# dobbiamo poter fare qui"). Accumulo/Distribuzione is left out: it uses
# future data (radar.UNRELIABLE, seed-0010). Shown labels carry no person's
# name.
BACKTESTS = [
    ("Lorenzo (trend-follower)", "Trend EMA"),
    ("Lorenzo HF (minuti on-chain)", "Trend HF"),
    ("Gabriele (RSI+BB reversal)", "RSI+BB"),
    ("Gabriele 2 (RSI+BB long/short)", "RSI+BB L/S"),
]
BACKTEST_MIN_BARS = 30
TREND_LABELS = frozenset({"Trend EMA", "Trend HF"})
# On-chain the wallet and the lists follow the long-only spot strategies:
# the trend followers and RSI+BB (Lorenzo, 30/09 evening, after seeing the
# wallet without its RSI+BB trades: "l'RSI piu' BB funziona apparentemente
# bene, e quindi implementiamolo nelle strategie, nei backtest"). RSI+BB L/S
# stays out: on-chain there is no short.
SPOT_LABELS = TREND_LABELS | {"RSI+BB"}
# Shown labels carry no person's name (Lorenzo, 27/09: "elimina il nome
# lorenzo dalla strategia"); strategy_name stays the internal key. Results
# stored under the old labels are renamed on load (alert_track._load).
LEGACY_LABELS = {"Lorenzo HF": "Trend HF", "Lorenzo": "Trend EMA"}
DEFAULT = CANDIDATES[0]["label"]
BY_LABEL = {c["label"]: c for c in CANDIDATES}


def frame(bars: list[list]) -> pd.DataFrame:
    """[[ts, open, high, low, close, volume], ...] -> the strategies' frame."""
    rows = sorted({int(b[0]): b for b in bars if len(b) >= 6 and b[4] and b[4] > 0}.values(), key=lambda b: b[0])
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["t"], unit="s", utc=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def follow(bars: list[list], cfg: dict, start: float, *, gone: bool = False, now: float | None = None,
           on_alert: bool = False) -> dict:
    """The strategy's trades from `start` (the alert) to start + WINDOW_S.

    on_alert: the alert itself is the entry (Lorenzo, 28/09: "la strategia
    deve entrare nel momento in cui arriva l'alert [...] Poi deve gestire
    esclusivamente la chiusura secondo la classica regola del trend
    follower"): buy at the first open after the alert, sell on the
    strategy's exit, and no re-entry after that.

    Returns ret (compounded, after costs), trades, state ("in", "out",
    "waiting" = no entry yet), in_at/entry price when in, and the events."""
    end = start + WINDOW_S
    df = frame(bars)
    out = {"ret": None, "trades": 0, "wins": 0, "state": "waiting", "events": [], "entry": None, "in_at": None,
           "done": False}
    if len(df) < 3:
        return out
    try:
        sig = radar.spot_signals(df, cfg)
    except Exception:
        return out
    ts = [int(t.timestamp()) for t in df.index]
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()
    entry_sig = sig["entry_long"].fillna(False).astype(bool).to_numpy()
    exit_sig = sig["exit_long"].fillna(False).astype(bool).to_numpy()
    if on_alert:
        # bought on the alert, not on the strategy's cross: close on the first
        # bar with the trend down, not only on a fresh cross that may never come
        exit_sig = exit_sig | strategies.trend_down(df, cfg["strategy_name"], cfg["params"]).to_numpy()
    equity, pos, entry, in_at = 1.0, False, 0.0, None
    trail, peak = on_alert and start >= TRAIL_FROM, 0.0
    events: list[dict] = []
    last_i = len(df) - 1
    strat_pos, joined = False, False
    for i in range(len(df) - 1):
        t_fill = ts[i + 1]
        if t_fill < start:
            # before the alert: warm-up only, but track where the strategy
            # already stands so a trend in progress can be joined.
            if entry_sig[i]:
                strat_pos = True
            elif exit_sig[i]:
                strat_pos = False
            continue
        if t_fill > end:
            break
        if not joined:
            joined = True
            if on_alert:
                pos, entry, in_at = True, opens[i + 1], t_fill
                peak = entry
                events.append({"t": t_fill, "side": "buy", "price": float(entry), "why": "alert"})
                continue
            if strat_pos and not pos and not exit_sig[i]:
                pos, entry, in_at = True, opens[i + 1], t_fill
                events.append({"t": t_fill, "side": "buy", "price": float(entry), "why": "trend"})
                continue
        if pos:
            peak = max(peak, closes[i])
        if not pos and entry_sig[i] and not on_alert:
            pos, entry, in_at = True, opens[i + 1], t_fill
            events.append({"t": t_fill, "side": "buy", "price": float(entry)})
        elif pos and trail and closes[i] <= peak * (1 - TRAIL):
            r = (opens[i + 1] / entry) * (1 - COST) ** 2
            equity *= r
            out["trades"] += 1
            out["wins"] += int(r > 1)
            pos = False
            events.append({"t": t_fill, "side": "sell", "price": float(opens[i + 1]), "ret": round(r - 1, 4),
                           "why": "trail", "drop": round(closes[i] / peak - 1, 4)})
        elif pos and exit_sig[i]:
            r = (opens[i + 1] / entry) * (1 - COST) ** 2
            equity *= r
            out["trades"] += 1
            out["wins"] += int(r > 1)
            pos = False
            bar_move = closes[i] / closes[i - 1] - 1 if i else 0.0
            gap = opens[i + 1] / closes[i] - 1
            why = "crash" if min(bar_move, gap) <= CRASH else "signal"
            events.append({"t": t_fill, "side": "sell", "price": float(opens[i + 1]), "ret": round(r - 1, 4),
                           "why": why, "drop": round(min(bar_move, gap), 4)})
    now = now or ts[last_i]
    window_over = now >= end
    if pos:
        if gone:
            equity *= 0.0
            out["trades"] += 1
            events.append({"t": int(now), "side": "rug", "price": 0.0, "ret": -1.0, "why": "rug"})
            pos = False
        else:
            j = max(k for k in range(len(ts)) if ts[k] <= end) if window_over else last_i
            mark = closes[j] / entry * (1 - COST) ** 2
            if window_over:
                equity *= mark
                out["trades"] += 1
                out["wins"] += int(mark > 1)
                events.append({"t": ts[j], "side": "sell", "price": float(closes[j]), "ret": round(mark - 1, 4),
                               "why": "48h"})
                pos = False
            else:
                # mark: the close the open trade is valued at, so a live
                # price can re-mark it (value * live / mark) without mixing
                # a candle's return with another reading's price
                out.update(state="in", entry=float(entry), in_at=in_at, mark=float(closes[j]))
                equity *= mark
    if not pos and out["trades"]:
        out["state"] = "out"
    out["ret"] = round(equity - 1, 4) if (out["trades"] or out["state"] == "in") else 0.0
    out["events"] = events[-12:]
    out["done"] = bool(window_over or gone)
    return out


def evaluate(bars: list[list], start: float, *, gone: bool = False, now: float | None = None,
             on_alert: bool = False) -> dict:
    """Every candidate on one alert: {label: follow(...)}."""
    return {c["label"]: follow(bars, c, start, gone=gone, now=now, on_alert=on_alert) for c in CANDIDATES}


def _backtest(bars: list[list], strategies_wanted: list[tuple[str, str]], known: dict | None = None) -> dict:
    """Shared engine: the given strategies backtested on the alert's whole
    chart, every entry and exit, fees included (radar.evaluate, the same
    engine as the main site). `known` is the rug already seen on-chain."""
    df = frame(bars)
    if len(df) < BACKTEST_MIN_BARS:
        return {"bars": len(df), "rows": [], "hold_pct": None, "best": None, "rug": None}
    res = radar.evaluate(df, [{"strategy_name": n, "params": None, "label": lab, "source": "predefinita"}
                              for n, lab in strategies_wanted], known)
    by_label = {r["label"]: r for r in res["tried"]}
    rows = []
    for _name, lab in strategies_wanted:
        r = by_label.get(lab)
        if not r:
            continue
        rows.append({"label": lab,
                     "return_pct": round(r["return_pct"], 2) if r["return_pct"] is not None else None,
                     "trades": r["trades"],
                     "win_rate": round(r["win_rate"], 2) if r["win_rate"] is not None else None,
                     "max_dd": round(r["max_dd"], 2) if r["max_dd"] is not None else None,
                     "profit_factor": round(r["profit_factor"], 2) if r["profit_factor"] is not None else None,
                     "in_position": r["in_position"]})
    hold = res["hold_pct"]
    ev = strategies.rug_event(df, known)
    rug = {"at": int(df.index[ev["i"]].timestamp()), "why": ev["why"], "kind": ev.get("kind", "rug")} if ev is not None else None
    return {"bars": len(df), "from": int(df.index[0].timestamp()), "to": int(df.index[-1].timestamp()),
            "last": float(df["close"].iloc[-1]),
            "hold_pct": round(hold, 2) if hold is not None else None, "rows": rows,
            "best": _best_trades(df, rows, known), "rug": rug}


def backtest_all(bars: list[list], known: dict | None = None) -> dict:
    """Every strategy backtested on the alert's whole chart."""
    return _backtest(bars, BACKTESTS, known)


def backtest_one(bars: list[list], name: str = "Lorenzo (trend-follower)", label: str = "Trend EMA",
                 known: dict | None = None) -> dict:
    """Only one strategy backtested, same engine and same output shape as
    backtest_all. Gems show the site's trend follower on the daily chart
    (Lorenzo, 28/09): the fast 5-minute strategies are for the sniper."""
    return _backtest(bars, [(name, label)], known)


def _best_trades(df: pd.DataFrame, rows: list[dict], known: dict | None = None) -> dict | None:
    """The best backtest's buys and sells, for the chart. Lorenzo, 28/09:
    "visualizza un alert ma non sappiamo se e' un ingresso o altro.. qui deve
    comparire la strategia [...] ingressi e uscite". The ranking shows the
    best backtest, so the chart shows the same one's trades."""
    traded = [r for r in rows if r["trades"] and r["return_pct"] is not None and r["label"] in SPOT_LABELS]
    if not traded:
        return None
    best = max(traded, key=lambda r: r["return_pct"])
    name = next(n for n, lab in BACKTESTS if lab == best["label"])
    try:
        result, signals, _pos = radar.run_spot(df, {"strategy_name": name, "params": None, "label": best["label"],
                                                    "source": "predefinita"}, known)
    except Exception:
        return None
    fee = 2 * radar.BACKTEST_PARAMS["commission_pct"] / 100     # as radar._long_only
    # A trade carries the time of the candle whose close gave the signal, but
    # it fills at the next candle's open (fill_at "next_open"): the arrow goes
    # on the fill candle, as radar.long_events does. Lorenzo, 30/09: "gli
    # ingressi registrati [...] non vengono visualizzati correttamente nel
    # grafico". A stop fills inside its own candle and keeps its time.
    times = [int(ts.timestamp()) for ts in df.index]
    step = times[-1] - times[-2] if len(times) > 1 else 0
    nxt = {times[i]: times[i + 1] for i in range(len(times) - 1)}
    fill_t = lambda t: nxt.get(int(t), int(t) + step)   # noqa: E731
    signal_t = {int(e.time.timestamp()) if hasattr(e.time, "timestamp") else int(e.time) for e in signals}
    events = []
    for tr in result.trades:
        if tr.direction != "long":
            continue
        out_t = int(tr.exit_time)
        events += [{"t": fill_t(tr.entry_time), "side": "buy", "price": tr.entry_price},
                   {"t": fill_t(out_t) if out_t in signal_t else out_t, "side": "sell", "price": tr.exit_price,
                    "ret": round(tr.pnl_pct / 100 - fee, 4)}]
    if best["in_position"] and (not events or events[-1]["side"] != "buy"):
        entry = next((e for e in reversed(signals) if e.type == "entry_long"), None)
        if entry is not None:
            t = entry.time
            t = int(t.timestamp()) if hasattr(t, "timestamp") else int(t)
            i = times.index(t) if t in nxt else None
            price = float(df["open"].iloc[i + 1]) if i is not None else entry.price
            events.append({"t": fill_t(t), "side": "buy", "price": price})
    return {"label": best["label"], "events": events, "fills": "next_open"}


def trend_trade_times(bars: list[list], known: dict | None = None) -> dict[str, set[tuple[int, str]]]:
    """Every buy and sell time of each spot strategy (SPOT_LABELS) on these
    candles, at the signal candle and at the fill candle: what tells which
    strategy made an old 5-minute trade saved without it (sim_backtest.relabel)."""
    df = frame(bars)
    out: dict[str, set[tuple[int, str]]] = {}
    if len(df) < BACKTEST_MIN_BARS:
        return out
    times = [int(ts.timestamp()) for ts in df.index]
    nxt = {times[i]: times[i + 1] for i in range(len(times) - 1)}
    for name, label in BACKTESTS:
        if label not in SPOT_LABELS:
            continue
        try:
            result, signals, _pos = radar.run_spot(df, {"strategy_name": name, "params": None, "label": label,
                                                        "source": "predefinita"}, known)
        except Exception:
            continue
        got: set[tuple[int, str]] = set()
        for tr in result.trades:
            if tr.direction == "long":
                for t, side in ((int(tr.entry_time), "buy"), (int(tr.exit_time), "sell")):
                    got |= {(t, side), (nxt.get(t, t), side)}
        for e in signals:
            if e.type == "entry_long":
                t = int(e.time.timestamp()) if hasattr(e.time, "timestamp") else int(e.time)
                got |= {(t, "buy"), (nxt.get(t, t), "buy")}
        out[label] = got
    return out


def _median(vals: list[float]) -> float | None:
    s = sorted(v for v in vals if v is not None and not math.isnan(v))
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def scoreboard(results: list[dict]) -> dict:
    """results: each alert's evaluate() output. Per candidate over decided
    alerts: n, median, mean, win share, total (sum of per-alert returns, as
    if the same stake went into each), and which one is the system."""
    rows = []
    for c in CANDIDATES:
        done = [r[c["label"]] for r in results if (r.get(c["label"]) or {}).get("done")]
        rets = [d["ret"] for d in done if d.get("ret") is not None]
        rows.append({"label": c["label"], "strategy_name": c["strategy_name"], "n": len(rets),
                     "median": round(_median(rets), 4) if rets else None,
                     "mean": round(sum(rets) / len(rets), 4) if rets else None,
                     "positive": round(sum(r > 0 for r in rets) / len(rets), 3) if rets else None,
                     "total": round(sum(rets), 4) if rets else None,
                     "traded": sum(1 for d in done if d.get("trades"))})
    ready = [r for r in rows if r["n"] >= MIN_DECIDED and r["median"] is not None]
    chosen = max(ready, key=lambda r: (r["median"], r["mean"] or 0))["label"] if ready else DEFAULT
    return {"chosen": chosen, "decided_by": "median" if ready else f"default until {MIN_DECIDED} alerts are decided",
            "candidates": rows, "window_h": WINDOW_S // 3600, "bar": "5m", "cost_pct": round(COST * 100, 2)}
