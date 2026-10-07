"""Strategies backtests first.

Lorenzo, 28/09: "la parte di backtest abbia la priorita' massima [...]
popolare quella schermata e poi fare tutte le altre attivita'". While any
meme alert is still waiting for its backtests, the other background jobs
(the Top's hourly backtests, the lab, the board review) wait.

Never forever: if the pending count has not moved for STUCK_S (GeckoTerminal
refusing one pool, say), the other jobs run again until it changes.
"""
from __future__ import annotations

import time

STUCK_S = 20 * 60
_seen = {"pending": -1, "since": 0.0}


def reset_for_tests() -> None:
    _seen.update(pending=-1, since=0.0)


def backtests_first(now: float | None = None) -> dict | None:
    """The meme backtest status while something is still pending, else None."""
    from app.config import settings
    if not settings.onchain_trading_enabled:
        return None      # no meme backtests run with trading off (07/10): nothing to wait for
    from app.trading.onchain import alert_track
    now = now or time.time()
    st = alert_track.backtest_status("meme", now=now)
    pending = st.get("pending", 0)
    if pending != _seen["pending"]:
        _seen.update(pending=pending, since=now)
    if pending <= 0 or now - _seen["since"] > STUCK_S:
        return None
    return st
