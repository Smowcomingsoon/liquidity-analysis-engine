"""
Automatic Fibonacci retracement + extension engine.

Reuses the same swing-detection primitive already powering the app's SMC
levels (smc.swing_highs_lows / smc_engine.SWING_LENGTH_BY_TIMEFRAME) so a
"swing" means the same thing here as it does for BOS/CHoCH/order blocks —
prototyped and eyeballed against real BTC/ETH data before being wired in.

Only the single most recent leg is used — same philosophy as
compute_dealing_range's "most recent swing low -> most recent swing high":
older legs are no longer actionable and just clutter the chart. Swing
detection runs on closed candles only (see drop_unclosed_candle) so a zone
never anchors on the still-forming last bar.
"""

import pandas as pd
from smartmoneyconcepts import smc

from app.analysis.smc_engine import SWING_LENGTH_BY_TIMEFRAME
from app.models import FibZone
from app.timeframes import drop_unclosed_candle

RETRACEMENT_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]
EXTENSION_RATIOS = [1.272, 1.414, 1.618, 2.0, 2.618]

# A leg smaller than this (as a fraction of its own lower end) is noise, not
# a tradeable impulse — skip it rather than draw a fib on a wiggle.
MIN_LEG_PCT = 0.008  # 0.8%


def _get_swing_pivots(df: pd.DataFrame, timeframe_key: str) -> list[dict]:
    swing_length = SWING_LENGTH_BY_TIMEFRAME.get(timeframe_key, 8)
    swings = smc.swing_highs_lows(df, swing_length=swing_length)
    valid = swings.dropna(subset=["HighLow"])
    return [
        {"pos": pos, "HighLow": row["HighLow"], "Level": float(row["Level"]), "ts": int(df.index[int(pos)].timestamp())}
        for pos, row in valid.iterrows()
    ]


def _leg_pct(a: float, b: float) -> float:
    return abs(b - a) / min(a, b)


def compute_fib_zones(df: pd.DataFrame, timeframe_key: str, interval: str) -> list[FibZone]:
    # A swing pivot anchored on the still-forming last candle isn't really
    # "structure" yet — on a weekly/monthly bar especially, that candle's
    # high/low can keep moving for days, so the fib zone would shift under
    # the user candle by candle instead of describing a confirmed turn.
    df = drop_unclosed_candle(df, interval)
    pivots = _get_swing_pivots(df, timeframe_key)
    if len(pivots) < 2:
        return []

    # Most recent completed leg: second-to-last pivot -> last pivot.
    a, b = pivots[-2], pivots[-1]
    if _leg_pct(a["Level"], b["Level"]) < MIN_LEG_PCT:
        return []

    bullish = b["HighLow"] == 1  # ends on a high => leg went up
    leg_size = b["Level"] - a["Level"]
    direction = "bullish" if bullish else "bearish"

    retracement_levels = {str(r): a["Level"] + leg_size * r for r in RETRACEMENT_RATIOS}
    # The extension continues *this same* leg past its 100% point (a
    # standard 2-point "trend-based" projection) rather than anchoring on
    # an earlier, opposite-direction leg — an earlier version of this did
    # that (a proper 3-point A-B-C extension using the leg before this
    # one), and it produced a downside projection sitting right next to an
    # upside retracement, both anchored at the same swing point: two
    # contradictory reads on the same turn, not a continuation of the move
    # the retracement was already describing.
    extension_levels = {str(r): a["Level"] + leg_size * r for r in EXTENSION_RATIOS}

    return [
        FibZone(
            kind="retracement",
            direction=direction,
            anchor_time=a["ts"],
            levels=retracement_levels,
            label=f"Fib retracement {'bullish' if bullish else 'bearish'}",
        ),
        FibZone(
            kind="extension",
            direction=direction,
            anchor_time=b["ts"],
            levels=extension_levels,
            label=f"Fib extension {'bullish' if bullish else 'bearish'}",
        ),
    ]
