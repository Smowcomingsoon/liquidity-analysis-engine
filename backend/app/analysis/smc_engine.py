import pandas as pd
from smartmoneyconcepts import smc

from app.models import LiquidityLevel

# Swing lookback (candles each side) tuned per timeframe: higher timeframes need
# fewer candles to define a meaningful swing, lower timeframes are noisier.
SWING_LENGTH_BY_TIMEFRAME = {"htf": 5, "mtf": 8, "ltf": 12}
LIQUIDITY_RANGE_PERCENT = 0.01

# Without a cap, every BOS/CHoCH/OB/FVG/EQH/EQL ever detected across the whole
# fetched history (hundreds of candles) becomes a permanent chart label —
# unreadable, especially on narrow mobile screens. Keep only the most recent
# few per type; older structure is no longer actionable anyway.
_LEVEL_LIMIT_PER_TYPE = {"bos": 6, "choch": 6, "order_block": 5, "fvg": 6, "eqh": 5, "eql": 5}


def _cap_recent_per_type(levels: list[LiquidityLevel]) -> list[LiquidityLevel]:
    grouped: dict[str, list[LiquidityLevel]] = {}
    for lvl in levels:
        grouped.setdefault(lvl.type, []).append(lvl)
    out: list[LiquidityLevel] = []
    for level_type, group in grouped.items():
        group.sort(key=lambda l: l.start_time, reverse=True)  # noqa: E741
        out.extend(group[: _LEVEL_LIMIT_PER_TYPE.get(level_type, 6)])
    return out


def _ts(df: pd.DataFrame, pos) -> int:
    return int(df.index[int(pos)].timestamp())


def _ts_or_none(df: pd.DataFrame, pos) -> int | None:
    # These SMC functions use 0 as a sentinel meaning "not yet resolved", not a real index.
    if pos is None or pd.isna(pos) or int(pos) <= 0:
        return None
    return _ts(df, pos)


def compute_swings(df: pd.DataFrame, timeframe_key: str) -> pd.DataFrame:
    """The one swing-high/low pass every other function here (and the fib
    engine) builds on. Called once per timeframe in main.py and threaded
    through — swing_highs_lows is a non-trivial rolling-window pass over the
    full candle history, and computing it separately per consumer (levels,
    dealing range, fib zones) tripled the work for identical output."""
    swing_length = SWING_LENGTH_BY_TIMEFRAME.get(timeframe_key, 8)
    return smc.swing_highs_lows(df, swing_length=swing_length)


def compute_dealing_range(df: pd.DataFrame, swings: pd.DataFrame) -> tuple[float, float] | None:
    """Current structural range (most recent swing low -> most recent swing high),
    used to place price in a premium/discount/equilibrium context."""
    valid = swings.dropna(subset=["HighLow"])
    highs = valid[valid["HighLow"] == 1]
    lows = valid[valid["HighLow"] == -1]
    if highs.empty or lows.empty:
        return None
    last_high = float(highs.iloc[-1]["Level"])
    last_low = float(lows.iloc[-1]["Level"])
    if last_high <= last_low:
        return None
    return last_low, last_high


def compute_levels(df: pd.DataFrame, swings: pd.DataFrame) -> list[LiquidityLevel]:
    levels: list[LiquidityLevel] = []
    levels.extend(_bos_choch_levels(df, swings))
    levels.extend(_order_block_levels(df, swings))
    levels.extend(_fvg_levels(df))
    levels.extend(_liquidity_levels(df, swings))
    return _cap_recent_per_type(levels)


def _bos_choch_levels(df: pd.DataFrame, swings: pd.DataFrame) -> list[LiquidityLevel]:
    bos = smc.bos_choch(df, swings, close_break=True)
    out = []
    for pos, row in bos.dropna(subset=["BOS", "CHOCH"], how="all").iterrows():
        is_bos = pd.notna(row["BOS"])
        value = row["BOS"] if is_bos else row["CHOCH"]
        direction = "bullish" if value > 0 else "bearish"
        kind = "bos" if is_bos else "choch"
        out.append(
            LiquidityLevel(
                type=kind,
                direction=direction,
                price_high=float(row["Level"]),
                price_low=float(row["Level"]),
                start_time=_ts(df, pos),
                end_time=_ts_or_none(df, row.get("BrokenIndex")),
                mitigated=True,
                label=f"{'BOS' if is_bos else 'CHoCH'} {'rialzista' if direction == 'bullish' else 'ribassista'}",
            )
        )
    return out


def _order_block_levels(df: pd.DataFrame, swings: pd.DataFrame) -> list[LiquidityLevel]:
    ob = smc.ob(df, swings, close_mitigation=False)
    out = []
    for pos, row in ob.dropna(subset=["OB"]).iterrows():
        direction = "bullish" if row["OB"] > 0 else "bearish"
        mitigated_ts = _ts_or_none(df, row.get("MitigatedIndex"))
        out.append(
            LiquidityLevel(
                type="order_block",
                direction=direction,
                price_high=float(row["Top"]),
                price_low=float(row["Bottom"]),
                start_time=_ts(df, pos),
                end_time=mitigated_ts,
                mitigated=mitigated_ts is not None,
                label=f"Order Block {'rialzista' if direction == 'bullish' else 'ribassista'} ({row['Percentage']:.0f}% forza)",
            )
        )
    return out


def _fvg_levels(df: pd.DataFrame) -> list[LiquidityLevel]:
    fvg = smc.fvg(df, join_consecutive=True)
    out = []
    for pos, row in fvg.dropna(subset=["FVG"]).iterrows():
        direction = "bullish" if row["FVG"] > 0 else "bearish"
        mitigated_ts = _ts_or_none(df, row.get("MitigatedIndex"))
        out.append(
            LiquidityLevel(
                type="fvg",
                direction=direction,
                price_high=float(row["Top"]),
                price_low=float(row["Bottom"]),
                start_time=_ts(df, pos),
                end_time=mitigated_ts,
                mitigated=mitigated_ts is not None,
                label=f"Fair Value Gap {'rialzista' if direction == 'bullish' else 'ribassista'}",
            )
        )
    return out


def _liquidity_levels(df: pd.DataFrame, swings: pd.DataFrame) -> list[LiquidityLevel]:
    liq = smc.liquidity(df, swings, range_percent=LIQUIDITY_RANGE_PERCENT)
    out = []
    for pos, row in liq.dropna(subset=["Liquidity"]).iterrows():
        is_high_pool = row["Liquidity"] > 0
        level_type = "eqh" if is_high_pool else "eql"
        direction = "bullish" if is_high_pool else "bearish"
        swept_ts = _ts_or_none(df, row.get("Swept"))
        out.append(
            LiquidityLevel(
                type=level_type,
                direction=direction,
                price_high=float(row["Level"]),
                price_low=float(row["Level"]),
                start_time=_ts(df, pos),
                end_time=swept_ts,
                mitigated=swept_ts is not None,
                label=("Equal Highs (buy-side liquidity)" if is_high_pool else "Equal Lows (sell-side liquidity)"),
            )
        )
    return out
