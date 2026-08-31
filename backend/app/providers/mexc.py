import httpx
import pandas as pd
from cachetools import TTLCache

from app.providers.base import DataProvider, ProviderError
from app.timeframes import INTERVAL_SECONDS

BASE_URL = "https://api.mexc.com"
_CANONICAL_TO_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "4h": "4h",
    "1d": "1d",
    "1w": "1W",
    "1M": "1M",
}
# MEXC's spot market has no bare-USD-quoted pairs (verified against /api/v3/exchangeInfo) —
# only USDT/USDC/etc. A trailing "USD" is normalized away below, not treated as already-quoted.
_QUOTE_ASSETS = ["USDT", "USDC", "FDUSD", "TUSD", "DAI", "BTC", "ETH"]
_MAX_SUGGESTIONS = 25

_symbols_cache: TTLCache = TTLCache(maxsize=1, ttl=3600)


def format_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper().replace("/", "").replace("-", "")
    for quote in _QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol
    if symbol.endswith("USD") and len(symbol) > 3:
        symbol = symbol[:-3]
    return f"{symbol}USDT"


class MexcProvider(DataProvider):
    """Crypto only — public market data, no API key required."""

    # MEXC's klines endpoint caps a single request at 500 rows. A caller
    # asking for more than that (the backtest fetch does — it wants the
    # deepest history the exchange actually has) gets it via pagination:
    # walk endTime backwards one page at a time until either outputsize is
    # satisfied or a page comes back short/empty, which means the listing
    # date has been reached and there's nothing earlier to fetch.
    _PAGE_LIMIT = 500
    _MAX_PAGES = 20

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or BASE_URL

    async def fetch_ohlcv(self, symbol: str, interval: str, outputsize: int) -> pd.DataFrame:
        formatted_symbol = format_symbol(symbol)
        api_interval = _CANONICAL_TO_INTERVAL[interval]
        # MEXC silently ignores a lone endTime (no startTime) and just
        # returns the latest `limit` candles again — verified empirically,
        # not documented. A startTime windowing the page is required for
        # endTime to actually take effect, so every paginated request after
        # the first carries both.
        interval_ms = INTERVAL_SECONDS[interval] * 1000

        rows: list[list] = []
        end_time: int | None = None
        remaining = outputsize
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for _ in range(self._MAX_PAGES):
                    if remaining <= 0:
                        break
                    limit = min(remaining, self._PAGE_LIMIT)
                    params = {"symbol": formatted_symbol, "interval": api_interval, "limit": limit}
                    if end_time is not None:
                        params["endTime"] = end_time
                        params["startTime"] = end_time - limit * interval_ms

                    resp = await client.get(f"{self._base_url}/api/v3/klines", params=params)
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise ProviderError(f"Risposta non valida da MEXC per {formatted_symbol}") from exc

                    if isinstance(payload, dict) and "code" in payload:
                        raise ProviderError(payload.get("msg", f"Errore MEXC per {formatted_symbol}"))
                    if not payload:
                        break

                    rows.extend(payload)
                    remaining -= len(payload)
                    if len(payload) < limit:
                        break  # short page — reached the symbol's listing date
                    end_time = min(int(row[0]) for row in payload) - 1
        except httpx.HTTPError as exc:
            # A page beyond the first failing (network blip, proxy hiccup)
            # doesn't have to sink the whole fetch — whatever was already
            # collected is still usable history, just less of it.
            if not rows:
                raise ProviderError(f"MEXC non raggiungibile per {formatted_symbol}: {exc}") from exc

        if not rows:
            raise ProviderError(f"Nessun dato disponibile per {formatted_symbol} ({api_interval})")

        # [openTime_ms, open, high, low, close, volume, closeTime_ms, quoteVolume]
        df = pd.DataFrame(
            rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"]
        )
        df["datetime"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
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
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self._base_url}/api/v3/exchangeInfo")
            payload = resp.json()
            symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
            _symbols_cache["all"] = symbols

        q = query.upper().replace("/", "")
        all_spot = [s for s in symbols if s.get("isSpotTradingAllowed")]
        starts = [s for s in all_spot if s.get("symbol", "").startswith(q)]
        contains = [s for s in all_spot if q in s.get("symbol", "") and not s.get("symbol", "").startswith(q)]
        matches = starts + contains
        matches.sort(key=lambda s: (not s.get("symbol", "").startswith(q), s.get("quoteAsset") != "USDT", len(s.get("symbol", ""))))

        out = []
        for s in matches[:_MAX_SUGGESTIONS]:
            base, quote = s.get("baseAsset", ""), s.get("quoteAsset", "")
            out.append({"symbol": s.get("symbol", ""), "name": s.get("fullName", "") or base, "exchange": f"MEXC ({quote})"})
        return out
