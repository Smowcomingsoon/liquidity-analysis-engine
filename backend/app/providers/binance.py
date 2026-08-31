import httpx
import pandas as pd
from cachetools import TTLCache

from app.providers.base import DataProvider, ProviderError
from app.timeframes import INTERVAL_SECONDS

BASE_URL = "https://api.binance.com"
_CANONICAL_TO_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
    "1M": "1M",
}
_QUOTE_ASSETS = ["USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "BTC", "ETH"]
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


class BinanceProvider(DataProvider):
    """Crypto only — public market data, no API key required. Deeper, more
    reliable history than MEXC for majors; MEXC stays the pick for freshly
    listed memecoins Binance doesn't carry yet."""

    # Binance's klines endpoint caps a single request at 1000 rows — paged
    # the same way as MexcProvider (see its docstring) for anything deeper.
    _PAGE_LIMIT = 1000
    _MAX_PAGES = 20

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or BASE_URL

    async def fetch_ohlcv(self, symbol: str, interval: str, outputsize: int) -> pd.DataFrame:
        formatted_symbol = format_symbol(symbol)
        api_interval = _CANONICAL_TO_INTERVAL[interval]
        # A lone endTime (no startTime) was unreliable on MEXC's identically
        # shaped klines endpoint — always sending both here too rather than
        # trusting endTime alone to window the page.
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
                        raise ProviderError(f"Risposta non valida da Binance per {formatted_symbol}") from exc

                    if isinstance(payload, dict) and "code" in payload:
                        raise ProviderError(payload.get("msg", f"Errore Binance per {formatted_symbol}"))
                    if not payload:
                        break

                    rows.extend(payload)
                    remaining -= len(payload)
                    if len(payload) < limit:
                        break  # short page — reached the symbol's listing date
                    end_time = min(int(row[0]) for row in payload) - 1
        except httpx.HTTPError as exc:
            if not rows:
                raise ProviderError(f"Binance non raggiungibile per {formatted_symbol}: {exc}") from exc

        if not rows:
            raise ProviderError(f"Nessun dato disponibile per {formatted_symbol} ({api_interval})")

        # [openTime_ms, open, high, low, close, volume, closeTime_ms, quoteVolume, ...]
        df = pd.DataFrame(rows, columns=range(len(rows[0])))
        df = df.iloc[:, :6]
        df.columns = ["open_time", "open", "high", "low", "close", "volume"]
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
        all_spot = [s for s in symbols if s.get("isSpotTradingAllowed") and s.get("status") == "TRADING"]
        starts = [s for s in all_spot if s.get("symbol", "").startswith(q)]
        contains = [s for s in all_spot if q in s.get("symbol", "") and not s.get("symbol", "").startswith(q)]
        matches = starts + contains
        matches.sort(key=lambda s: (not s.get("symbol", "").startswith(q), s.get("quoteAsset") != "USDT", len(s.get("symbol", ""))))

        out = []
        for s in matches[:_MAX_SUGGESTIONS]:
            base, quote = s.get("baseAsset", ""), s.get("quoteAsset", "")
            out.append({"symbol": s.get("symbol", ""), "name": base, "exchange": f"Binance ({quote})"})
        return out
