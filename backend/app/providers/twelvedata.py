import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
from cachetools import TTLCache

from app.config import settings
from app.providers.base import (
    DataProvider,
    ProviderError,
    ProviderUnreachable,
    QuotaExceeded,
)

_PREFERRED_EXCHANGES = {"NASDAQ", "NYSE"}
_PREFERRED_INSTRUMENT_TYPES = {"Common Stock", "ETF"}
_MAX_SUGGESTIONS = 8
# I crediti Twelve Data si azzerano a mezzanotte UTC: e' il momento in cui vale
# la pena riprovare, non fra un minuto. Il codice 429 lo dice, ma il piano
# gratuito risponde spesso con 400 e il motivo solo nel testo, quindi si guarda
# anche quello.
_QUOTA_PHRASES = ("api credits", "run out of api credits", "daily limit", "out of credits")


def _is_quota_error(code, message: str) -> bool:
    if code in (429, "429"):
        return True
    text = (message or "").lower()
    return any(phrase in text for phrase in _QUOTA_PHRASES)


def _seconds_to_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


_CANONICAL_TO_INTERVAL = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day",
    "1w": "1week",
    "1M": "1month",
}

_stock_search_cache: TTLCache = TTLCache(maxsize=256, ttl=300)

# Twelve Data's free tier allows 8 requests/minute *total* — shared across
# every browser tab (own + friends', once the app is shared via a tunnel) and
# the watchlist background loop. Rather than let bursts hit that cap and come
# back as ugly 429-style errors, queue our own outgoing requests to stay a
# little under it; callers just see a brief delay instead of a failure.
_RATE_LIMIT_PER_MINUTE = 7
_RATE_WINDOW_SECONDS = 60.0
_request_times: list[float] = []
_rate_lock = asyncio.Lock()


async def _throttle() -> None:
    async with _rate_lock:
        while True:
            now = time.monotonic()
            while _request_times and now - _request_times[0] > _RATE_WINDOW_SECONDS:
                _request_times.pop(0)
            if len(_request_times) < _RATE_LIMIT_PER_MINUTE:
                _request_times.append(now)
                return
            wait = _RATE_WINDOW_SECONDS - (now - _request_times[0]) + 0.1
            await asyncio.sleep(wait)


class TwelveDataProvider(DataProvider):
    """Stocks/equities only — crypto is served by MexcProvider instead."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key or settings.twelve_data_api_key
        self._base_url = base_url or settings.twelve_data_base_url

    async def fetch_ohlcv(self, symbol: str, interval: str, outputsize: int, since_ms: int | None = None) -> pd.DataFrame:
        if not self._api_key:
            raise ProviderError("Twelve Data API key missing. Set it in backend/.env")

        formatted_symbol = symbol.strip().upper()
        interval = _CANONICAL_TO_INTERVAL[interval]
        params = {
            "symbol": formatted_symbol,
            "interval": interval,
            # 5000 = Twelve Data's own hard max for a single time_series
            # request (every plan, including free) — no pagination needed
            # below that ceiling, unlike the crypto exchanges.
            "outputsize": min(outputsize, 5000),
            "apikey": self._api_key,
            "format": "JSON",
            "timezone": "UTC",
        }

        await _throttle()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self._base_url}/time_series", params=params)
        except httpx.HTTPError as exc:
            # Rete o TLS, non un rifiuto del servizio: e' passeggero, e chi
            # chiama deve poterlo distinguere per riprovare invece di allarmare.
            raise ProviderUnreachable(
                f"Twelve Data unreachable for {formatted_symbol}: {exc}"
            ) from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError(f"Invalid response from Twelve Data for {formatted_symbol}") from exc

        if isinstance(payload, dict) and payload.get("status") == "error":
            message = payload.get("message", f"Twelve Data error for {formatted_symbol}")
            if _is_quota_error(payload.get("code"), message):
                raise QuotaExceeded(message, retry_after_s=_seconds_to_utc_midnight())
            raise ProviderError(message)

        values = payload.get("values") if isinstance(payload, dict) else None
        if not values:
            raise ProviderError(f"No data available for {formatted_symbol} ({interval})")

        df = pd.DataFrame(values)
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(float) if "volume" in df.columns else 0.0

        df = df.set_index("datetime").sort_index()
        return df[["open", "high", "low", "close", "volume"]]

    async def search_symbols(self, query: str) -> list[dict]:
        query = query.strip()
        if not query or not self._api_key:
            return []

        cache_key = query.upper()
        cached = _stock_search_cache.get(cache_key)
        if cached is not None:
            return cached

        await _throttle()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._base_url}/symbol_search",
                params={"symbol": query, "outputsize": 30, "apikey": self._api_key},
            )
        payload = resp.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []

        q = query.upper()

        def sort_key(item: dict):
            symbol = item.get("symbol", "").upper()
            return (
                symbol != q,
                item.get("exchange") not in _PREFERRED_EXCHANGES,
                item.get("instrument_type") not in _PREFERRED_INSTRUMENT_TYPES,
                not symbol.startswith(q),
                len(symbol),
            )

        items.sort(key=sort_key)

        seen: set[tuple[str, str]] = set()
        out = []
        for item in items:
            symbol, exchange = item.get("symbol", ""), item.get("exchange", "")
            if (symbol, exchange) in seen:
                continue
            seen.add((symbol, exchange))
            out.append({"symbol": symbol, "name": item.get("instrument_name", ""), "exchange": exchange})
            if len(out) >= _MAX_SUGGESTIONS:
                break

        _stock_search_cache[cache_key] = out
        return out
