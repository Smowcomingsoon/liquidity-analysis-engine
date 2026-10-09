import pandas as pd
import pandas_ta  # noqa: F401  (registers the .ta accessor on DataFrame)

from app.models import IndicatorSeries


def _series_to_values(s: pd.Series) -> list[float | None]:
    return [None if pd.isna(v) else float(v) for v in s]


def _first_col(df: pd.DataFrame | None, prefix: str) -> pd.Series | None:
    if df is None:
        return None
    col = next((c for c in df.columns if c.startswith(prefix)), None)
    return df[col] if col is not None else None


def _as_series(x: object, n: int) -> pd.Series | None:
    # When a requested indicator's length exceeds the available history (e.g.
    # a 50-period EMA on a symbol with only 48 candles at this timeframe),
    # pandas_ta silently returns the *input* DataFrame back unchanged instead
    # of a NaN-filled Series. Iterating that as if it were a series of prices
    # yields its column names ('open', 'high', ...) — not floats — and blows
    # up downstream. Anything that isn't a proper same-length Series is
    # simply "not enough data for this indicator" and gets dropped.
    return x if isinstance(x, pd.Series) and len(x) == n else None


def _as_dataframe(x: object, n: int) -> pd.DataFrame | None:
    return x if isinstance(x, pd.DataFrame) and len(x) == n else None


def compute_indicators(df: pd.DataFrame) -> list[IndicatorSeries]:
    out: list[IndicatorSeries] = []
    n = len(df)

    ema20 = _as_series(df.ta.ema(length=20), n)
    ema50 = _as_series(df.ta.ema(length=50), n)
    rsi14 = _as_series(df.ta.rsi(length=14), n)
    macd = _as_dataframe(df.ta.macd(), n)
    bbands = _as_dataframe(df.ta.bbands(length=20), n)
    vwap = _as_series(df.ta.vwap(), n)
    stoch = _as_dataframe(df.ta.stoch(k=14, d=3, smooth_k=3), n)

    if ema20 is not None:
        out.append(IndicatorSeries(name="EMA20", values=_series_to_values(ema20)))
    if ema50 is not None:
        out.append(IndicatorSeries(name="EMA50", values=_series_to_values(ema50)))
    if rsi14 is not None:
        out.append(IndicatorSeries(name="RSI14", values=_series_to_values(rsi14)))

    macd_line = _first_col(macd, "MACD_")
    macd_signal = _first_col(macd, "MACDs_")
    macd_hist = _first_col(macd, "MACDh_")
    if macd_line is not None:
        out.append(IndicatorSeries(name="MACD", values=_series_to_values(macd_line)))
    if macd_signal is not None:
        out.append(IndicatorSeries(name="MACD_signal", values=_series_to_values(macd_signal)))
    if macd_hist is not None:
        out.append(IndicatorSeries(name="MACD_hist", values=_series_to_values(macd_hist)))

    bb_lower = _first_col(bbands, "BBL_")
    bb_mid = _first_col(bbands, "BBM_")
    bb_upper = _first_col(bbands, "BBU_")
    if bb_lower is not None:
        out.append(IndicatorSeries(name="BB_lower", values=_series_to_values(bb_lower)))
    if bb_mid is not None:
        out.append(IndicatorSeries(name="BB_mid", values=_series_to_values(bb_mid)))
    if bb_upper is not None:
        out.append(IndicatorSeries(name="BB_upper", values=_series_to_values(bb_upper)))

    if vwap is not None:
        out.append(IndicatorSeries(name="VWAP", values=_series_to_values(vwap)))

    stoch_k = _first_col(stoch, "STOCHk_")
    stoch_d = _first_col(stoch, "STOCHd_")
    if stoch_k is not None:
        out.append(IndicatorSeries(name="STOCH_K", values=_series_to_values(stoch_k)))
    if stoch_d is not None:
        out.append(IndicatorSeries(name="STOCH_D", values=_series_to_values(stoch_d)))

    return out


def bull_market_support_band(df: pd.DataFrame, weekly: pd.DataFrame) -> list[IndicatorSeries]:
    """Bull Market Support Band (segnalazione 893ea67a): SMA 20 and EMA 21 of
    the weekly close, drawn on any chart timeframe. Each chart candle takes the
    value of the weekly bar it falls in, as TradingView does live; candles
    before the 21st week get nothing."""
    if weekly is None or weekly.empty or df.empty:
        return []
    close = weekly["close"].astype(float)
    bands = {"BMSB_SMA20W": close.rolling(20).mean(), "BMSB_EMA21W": close.ewm(span=21, adjust=False).mean()}
    bands["BMSB_EMA21W"].iloc[: min(20, len(close))] = float("nan")   # same warm-up as the SMA
    out = []
    for name, s in bands.items():
        mapped = s.reindex(df.index, method="ffill") if s.index.is_monotonic_increasing else None
        if mapped is None or mapped.isna().all():
            continue
        out.append(IndicatorSeries(name=name, values=_series_to_values(mapped)))
    return out
