"""The simulated wallet replayed on the Top 50's backtests.

Lorenzo, 30/09: "voglio che tu applichi a quel portafoglio [10.000 dollari]
tutti gli ingressi e tutti i rendimenti di tutte le gemme in top 50 per
vedere come avrebbe performato". Every token in the Top carries the trades
of the backtest its row shows (strategy_lab: the 8h/1d trend follower, or
the best 5-minute strategy when that one has not traded). Here all those
trades share one wallet, in time order: a buy spends `share` of the equity
at that moment (never more than the free cash), its sell returns the stake
times the trade's return, fees included as in the backtest. Open trades are
marked at the live price when there is one, at cost otherwise.

Lorenzo, poi: "farlo visualizzare a tutti gli utenti indipendentemente
dall'account" (one $10,000 wallet, the same for everyone) e "deve continuare
ad aggiornarsi [...] sui prossimi ingressi e futuri, non soltanto sul
backtest". So every trade seen is kept in a ledger that only grows
(`remember`): a 5-minute window sliding past an old trade, or a token
leaving the Top, does not erase it. The live simulated positions are not
touched.
"""
from __future__ import annotations

import json
import math

from app.atomic_write import write_text_atomic
from app.trading.onchain.storage import data_file
from app.trading.onchain.system import SPOT_LABELS, TREND_LABELS

_FILE = data_file("top_backtest_ledger.json")
_ledger: dict[str, dict] = {}     # chain:token -> {chain, token, symbol, interval, events: {t:side -> event}}
_loaded = False

DEFAULT_CAPITAL = 10_000.0
DEFAULT_SHARE = 0.10
MIN_STAKE = 1.0


def token_trades(bt: dict | None) -> tuple[str, list[dict]] | None:
    """(interval, events) of the backtest the Top row shows: the 8h/1d trend
    follower when it has traded, otherwise the best 5-minute strategy."""
    if not bt:
        return None
    daily = bt.get("daily") or {}
    drow = (daily.get("rows") or [None])[0]
    if drow and drow.get("trades") and (daily.get("best") or {}).get("events"):
        return daily.get("interval") or "1d", daily["best"]["events"]
    # The 5-minute best: a trend follower or RSI+BB (system.SPOT_LABELS).
    # Lorenzo, 30/09: trend-only first, then, seeing the wallet without the
    # RSI+BB trades, "implementiamolo nelle strategie, nei backtest".
    best = bt.get("best") or {}
    if best.get("events") and best.get("label") in SPOT_LABELS:
        return "5m", best["events"]
    return None



def _label(bt: dict | None, interval: str) -> str | None:
    daily = (bt or {}).get("daily") or {}
    if daily.get("interval") == interval and (daily.get("best") or {}).get("events"):
        return (daily.get("best") or {}).get("label") or "Trend EMA"
    return ((bt or {}).get("best") or {}).get("label")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        _ledger.update({k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("events"), dict)})


def reset_for_tests() -> None:
    global _loaded
    _ledger.clear()
    _loaded = True


def remember(chain: str, token: str, symbol: str | None, bt: dict | None, save: bool = True) -> bool:
    """Add the trades the Top row shows now to the ledger. Old trades stay:
    a later backtest only adds. True if something new came in."""
    got = token_trades(bt)
    if not got:
        return False
    _load()
    interval, events = got
    row = _ledger.setdefault(f"{chain}:{token}", {"chain": chain, "token": token, "events": {}})
    row["symbol"] = symbol or row.get("symbol")
    row["interval"] = interval
    # Since 30/09 the arrows sit on the fill candle, one candle after the
    # signal (system._best_trades, "fills"): the same trade saved before
    # under the signal's time is replaced, not kept twice.
    fills = any(e.get("fill") for e in events) or _fills(bt, interval)
    step = STEP_S.get(interval, 0)
    label = _label(bt, interval)
    new = False
    for e in events:
        if e.get("side") not in ("buy", "sell", "rug") or not _ok(e.get("t")):
            continue
        k = f"{int(e['t'])}:{e['side']}"
        if k not in row["events"]:
            if fills and step:
                old = row["events"].get(f"{int(e['t']) - step}:{e['side']}")
                if old and old.get("iv") == interval and not old.get("fill"):
                    del row["events"][f"{int(e['t']) - step}:{e['side']}"]
            row["events"][k] = {**{f: e[f] for f in ("t", "side", "price", "ret") if f in e}, "iv": interval,
                                "lab": label,
                                **({"fill": True} if fills else {})}
            new = True
    if new and save:
        _save()
    return new


