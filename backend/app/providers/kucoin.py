import time

import httpx
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


def format_symbol(symbol: str) -> str:
    """KuCoin is scoped exclusively to .../BTC pairs — whatever quote the
    user typed (or none) is stripped and replaced with BTC."""
    symbol = symbol.strip().upper().replace("/", "-").replace("_", "-")
    if symbol.endswith("-BTC"):
        return symbol
    base = symbol[:-3] if symbol.endswith("BTC") and len(symbol) > 3 else symbol
    base = base.rstrip("-")
    return f"{base}-BTC"


class KucoinProvider(DataProvider):
    """Crypto only, restricted to BTC-quoted pairs — a third crypto source
    alongside MEXC/Binance specifically for coins traders track priced in
    BTC rather than USDT, which the other two default to."""

    # KuCoin's candles endpoint caps a single request at 1500 rows regardless
    # of how wide a startAt/endAt window is requested — paged the same way as
    # MexcProvider (see its docstring) for anything deeper.
    _PAGE_LIMIT = 1500
    _MAX_PAGES = 20

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or BASE_URL

    async def fetch_ohlcv(self, symbol: str, interval: str, outputsize: int, since_ms: int | None = None) -> pd.DataFrame:
        formatted_symbol = format_symbol(symbol)
        candle_type = _CANONICAL_TO_TYPE.get(interval)
        if candle_type is None:
            raise ProviderError(f"Timeframe {interval} not supported on KuCoin")
        interval_seconds = _INTERVAL_SECONDS[interval]

        rows: list[list] = []
        end_at = int(time.time())
        remaining = outputsize
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
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
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self._base_url}/api/v1/symbols")
            payload = resp.json()
            all_symbols = payload.get("data", []) if isinstance(payload, dict) else []
            # BTC-quoted pairs only — the point of a third crypto provider
            # is coins tracked in BTC terms, not another USDT source.
            symbols = [s for s in all_symbols if s.get("quoteCurrency") == "BTC"]
            _symbols_cache["all"] = symbols

        q = query.upper().replace("/", "-")
        matches = [s for s in symbols if s.get("baseCurrency", "").startswith(q) and s.get("enableTrading")]
        matches.sort(key=lambda s: len(s.get("symbol", "")))

        out = []
        for s in matches[:_MAX_SUGGESTIONS]:
            out.append({"symbol": s.get("symbol", ""), "name": s.get("baseCurrency", ""), "exchange": "KuCoin (BTC)"})
        return out
