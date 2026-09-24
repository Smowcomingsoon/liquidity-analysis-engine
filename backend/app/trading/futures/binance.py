"""Binance USDT-M perpetui — /fapi/...

L'implementazione vera vive ancora in `app/trading/binance_futures_exec.py`:
e' il modulo che ha girato in produzione, e riscriverlo per farlo entrare in
un'interfaccia sarebbe stato un rischio gratuito su denaro reale. Qui c'e'
solo l'adattatore che lo espone come tutti gli altri.
"""

from __future__ import annotations

import httpx

from app.trading import binance_futures_exec as _b
from app.trading.futures.base import FuturesAdapter, FuturesCreds, FuturesExecutionError, Position


class BinanceFutures(FuturesAdapter):
    slug = "binance"
    name = "Binance Futures"
    needs_passphrase = False
    max_leverage = 10
    key_hint = "API key with the «Futures» permission enabled (a spot key is not enough)."

    def _client(self, creds: FuturesCreds) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_b.FAPI_BASE, headers={"X-MBX-APIKEY": creds.api_key}, timeout=15.0
        )

    async def available_margin(self, creds: FuturesCreds) -> float:
        async with self._client(creds) as c:
            return await _b.get_available_margin_usdt(c, creds.api_secret)

    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        async with self._client(creds) as c:
            return await _b.get_mark_price(c, self.symbol_for(symbol))

    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        async with self._client(creds) as c:
            raw = await _b.get_open_positions(c, creds.api_secret)
        out = []
        for p in raw:
            amt = float(p.get("positionAmt", 0))
            out.append(Position(
                exchange=self.slug, symbol=p.get("symbol", ""),
                side="long" if amt > 0 else "short", quantity=abs(amt),
                entry_price=float(p.get("entryPrice", 0) or 0),
                mark_price=float(p.get("markPrice", 0) or 0),
                leverage=int(float(p.get("leverage", 1) or 1)),
                margin=float(p.get("isolatedMargin", 0) or 0),
                notional=abs(float(p.get("notional", 0) or 0)),
                unrealized_pnl=float(p.get("unRealizedProfit", 0) or 0),
                liquidation_price=float(p.get("liquidationPrice", 0) or 0),
            ))
        return out

    async def place_order(self, symbol, side, size_mode, size_value, leverage, creds, max_usdt=None):
        try:
            return await _b.place_futures_order(
                self.symbol_for(symbol), side, size_mode, size_value, leverage,
                creds.api_key, creds.api_secret, max_usdt=max_usdt,
            )
        except _b.FuturesExecutionError as exc:
            raise FuturesExecutionError(str(exc)) from exc

    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        try:
            return await _b.close_position(self.symbol_for(symbol), creds.api_key, creds.api_secret)
        except _b.FuturesExecutionError as exc:
            raise FuturesExecutionError(str(exc)) from exc