STEP_S = {"1d": 86400, "12h": 43200, "8h": 28800, "6h": 21600, "4h": 14400, "2h": 7200, "1h": 3600,
          "30m": 1800, "15m": 900, "5m": 300}


def _fills(bt: dict | None, interval: str) -> bool:
    daily = (bt or {}).get("daily") or {}
    best = (daily.get("best") if daily.get("interval") == interval and (daily.get("best") or {}).get("events")
            else (bt or {}).get("best")) or {}
    return best.get("fills") == "next_open"


def _save() -> None:
    try:
        write_text_atomic(_FILE, json.dumps(_ledger, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def ledger_tokens() -> list[dict]:
    _load()
    return [{"chain": r["chain"], "token": r["token"], "symbol": r.get("symbol"), "interval": r.get("interval"),
             "events": _one_timeframe(r)} for r in _ledger.values()]


def needs_relabel(chain: str, token: str) -> bool:
    """Old 5-minute trades saved without their strategy (before 30/09 evening),
    or marked only "not trend" before RSI+BB was looked for."""
    _load()
    row = _ledger.get(f"{chain}:{token}")
    return bool(row) and any(e.get("iv") == "5m" and e.get("lab", "not trend") == "not trend"
                             for e in row["events"].values())


def relabel(chain: str, token: str, times: dict[str, set[tuple[int, str]]], window_from: int) -> int:
    """Give an old unlabelled 5-minute trade the strategy that, rerun on the
    same candles, made that very trade (Lorenzo, 30/09: every trade shows
    which strategy took it). Inside the candles and made by none of them:
    "not trend". Older than the candles: left unknown."""
    _load()
    row = _ledger.get(f"{chain}:{token}")
    if not row:
        return 0
    done = 0
    for e in row["events"].values():
        if e.get("iv") != "5m" or e.get("lab", "not trend") != "not trend":
            continue
        t = int(e["t"])
        lab = next((lab for lab, got in times.items()
                    if any((t + d, e["side"]) in got for d in (0, -300, 300))), None)
        if lab:
            e["lab"] = lab
            done += 1
        elif t >= window_from and "lab" not in e:
            e["lab"] = "not trend"
            done += 1
    if done:
        _save()
    return done


def _trend(e: dict) -> bool:
    """Every saved trade counts: trend followers and RSI+BB alike (Lorenzo,
    30/09). Each one says which strategy took it (`strategy_of`)."""
    return True


def strategy_of(e: dict) -> str:
    """The strategy that took the trade: its label ("Trend EMA", "Trend HF",
    "RSI+BB"); "RSI+BB" too for an old 5-minute trade no trend follower made
    (the 5-minute best was one of those); "unknown" when saved before
    labels were kept and never proven."""
    lab = e.get("lab")
    if e.get("iv") != "5m":
        return lab if lab in SPOT_LABELS else "Trend EMA"
    if lab in SPOT_LABELS or lab in TREND_LABELS:
        return lab
    if lab == "not trend":
        return "RSI+BB"
    return lab or "unknown"


def _one_timeframe(r: dict) -> list[dict]:
    """A token's trades on its current timeframe, and the older ones of an
    earlier timeframe only before those start. The timeframe climbs as the
    chart grows (strategy_lab.LADDER): two timeframes' trades interleaved
    would sell one's buy on the other's exit."""
    ev = sorted((e for e in r["events"].values() if _trend(e)), key=lambda e: e["t"])
    cur = [e for e in ev if e.get("iv") == r.get("interval")]
    if not cur:
        return ev
    first = cur[0]["t"]
    return [e for e in ev if e.get("iv") != r.get("interval") and e["t"] < first] + cur


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def replay(tokens: list[dict], capital: float = DEFAULT_CAPITAL, share: float = DEFAULT_SHARE,
           marks: dict[str, float] | None = None, now: float | None = None) -> dict:
    """tokens: [{chain, token, symbol, interval, events}]; marks: {"chain:token": price}."""
    marks = marks or {}
    flow = []
    for tk in tokens:
        for e in tk.get("events") or []:
            if e.get("side") in ("buy", "sell", "rug") and _ok(e.get("t")):
                # Sells before buys on the same second: the cash is free again.
                flow.append((e["t"], 0 if e["side"] == "buy" else -1, tk, e))
    flow.sort(key=lambda x: (x[0], x[1]))
    cash = float(capital)
    held: dict[str, dict] = {}
    closed: list[dict] = []
    curve: list[list[float]] = []
    skipped = 0
    peak, max_dd = float(capital), 0.0

    def equity() -> float:
        return cash + sum(p["stake"] for p in held.values())

    for t, _o, tk, e in flow:
        key = f"{tk['chain']}:{tk['token']}"
        p = held.get(key)
        if e["side"] == "buy":
            if p:
                continue
            stake = min(cash, equity() * share)
            if stake < MIN_STAKE:
                skipped += 1
                continue
            cash -= stake
            held[key] = {"chain": tk["chain"], "token": tk["token"], "symbol": tk.get("symbol"),
                         "interval": e.get("iv") or tk.get("interval"), "strategy": strategy_of(e),
                         "at": int(t), "entry_price": e.get("price"),
                         "stake": round(stake, 2)}
        elif p:
            ret = -1.0 if e["side"] == "rug" else e.get("ret")
            if not _ok(ret):
                ep, xp = p.get("entry_price"), e.get("price")
                ret = xp / ep - 1 if _ok(ep) and _ok(xp) and ep > 0 else 0.0
            back = p["stake"] * (1 + ret)
            cash += back
            del held[key]
            closed.append({**p, "exit_at": int(t), "exit_price": e.get("price"), "ret": round(ret, 4),
                           "pnl": round(back - p["stake"], 2)})
        else:
            continue
        eq = equity()
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
        curve.append([int(t), round(eq, 2)])

    open_rows = []
    for key, p in held.items():
        px, ep = marks.get(key), p.get("entry_price")
        ret = px / ep - 1 if _ok(px) and _ok(ep) and ep > 0 and px > 0 else None
        value = p["stake"] * (1 + (ret or 0.0))
        open_rows.append({**p, "price": px if ret is not None else None,
                          "ret": round(ret, 4) if ret is not None else None,
                          "value": round(value, 2), "pnl": round(value - p["stake"], 2)})
    final = cash + sum(r["value"] for r in open_rows)
    if flow:
        t_end = int(now) if now else curve[-1][0] if curve else int(flow[-1][0])
        curve.append([max(t_end, curve[-1][0] if curve else t_end), round(final, 2)])
        peak = max(peak, final)
        max_dd = max(max_dd, (peak - final) / peak if peak > 0 else 0.0)
    wins = sum(1 for c in closed if c["ret"] > 0)
    closed.sort(key=lambda c: c["exit_at"], reverse=True)
    open_rows.sort(key=lambda r: r["at"], reverse=True)
    return {"capital": round(float(capital), 2), "share": share, "equity": round(final, 2),
            "cash": round(cash, 2), "ret": round(final / capital - 1, 4) if capital else None,
            "max_dd": round(max_dd, 4), "trades": len(closed), "wins": wins,
            "win_rate": round(wins / len(closed), 4) if closed else None, "skipped": skipped,
            "tokens": len(tokens), "from": int(flow[0][0]) if flow else None,
            "open": open_rows, "closed": closed, "curve": curve}
