import time

import httpx

from app import http_client
import pandas as pd
from cachetools import TTLCache

from app.providers.base import DataProvider, ProviderError, ProviderUnreachable

BASE_URL = "https://api.kucoin.com"
_CANONICAL_TO_TYPE = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1hour",
    "4h": "4hour",
    "1d": "1day",
    "1w": "1week",
}
_INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}
_MAX_SUGGESTIONS = 25

_symbols_cache: TTLCache = TTLCache(maxsize=1, ttl=3600)

# Every quote KuCoin lists, the commonest first: "ETHUSDT" is read as
# ETH-USDT, "ETHBTC" as ETH-BTC. Longest first among those sharing a tail
# (USDT before a hypothetical "DT").
_QUOTES = ("USDT", "USDC", "BTC", "ETH", "KCS", "TUSD", "DAI", "EUR", "TRX", "USDD", "BRL", "TRY")
# Search results: USDT pairs first, then BTC, then the rest.
_QUOTE_ORDER = {q: i for i, q in enumerate(("USDT", "BTC", "USDC", "ETH", "KCS"))}


def format_symbol(symbol: str) -> str:
    """KuCoin's BASE-QUOTE form, for any quote it lists.

    15/10 (Lorenzo: "nei pair a disposizione sul sito, quelli relativi a
    KuCoin non compaiono"): KuCoin used to be scoped to .../BTC pairs only —
    any quote was replaced with BTC, so ETHUSDT became "ETHUSDT-BTC", which
    does not exist, and the search listed BTC pairs alone. A bare base
    ("ETH") still means the BTC pair, as before, so symbols already saved
    (drawings, alerts) keep their chart."""
    symbol = symbol.strip().upper().replace("/", "-").replace("_", "-")
    if "-" in symbol:
        base, _, quote = symbol.partition("-")
        return f"{base}-{quote}" if quote else f"{base}-BTC"
    for quote in sorted(_QUOTES, key=len, reverse=True):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}-{quote}"
    return f"{symbol}-BTC"


class KucoinProvider(DataProvider):
    """Crypto only, every spot pair KuCoin lists (USDT, BTC, USDC, ETH…).
    Until 15/10 it was restricted to BTC-quoted pairs."""

    # KuCoin's candles endpoint caps a single request at 1500 rows regardless
    # of how wide a startAt/endAt window is requested — paged the same way as
    # MexcProvider (see its docstring) for anything deeper.
    _PAGE_LIMIT = 1500
    _MAX_PAGES = 20

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or BASE_URL

    async def fetch_ohlcv(self, symbol: str, interval: str, outputsize: int, since_ms: int | None = None) -> pd.DataFrame:
        # KuCoin has no monthly candles (its widest is 1week). 15/10, Lorenzo:
        # "quando lo seleziono mi dice time frame 1 month not supported on
        # KuCoin. Pero' era selezionato 1d": the analysis also loads the
        # higher timeframe (1M by default), and that request failed the page.
        # Calendar months folded from the daily candles.
        if interval == "1M":
            days = min(max(outputsize, 1) * 31, self._PAGE_LIMIT * self._MAX_PAGES)
            daily = await self.fetch_ohlcv(symbol, "1d", days)
            month = daily.resample("MS").agg({"open": "first", "high": "max", "low": "min",
                                              "close": "last", "volume": "sum"}).dropna(subset=["close"])
            return month.tail(outputsize)
        formatted_symbol = format_symbol(symbol)
        candle_type = _CANONICAL_TO_TYPE.get(interval)
        if candle_type is None:
            raise ProviderError(f"Timeframe {interval} not supported on KuCoin")
        interval_seconds = _INTERVAL_SECONDS[interval]

        rows: list[list] = []
        end_at = int(time.time())
        remaining = outputsize
        try:
            async with http_client.pooled(timeout=15.0) as client:
                for _ in range(self._MAX_PAGES):
                    if remaining <= 0:
                        break
                    page_count = min(remaining, self._PAGE_LIMIT)
                    start_at = end_at - interval_seconds * page_count
                    params = {"type": candle_type, "symbol": formatted_symbol, "startAt": start_at, "endAt": end_at}

                    resp = await client.get(f"{self._base_url}/api/v1/market/candles", params=params)
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise ProviderError(f"Invalid response from KuCoin for {formatted_symbol}") from exc

                    if payload.get("code") != "200000":
                        raise ProviderError(payload.get("msg") or f"KuCoin error for {formatted_symbol}")

                    page_rows = payload.get("data") or []
                    if not page_rows:
                        break

                    rows.extend(page_rows)
                    remaining -= len(page_rows)
                    if len(page_rows) < page_count:
                        break  # short page — reached the symbol's listing date
                    end_at = min(int(r[0]) for r in page_rows) - 1
        except httpx.HTTPError as exc:
            if not rows:
                raise ProviderUnreachable(f"KuCoin unreachable for {formatted_symbol}: {exc}") from exc

        if not rows:
            raise ProviderError(f"No data available for {formatted_symbol} ({interval})")

        # [time_s, open, close, high, low, volume, turnover] — newest first
        df = pd.DataFrame(rows, columns=["time", "open", "close", "high", "low", "volume", "turnover"])
        df["datetime"] = pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        df = df.set_index("datetime").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        return df[["open", "high", "low", "close", "volume"]].tail(outputsize)

    async def search_symbols(self, query: str) -> list[dict]:
        query = query.strip()
        if not query:
            return []

        symbols = _symbols_cache.get("all")
        if symbols is None:
            async with http_client.pooled(timeout=15.0) as client:
                resp = await client.get(f"{self._base_url}/api/v1/symbols")
            payload = resp.json()
            all_symbols = payload.get("data", []) if isinstance(payload, dict) else []
            # every quote (15/10): it used to keep the BTC pairs only
            symbols = [s for s in all_symbols if s.get("enableTrading")]
            if symbols:
                _symbols_cache["all"] = symbols

        q = query.upper().replace("/", "").replace("-", "").replace("_", "")
        matches = [s for s in symbols
                   if s.get("baseCurrency", "").upper().startswith(q)
                   or s.get("symbol", "").upper().replace("-", "").startswith(q)]
        matches.sort(key=lambda s: (s.get("baseCurrency", "").upper() != q,
                                    _QUOTE_ORDER.get(s.get("quoteCurrency", ""), 99),
                                    len(s.get("symbol", ""))))

        out = []
        for s in matches[:_MAX_SUGGESTIONS]:
            out.append({"symbol": s.get("symbol", ""), "name": s.get("baseCurrency", ""),
                        "exchange": f"KuCoin ({s.get('quoteCurrency', '')})"})
        return out
